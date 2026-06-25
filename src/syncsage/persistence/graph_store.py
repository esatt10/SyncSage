from __future__ import annotations

import json
import os
import re
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


class GraphStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def kb_dir(self, kb_id: str) -> Path:
        path = self.root / kb_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def graph_path(self, kb_id: str) -> Path:
        return self.kb_dir(kb_id) / "graph.latest.json"

    def save(self, kb_id: str, graph: SimpleMultiDiGraph) -> Path:
        path = self.graph_path(kb_id)
        tmp = path.with_suffix(".tmp")
        # Durable tmp+rename (Synapse step 21.2): fsync the file before the
        # rename and best-effort fsync the directory after it, so a crash
        # never leaves a torn or vanished graph.latest.json.
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(graph.to_node_link(), indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        self._fsync_dir(path.parent)
        return path

    def load(self, kb_id: str) -> SimpleMultiDiGraph:
        path = self.graph_path(kb_id)
        if not path.exists():
            return SimpleMultiDiGraph()
        return SimpleMultiDiGraph.from_node_link(json.loads(path.read_text(encoding="utf-8")))

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
        tmp = path.with_suffix(".tmp")
        payload = json.dumps(graph.to_node_link(), indent=2, sort_keys=True).encode("utf-8")
        compressed = zstd.ZstdCompressor(level=10).compress(payload)
        with tmp.open("wb") as fh:
            fh.write(compressed)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
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
        snapshots = [p for p in kb_dir.iterdir() if SNAPSHOT_PATTERN.match(p.name)]
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
