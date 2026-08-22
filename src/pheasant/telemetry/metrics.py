"""Prometheus metrics for pheasant, in the exposition format, with no new dep.

``GET /metrics`` existed before this module and returned the literal string
``pheasant_up 1``. That is not a metrics endpoint, it is a liveness probe
wearing one's clothes: nothing about it could drive a scaling decision, and
nothing about it could answer "is the indexer moving?". Phase 35 needs both —
an autoscaler needs a queue depth to scale on, and an operator staring at a
multi-hour first index needs a throughput number.

Deliberately hand-rolled rather than pulling in ``prometheus-client``:

* The exposition format is a dozen lines of string building, and pheasant's
  core dependency list is a product decision (a region has to stay installable
  and runnable with nothing but the wheel).
* A ``CollectorRegistry`` shared across processes is the part of that library
  people actually need, and it is exactly the part that does not work for us:
  the indexer runs in a **child process** (``sync/worker.py``), so in-process
  counters there die with the child regardless of library. Indexing throughput
  reaches the parent over the existing progress wire and is served from the
  job registry, not from a counter this module owns.

So: counters and gauges here are for things the *serving* process knows
first-hand (requests it answered, its own memory, the graph it has loaded),
and everything about indexing is rendered from live job state at scrape time
via :func:`render_with`.

Label values are escaped per the exposition spec. Metric names are validated
on registration, because a malformed name silently breaks a whole scrape
rather than one series.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: Prometheus metric and label names. Enforced at registration: one bad name
#: makes the entire scrape unparseable, so this fails loudly and early.
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

#: Default histogram buckets, in seconds. Chosen around what pheasant actually
#: serves: sub-millisecond cache hits through to the multi-second agentic
#: answers that the 2026-08-03 work measured.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

LabelValues = tuple[tuple[str, str], ...]


def _escape(value: str) -> str:
    """Escape a label value: backslash, double quote, newline (in that order)."""

    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_to_key(labels: dict[str, str] | None) -> LabelValues:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(labels: LabelValues, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = [*labels, *extra]
    if not pairs:
        return ""
    body = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + body + "}"


def _format(value: float) -> str:
    """Render a number the way Prometheus expects.

    Integral floats print without a trailing ``.0`` — cosmetic, but counters
    are integral in the overwhelming majority and ``12`` reads better than
    ``12.0`` in a curl. ``+Inf`` is the spec's spelling for the last bucket.
    """

    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return repr(value)


@dataclass
class _Metric:
    name: str
    help: str
    kind: str
    label_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid metric name: {self.name!r}")
        for label in self.label_names:
            if not _NAME_RE.match(label):
                raise ValueError(f"invalid label name: {label!r}")


@dataclass
class _Series(_Metric):
    values: dict[LabelValues, float] = field(default_factory=dict)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_render_labels(labels)} {_format(value)}")
        return lines


@dataclass
class _Histogram(_Metric):
    buckets: tuple[float, ...] = DEFAULT_BUCKETS
    counts: dict[LabelValues, list[int]] = field(default_factory=dict)
    sums: dict[LabelValues, float] = field(default_factory=dict)
    totals: dict[LabelValues, int] = field(default_factory=dict)

    def observe(self, value: float, labels: LabelValues) -> None:
        counts = self.counts.setdefault(labels, [0] * len(self.buckets))
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                counts[index] += 1
        self.sums[labels] = self.sums.get(labels, 0.0) + value
        self.totals[labels] = self.totals.get(labels, 0) + 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for labels in sorted(self.counts):
            # Already cumulative: observe() increments every bucket whose bound
            # is >= the value, which is what the exposition format wants. A
            # per-bucket count here would render a histogram no scraper reads
            # correctly, and it would still look plausible in a curl.
            counts = self.counts[labels]
            for index, bound in enumerate(self.buckets):
                bucket_labels = _render_labels(labels, (("le", _format(bound)),))
                lines.append(f"{self.name}_bucket{bucket_labels} {counts[index]}")
            total = self.totals.get(labels, 0)
            inf_labels = _render_labels(labels, (("le", "+Inf"),))
            rendered = _render_labels(labels)
            lines.append(f"{self.name}_bucket{inf_labels} {total}")
            lines.append(f"{self.name}_sum{rendered} {_format(self.sums.get(labels, 0.0))}")
            lines.append(f"{self.name}_count{rendered} {total}")
        return lines


class MetricsRegistry:
    """A tiny, thread-safe metric store that renders exposition text.

    Every mutation takes one lock. Metrics are written from request threads,
    background sync threads and the scheduler beat, and read by whatever is
    scraping — handing out a partially-updated histogram would produce a
    ``_count`` that disagrees with its buckets, which breaks the scrape rather
    than merely skewing it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Series | _Histogram] = {}

    # -- registration -----------------------------------------------------

    def counter(self, name: str, help: str, label_names: tuple[str, ...] = ()) -> None:
        self._register(_Series(name=name, help=help, kind="counter", label_names=label_names))

    def gauge(self, name: str, help: str, label_names: tuple[str, ...] = ()) -> None:
        self._register(_Series(name=name, help=help, kind="gauge", label_names=label_names))

    def histogram(
        self,
        name: str,
        help: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        self._register(
            _Histogram(
                name=name,
                help=help,
                kind="histogram",
                label_names=label_names,
                buckets=tuple(sorted(buckets)),
            )
        )

    def _register(self, metric: _Series | _Histogram) -> None:
        with self._lock:
            # Idempotent: create_app() runs per process but also per TestClient,
            # and re-registering must not wipe values a caller already recorded.
            if metric.name not in self._metrics:
                self._metrics[metric.name] = metric

    # -- recording --------------------------------------------------------

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = _labels_to_key(labels)
        with self._lock:
            metric = self._metrics.get(name)
            if isinstance(metric, _Series):
                metric.values[key] = metric.values.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: str) -> None:
        key = _labels_to_key(labels)
        with self._lock:
            metric = self._metrics.get(name)
            if isinstance(metric, _Series):
                metric.values[key] = value

    def replace(self, name: str, values: dict[str, float], *, label: str = "source") -> None:
        """Set a single-label gauge's whole series, dropping absent labels.

        The counterpart to :meth:`set` for gauges sampled at scrape time: what
        is not in ``values`` no longer exists, and must stop being exported
        rather than freeze at its last reading.
        """

        with self._lock:
            metric = self._metrics.get(name)
            if isinstance(metric, _Series):
                metric.values = {
                    _labels_to_key({label: key}): float(inner) for key, inner in values.items()
                }

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = _labels_to_key(labels)
        with self._lock:
            metric = self._metrics.get(name)
            if isinstance(metric, _Histogram):
                metric.observe(value, key)

    # -- reading ----------------------------------------------------------

    def value(self, name: str, **labels: str) -> float | None:
        """Current value of one series. For tests and internal assertions."""

        key = _labels_to_key(labels)
        with self._lock:
            metric = self._metrics.get(name)
            if isinstance(metric, _Series):
                return metric.values.get(key)
            if isinstance(metric, _Histogram):
                total = metric.totals.get(key)
                return None if total is None else float(total)
        return None

    def render(self) -> str:
        with self._lock:
            metrics = list(self._metrics.values())
        lines: list[str] = []
        for metric in sorted(metrics, key=lambda m: m.name):
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


#: The process-wide registry. One per process by design — see the module
#: docstring on why cross-process aggregation is not attempted here.
REGISTRY = MetricsRegistry()


def record_memory_write(outcome: str, fold: str | None) -> None:
    """Record one memory write's outcome and refresh the derived ratio.

    Call this instead of incrementing `pheasant_memory_writes_total` by hand
    — MCP's `memory_write` and `POST /memory` both do, the same duplication
    `memory.policy` already accepts for MCP/HTTP parity, rather than one
    surface calling the other. `fold` is `MemoryStore.last_fold`.

    The ratio is **not** derivable from `outcome` alone, which is why this
    takes `fold` as well. With `reinforcement_enabled` on (the default) a
    byte-identical re-write also reports `outcome="reinforced"` — correctly,
    since it really does bump the record's counters — so a ratio computed
    from `reinforced / (created + reinforced)` would count the pre-Phase-1
    exact dedup as if reinforcement had earned it, and read high on a store
    that only ever sees verbatim repeats. `fold` is what separates the two:

        ratio = normalized folds / (records created + normalized folds)

    An exact fold sits in neither half — it never would have become a record
    in the first place, so it is not redundancy reinforcement removed.
    """
    REGISTRY.inc("pheasant_memory_writes_total", outcome=outcome)
    if fold:
        REGISTRY.inc("pheasant_memory_l0_folds_total", kind=fold)
    created = REGISTRY.value("pheasant_memory_writes_total", outcome="created") or 0.0
    normalized = REGISTRY.value("pheasant_memory_l0_folds_total", kind="normalized") or 0.0
    total = created + normalized
    ratio = round(normalized / total, 6) if total else 0.0
    REGISTRY.set("pheasant_memory_reinforcement_ratio", ratio)


def resident_bytes() -> float | None:
    """This process's resident set size, or None where it cannot be read.

    ``/proc/self/status`` on Linux (which is what the container runs), falling
    back to ``resource.getrusage``. ``ru_maxrss`` is a high-water mark, not a
    current reading, and its unit differs by platform — kilobytes on Linux,
    bytes on macOS — so it is only used when /proc is unavailable, and the
    difference is documented rather than silently normalized wrong.
    """

    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) * 1024.0
    except OSError:
        pass
    try:
        import resource
        import sys

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return float(maxrss) if sys.platform == "darwin" else float(maxrss) * 1024.0
    except Exception:  # pragma: no cover - platform-specific
        return None


def register_default_metrics(version: str) -> None:
    """Declare pheasant's metric surface. Idempotent."""

    REGISTRY.gauge("pheasant_up", "1 when the process is serving.")
    REGISTRY.gauge("pheasant_build_info", "Build metadata; value is always 1.", ("version",))
    REGISTRY.gauge("pheasant_process_resident_bytes", "Resident set size of this process.")
    REGISTRY.gauge("pheasant_process_start_time_seconds", "Unix start time of this process.")

    # Indexing. Rendered from live job state at scrape time (see render_with)
    # because the work happens in a child process.
    REGISTRY.gauge("pheasant_index_queue_depth", "Sources queued or running in an index job.")
    REGISTRY.gauge("pheasant_index_inflight", "Index jobs currently running.")
    REGISTRY.gauge(
        "pheasant_index_dead_letters",
        "Index tasks that exhausted their attempts and need attention.",
    )
    REGISTRY.gauge(
        "pheasant_index_progress_ratio",
        "Fraction of the current pass complete, per source (0-1).",
        ("source",),
    )
    REGISTRY.gauge(
        "pheasant_index_files_per_second",
        "Observed throughput of the current pass, per source.",
        ("source",),
    )
    REGISTRY.gauge(
        "pheasant_index_eta_seconds",
        "Estimated seconds remaining for the current pass, per source.",
        ("source",),
    )
    REGISTRY.gauge(
        "pheasant_index_stalled",
        "1 when a running source has not reported progress within its stall window.",
        ("source",),
    )
    REGISTRY.gauge(
        "pheasant_sync_last_success_timestamp_seconds",
        "Unix time of the last successful sync, per source.",
        ("source",),
    )
    REGISTRY.counter(
        "pheasant_index_files_total",
        "Files resolved by an index pass, by outcome.",
        ("source", "outcome"),
    )
    REGISTRY.counter("pheasant_index_bytes_total", "Bytes read by index passes.", ("source",))
    REGISTRY.counter(
        "pheasant_index_jobs_total", "Index jobs that reached a terminal state.", ("status",)
    )

    # Serving.
    REGISTRY.counter(
        "pheasant_requests_shed_total",
        "Requests refused with 429 because this replica was at its concurrency limit.",
        ("path",),
    )
    REGISTRY.gauge("pheasant_requests_inflight", "Requests currently being served.")
    REGISTRY.gauge("pheasant_draining", "1 while this process is draining after SIGTERM.")
    REGISTRY.histogram("pheasant_search_duration_seconds", "Search latency.", ("mode",))
    REGISTRY.counter(
        "pheasant_search_total", "Searches answered, by mode and outcome.", ("mode", "outcome")
    )
    REGISTRY.counter(
        "pheasant_embedding_requests_total",
        "Embedding provider requests, by outcome.",
        ("outcome",),
    )

    # Graph.
    REGISTRY.gauge("pheasant_graph_nodes", "Nodes in the loaded graph.")
    REGISTRY.gauge("pheasant_graph_edges", "Edges in the loaded graph.")

    # Agent memory (compaction Phase 0). Refreshed by `run_memory_maintenance`
    # (`pheasant_memory_records`, `pheasant_memory_maintenance_seconds`) and
    # at write time (`pheasant_memory_writes_total`) — nothing here existed
    # before, so a compaction change is otherwise unfalsifiable.
    REGISTRY.gauge(
        "pheasant_memory_records",
        # Phase 5: `tier` joined `scope` so a compaction pass's effect (hot
        # records demoted to cold) is visible here too, not just in the
        # ledger.
        "Live (non-archived) memory records, per scope and tier.",
        ("scope", "tier"),
    )
    REGISTRY.counter(
        "pheasant_memory_writes_total", "memory_write calls, by outcome.", ("outcome",)
    )
    REGISTRY.histogram("pheasant_memory_maintenance_seconds", "One consolidation pass.")

    # Compaction / synthesis (Phase 3-5). `pheasant_memory_compactions_total`
    # counts ledger rows actually written this process — an idempotent
    # re-run over an unchanged cluster increments nothing, by the same
    # `INSERT OR IGNORE` the ledger itself uses for idempotency.
    REGISTRY.counter(
        "pheasant_memory_compactions_total",
        "New memory_compactions ledger rows written, by op.",
        ("op",),
    )
    REGISTRY.histogram("pheasant_memory_compaction_seconds", "One L1/L2 clustering pass.")
    REGISTRY.counter(
        "pheasant_memory_synthesis_calls_total",
        "L3 synthesis cluster attempts, by outcome "
        "(synthesized|cached|empty|collision) — never incremented when "
        "memory.synthesis.enabled is false, since no cluster is attempted.",
        ("outcome",),
    )
    # How L0 admission folded, when it folded at all. Finer-grained than
    # `pheasant_memory_writes_total{outcome}` on purpose: `outcome` is public
    # API and stays three-valued, but a `reinforced` write can mean either
    # "a byte-identical record already existed" (the dedup that predates
    # Phase 1, free either way) or "a *paraphrase* matched" (what Phase 1
    # newly does). Only the second says reinforcement is earning its keep.
    REGISTRY.counter(
        "pheasant_memory_l0_folds_total",
        "Writes folded into an existing record by L0 admission, by kind: "
        "'exact' (byte-identical, pre-Phase-1 dedup) or 'normalized' "
        "(a paraphrase matched on its normalized key).",
        ("kind",),
    )
    # The number that says whether reinforcement is doing anything.
    REGISTRY.gauge(
        "pheasant_memory_reinforcement_ratio",
        "Of the writes that either created a record or were folded as a "
        "paraphrase, the fraction folded. Byte-identical repeats are in "
        "neither half — they never would have become a record.",
    )

    # Remote preparation workers (Phase 35.5 hardens these; the gauge exists
    # now so a scaling policy has something to read from the first release).
    REGISTRY.gauge(
        "pheasant_worker_up",
        "1 when a remote preparation worker answered its last probe.",
        ("endpoint",),
    )

    REGISTRY.set("pheasant_up", 1.0)
    REGISTRY.set("pheasant_build_info", 1.0, version=version)
    REGISTRY.set("pheasant_process_start_time_seconds", _PROCESS_START)


#: Captured at import so the gauge means "when did this process start", not
#: "when was it first scraped".
_PROCESS_START = time.time()


def render_with(sample: dict[str, Any] | None = None) -> str:
    """Render the exposition text, refreshing scrape-time gauges first.

    ``sample`` carries values only the caller can supply — live job state and
    the loaded graph — so this module needs no import of the API or the engine
    and stays trivially testable.
    """

    rss = resident_bytes()
    if rss is not None:
        REGISTRY.set("pheasant_process_resident_bytes", rss)
    for name, value in (sample or {}).items():
        if isinstance(value, dict):
            # Replaced wholesale, not merged. These are *live* gauges — the
            # progress of jobs running right now — so a per-source series set
            # once and never cleared kept reporting a finished (or deleted)
            # source's last-known progress forever, as a current reading.
            # Prometheus has no way to tell that apart from a genuinely stuck
            # source, and the label set grows without bound as sources come and
            # go. Counters, which must never go backwards, are not passed this
            # way — they are incremented at their event site.
            REGISTRY.replace(name, {str(k): float(v) for k, v in value.items()})
        elif value is not None:
            REGISTRY.set(name, float(value))
    return REGISTRY.render()
