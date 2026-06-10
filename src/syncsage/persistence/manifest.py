from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from syncsage.persistence.state_store import StateStore

logger = logging.getLogger(__name__)

LEGACY_SUFFIX = ".manifest.json"
MIGRATED_SUFFIX = ".migrated"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class ManifestStore:
    """Per-source sync manifests stored in the ``manifests`` SQLite table.

    Synapse step 21.2 moved manifests out of loose JSON files (split-brain
    risk beside the authoritative DB) into the StateStore. ``root`` is kept
    as the legacy location: at construction, every remaining
    ``<src>.manifest.json`` is migrated exactly once — inserted into the
    table (skipped if a row already exists) and renamed to
    ``<src>.manifest.json.migrated``. Originals are never deleted.
    """

    def __init__(self, root: str | Path, state: StateStore):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.migrate_legacy_files()

    def path_for(self, source_id: str) -> Path:
        """Legacy loose-JSON path (pre-21.2); manifests now live in SQLite."""
        safe = source_id.replace("/", "__")
        return self.root / f"{safe}{LEGACY_SUFFIX}"

    def exists(self, source_id: str) -> bool:
        return self.state.manifest_exists(source_id)

    def load(self, source_id: str) -> dict[str, Any]:
        payload = self.state.get_manifest(source_id)
        if payload is None:
            return {"source_id": source_id, "artifacts": {}}
        return payload

    def save(self, source_id: str, manifest: dict[str, Any]) -> None:
        self.state.set_manifest(source_id, manifest, _utc_now())

    def delete(self, source_id: str) -> None:
        self.state.delete_manifest(source_id)

    def migrate_legacy_files(self) -> int:
        """One-shot idempotent migration of legacy JSON manifests into SQLite.

        Insert-then-rename: if a crash lands between the two, the next startup
        sees the existing row, skips the insert, and finishes the rename.
        A second startup finds no ``*.manifest.json`` files and is a no-op.
        """
        migrated = 0
        for path in sorted(self.root.glob(f"*{LEGACY_SUFFIX}")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable legacy manifest %s: %s", path, exc)
                continue
            source_id = str(
                payload.get("source_id") or path.name[: -len(LEGACY_SUFFIX)].replace("__", "/")
            )
            if not self.state.manifest_exists(source_id):
                self.state.set_manifest(source_id, payload, _utc_now())
            target = path.with_name(path.name + MIGRATED_SUFFIX)
            counter = 1
            while target.exists():
                target = path.with_name(f"{path.name}{MIGRATED_SUFFIX}.{counter}")
                counter += 1
            path.rename(target)
            migrated += 1
        if migrated:
            logger.info(
                "Migrated %d legacy manifest file(s) from %s into SQLite",
                migrated,
                self.root,
            )
        return migrated
