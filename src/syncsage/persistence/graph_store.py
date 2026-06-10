from __future__ import annotations

import json
import os
from pathlib import Path

from syncsage.graph.simple import SimpleMultiDiGraph


class GraphStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def graph_path(self, kb_id: str) -> Path:
        path = self.root / kb_id
        path.mkdir(parents=True, exist_ok=True)
        return path / "graph.latest.json"

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
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:  # pragma: no cover - platform-dependent best effort
            pass
        return path

    def load(self, kb_id: str) -> SimpleMultiDiGraph:
        path = self.graph_path(kb_id)
        if not path.exists():
            return SimpleMultiDiGraph()
        return SimpleMultiDiGraph.from_node_link(json.loads(path.read_text(encoding="utf-8")))
