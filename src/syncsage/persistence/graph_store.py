from __future__ import annotations

import json
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
        tmp.write_text(json.dumps(graph.to_node_link(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, kb_id: str) -> SimpleMultiDiGraph:
        path = self.graph_path(kb_id)
        if not path.exists():
            return SimpleMultiDiGraph()
        return SimpleMultiDiGraph.from_node_link(json.loads(path.read_text(encoding="utf-8")))
