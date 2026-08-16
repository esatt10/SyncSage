from __future__ import annotations

import inspect
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pheasant.config.schema import FILESYSTEM_SOURCE_TYPES, PheasantConfig, SourceConfig
from pheasant.graph.builder import GraphBuilder
from pheasant.ingestion.captioner import captioner_from_config, source_includes_images
from pheasant.ingestion.content_types import TEXT_EXTENSIONS
from pheasant.ingestion.extractor import extractor_from_config, source_includes_documents
from pheasant.ingestion.pipeline import (
    ParsedArtifact,
    git_state,
    parse_connector_payload,
    sha256_bytes,
    utc_now,
)
from pheasant.ingestion.transcriber import source_includes_audio, transcriber_from_config
from pheasant.ingestion.walk import (
    SyncBudgetExceeded,
    WalkBudget,
    aggregate_depth_counts,
    walk_source,
)
from pheasant.persistence.graph_store import GraphStore
from pheasant.persistence.manifest import ManifestStore
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.node_index import NodeIndex
from pheasant.search.vector_store import (
    VectorSearcher,
    vector_indexer_from_config,
)
from pheasant.synapse.events import EventStream, RouterWebhook
from pheasant.synapse.publisher import ContractPublisher
from pheasant.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ItemNotModified,
    connector_for_source,
)
from pheasant.sync.fingerprint import (
    EMBEDDING_SCOPE,
    SOURCE_SCOPE,
    embedding_change,
    embedding_fingerprint,
    source_fingerprint,
)
from pheasant.sync.locks import EngineLease, source_lock
from pheasant.sync.pacing import serve_yield

logger = logging.getLogger(__name__)

SyncMode = Literal["incremental", "full", "validate_only", "repair"]
SYNC_MODES = {"incremental", "full", "validate_only", "repair"}

#: ``(phase, current, total, detail)``, optionally followed by a ``meta``
#: mapping (Phase 35.1) carrying ``source`` plus whatever counters the
#: emitting pass has — ``indexed``, ``skipped``, ``bytes_done``.
#: ``total`` is None until the connector has finished listing, because it is
#: not known before then and a made-up denominator is worse than none.
#: Four-argument callbacks stay supported: :func:`_hook_accepts_meta` decides
#: by signature which form to call, so an existing caller is unaffected.
ProgressHook = Callable[..., None]


def _hook_accepts_meta(hook: ProgressHook) -> bool:
    """Does this callback take the Phase-35.1 ``meta`` argument?

    Determined once per wrap, by signature, rather than by catching
    ``TypeError`` around the call — a ``TypeError`` raised *inside* a
    four-argument hook would otherwise be misread as "wrong arity" and the
    hook silently re-invoked with different arguments.
    """

    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):  # builtins, C callables
        return False
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 5


def _progress_reporter(hook: ProgressHook | None, source_name: str):
    """Wrap a progress hook so it can never fail the sync that calls it.

    This fires from inside the per-artifact loop. A caller's bad callback —
    a closed queue, an evicted job, a typo — must cost a log line, not an
    index. Returns a no-op when there is no hook, so the un-instrumented path
    stays exactly as cheap as it was.

    Every event carries its ``source`` in ``meta`` (Phase 35.1). Before this,
    ``sync_all`` prefixed the source name into the human-readable ``detail``
    string and nothing could tell the eight sources of one job apart without
    parsing prose back out of it.
    """

    if hook is None:
        return lambda *_args, **_kwargs: None

    wants_meta = _hook_accepts_meta(hook)

    def report(
        phase: str,
        current: int = 0,
        total: int | None = None,
        detail: str = "",
        **stats: Any,
    ) -> None:
        try:
            if wants_meta:
                hook(phase, current, total, detail, {"source": source_name, **stats})
            else:
                hook(phase, current, total, detail)
        except Exception:  # pragma: no cover - progress is never load-bearing
            logger.debug("progress hook failed for %s", source_name, exc_info=True)

    return report


def _restore_iso_ts(filename_ts: str) -> str:
    """Restore an ISO-8601 timestamp from its filesystem-safe snapshot form.

    ``write_snapshot`` builds filenames as ``utc_ts.replace(":", "-")`` over a
    ``YYYY-MM-DDTHH:MM:SSZ`` timestamp. The date hyphens must stay; only the
    time portion's ``-`` separators become ``:`` again. The trailing ``Z`` is
    dropped so ``datetime.fromisoformat`` parses it on Python 3.11.
    """

    ts = filename_ts.rstrip("Z")
    if "T" in ts:
        date_part, _, time_part = ts.partition("T")
        return f"{date_part}T{time_part.replace('-', ':')}"
    return ts


@dataclass(frozen=True)
class SyncResult:
    source_id: str
    indexed_artifacts: int
    skipped_artifacts: int
    graph_nodes: int
    graph_edges: int
    status: str = "healthy"
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _PreparedItem:
    """Immutable worker output consumed by the single coordinated writer."""

    position: int
    item: ConnectorItem
    previous: dict[str, Any] | None
    parsed: ParsedArtifact | None = None
    fetched: bool = False
    skipped: bool = False
    transfer_skipped: bool = False


_PROCESS_SAFE_TEXT_EXTENSIONS = TEXT_EXTENSIONS - {".html"}


def _process_cpu_capacity() -> int:
    """CPU count visible to this process (affinity/cgroup aware where available)."""

    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        return max(1, int(process_cpu_count() or 1))
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, int(os.cpu_count() or 1))


def _prepare_filesystem_item_process(
    source: SourceConfig,
    mode: SyncMode,
    git_metadata: tuple[str | None, str | None, bool] | None,
    position: int,
    item: ConnectorItem,
    previous: dict[str, Any] | None,
) -> _PreparedItem:
    """Stateless process-worker entry point for ordinary filesystem text."""

    if (
        mode == "incremental"
        and previous is not None
        and item.sha256 is not None
        and previous.get("sha256") == item.sha256
    ):
        return _PreparedItem(
            position,
            item,
            previous,
            skipped=True,
            transfer_skipped=True,
        )
    path = Path(str(item.metadata["path"]))
    content = path.read_bytes()
    content_hash = sha256_bytes(content)
    if mode == "incremental" and previous and previous.get("sha256") == content_hash:
        return _PreparedItem(
            position,
            item,
            previous,
            fetched=True,
            skipped=True,
        )
    payload = ConnectorPayload(
        item=item,
        content=content,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        sha256=content_hash,
        mtime=item.mtime,
        metadata={"path": str(path)},
    )
    parsed = parse_connector_payload(source, item, payload, git_metadata)
    if parsed is None:
        return _PreparedItem(position, item, previous, fetched=True)
    if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
        return _PreparedItem(
            position,
            item,
            previous,
            parsed=parsed,
            fetched=True,
            skipped=True,
        )
    return _PreparedItem(position, item, previous, parsed=parsed, fetched=True)


class SyncEngine:
    def __init__(
        self,
        config: PheasantConfig,
        paths: StatePaths | None = None,
        state: StateStore | None = None,
        lease: EngineLease | None = None,
    ):
        self.config = config
        self.paths = paths or StatePaths.from_config(config)
        self.paths.ensure()
        self.state = state or StateStore.from_config(self.config, self.paths.sqlite)
        self.state.migrate()
        # Single-writer lease (Synapse 21.2): acquired lazily on the first
        # sync so read-only engines (e.g. docker-exec MCP stdio beside a
        # running server) keep working against a served state directory.
        self.lease = lease or EngineLease(self.paths.state)
        # In-process serialization across all writers (watcher, scheduler,
        # API startup executor, HTTP /sync). Preparation may run concurrently;
        # only state/graph/vector commits take this mutex.
        self._sync_mutex = threading.RLock()
        self._lease_mutex = threading.Lock()
        SourceRegistry(self.config, self.state).initialize()
        self.manifests = ManifestStore(self.paths.manifests, self.state)
        self.graph_store = GraphStore(self.paths.graphs)
        self.graph_builder = GraphBuilder(config)
        existing = self.graph_store.load(config.knowledge_base_id)
        if len(existing):
            self.graph_builder.graph = existing
        # Optional embed-on-sync (Synapse 21.4): None when
        # search.embeddings.enabled is false, leaving sync byte-identical
        # to pre-21.4 behavior (no vector dir, no embedder calls).
        self.vectors = vector_indexer_from_config(config)
        # Multi-modal image ingestion (Synapse 25.4 session A): None unless a
        # source's include globs admit image extensions, so a text-only region
        # is byte-identical to pre-25.4 behavior (no captioner built, no
        # captioning network call possible). Default provider is the offline
        # deterministic stub.
        self.captioner = captioner_from_config(config)
        # Multi-modal audio ingestion (Synapse 25.4 session B): None unless a
        # source's include globs admit audio extensions — same opt-in,
        # zero-network-when-absent contract as the image captioner above.
        self.transcriber = transcriber_from_config(config)
        # Document text extraction (PDF/DOCX, optionally HTML): None unless a
        # source's include globs admit a document extension (or html_text is
        # on), so a text-only region behaves exactly as it did when .pdf/.docx
        # were accepted and then silently produced no text. Fully offline and
        # deterministic — no model, no network on any provider.
        self.extractor = extractor_from_config(config)
        # Synapse 21.5: contract publisher + NDJSON event stream. The event
        # log is always written (local, useful standalone); contract
        # publication + the router webhook are gated by synapse.publish /
        # synapse.router_url so a router-less region is unchanged.
        self.events = EventStream(self.paths.state)
        self.router_webhook = RouterWebhook(
            config.synapse.router_url,
            timeout=config.synapse.webhook_timeout_seconds,
        )
        # Mid-sync graph checkpointing (see _maybe_checkpoint_graph).
        self._graph_dirty = False
        self._last_checkpoint = time.monotonic()
        self._last_save_seconds = 0.0
        # Derived full-text index over graph nodes, so graph search does not
        # have to scan the graph. Built from the graph, never authoritative.
        self.node_index = NodeIndex(self.state)

    def close(self) -> None:
        """Persist in-flight graph work, release the lease, close the store.

        Called from the server's shutdown path, so `docker compose stop`
        keeps whatever the running sync had built instead of discarding it.
        """
        try:
            self.checkpoint_graph()
        except Exception as exc:  # pragma: no cover - shutdown must not raise
            logger.warning("Could not checkpoint graph on shutdown: %s", exc)
        if self.vectors is not None:
            try:
                self.vectors.flush()
            except Exception as exc:  # pragma: no cover - shutdown must not raise
                logger.warning("Could not flush vector store on shutdown: %s", exc)
        self.lease.release()
        self.state.close()

    def checkpoint_graph(
        self,
        source_name: str | None = None,
        manifest: dict | None = None,
    ) -> bool:
        """Persist in-flight sync work now. True if anything was written.

        The manifest goes with the graph: it is what an incremental pass reads
        to decide a file is unchanged, so checkpointing one without the other
        would still make a resumed sync re-read everything.
        """

        if not self._graph_dirty:
            return False
        if source_name and manifest is not None:
            self.manifests.save(source_name, manifest)
        self.flush_node_index()
        started = time.monotonic()
        self.graph_store.save(self.config.knowledge_base_id, self.graph_builder.graph)
        self._last_save_seconds = time.monotonic() - started
        self._graph_dirty = False
        self._last_checkpoint = time.monotonic()
        return True

    def reload_graph(self) -> int:
        """Re-read the graph from /state, e.g. after a worker process synced.

        The server does not index (see :mod:`pheasant.sync.worker`), so this is
        how a freshly-indexed graph reaches the process that is serving it.
        Reading a compressed graph is cheap; the swap is atomic from a
        reader's point of view because the new graph replaces the reference in
        one assignment.
        """

        loaded = self.graph_store.load(self.config.knowledge_base_id)
        if not len(loaded):
            return 0
        self.graph_builder.graph = loaded
        # The index on disk was written by the worker for this same graph, so
        # nothing is owed; drop the delta the load itself generated.
        loaded.take_index_delta()
        self.node_index._populated = False
        logger.info("Reloaded graph from state: %d nodes", loaded.number_of_nodes())
        return loaded.number_of_nodes()

    #: Above this many changed nodes, rebuild the FTS index wholesale instead
    #: of deleting each stale row individually. `node_id` on graph_nodes_fts
    #: is UNINDEXED (it's read-only lookup data, not searched), so a batched
    #: `DELETE ... WHERE node_id IN (...)` against it has no index to use and
    #: degrades to a table scan per batch. Measured: concept-node
    #: reconciliation removing ~387k of 543k nodes stalled for 10+ minutes
    #: going through the per-row delete path; `replace_all` (one unfiltered
    #: DELETE + bulk INSERT) is the cheap way to apply a change this size.
    _INDEX_REBUILD_THRESHOLD = 20_000

    def flush_node_index(self) -> int:
        """Push pending node writes into the search index. Never fatal.

        The index is derived from the graph, so a failure here costs query
        speed until the next flush or rebuild — never correctness, and never
        a failed sync.
        """

        try:
            upserts, removals = self.graph_builder.graph.take_index_delta()
            if not upserts and not removals:
                return 0
            if len(upserts) + len(removals) > self._INDEX_REBUILD_THRESHOLD:
                return self.rebuild_node_index()
            return self.node_index.apply(upserts, removals)
        except Exception as exc:  # pragma: no cover - cache maintenance
            logger.warning("Could not update the graph node index: %s", exc)
            return 0

    def rebuild_node_index(self) -> int:
        """Rebuild the whole index from the in-memory graph."""

        graph = self.graph_builder.graph
        rows = graph.index_rows()
        written = self.node_index.replace_all(rows)
        # Everything pending is now represented.
        graph.take_index_delta()
        logger.info("Graph node index rebuilt: %d nodes", written)
        return written

    def ensure_node_index(self) -> int:
        """Build the index if it is missing, e.g. after an upgrade.

        Cheap when it already exists: one COUNT.
        """

        graph = self.graph_builder.graph
        if graph.number_of_nodes() <= 1:
            return 0
        if self.node_index.count() > 0:
            self.flush_node_index()
            return 0
        return self.rebuild_node_index()

    def _maybe_checkpoint(self, source_name: str, manifest: dict) -> None:
        """Checkpoint at most once per configured interval during a sync.

        Time-based rather than every-N-artifacts: the cost of a save scales
        with graph size, not with how many artifacts went into it, so a fixed
        count would checkpoint far too often on a large graph.
        """

        self._graph_dirty = True
        interval = float(getattr(self.config.storage, "graph_checkpoint_seconds", 0) or 0)
        if interval <= 0:
            return
        # Self-throttling: a checkpoint costs whatever the graph costs to
        # serialize, and that grows with the index. Spacing checkpoints at
        # ten times the last save keeps their overhead near 10% of the sync
        # no matter how large the graph gets — a fixed interval eventually
        # means a sync that spends most of its time saving.
        interval = max(interval, self._last_save_seconds * 10)
        if time.monotonic() - self._last_checkpoint < interval:
            return
        try:
            self.checkpoint_graph(source_name, manifest)
        except Exception as exc:  # a checkpoint hiccup must not fail the sync
            logger.warning("Checkpoint failed (continuing): %s", exc)

    def startup(self) -> list[SyncResult]:
        """Bring a restarted container back to a serving state.

        The expensive work (reading files, chunking, embedding) has already
        happened and lives in /state, so a restart over unchanged content and
        an unchanged config is close to free. Only two things can be stale,
        and each is repaired the cheap way:

        * the vector space, if the embedding config changed — re-embedded from
          the chunks already in SQLite, without re-reading a single file;
        * the graph, if a sync was interrupted before its last checkpoint —
          healed by a repair pass that re-reads only the artifacts actually
          missing from it.
        """

        self.paths.ensure()
        self.state.migrate()
        SourceRegistry(self.config, self.state).initialize()
        self.reconcile_embeddings()
        self.ensure_node_index()
        results = []
        for source in self.config.sources:
            if source.enabled and source.sync.on_startup:
                mode: SyncMode = "incremental"
                # Unconditional: a graph missing artifacts SQLite already has
                # is never a state anyone wants served, and repair is the
                # cheap fix (re-reads only what is missing, re-embeds nothing).
                if self._graph_gap(source.name):
                    logger.info(
                        "Source %s has indexed artifacts missing from the graph "
                        "(interrupted sync?) — repairing instead of re-indexing",
                        source.name,
                    )
                    mode = "repair"
                results.append(self.sync_source(source.name, mode))
        return results

    def reconcile_embeddings(self) -> dict:
        """Align the vector store with the current embedding config.

        Returns a small report; a no-op transition reports ``embedded: 0`` and
        makes no embedder call, so an unchanged restart costs nothing.
        """

        current = embedding_fingerprint(self.config)
        previous = self.state.get_fingerprint(EMBEDDING_SCOPE)
        change = embedding_change(previous, current)
        report = {"change": change, "embedded": 0, "dropped": False}

        if change in {"none", "disabled"} or self.vectors is None:
            # "disabled" still records the transition, so turning embeddings
            # back on later is recognised as a return to a known space rather
            # than a fresh introduction.
            if change != "none":
                self.state.set_fingerprint(EMBEDDING_SCOPE, current, utc_now())
            return report

        # "invalidated" = a different model/dimension/store: the old vectors
        # are not comparable with new ones and must go. "introduced" = chunks
        # exist but were never embedded; keep whatever is there and fill in.
        drop_existing = change == "invalidated"
        logger.info(
            "Embedding config %s — re-embedding from stored chunks (drop_existing=%s)",
            change,
            drop_existing,
        )
        report["dropped"] = drop_existing
        report["embedded"] = self.rebuild_vectors(drop_existing=drop_existing)
        self.state.set_fingerprint(EMBEDDING_SCOPE, current, utc_now())
        return report

    def rebuild_vectors(self, drop_existing: bool = False) -> int:
        """Embed everything already indexed, without re-reading any source.

        The text is already in SQLite and chunk ids are content-addressed, so
        this is both cheaper than a re-scan and idempotent: a second run makes
        zero embedder calls.
        """

        if self.vectors is None:
            return 0
        if drop_existing:
            # LanceDB's vector width lives in the table schema. Deleting rows
            # leaves that schema behind, so a model dimension change must
            # reset the store itself before the first new vector is inserted.
            self.vectors.reset()
        embedded = 0
        for artifact in self.state.rows("SELECT id, source_id FROM artifacts ORDER BY id"):
            chunks = self.state.rows(
                "SELECT id, text, text_hash FROM chunks WHERE artifact_id=? ORDER BY chunk_index",
                (artifact["id"],),
            )
            if not chunks:
                continue
            embedded += self.vectors.index_artifact(
                str(artifact["source_id"]),
                str(artifact["id"]),
                [dict(chunk) for chunk in chunks],
            )
        self.vectors.flush()
        return embedded

    def _graph_gap(self, source_name: str) -> bool:
        """True when SQLite has artifacts the loaded graph does not know about."""

        graph = self.graph_builder.graph
        for row in self.state.rows(
            "SELECT id FROM artifacts WHERE source_id=? LIMIT 2000", (source_name,)
        ):
            if not graph.has_node(str(row["id"])):
                return True
        return False

    def sync_all(
        self,
        mode: SyncMode = "incremental",
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
        on_progress: ProgressHook | None = None,
    ) -> list[SyncResult]:
        sources = [source for source in self.config.sources if source.enabled]
        if not sources:
            return []
        workers = min(
            len(sources),
            max(1, int(self.config.sync.concurrency.max_parallel_sources or 1)),
        )
        if workers == 1:
            return [
                self.sync_source(
                    source.name,
                    mode,
                    max_depth=max_depth,
                    full_scan=full_scan,
                    on_progress=on_progress,
                )
                for source in sources
            ]

        # A shared callback may write NDJSON or mutate a job record; serialize
        # calls while still allowing the source pipelines themselves to overlap.
        progress_lock = threading.Lock()

        def progress_for(source_name: str) -> ProgressHook:
            def source_progress(
                phase: str,
                current: int,
                total: int | None,
                detail: str,
                meta: dict[str, Any] | None = None,
            ) -> None:
                if on_progress is None:
                    return
                with progress_lock:
                    on_progress(phase, current, total, detail, meta)

            return source_progress

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="pheasant-source",
        ) as executor:
            futures = [
                executor.submit(
                    self.sync_source,
                    source.name,
                    mode,
                    max_depth=max_depth,
                    full_scan=full_scan,
                    on_progress=progress_for(source.name),
                )
                for source in sources
            ]
            # Preserve configured source order in the public result even when
            # a later source finishes first.
            return [future.result() for future in futures]

    def _prepare_item(
        self,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        git_metadata: tuple[str | None, str | None, bool] | None,
        position: int,
        item: ConnectorItem,
    ) -> _PreparedItem:
        """Read and parse one item without mutating authoritative state."""

        previous = artifacts.get(item.relative_path)
        if self._can_skip_before_read(mode, previous, item):
            return _PreparedItem(
                position,
                item,
                previous,
                skipped=True,
                transfer_skipped=True,
            )
        try:
            payload = connector.read_item(item)
        except ItemNotModified:
            return _PreparedItem(
                position,
                item,
                previous,
                skipped=True,
                transfer_skipped=True,
            )
        content_hash = payload.sha256 or item.sha256 or sha256_bytes(payload.content)
        if mode == "incremental" and previous and previous.get("sha256") == content_hash:
            return _PreparedItem(
                position,
                item,
                previous,
                fetched=True,
                skipped=True,
            )
        if payload.sha256 != content_hash:
            payload = replace(payload, sha256=content_hash)
        parsed = parse_connector_payload(
            source,
            item,
            payload,
            git_metadata,
            captioner=self.captioner,
            transcriber=self.transcriber,
            extractor=self.extractor,
        )
        if parsed is None:
            return _PreparedItem(position, item, previous, fetched=True)
        if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
            return _PreparedItem(
                position,
                item,
                previous,
                parsed=parsed,
                fetched=True,
                skipped=True,
            )
        if mode == "repair" and not self._needs_repair(
            source.name,
            parsed.id,
            parsed.sha256,
            previous,
            len(parsed.chunks),
        ):
            return _PreparedItem(
                position,
                item,
                previous,
                parsed=parsed,
                fetched=True,
                skipped=True,
            )
        return _PreparedItem(
            position,
            item,
            previous,
            parsed=parsed,
            fetched=True,
        )

    def _prepare_item_remote(
        self,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        git_metadata: tuple[str | None, str | None, bool] | None,
        position: int,
        item: ConnectorItem,
        endpoint: str,
        token: str,
    ) -> _PreparedItem:
        """Read locally, prepare on an authenticated stateless worker."""

        previous = artifacts.get(item.relative_path)
        if self._can_skip_before_read(mode, previous, item):
            return _PreparedItem(
                position,
                item,
                previous,
                skipped=True,
                transfer_skipped=True,
            )
        try:
            payload = connector.read_item(item)
        except ItemNotModified:
            return _PreparedItem(
                position,
                item,
                previous,
                skipped=True,
                transfer_skipped=True,
            )
        content_hash = payload.sha256 or item.sha256 or sha256_bytes(payload.content)
        if mode == "incremental" and previous and previous.get("sha256") == content_hash:
            return _PreparedItem(
                position,
                item,
                previous,
                fetched=True,
                skipped=True,
            )
        if payload.sha256 != content_hash:
            payload = replace(payload, sha256=content_hash)
        from pheasant.sync.remote_worker import prepare_remote, task_payload

        parsed = prepare_remote(
            endpoint,
            token,
            task_payload(source, item, payload, git_metadata),
            timeout=float(self.config.sync.concurrency.remote_worker_timeout_seconds or 120),
        )
        if parsed is None:
            return _PreparedItem(position, item, previous, fetched=True)
        if parsed.source_id != source.name or parsed.relative_path != item.relative_path:
            raise ValueError(
                "Remote worker returned an artifact for a different source/item: "
                f"{parsed.source_id}/{parsed.relative_path}"
            )
        expected_hash = payload.sha256 or item.sha256
        if expected_hash is not None and parsed.sha256 != expected_hash:
            raise ValueError(
                f"Remote worker returned the wrong content hash for {item.relative_path}"
            )
        if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
            return _PreparedItem(
                position,
                item,
                previous,
                parsed=parsed,
                fetched=True,
                skipped=True,
            )
        return _PreparedItem(position, item, previous, parsed=parsed, fetched=True)

    def _prepared_items(
        self,
        connector: Any,
        source: SourceConfig,
        mode: SyncMode,
        artifacts: dict[str, Any],
        git_metadata: tuple[str | None, str | None, bool] | None,
        items: list[ConnectorItem],
    ):
        """Yield prepared items in discovery order with bounded look-ahead.

        Workers never touch manifests, SQLite, vectors, or the graph. Keeping
        at most two work windows in flight avoids turning a 50k-file source
        into a second in-memory copy of the repository.
        """

        if not items:
            return
        workers = min(
            len(items),
            max(1, int(self.config.sync.concurrency.max_parallel_files or 1)),
        )
        requested_executor = str(self.config.sync.concurrency.file_executor or "thread").lower()
        if workers <= 1 and requested_executor == "thread":
            for position, item in enumerate(items, start=1):
                yield self._prepare_item(
                    connector,
                    source,
                    mode,
                    artifacts,
                    git_metadata,
                    position,
                    item,
                )
            return

        plain_text_source = (
            connector.connector_type == "filesystem"
            and mode != "repair"
            and not bool(getattr(source.taxonomy, "enabled", False))
            and all(
                Path(item.relative_path).suffix.lower() in _PROCESS_SAFE_TEXT_EXTENSIONS
                for item in items
            )
        )
        process_safe = requested_executor == "process" and plain_text_source
        remote_urls = list(self.config.sync.concurrency.remote_worker_urls or [])
        remote_safe = requested_executor == "remote" and plain_text_source and bool(remote_urls)
        remote_token = ""
        if remote_safe:
            from pheasant.sync.remote_worker import configured_token

            remote_token = configured_token(self.config.sync.concurrency.remote_worker_token_env)
        if requested_executor not in {"thread", "process", "remote"}:
            logger.warning(
                "Unknown sync.concurrency.file_executor=%r; using thread workers",
                requested_executor,
            )
        elif requested_executor == "process" and not process_safe:
            logger.info(
                "Source %s needs connector/modal/repair state; using thread workers",
                source.name,
            )
        elif requested_executor == "remote" and not remote_safe:
            logger.warning(
                "Source %s cannot use remote workers (requires URLs and plain text without "
                "taxonomy/repair); using thread workers",
                source.name,
            )
        executor_type = ProcessPoolExecutor if process_safe else ThreadPoolExecutor
        if process_safe:
            workers = min(workers, _process_cpu_capacity())

        with executor_type(
            max_workers=workers,
            **({} if process_safe else {"thread_name_prefix": f"pheasant-file-{source.name}"}),
        ) as executor:
            pending: deque[Future[_PreparedItem]] = deque()
            iterator = iter(enumerate(items, start=1))

            def submit(position: int, item: ConnectorItem) -> Future[_PreparedItem]:
                if process_safe:
                    return executor.submit(
                        _prepare_filesystem_item_process,
                        source,
                        mode,
                        git_metadata,
                        position,
                        item,
                        artifacts.get(item.relative_path),
                    )
                if remote_safe:
                    endpoint = remote_urls[(position - 1) % len(remote_urls)]
                    return executor.submit(
                        self._prepare_item_remote,
                        connector,
                        source,
                        mode,
                        artifacts,
                        git_metadata,
                        position,
                        item,
                        endpoint,
                        remote_token,
                    )
                return executor.submit(
                    self._prepare_item,
                    connector,
                    source,
                    mode,
                    artifacts,
                    git_metadata,
                    position,
                    item,
                )

            for _ in range(min(len(items), workers * 2)):
                position, item = next(iterator)
                pending.append(submit(position, item))
            while pending:
                yield pending.popleft().result()
                try:
                    position, item = next(iterator)
                except StopIteration:
                    continue
                pending.append(submit(position, item))

    def _ensure_modal_handlers(self, source: Any) -> None:
        """Build the caption/transcribe/extract handlers this source needs.

        The three are built once in ``__init__`` from whether *any* configured
        source admits their extensions. That misses the source registered at
        **runtime** — `POST /sources`, `POST /sources/quick-add`, the UI's
        add-source flow — which is how most sources actually arrive: the engine
        was constructed before that source existed, so no handler was built,
        and its documents were indexed with **no text at all**. Exactly the
        silent-empty-content failure document extraction exists to fix, on the
        path most people use.

        Topping the handlers up here, where the source is known, also makes the
        test more precise than the constructor's: it asks whether *this* source
        needs a handler rather than whether any source does. Idempotent, and it
        never discards a handler that already exists — a second sync of an
        unchanged source does no work here.
        """
        if self.extractor is None and source_includes_documents(source):
            self.extractor = extractor_from_config(self.config, source=source)
        if self.captioner is None and source_includes_images(source):
            self.captioner = captioner_from_config(self.config, source=source)
        if self.transcriber is None and source_includes_audio(source):
            self.transcriber = transcriber_from_config(self.config, source=source)

    @staticmethod
    def _memory_acl(path: Any) -> dict[str, Any] | None:
        """`{scope, written_by}` read from the record file (Step 33.11).

        Read from the file rather than the `memory_records` projection because
        this runs *inside* the artifact loop, before the projection for this
        sync exists. Fail-soft: an unreadable record simply expresses no ACL
        and falls back to the region default, which is what it did before 33.11.
        """
        try:
            from pheasant.memory.store import MemoryStore

            record = MemoryStore(Path(path).parent).load(Path(path))
        except Exception:
            return None
        return {"scope": record.scope, "written_by": record.written_by}

    def _bridge_memory(self) -> None:
        """Emit memory `supersedes` + `about` edges (Step 33.7).

        Runs on the global-enrichment beat rather than per source, because a
        record can be about content that lives in a *different* source and the
        bridge can only see it once that source is indexed too.

        Fail-soft, matching every other global pass here: a bridge that cannot
        be derived costs some edges, never the sync that produced the content.
        """
        if not self.config.graph.memory_entity_bridging:
            return
        try:
            report = self.graph_builder.add_memory_edges(
                self.state, self.config.memory.about_max_targets
            )
        except Exception:
            logger.warning("memory graph bridge failed; edges not updated", exc_info=True)
            return
        if report.get("records"):
            logger.info(
                "Memory bridged: %s records, %s about edges %s, %s supersedes, %s unbridged",
                report["records"],
                report["about"],
                report.get("by_signal") or {},
                report["supersedes"],
                len(report.get("unbridged") or []),
            )

    def _project_memory_records(self, source: Any) -> None:
        """Rebuild ``memory_records`` for a ``type: memory`` source (Step 33.5).

        A no-op for every other source type, so a region without agent memory
        is untouched. Rebuilding wholesale — rather than diffing — is what
        keeps the table a *projection*: it cannot drift from the files, an
        archived record disappears without needing its own delete path, and a
        second sync over unchanged records writes identical rows.

        Fail-soft, the same posture document extraction takes: one unreadable
        record must not abort a sync that indexed everything else. The rows
        from the previous pass survive (the write is a single transaction that
        never runs), and the warning says so.
        """
        if getattr(source.type, "value", source.type) != "memory":
            return
        try:
            from pheasant.memory.projection import project
            from pheasant.memory.store import MemoryStore

            records = MemoryStore(source.path).list_records()
            self.state.replace_memory_records(source.name, project(source.name, records))
        except Exception:
            logger.warning(
                "memory projection failed for %s; keeping the previous rows",
                source.name,
                exc_info=True,
            )

    def _finalize_index_state(
        self,
        *,
        source: SourceConfig,
        connector: Any,
        mode: SyncMode,
        manifest: dict[str, Any],
        indexed: int,
        embedded_chunks: int,
        changed_ids: set[str],
        total_items: int,
        report: Callable[[str, int, int | None, str], None],
        embedding_progress: Callable[[int], None],
    ) -> int:
        """Finalize one source while holding the coordinated writer mutex."""

        with self._sync_mutex:
            pruned_vectors = 0
            if self.vectors is not None:
                live_chunk_ids = {
                    str(row["id"])
                    for row in self.state.rows(
                        "SELECT id FROM chunks WHERE source_id=?", (source.name,)
                    )
                }
                pruned_vectors = self.vectors.prune_source(source.name, live_chunk_ids)

            self._project_memory_records(source)
            if indexed or mode == "full":
                report("enriching", total_items, total_items, "linking the graph")
                self.graph_builder.add_similarity_edges(
                    source.name,
                    changed_ids=None if mode == "full" else changed_ids,
                )
                self.graph_builder.add_cross_source_edges()
                self._bridge_memory()
                pruned = self.graph_builder.reconcile_concepts(
                    self.state,
                    min_documents=int(self.config.graph.concept_min_documents),
                    changed_ids=changed_ids,
                )
                if pruned["removed"] or pruned["restored"]:
                    logger.info(
                        "Concepts reconciled: kept=%s removed=%s restored=%s",
                        pruned["kept"],
                        pruned["removed"],
                        pruned["restored"],
                    )

            if self.vectors is not None:
                # Finish provider work before advertising a saved, completed
                # source. Network batches run concurrently inside this call;
                # the vector-store upsert itself remains ordered.
                self.vectors.flush(on_progress=embedding_progress)
                report(
                    "embedding",
                    embedded_chunks,
                    embedded_chunks,
                    f"embedded {embedded_chunks} changed chunk(s)",
                )
            report("saving", total_items, total_items, "writing the graph and index")
            manifest["last_indexed_at"] = utc_now()
            manifest["connector"] = {"type": connector.connector_type}
            self.manifests.save(source.name, manifest)
            self.flush_node_index()
            self.graph_store.save(self.config.knowledge_base_id, self.graph_builder.graph)
            self._graph_dirty = False
            self._last_checkpoint = time.monotonic()
            return pruned_vectors

    def sync_source(
        self,
        source_name: str,
        mode: SyncMode = "incremental",
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
        on_progress: ProgressHook | None = None,
    ) -> SyncResult:
        """Sync one source.

        ``max_depth`` caps directory depth for this run only, and
        ``full_scan`` lifts both the depth cap and the size budget — the
        explicit "yes, index all of it" switch. Neither is persisted to the
        source config, so a one-off wide sync cannot silently become the
        standing behavior of a scheduled one.

        ``on_progress`` is called as the pass advances so a caller can show
        real progress rather than a spinner. It is best-effort in the strongest
        sense: it rides the per-artifact yield point that already exists, and
        an exception raised inside it can never fail the sync (see
        :func:`_report_progress`).
        """
        report = _progress_reporter(on_progress, source_name)
        if mode not in SYNC_MODES:
            raise ValueError(f"Unsupported sync mode: {mode}")
        source = self.config.effective_source(
            self._source(source_name), max_depth=max_depth, full_scan=full_scan
        )
        self._ensure_modal_handlers(source)
        # A container restart must never re-index by itself, but a config edit
        # that changes what the content pipeline produces must. Compare what
        # this source was last indexed with against what it is configured with
        # now; only a real difference escalates to a full pass.
        fingerprint = source_fingerprint(source)
        previous_fingerprint = self.state.get_fingerprint(SOURCE_SCOPE.format(name=source.name))
        config_changed = previous_fingerprint is not None and previous_fingerprint != fingerprint
        if config_changed and mode in {"incremental", "repair"}:
            logger.info(
                "Source %s config changed since it was indexed (globs/chunking) — "
                "escalating %s sync to full",
                source.name,
                mode,
            )
            mode = "full"
        with self._lease_mutex:
            self.lease.acquire()
        # Test-only hook (Synapse 21.2 crash-safety tests): widens the window
        # between per-artifact writes so a kill -9 can land mid-sync.
        slow_sync_s = float(os.environ.get("PHEASANT_TEST_SLOW_SYNC_MS", "0") or 0) / 1000.0
        with source_lock(
            self.paths.locks,
            source.name,
            timeout_s=float(self.config.sync.concurrency.lock_timeout_seconds or 0),
        ):
            connector = connector_for_source(source, self.state)
            if mode == "validate_only":
                health = connector.validate()
                status = "validated" if health.ok else health.status
                self.state.mark_source_status(source.name, status)
                return SyncResult(
                    source.name,
                    0,
                    0,
                    self.graph_builder.graph.number_of_nodes(),
                    self.graph_builder.graph.number_of_edges(),
                    status,
                    {
                        "connector_type": connector.connector_type,
                        "item_count": health.item_count,
                        "checked_items": health.checked_items,
                        "errors": health.errors,
                        "checkpoint": health.checkpoint,
                    },
                )
            started_at = utc_now()
            details_extra: dict = {}
            manifest = self.manifests.load(source.name)
            artifacts = manifest.setdefault("artifacts", {})
            # Synapse 21.3: thread the previous checkpoint into the connector
            # so non-filesystem sources can skip unchanged remote items.
            connector.begin_sync(mode)
            # A filesystem source whose root is absent (typically: the path is
            # not mounted into this container) would otherwise index 0 files
            # silently and report success. Surface it as a first-class status so
            # the operator sees *why* nothing was indexed instead of a clean
            # zero. (validate() only runs in validate_only mode.)
            if source.type in FILESYSTEM_SOURCE_TYPES and not source.path.exists():
                self.state.mark_source_status(source.name, "path_missing")
                return SyncResult(
                    source.name,
                    0,
                    0,
                    self.graph_builder.graph.number_of_nodes(),
                    self.graph_builder.graph.number_of_edges(),
                    "path_missing",
                    {
                        "connector_type": connector.connector_type,
                        "error": (
                            f"source path does not exist in this environment: "
                            f"{source.path}. If pheasant runs in a container, "
                            f"ensure this path is mounted into the container."
                        ),
                    },
                )
            report("listing", 0, None, f"listing {source.name}")
            try:
                items = connector.list_items()
            except SyncBudgetExceeded as exc:
                # The source is bigger than its budget. Refuse the whole sync
                # rather than indexing a prefix of it: a partial index is
                # non-deterministic, and the operator needs to make a choice
                # (narrow it, or ask for it explicitly with full_scan).
                self.state.mark_source_status(source.name, "limit_exceeded")
                return SyncResult(
                    source.name,
                    0,
                    0,
                    self.graph_builder.graph.number_of_nodes(),
                    self.graph_builder.graph.number_of_edges(),
                    "limit_exceeded",
                    {
                        "connector_type": connector.connector_type,
                        "error": str(exc),
                        "scan": exc.report.as_dict(),
                    },
                )
            if mode == "full":
                with self._sync_mutex:
                    self.state.delete_source_artifacts(source.name)
                    self.graph_builder.remove_source_content(source.name)
                manifest = {"source_id": source.name, "artifacts": {}}
                artifacts = manifest["artifacts"]
            indexed = 0
            skipped = 0
            fetched = 0
            transfer_skipped = 0
            embedded_chunks = 0
            indexed_bytes = 0
            changed_ids: set[str] = set()
            git_metadata = git_state(source.path) if source.type.value == "repository" else None
            # The denominator only exists now — listing is what produced it.
            total_items = len(items)
            report("preparing", 0, total_items, f"queued {total_items} item(s)")
            embedding_completed = 0

            def embedding_progress(completed: int) -> None:
                nonlocal embedding_completed
                embedding_completed += completed
                report(
                    "embedding",
                    embedding_completed,
                    None,
                    f"embedded {embedding_completed} changed chunk(s)",
                )

            prepared_items = self._prepared_items(
                connector,
                source,
                mode,
                dict(artifacts),
                git_metadata,
                items,
            )
            for prepared in prepared_items:
                position = prepared.position
                item = prepared.item
                fetched += int(prepared.fetched)
                skipped += int(prepared.skipped)
                transfer_skipped += int(prepared.transfer_skipped)
                report(
                    "preparing",
                    position,
                    total_items,
                    item.relative_path,
                    indexed=indexed,
                    skipped=skipped,
                    bytes_done=indexed_bytes,
                )
                parsed = prepared.parsed
                if prepared.skipped or parsed is None:
                    continue
                now = utc_now()
                from pheasant.security.acl import normalize_acl

                acl_raw = (item.metadata or {}).get("acl")
                acl_kind = connector.connector_type
                if getattr(source.type, "value", source.type) == "memory":
                    # Step 33.11 — a memory record is served by the ordinary
                    # filesystem connector, so its connector type says nothing
                    # about who may read it. Its *scope* does, and that lives
                    # in the record, not in the file's metadata.
                    acl_kind = "memory"
                    acl_raw = self._memory_acl(parsed.path)
                acl_doc = normalize_acl(acl_kind, acl_raw)
                artifact_row = {
                    "acl": json.dumps(acl_doc, sort_keys=True) if acl_doc else None,
                    "id": parsed.id,
                    "source_id": parsed.source_id,
                    "type": parsed.type,
                    "path": str(parsed.path),
                    "relative_path": parsed.relative_path,
                    "mime_type": parsed.mime_type,
                    "size_bytes": parsed.size_bytes,
                    "sha256": parsed.sha256,
                    "mtime": parsed.mtime,
                    "git_branch": parsed.git_branch,
                    "git_commit": parsed.git_commit,
                    "last_indexed_at": now,
                    "status": "indexed",
                }
                chunk_rows = [
                    {
                        "id": (
                            f"chunk:{source.name}:{parsed.relative_path}:"
                            f"sha256={chunk.text_hash}:chunk={chunk.index:04d}"
                        ),
                        "artifact_id": parsed.id,
                        "source_id": source.name,
                        "chunk_index": chunk.index,
                        "heading_path": chunk.heading_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "text": chunk.text,
                        "text_hash": chunk.text_hash,
                        "summary": chunk.text[:240],
                        "token_estimate": chunk.token_estimate,
                    }
                    for chunk in parsed.chunks
                ]
                with self._sync_mutex:
                    self.state.replace_artifact_chunks(artifact_row, chunk_rows)
                    if self.vectors is not None:
                        # Chunk ids are content-addressed (text_hash in the id),
                        # so only new/changed chunk text reaches the embedder.
                        embedded_chunks += self.vectors.index_artifact(
                            source.name,
                            parsed.id,
                            chunk_rows,
                            on_progress=embedding_progress,
                        )
                    enrichment = self.graph_builder.add_artifact(source, parsed)
                    self.state.replace_artifact_enrichment(
                        parsed.id,
                        parsed.source_id,
                        enrichment.terms,
                        enrichment.symbols,
                    )
                    artifacts[parsed.relative_path] = {
                        "sha256": parsed.sha256,
                        "artifact_id": parsed.id,
                        "last_indexed_at": now,
                        "git_branch": parsed.git_branch,
                        "git_commit": parsed.git_commit,
                    }
                    changed_ids.add(parsed.id)
                    self._maybe_checkpoint(source.name, manifest)
                indexed += 1
                # Compatibility phase retained for existing CLI/API/MCP
                # progress consumers; ``committing`` is the more precise new
                # phase for callers that understand the staged pipeline.
                indexed_bytes += int(parsed.size_bytes or 0)
                report(
                    "indexing",
                    position,
                    total_items,
                    parsed.relative_path,
                    indexed=indexed,
                    skipped=skipped,
                    bytes_done=indexed_bytes,
                )
                report(
                    "committing",
                    position,
                    total_items,
                    parsed.relative_path,
                    indexed=indexed,
                    skipped=skipped,
                    bytes_done=indexed_bytes,
                )
                # Let anything waiting on the API get a turn before the next
                # file. Without this a long index makes the UI unusable.
                serve_yield()
                # Checkpoint mid-run. Without this the graph and the manifest
                # only reach /state when a sync *finishes*, so stopping the
                # container an hour into a first index threw that hour away:
                # the next start re-read every file (stale manifest) and
                # rebuilt the whole graph.
                if slow_sync_s:
                    time.sleep(slow_sync_s)
            pruned_vectors = self._finalize_index_state(
                source=source,
                connector=connector,
                mode=mode,
                manifest=manifest,
                indexed=indexed,
                embedded_chunks=embedded_chunks,
                changed_ids=changed_ids,
                total_items=total_items,
                report=report,
                embedding_progress=embedding_progress,
            )
            # Recorded only after the pass succeeded, so a sync that dies
            # halfway is retried rather than being remembered as "done with
            # this config".
            self.state.set_fingerprint(
                SOURCE_SCOPE.format(name=source.name), fingerprint, utc_now()
            )
            # Synapse 21.6A: compressed timestamped graph snapshots + retention.
            # Additive history beside graph.latest.json, throttled by the
            # configured interval and bounded by max_state_size_gb. Fail-soft so
            # a snapshot/retention hiccup never fails the sync.
            self._snapshot_after_sync()
            now = utc_now()
            cursor, high_watermark = connector.checkpoint_from_items(items)
            high_watermark.update({"indexed_artifacts": indexed, "skipped_artifacts": skipped})
            connector.set_checkpoint(cursor, high_watermark, "healthy")
            self.state.mark_source_indexed(source.name, now)
            if self.vectors is not None:
                # Synapse 21.4: additive keys, present only when embeddings
                # are enabled so default sync_events stay byte-identical.
                details_extra = {
                    "embedded_chunks": embedded_chunks,
                    "pruned_vectors": pruned_vectors,
                }
            # Synapse 21.3: record skipped-vs-fetched transfer counts per sync.
            self.state.append_sync_event(
                uuid.uuid4().hex,
                source.name,
                "sync.completed",
                "healthy",
                started_at,
                now,
                {
                    "mode": mode,
                    "connector_type": connector.connector_type,
                    "fetched": fetched,
                    "skipped": transfer_skipped,
                    "indexed_artifacts": indexed,
                    "skipped_artifacts": skipped,
                    **details_extra,
                },
            )
            # Synapse 21.5: (re)publish the contract + emit/POST sync.completed.
            # Still inside the writer lock + per-source lock; fail-soft so a
            # publish/webhook hiccup never fails the sync.
            self._publish_after_sync(source.name, mode, started_at, now, indexed, skipped)
            capacity = self._graph_capacity_notice()
            if capacity:
                details_extra = {**details_extra, "capacity": capacity}
            return SyncResult(
                source.name,
                indexed,
                skipped,
                self.graph_builder.graph.number_of_nodes(),
                self.graph_builder.graph.number_of_edges(),
                "healthy",
                {
                    "connector_type": connector.connector_type,
                    "fetched": fetched,
                    "skipped": transfer_skipped,
                    "checkpoint": self.state.get_source_checkpoint(source.name),
                    **details_extra,
                },
            )

    def _graph_capacity_notice(self) -> dict[str, Any] | None:
        """Say something once a graph outgrows one container (Phase 35.3).

        Deliberately a *notice*, not a refusal. `sync.limits` can refuse
        because it checks before any work happens; by the time this can fire,
        the index exists and throwing it away would help nobody.

        The threshold is measured, not guessed — see `graph/capacity.py`. The
        binding constraint is not RAM but checkpoint serialization: at 1.58M
        nodes one `graph.latest.json.zst` write takes ~20s against a default
        60s `storage.graph_checkpoint_seconds`, so a third of a long sync goes
        on writing the graph out, and it gets worse linearly.
        """

        limit = getattr(self.config.graph, "max_nodes", None)
        if not limit:
            return None
        nodes = self.graph_builder.graph.number_of_nodes()
        if nodes <= int(limit):
            return None
        # ~2.4 KB of process RSS per node, flat across four measured scales.
        projected_bytes = nodes * 2400
        projected_gb = projected_bytes / 1e9
        notice = {
            "nodes": nodes,
            "max_nodes": int(limit),
            # Exact bytes as well as the rounded figure: at a low threshold the
            # GB value rounds to 0.0, and a capacity notice reporting "0.0 GB"
            # reads as a bug rather than as a small graph.
            "projected_rss_bytes": projected_bytes,
            "projected_rss_gb": round(projected_gb, 2),
            "advice": (
                "This graph has outgrown what one region holds comfortably. Split the "
                "corpus across several pheasant regions and let the router fan out "
                "(see docs/how-to/capacity-planning.md); raising graph.max_nodes "
                "silences this without changing the cost."
            ),
        }
        logger.warning(
            "Graph is %d nodes, past graph.max_nodes=%d (~%.1f GB RSS). %s",
            nodes,
            int(limit),
            projected_gb,
            notice["advice"],
        )
        return notice

    @property
    def stats(self) -> dict[str, int]:
        return {
            "node_count": self.graph_builder.graph.number_of_nodes(),
            "edge_count": self.graph_builder.graph.number_of_edges(),
            "artifact_count": int(self.state.rows("SELECT COUNT(*) AS c FROM artifacts")[0]["c"]),
            "chunk_count": int(self.state.rows("SELECT COUNT(*) AS c FROM chunks")[0]["c"]),
        }

    def vector_searcher(self) -> VectorSearcher | None:
        """Query-time vector searcher sharing this engine's embedder/store."""

        if self.vectors is None:
            return None
        return VectorSearcher(self.vectors.embedder, self.vectors.store, self.state)

    def search_context(self, query: str, max_results: int = 10) -> dict:
        from pheasant.search.hybrid import HybridSearch
        from pheasant.search.sqlite_store import SearchStore

        return HybridSearch(
            SearchStore(self.state),
            vector=self.vector_searcher(),
            wasm_relationship_search=self.config.search.wasm_relationship_search,
        ).search_context(
            self.config.knowledge_base_id,
            query,
            max_results=max_results,
        )

    def _snapshot_after_sync(self) -> None:
        """Write a compressed graph snapshot (interval-throttled) + prune.

        Gated by ``storage.graph_snapshots`` (default on). At most one snapshot
        per ``graph_snapshot_interval_seconds``: if the newest existing snapshot
        is younger than the interval, no new snapshot is written. Retention then
        evicts oldest snapshots beyond ``max_state_size_gb``. Every step is
        fail-soft — the snapshot is additive history, never load-bearing for a
        sync.
        """

        storage = self.config.storage
        if not storage.graph_snapshots:
            return
        kb_id = self.config.knowledge_base_id
        try:
            now = utc_now()
            if self._snapshot_due(kb_id, now, storage.graph_snapshot_interval_seconds):
                self.graph_store.write_snapshot(kb_id, self.graph_builder.graph, now)
            max_bytes = int(float(storage.max_state_size_gb) * 1024**3)
            self.graph_store.enforce_retention(kb_id, max_bytes)
        except Exception as exc:  # noqa: BLE001 - snapshots must not fail sync
            import logging

            logging.getLogger(__name__).warning(
                "Graph snapshot/retention failed for %s: %s", kb_id, exc
            )

    def _snapshot_due(self, kb_id: str, now: str, interval_seconds: int) -> bool:
        """True when no snapshot exists yet or the newest is older than interval."""

        if interval_seconds <= 0:
            return True
        snapshots = self.graph_store.list_snapshots(kb_id)
        if not snapshots:
            return True
        from datetime import datetime

        from pheasant.persistence.graph_store import SNAPSHOT_PATTERN

        match = SNAPSHOT_PATTERN.match(snapshots[-1].name)
        if not match:
            return True
        # Filename ts is the ISO-8601 form with ':' -> '-'; restore the time
        # separators (positions of the two ':' in HH:MM:SS) for parsing.
        raw = match.group("ts")
        iso = _restore_iso_ts(raw)
        try:
            last = datetime.fromisoformat(iso)
            current = datetime.fromisoformat(now.rstrip("Z"))
        except ValueError:
            return True
        return (current - last).total_seconds() >= interval_seconds

    def _publish_after_sync(
        self,
        source_name: str,
        mode: str,
        started_at: str,
        finished_at: str,
        indexed: int,
        skipped: int,
    ) -> None:
        """Publish the contract + emit/POST the sync.completed event.

        Entirely gated by ``synapse.publish`` (default off) so a router-less
        standalone region is byte-for-byte unchanged. When on, (re)derives +
        writes ``<state>/contract.latest.json``, appends the local NDJSON
        ``sync.completed`` event, and — when ``synapse.router_url`` is set —
        POSTs the event (with the inline contract) to the router. Every step is
        fail-soft: a publish/webhook hiccup never fails the sync.
        """

        if not self.config.synapse.publish:
            return
        contract: dict | None = None
        try:
            store = self.vectors.store if self.vectors is not None else None
            contract = ContractPublisher(self.config, self.state, vector_store=store).publish(
                generated_at=finished_at
            )
        except Exception as exc:  # noqa: BLE001 - publication must not fail sync
            import logging

            logging.getLogger(__name__).warning(
                "Synapse contract publication failed for %s: %s", source_name, exc
            )
        event: dict = {
            "type": "sync.completed",
            "kb_id": self.config.knowledge_base_id,
            "source_id": source_name,
            "mode": mode,
            "started_at": started_at,
            "finished_at": finished_at,
            "indexed_artifacts": indexed,
            "skipped_artifacts": skipped,
        }
        if self.config.synapse.fleet_id:
            event["fleet_id"] = self.config.synapse.fleet_id
        if self.config.synapse.endpoint:
            event["endpoint"] = self.config.synapse.endpoint
        self.events.append(event, now=finished_at)
        if self.router_webhook.enabled:
            payload = dict(event)
            if contract is not None:
                payload["contract"] = contract
            self.router_webhook.post_event(payload)

    def _emit_source_changed(self, source_name: str, now: str | None = None) -> None:
        """Emit a local ``source.changed`` event (used by the watcher)."""

        self.events.append(
            {
                "type": "source.changed",
                "kb_id": self.config.knowledge_base_id,
                "source_id": source_name,
            },
            now=now,
        )

    def scan_source(
        self,
        source_name: str,
        *,
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Estimate what a source would index, without indexing anything.

        The pre-flight for pointing a source at something large: it reports
        the file count, total size, where the weight actually sits, and — via
        ``depth_options`` — how many files each depth cap would admit, so a
        depth can be chosen from evidence instead of guessed. Reads no file
        content and writes nothing.

        Runs with the budget *disabled* on purpose: the whole point is to
        learn the real size, including when it is over the limit. The
        absolute entry ceiling in the walker still applies.
        """
        source = self.config.effective_source(
            self._source(source_name), max_depth=max_depth, full_scan=full_scan
        )
        configured = WalkBudget.from_settings(source.limits)
        if source.type not in FILESYSTEM_SOURCE_TYPES:
            return {
                "source_id": source.name,
                "type": source.type.value,
                "scannable": False,
                "reason": "scan estimates filesystem sources; this source pulls from a service.",
            }
        if not source.path.exists():
            return {
                "source_id": source.name,
                "type": source.type.value,
                "scannable": False,
                "reason": f"source path does not exist in this environment: {source.path}",
            }
        report = walk_source(
            source.path,
            include=source.include,
            exclude=source.exclude,
            max_depth=source.max_depth,
            # Size the *real* tree, then report which limits it would trip.
            budget=WalkBudget(
                max_files=None,
                max_file_size_bytes=configured.max_file_size_bytes,
                max_total_bytes=None,
            ),
            follow_symlinks=bool(getattr(source.limits, "follow_symlinks", False)),
        )
        would_exceed = []
        if configured.max_files is not None and report.file_count > configured.max_files:
            would_exceed.append(f"max_files ({report.file_count} > {configured.max_files})")
        over_total = (
            configured.max_total_bytes is not None
            and report.total_bytes > configured.max_total_bytes
        )
        if over_total:
            would_exceed.append(
                f"max_total_mb ({report.total_bytes // (1024 * 1024)} > "
                f"{configured.max_total_bytes // (1024 * 1024)})"
            )
        return {
            "source_id": source.name,
            "type": source.type.value,
            "path": str(source.path),
            "scannable": True,
            **report.as_dict(),
            "depth_options": aggregate_depth_counts(report),
            "limits": {
                "max_files": configured.max_files,
                "max_file_size_mb": (
                    configured.max_file_size_bytes // (1024 * 1024)
                    if configured.max_file_size_bytes
                    else None
                ),
                "max_total_mb": (
                    configured.max_total_bytes // (1024 * 1024)
                    if configured.max_total_bytes
                    else None
                ),
            },
            "would_exceed": would_exceed,
            "would_sync": not would_exceed,
        }

    def _source(self, name: str) -> SourceConfig:
        for source in self.config.sources:
            if source.name == name:
                return source
        # Sources added at runtime (the UI's quick-add, POST /sources) live in
        # the state registry, not the config file. Reading them back is what
        # lets a *different process* — the sync worker, or the CLI — index a
        # source that was never written to YAML.
        row = self.state.get_source(name)
        if row and row.get("config_json"):
            payload = json.loads(row["config_json"])
            resolved = PheasantConfig.model_validate({"sources": [payload]}).sources[0]
            self.config.sources.append(resolved)
            return resolved
        raise KeyError(f"Unknown source: {name}")

    def _can_skip_before_read(
        self,
        mode: SyncMode,
        previous: dict | None,
        item: ConnectorItem,
    ) -> bool:
        return (
            mode == "incremental"
            and previous is not None
            and item.sha256 is not None
            and previous.get("sha256") == item.sha256
        )

    def _needs_repair(
        self,
        source_id: str,
        artifact_id: str,
        sha256: str,
        previous: dict | None,
        expected_chunks: int,
    ) -> bool:
        if not previous or previous.get("sha256") != sha256:
            return True
        state = self.state.artifact_state(source_id, artifact_id)
        if state is None:
            return True
        # A node missing from the graph is as broken as a missing chunk row —
        # it is the case an interrupted sync leaves behind, and it is what
        # makes "repair" cheaper than "index it all again".
        if not self.graph_builder.graph.has_node(artifact_id):
            return True
        return (
            state.get("sha256") != sha256
            or state.get("status") != "indexed"
            or int(state.get("chunk_count") or 0) < expected_chunks
        )
