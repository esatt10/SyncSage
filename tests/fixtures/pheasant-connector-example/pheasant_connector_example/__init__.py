"""Example pheasant connector plugin — the canonical third-party shape.

Registers source type ``staticdir`` (see ``pyproject.toml``): a
deliberately tiny connector that indexes ``*.txt`` files under the
source's ``path``. Real connectors follow exactly this outline —
subclass :class:`~pheasant.sync.connectors.SourceConnector`, emit
deterministic identities, and let ``checkpoint_from_items`` capture the
incremental cursor. Hold it to the public bar with
``pheasant.testing.ConnectorConformance``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from pheasant.sync.connectors import ConnectorItem, ConnectorPayload, SourceConnector


class StaticDirConnector(SourceConnector):
    connector_type = "staticdir"

    def list_items(self) -> list[ConnectorItem]:
        root = self.source.path
        if not root.is_dir():
            return []
        items: list[ConnectorItem] = []
        for path in sorted(root.rglob("*.txt")):
            stat = path.stat()
            content = path.read_bytes()
            items.append(
                ConnectorItem(
                    identity=f"staticdir:{self.source.name}:{path.relative_to(root).as_posix()}",
                    relative_path=path.relative_to(root).as_posix(),
                    uri=path.resolve().as_uri(),
                    mime_type="text/plain",
                    size_bytes=stat.st_size,
                    sha256=hashlib.sha256(content).hexdigest(),
                    mtime=datetime.fromtimestamp(stat.st_mtime, UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    metadata={"path": str(path)},
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        content = bytes((self.source.path / item.relative_path).read_bytes())
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=item.mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=item.mtime,
            metadata=dict(item.metadata),
        )

    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        newest = max((item.mtime or "" for item in items), default="")
        return {"item_count": len(items)}, {"max_mtime": newest}
