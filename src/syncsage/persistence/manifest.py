from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ManifestStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, source_id: str) -> Path:
        safe = source_id.replace("/", "__")
        return self.root / f"{safe}.manifest.json"

    def load(self, source_id: str) -> dict[str, Any]:
        path = self.path_for(source_id)
        if not path.exists():
            return {"source_id": source_id, "artifacts": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, source_id: str, manifest: dict[str, Any]) -> None:
        path = self.path_for(source_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
