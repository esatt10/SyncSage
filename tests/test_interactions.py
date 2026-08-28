"""The observation plane: what gets recorded, and what gets dropped instead.

The load-bearing claims here are the ones a reviewer would otherwise have to
take on trust:

* with observation off, nothing changes anywhere (CLAUDE.md rule 7);
* the request path never blocks and never raises, whatever the sink does;
* under pressure the tier loses **data**, not latency, and says so in a metric;
* an observation is never a memory record, and nothing here writes one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.state_store import StateStore
from pheasant.telemetry.interactions import (
    COLUMNS,
    INSERT_SQL,
    InteractionBuffer,
    InteractionContext,
    InteractionEvent,
    Modality,
    NullSink,
    SpoolSink,
    StateSink,
    configure_tracing,
    event_id,
    observe,
    parse_traceparent,
    process_buffer,
    redact,
    resolve_sink,
    set_process_buffer,
)
from pheasant.telemetry.metrics import REGISTRY, register_default_metrics


@pytest.fixture(autouse=True)
def _metrics() -> None:
    register_default_metrics("0.0.0-test")


@pytest.fixture(autouse=True)
def _isolate_tracing() -> Any:
    """`TRACING` is process-wide, so a test that configures it must not leave
    it configured for the next one -- which would silently turn an offline
    assertion into one that depends on whatever the previous test attached."""

    from pheasant.telemetry import interactions as module

    previous = (module.TRACING.tracer, module.TRACING.enabled)
    yield
    module.TRACING.tracer, module.TRACING.enabled = previous


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "p.db")
    store.migrate()
    return store


def _event(index: int = 0, **kwargs: Any) -> InteractionEvent:
    payload: dict[str, Any] = {
        "kb_id": "kb",
        "operation": "search_context",
        "trace_id": f"{index:032x}",
        "span_id": f"{index:016x}",
        "started_at": "2026-01-01T00:00:00.000000Z",
    }
    payload.update(kwargs)
    return InteractionEvent(**payload)


def _dropped(reason: str) -> float:
    return REGISTRY.value("pheasant_interaction_events_dropped_total", reason=reason) or 0.0


# --------------------------------------------------------------------------
# Identity, trace correlation, and the row
# --------------------------------------------------------------------------


def test_an_inbound_traceparent_is_adopted_so_one_call_is_one_trace() -> None:
    """An agent's own trace must not become a second, unrelated one."""

    context = InteractionContext.create(
        Modality.MCP, traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    )

    assert context.trace_id == "a" * 32
    assert context.parent_span_id == "b" * 16
    assert context.span_id not in ("", "b" * 16)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        "00-tooshort-" + "b" * 16 + "-01",
        # All-zero ids are reserved as invalid by the spec. Accepting one
        # would collapse every such caller into a single shared trace.
        "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",
    ],
)
def test_an_unusable_traceparent_starts_a_fresh_trace_rather_than_failing(header: str) -> None:
    assert parse_traceparent(header) is None
    context = InteractionContext.create(Modality.UI, traceparent=header)
    assert len(context.trace_id) == 32
    assert context.parent_span_id is None


def test_an_unknown_modality_is_data_not_a_crash() -> None:
    """This is reached from a header on an unauthenticated API."""

    assert InteractionContext.create("not-a-surface").modality is Modality.UI


def test_the_row_id_is_content_addressed_on_the_span() -> None:
    """What makes at-least-once redelivery a no-op instead of a duplicate."""

    assert event_id("a" * 32, "b" * 16) == event_id("a" * 32, "b" * 16)
    assert event_id("a" * 32, "b" * 16) != event_id("a" * 32, "c" * 16)


def test_the_row_matches_the_insert_it_is_written_with() -> None:
    """Three column lists that could drift are derived from one tuple."""

    assert len(_event().as_row()) == len(COLUMNS)
    assert INSERT_SQL.count("?") == len(COLUMNS)
    for name in COLUMNS:
        assert name in INSERT_SQL


def test_an_event_round_trips_through_json_for_the_queue() -> None:
    event = _event(query_text="where is the watcher", result_ids=["chunk:a"], top_score=0.9)
    restored = InteractionEvent.from_json(json.loads(json.dumps(event.as_json())))
    assert restored.as_row() == event.as_row()


# --------------------------------------------------------------------------
# The request path
# --------------------------------------------------------------------------


def test_observe_fills_in_timing_and_hands_the_event_over() -> None:
    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(Modality.MCP, principal="user:ada", session_id="s1")

    with observe(buffer, context, kb_id="kb", operation="search_context") as event:
        event.result_ids = ["chunk:a", "chunk:b"]

    assert buffer.depth == 1
    recorded = buffer._drain(1)[0]
    assert recorded.principal == "user:ada"
    assert recorded.session_id == "s1"
    assert recorded.modality == "mcp"
    assert recorded.status == "ok"
    assert recorded.result_count == 2
    assert recorded.duration_ms is not None


def test_a_failing_handler_is_recorded_as_an_error_and_re_raised_unchanged() -> None:
    """Observation never swallows a caller's failure, and never invents one."""

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(Modality.UI)

    with pytest.raises(ValueError, match="Unknown source: typo"):
        with observe(buffer, context, kb_id="kb", operation="sync_source"):
            raise ValueError("Unknown source: typo")

    assert buffer._drain(1)[0].status == "error"


def test_a_broken_sink_never_reaches_the_caller() -> None:
    class Exploding:
        name = "exploding"

        def write(self, events: Any) -> int:
            raise RuntimeError("the database is on fire")

        def close(self) -> None:
            return None

    buffer = InteractionBuffer(Exploding(), capacity=10, batch_size=1, interval_seconds=99)
    before = _dropped("error")

    with observe(buffer, InteractionContext.create(Modality.UI), kb_id="kb", operation="x"):
        pass
    assert buffer.flush() == 0

    assert _dropped("error") > before


# --------------------------------------------------------------------------
# Backpressure — the invariant
# --------------------------------------------------------------------------


def test_a_full_buffer_drops_the_oldest_and_counts_it() -> None:
    """Bounded, and it says so. Under sustained overload the recent past is
    the more useful half, so the newest event is the one kept."""

    buffer = InteractionBuffer(NullSink(), capacity=2, batch_size=99, interval_seconds=99)
    before = _dropped("buffer_full")

    accepted = [buffer.record(_event(index)) for index in range(5)]

    assert buffer.depth == 2
    assert accepted[:2] == [True, True]
    assert accepted[2:] == [False, False, False]
    assert _dropped("buffer_full") - before == 3
    # The two survivors are the newest, not the oldest.
    assert [event.trace_id for event in buffer._drain(2)] == [f"{3:032x}", f"{4:032x}"]


def test_a_drowning_queue_is_not_published_into() -> None:
    """Otherwise a stalled log tier turns a bounded buffer into an unbounded
    table -- the same failure wearing a different hat."""

    from pheasant.telemetry.interactions import QueueSink

    class Drowning:
        def depth(self) -> dict[str, int]:
            return {"pending": 10_000, "inflight": 0}

        def publish_batch(self, task_id: str, payload: dict) -> None:  # pragma: no cover
            raise AssertionError("must not publish into a queue past its depth limit")

    sink = QueueSink(Drowning(), kb_id="kb", max_depth=100)
    before = _dropped("queue_full")

    assert sink.write([_event()]) == 0
    assert _dropped("queue_full") - before == 1


def test_an_unreadable_queue_depth_is_not_a_reason_to_stop_recording() -> None:
    from pheasant.telemetry.interactions import QueueSink

    published: list[str] = []

    class Flaky:
        def depth(self) -> dict[str, int]:
            raise RuntimeError("no")

        def publish_batch(self, task_id: str, payload: dict) -> None:
            published.append(task_id)

    assert QueueSink(Flaky(), kb_id="kb", max_depth=1).write([_event()]) == 1
    assert len(published) == 1


# --------------------------------------------------------------------------
# Sink selection — a capability probe, not a config switch
# --------------------------------------------------------------------------


def test_sink_selection_follows_what_this_process_can_actually_do(state: StateStore) -> None:
    settings = PheasantConfig().observability.interactions

    queue = type("Q", (), {"publish_batch": lambda self, i, p: None, "depth": lambda self: {}})()
    assert resolve_sink(settings, state=state, queue=queue, kb_id="kb").name == "queue"
    assert resolve_sink(settings, state=state).name == "state"
    # `/state:ro` on an API replica under SQLite: there is nowhere to write.
    assert resolve_sink(settings, state=state, state_writable=False).name == "null"
    assert resolve_sink(settings, state=None).name == "null"


def test_a_read_only_replica_spools_when_told_where(tmp_path: Path, state: StateStore) -> None:
    settings = PheasantConfig().observability.interactions
    settings.spool_path = tmp_path / "spool"

    sink = resolve_sink(settings, state=state, state_writable=False, owner="api/2")
    assert isinstance(sink, SpoolSink)
    assert sink.write([_event(1), _event(2)]) == 2

    files = list((tmp_path / "spool").rglob("*.ndjson"))
    assert len(files) == 1
    # The owner is part of the path so two replicas cannot interleave lines.
    assert "api-2" in str(files[0])
    assert len(files[0].read_text().strip().splitlines()) == 2


def test_a_null_sink_warns_once_rather_than_per_request(caplog: Any) -> None:
    sink = NullSink("state_read_only")
    with caplog.at_level("WARNING"):
        for _ in range(5):
            sink.write([_event()])
    assert sum("nothing here can persist it" in record.message for record in caplog.records) == 1


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_writing_the_same_span_twice_leaves_one_row(state: StateStore) -> None:
    """At-least-once redelivery is the queue's normal mode, not an error."""

    sink = StateSink(state)
    sink.write([_event(1), _event(2)])
    sink.write([_event(1), _event(2)])

    assert state.rows("SELECT COUNT(*) AS c FROM interaction_events", ())[0]["c"] == 2


def test_an_observation_is_never_a_memory_record(state: StateStore) -> None:
    """The boundary the whole design rests on."""

    StateSink(state).write([_event(1), _event(2)])

    assert state.rows("SELECT COUNT(*) AS c FROM memory_records", ())[0]["c"] == 0
    assert state.rows("SELECT COUNT(*) AS c FROM artifacts", ())[0]["c"] == 0
    assert state.rows("SELECT COUNT(*) AS c FROM chunks", ())[0]["c"] == 0


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_redaction_keeps_everything_the_structural_rules_need() -> None:
    """A region can keep learning its own shape without keeping what anyone
    typed. Only the lexical rule goes quiet."""

    event = redact(
        _event(
            criteria={"mode": "hybrid"},
            query_text="who owns billing",
            result_ids=["chunk:a"],
            principal="user:ada",
            session_id="s1",
        ),
        enabled=True,
    )

    assert event.query_text is None
    assert event.attributes["query_redacted"] is True
    # Everything `path-affinity-v1` and `retrieval-gap-v1` count on survives.
    assert event.criteria == {"mode": "hybrid"}
    assert event.result_ids == ["chunk:a"]
    # Identity is deliberately *not* what this knob redacts: it is what scopes
    # a formed memory, and dropping it would make every observation org-wide.
    assert (event.principal, event.session_id) == ("user:ada", "s1")


def test_redaction_off_leaves_the_query_alone() -> None:
    assert redact(_event(query_text="q"), enabled=False).query_text == "q"


# --------------------------------------------------------------------------
# Rule 7 — the no-infrastructure path
# --------------------------------------------------------------------------


def test_observation_is_off_by_default() -> None:
    settings = PheasantConfig().observability

    assert settings.interactions.enabled is False
    assert settings.interactions.queue.enabled is False
    assert settings.otlp_endpoint is None
    assert settings.interactions.cold_enabled is False


def test_no_otlp_endpoint_attaches_no_exporter_so_the_suite_stays_offline() -> None:
    """Network-freedom by construction rather than by mocking."""

    assert configure_tracing(PheasantConfig().observability) is False


def test_a_configured_endpoint_without_the_extra_degrades_rather_than_raising(
    caplog: Any,
) -> None:
    settings = PheasantConfig().observability
    settings.otlp_endpoint = "http://127.0.0.1:4318/v1/traces"

    with caplog.at_level("WARNING"):
        attached = configure_tracing(settings)

    try:
        # Either the extra is installed and it attaches, or it is not and we
        # say so -- never a crash, and never a silent no-op.
        assert attached in (True, False)
        if not attached:
            assert any("[otel] extra" in record.message for record in caplog.records)
    finally:
        # Nothing is exported here -- no span is created through this provider
        # -- but the batch processor it installed owns a thread, and leaving it
        # attached would make a later test's spans try to reach a collector
        # that is not there. The suite is offline by construction, and this is
        # the one place that could make it otherwise.
        if attached:
            from opentelemetry import trace as ot_trace

            provider = ot_trace.get_tracer_provider()
            shutdown = getattr(provider, "shutdown", None)
            if shutdown is not None:
                shutdown()


def test_the_process_buffer_is_a_single_shared_slot() -> None:
    """So a mounted MCP app observes through the API's buffer instead of
    opening a second one that would double-count every /mcp call."""

    assert process_buffer() is None
    buffer = InteractionBuffer(NullSink())
    try:
        set_process_buffer(buffer)
        assert process_buffer() is buffer
    finally:
        set_process_buffer(None)
    assert process_buffer() is None


# --------------------------------------------------------------------------
# OpenTelemetry, when the extra is installed
# --------------------------------------------------------------------------


@pytest.fixture
def otel_spans() -> Any:
    """A real SDK exporting into memory. Skipped without the [otel] extra."""

    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace as ot_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from pheasant.telemetry import interactions as module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    ot_trace.set_tracer_provider(provider)

    previous = (module.TRACING.tracer, module.TRACING.enabled)
    module.TRACING.tracer = provider.get_tracer("pheasant")
    module.TRACING.enabled = True
    try:
        yield exporter
    finally:
        module.TRACING.tracer, module.TRACING.enabled = previous


def test_a_ledger_row_and_its_exported_span_name_the_same_call(otel_spans: Any) -> None:
    """Most of the reason to export spans at all.

    The event is built with locally minted ids, because they are its primary
    key and must exist with or without the extra. When a real span is running,
    *its* ids are what the operator's collector shows -- so the row has to
    adopt them, or correlating a slow span to a row finds nothing.
    """

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)

    with observe(
        buffer, InteractionContext.create(Modality.MCP), kb_id="kb", operation="search_context"
    ):
        pass

    span = otel_spans.get_finished_spans()[0]
    event = buffer._drain(1)[0]
    assert event.trace_id == format(span.get_span_context().trace_id, "032x")
    assert event.span_id == format(span.get_span_context().span_id, "016x")


def test_an_inbound_trace_continues_into_the_exported_span(otel_spans: Any) -> None:
    """Otherwise the SDK starts its own root trace for a call the caller
    already had one for, and the collector shows two unrelated traces where
    there was one request."""

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(
        Modality.MCP, traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    )

    with observe(buffer, context, kb_id="kb", operation="search_context"):
        pass

    span = otel_spans.get_finished_spans()[0]
    assert format(span.get_span_context().trace_id, "032x") == "a" * 32
    assert format(span.parent.span_id, "016x") == "b" * 16


def test_a_span_carries_the_shape_of_a_call_and_never_its_content(otel_spans: Any) -> None:
    """A collector is a different system with different retention. Query text
    and principal are the ledger's business, governed by `redact_query_text`
    there; they must not leak out through a span."""

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(Modality.MCP, principal="user:ada", session_id="s1")

    with observe(buffer, context, kb_id="kb", operation="search_context") as event:
        event.query_text = "who owns billing"
        event.result_ids = ["chunk:a", "chunk:b"]

    attributes = dict(otel_spans.get_finished_spans()[0].attributes)
    assert attributes == {
        "pheasant.kb": "kb",
        "pheasant.modality": "mcp",
        "pheasant.operation": "search_context",
        "pheasant.result_count": 2,
    }
    serialized = str(attributes)
    assert "who owns billing" not in serialized
    assert "user:ada" not in serialized
    assert "s1" not in serialized


def test_a_failing_call_marks_its_span_as_an_error(otel_spans: Any) -> None:
    from opentelemetry.trace import StatusCode

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)

    with pytest.raises(ValueError):
        with observe(buffer, InteractionContext.create(Modality.UI), kb_id="kb", operation="boom"):
            raise ValueError("nope")

    assert otel_spans.get_finished_spans()[0].status.status_code is StatusCode.ERROR


def test_exporter_headers_come_from_an_environment_variable_never_the_config() -> None:
    """The config file names the variable; the value stays out of it. Same
    rule `storage.dsn_env` follows, for the same reason."""

    from pheasant.telemetry.interactions import _parse_headers

    assert _parse_headers("authorization=Bearer x,x-scope=team") == {
        "authorization": "Bearer x",
        "x-scope": "team",
    }
    assert _parse_headers("") == {}
    assert _parse_headers("nonsense") == {}
    assert PheasantConfig().observability.otlp_headers_env == "PHEASANT_OTLP_HEADERS"


# --------------------------------------------------------------------------
# The read-only /state case, which only exists in a container
# --------------------------------------------------------------------------


def test_a_read_only_state_is_detected_and_never_written(monkeypatch: Any, tmp_path: Path) -> None:
    """`docker-compose.scale.yml` mounts `/state:ro` on API replicas so the
    indexer is the only writer. Under SQLite that means an API replica must
    not try -- and it must not need an operator to have configured per-role
    what the mount already decided.

    The probe is exercised here by faking the filesystem answer; the real
    thing was checked against an actual read-only bind mount, which the kernel
    reports unwritable even to uid 0 (unlike bare permission bits, which root
    bypasses).
    """

    from pheasant.api.app import _state_is_writable

    config = PheasantConfig()
    config.storage.sqlite_path = tmp_path / "state" / "p.db"

    # `sys.modules`, not `import pheasant.api.app as ...`: the package's
    # __init__ binds `app = None`, which shadows the submodule attribute.
    import sys

    monkeypatch.setattr(sys.modules["pheasant.api.app"].os, "access", lambda path, mode: False)
    assert _state_is_writable(config, object()) is False

    # Postgres does not care what the state volume allows: the ledger is in
    # the database, which is why the shipped fleet needs no spool at all.
    config.storage.backend = "postgres"
    assert _state_is_writable(config, object()) is True


def test_a_writable_state_is_detected(tmp_path: Path) -> None:
    from pheasant.api.app import _state_is_writable

    config = PheasantConfig()
    config.storage.sqlite_path = tmp_path / "p.db"
    assert _state_is_writable(config, object()) is True
