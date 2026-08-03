from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from syncsage.graph.simple import SimpleMultiDiGraph

# Synapse step 21.6 (session A): compressed timestamped graph snapshots.
# A snapshot lives beside the (uncompressed, fast-load) ``graph.latest.json``
# as ``graph.<utc-ts>.json.zst`` so history accrues without bloating the hot
# load path. The timestamp is a filesystem-safe ISO-8601 form (``:`` → ``-``).
SNAPSHOT_PATTERN = re.compile(r"^graph\.(?P<ts>.+)\.json\.zst$")


def _ts_for_filename(utc_ts: str) -> str:
    """Filesystem-safe form of an ISO-8601 UTC timestamp."""

    return utc_ts.replace(":", "-")


def _tmp_for(path: Path) -> Path:
    """A temp sibling of ``path`` unique to the writer.

    A fixed ``*.tmp`` name is not safe once two threads can save at the same
    time — a sync pass and a ``DELETE /sources`` handler, say. Both wrote the
    same file, the first ``os.replace`` renamed it away and the second raised
    ``FileNotFoundError``; worse, their interleaved writes could leave a
    corrupt payload behind. Keying the name on pid+thread keeps tmp+rename
    atomic per writer, so concurrent saves are last-writer-wins over two
    individually-complete files.
    """

    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


class GraphStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # One store instance is shared by the sync thread and the API request
        # threads (``DELETE /sources`` saves the graph too), so writes are
        # serialized here: it keeps two threads from serializing the same
        # graph twice, and on Windows a concurrent ``os.replace`` onto a
        # destination another thread is replacing fails outright.
        self._write_lock = threading.Lock()

    def kb_dir(self, kb_id: str) -> Path:
        path = self.root / kb_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def graph_path(self, kb_id: str) -> Path:
        """Where the current graph lives: zstd-compressed node-link JSON.

        Uncompressed, a real index is enormous — 445k nodes and 1.5M edges
        pretty-printed came to 928MB, which every checkpoint then rewrote and
        every start re-parsed. The payload is highly repetitive JSON, so zstd
        takes it down by roughly an order of magnitude for a few percent of
        the CPU the write was already costing.
        """

        return self.kb_dir(kb_id) / "graph.latest.json.zst"

    def legacy_graph_path(self, kb_id: str) -> Path:
        """Pre-compression location, still read once so no state is stranded."""

        return self.kb_dir(kb_id) / "graph.latest.json"

    def save(self, kb_id: str, graph: SimpleMultiDiGraph) -> Path:
        import zstandard as zstd

        path = self.graph_path(kb_id)
        tmp = _tmp_for(path)
        # Durable tmp+rename (Synapse step 21.2): fsync the file before the
        # rename and best-effort fsync the directory after it, so a crash
        # never leaves a torn or vanished graph.latest.json.
        try:
            with self._write_lock:
                # Compact, key-sorted: sorting keeps the bytes deterministic
                # for a given graph, and dropping the indentation removes
                # hundreds of megabytes of whitespace nothing reads.
                payload = json.dumps(
                    graph.to_node_link(), sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                with tmp.open("wb") as fh:
                    # Level 3: the write path is latency-sensitive (it runs
                    # mid-sync), and levels above this cost far more CPU for a
                    # few percent of size. Snapshots, written rarely, use 10.
                    fh.write(zstd.ZstdCompressor(level=3).compress(payload))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._fsync_dir(path.parent)
        self._retire_legacy(kb_id)
        self._sweep_stale_tmp(path.parent)
        return path

    @staticmethod
    def _sweep_stale_tmp(directory: Path, max_age_s: float = 3600.0) -> None:
        """Delete temp files no live writer could still own.

        A writer cleans up its own temp on failure, but a killed process (a
        container recreate mid-save) cannot — and those leftovers accumulate in
        /state at full graph size. Only ``*.tmp`` written by this store is
        touched, and only once it is far too old to belong to a running save.
        """

        cutoff = time.time() - max_age_s
        for stale in directory.glob("*.tmp"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
            except OSError:  # pragma: no cover - racing another writer is fine
                pass

    def _retire_legacy(self, kb_id: str) -> None:
        """One-shot: park a pre-compression graph beside the new one.

        Renamed, never deleted — /state is user data — and only after the
        compressed file it was replaced by is safely on disk.
        """

        legacy = self.legacy_graph_path(kb_id)
        if not legacy.exists():
            return
        try:
            legacy.rename(legacy.with_suffix(".json.migrated"))
        except OSError:  # pragma: no cover - best effort, never fails a sync
            pass

    def load(self, kb_id: str) -> SimpleMultiDiGraph:
        import zstandard as zstd

        path = self.graph_path(kb_id)
        if path.exists():
            raw = zstd.ZstdDecompressor().decompress(path.read_bytes())
            return SimpleMultiDiGraph.from_node_link(json.loads(raw))
        # Pre-compression state directory: read it once; the next save writes
        # the compressed form and retires this file.
        legacy = self.legacy_graph_path(kb_id)
        if legacy.exists():
            return SimpleMultiDiGraph.from_node_link(json.loads(legacy.read_bytes()))
        return SimpleMultiDiGraph()

    # --- snapshots (Synapse step 21.6, session A) ---------------------------

    def snapshot_path(self, kb_id: str, utc_ts: str) -> Path:
        return self.kb_dir(kb_id) / f"graph.{_ts_for_filename(utc_ts)}.json.zst"

    def write_snapshot(self, kb_id: str, graph: SimpleMultiDiGraph, utc_ts: str) -> Path:
        """Write a zstd-compressed, durably-fsynced graph snapshot.

        The current graph is serialized identically to ``graph.latest.json``
        (``indent=2, sort_keys=True``) then zstd-compressed. tmp+rename+fsync
        keeps a crash from leaving a torn ``.zst`` file.
        """

        import zstandard as zstd

        path = self.snapshot_path(kb_id, utc_ts)
        tmp = _tmp_for(path)
        payload = json.dumps(graph.to_node_link(), indent=2, sort_keys=True).encode("utf-8")
        compressed = zstd.ZstdCompressor(level=10).compress(payload)
        try:
            with self._write_lock:
                with tmp.open("wb") as fh:
                    fh.write(compressed)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._fsync_dir(path.parent)
        return path

    def read_snapshot(self, path: str | Path) -> SimpleMultiDiGraph:
        """Decompress + load a ``graph.<ts>.json.zst`` snapshot."""

        import zstandard as zstd

        raw = Path(path).read_bytes()
        text = zstd.ZstdDecompressor().decompress(raw).decode("utf-8")
        return SimpleMultiDiGraph.from_node_link(json.loads(text))

    def list_snapshots(self, kb_id: str) -> list[Path]:
        """All snapshot files for a KB, oldest-first (by embedded timestamp)."""

        kb_dir = self.root / kb_id
        if not kb_dir.exists():
            return []
        # The current graph is `graph.latest.json.zst`, which matches the
        # snapshot pattern by shape. It is not history and must never be
        # offered to retention, which deletes what it is given.
        live = self.graph_path(kb_id).name
        snapshots = [
            p for p in kb_dir.iterdir() if p.name != live and SNAPSHOT_PATTERN.match(p.name)
        ]
        # Filenames embed a sortable ISO-8601 timestamp, so lexical sort on the
        # name is chronological; ties fall back to mtime for robustness.
        snapshots.sort(key=lambda p: (p.name, p.stat().st_mtime))
        return snapshots

    def enforce_retention(self, kb_id: str, max_bytes: int) -> list[Path]:
        """Delete oldest snapshots until total snapshot bytes ≤ ``max_bytes``.

        Only snapshot ``.zst`` files are ever evicted — never
        ``graph.latest.json``, the SQLite db, or the contract. Returns the list
        of evicted paths (oldest-first). A non-positive ``max_bytes`` disables
        retention (keep everything).
        """

        if max_bytes <= 0:
            return []
        snapshots = self.list_snapshots(kb_id)
        total = sum(p.stat().st_size for p in snapshots)
        evicted: list[Path] = []
        # Always keep the newest snapshot even if it alone exceeds the cap — a
        # region must retain at least one point-in-time graph history entry.
        for path in snapshots[:-1]:
            if total <= max_bytes:
                break
            size = path.stat().st_size
            path.unlink()
            total -= size
            evicted.append(path)
        if evicted:
            self._fsync_dir(self.root / kb_id)
        return evicted

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:  # pragma: no cover - platform-dependent best effort
            pass
