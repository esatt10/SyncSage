from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from syncsage.config.schema import SourceConfig, SyncSageConfig
from syncsage.graph.builder import GraphBuilder
from syncsage.ingestion.pipeline import git_state, parse_connector_payload, utc_now
from syncsage.persistence.graph_store import GraphStore
from syncsage.persistence.manifest import ManifestStore
from syncsage.persistence.paths import StatePaths
from syncsage.persistence.state_store import StateStore
from syncsage.registry.source_registry import SourceRegistry
from syncsage.search.vector_store import (
    VectorSearcher,
    vector_indexer_from_config,
)
from syncsage.synapse.events import EventStream, RouterWebhook
from syncsage.synapse.publisher import ContractPublisher
from syncsage.sync.connectors import ConnectorItem, ItemNotModified, connector_for_source
from syncsage.sync.locks import EngineLease, source_lock

SyncMode = Literal["incremental", "full", "validate_only", "repair"]
SYNC_MODES = {"incremental", "full", "validate_only", "repair"}


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


class SyncEngine:
    def __init__(
        self,
        config: SyncSageConfig,
        paths: StatePaths | None = None,
        state: StateStore | None = None,
        lease: EngineLease | None = None,
    ):
        self.config = config
        self.paths = paths or StatePaths.from_config(config)
        self.paths.ensure()
        self.state = state or StateStore(self.paths.sqlite)
        self.state.migrate()
        # Single-writer lease (Synapse 21.2): acquired lazily on the first
        # sync so read-only engines (e.g. docker-exec MCP stdio beside a
        # running server) keep working against a served state directory.
        self.lease = lease or EngineLease(self.paths.state)
        # In-process serialization across all writers (watcher, scheduler,
        # API startup executor, HTTP /sync): SyncEngine is not safe for
        # concurrent syncs within a process.
        self._sync_mutex = threading.RLock()
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
        # Synapse 21.5: contract publisher + NDJSON event stream. The event
        # log is always written (local, useful standalone); contract
        # publication + the router webhook are gated by synapse.publish /
        # synapse.router_url so a router-less region is unchanged.
        self.events = EventStream(self.paths.state)
        self.router_webhook = RouterWebhook(
            config.synapse.router_url,
            timeout=config.synapse.webhook_timeout_seconds,
        )

    def close(self) -> None:
        """Release the writer lease and close the state store."""
        self.lease.release()
        self.state.close()

    def startup(self) -> list[SyncResult]:
        self.paths.ensure()
        self.state.migrate()
        SourceRegistry(self.config, self.state).initialize()
        results = []
        for source in self.config.sources:
            if source.enabled and source.sync.on_startup:
                results.append(self.sync_source(source.name, "incremental"))
        return results

    def sync_all(self, mode: SyncMode = "incremental") -> list[SyncResult]:
        return [
            self.sync_source(source.name, mode) for source in self.config.sources if source.enabled
        ]

    def sync_source(self, source_name: str, mode: SyncMode = "incremental") -> SyncResult:
        if mode not in SYNC_MODES:
            raise ValueError(f"Unsupported sync mode: {mode}")
        source = self._source(source_name)
        self.lease.acquire()
        # Test-only hook (Synapse 21.2 crash-safety tests): widens the window
        # between per-artifact writes so a kill -9 can land mid-sync.
        slow_sync_s = float(os.environ.get("SYNCSAGE_TEST_SLOW_SYNC_MS", "0") or 0) / 1000.0
        with self._sync_mutex, source_lock(self.paths.locks, source.name):
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
            items = connector.list_items()
            if mode == "full":
                self.state.delete_source_artifacts(source.name)
                self.graph_builder.remove_source_content(source.name)
                manifest = {"source_id": source.name, "artifacts": {}}
                artifacts = manifest["artifacts"]
            indexed = 0
            skipped = 0
            fetched = 0
            transfer_skipped = 0
            embedded_chunks = 0
            git_metadata = git_state(source.path) if source.type.value == "repository" else None
            for item in items:
                previous = artifacts.get(item.relative_path)
                if self._can_skip_before_read(mode, previous, item):
                    skipped += 1
                    transfer_skipped += 1
                    continue
                try:
                    payload = connector.read_item(item)
                except ItemNotModified:
                    skipped += 1
                    transfer_skipped += 1
                    continue
                fetched += 1
                parsed = parse_connector_payload(source, item, payload, git_metadata)
                if parsed is None:
                    continue
                if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
                    skipped += 1
                    continue
                if mode == "repair" and not self._needs_repair(
                    source.name,
                    parsed.id,
                    parsed.sha256,
                    previous,
                    len(parsed.chunks),
                ):
                    skipped += 1
                    continue
                now = utc_now()
                artifact_row = {
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
                self.state.replace_artifact_chunks(artifact_row, chunk_rows)
                if self.vectors is not None:
                    # Chunk ids are content-addressed (text_hash in the id),
                    # so only new/changed chunk text reaches the embedder.
                    embedded_chunks += self.vectors.index_artifact(
                        source.name, parsed.id, chunk_rows
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
                indexed += 1
                if slow_sync_s:
                    time.sleep(slow_sync_s)
            pruned_vectors = 0
            if self.vectors is not None:
                # Drop vectors whose chunks left the chunks table (removed
                # artifacts, re-chunked text, full-mode deletions).
                live_chunk_ids = {
                    str(row["id"])
                    for row in self.state.rows(
                        "SELECT id FROM chunks WHERE source_id=?", (source.name,)
                    )
                }
                pruned_vectors = self.vectors.prune_source(source.name, live_chunk_ids)
            self.graph_builder.add_similarity_edges(source.name)
            manifest["last_indexed_at"] = utc_now()
            manifest["connector"] = {"type": connector.connector_type}
            self.manifests.save(source.name, manifest)
            self.graph_store.save(self.config.knowledge_base_id, self.graph_builder.graph)
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
        from syncsage.search.hybrid import HybridSearch
        from syncsage.search.sqlite_store import SearchStore

        return HybridSearch(SearchStore(self.state), vector=self.vector_searcher()).search_context(
            self.config.knowledge_base_id,
            query,
            max_results=max_results,
        )

    def export_obsidian_notes(
        self,
        vault_path=None,
        preview: bool = False,
        template_profile: str | None = None,
    ) -> dict:
        from syncsage.obsidian.exporter import ObsidianExporter

        if vault_path is not None:
            self.config.syncsage.vault_path = vault_path
        return ObsidianExporter(self.config, self.state).export(
            preview=preview,
            template_profile=template_profile,
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

        from syncsage.persistence.graph_store import SNAPSHOT_PATTERN

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

    def _source(self, name: str) -> SourceConfig:
        for source in self.config.sources:
            if source.name == name:
                return source
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
        return (
            state.get("sha256") != sha256
            or state.get("status") != "indexed"
            or int(state.get("chunk_count") or 0) < expected_chunks
        )
