"""One-shot SQLite → Postgres state migration (Phase 35.2).

``/state`` is user data (CLAUDE.md rule 2), so this obeys the same three rules
every other migration in pheasant does: **one-shot, idempotent, and it never
destroys the original.** The SQLite file is renamed ``*.migrated``, not
deleted, so a migration that turns out to have been a mistake is a rename away
from being undone.

Copying rather than re-indexing is the point. A large corpus takes hours to
index and may need an embedding provider that costs money; the rows are already
correct, and the stable IDs in them are a contract
(:mod:`pheasant.persistence.schema` shares every table definition verbatim so
they carry over byte-identically).

``chunks_fts`` is deliberately **not** copied. It is a derived cache — the same
argument the FTS title rebuild makes — and on Postgres its ``search_vector`` is
a generated column, so re-inserting the rows regenerates it correctly for the
target dialect. Copying it row-for-row would carry SQLite's tokenization into a
database that tokenizes differently.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pheasant.persistence.backends import PostgresBackend, SqliteBackend
from pheasant.persistence.secrets import redact_dsn
from pheasant.persistence.state_store import StateStore

logger = logging.getLogger(__name__)

#: Copied in dependency order: a row whose foreign key is not yet present would
#: be rejected. ``chunks_fts`` is rebuilt from ``chunks``, never copied.
TABLE_ORDER = (
    "knowledge_bases",
    "sources",
    "artifacts",
    "chunks",
    "symbols",
    "artifact_terms",
    "sync_events",
    "source_checkpoints",
    "source_audit_events",
    "manifests",
    "idp_groups",
    "idp_sync_meta",
    "memory_records",
    "sync_fingerprints",
)

#: Rows per INSERT batch. Large enough that a million-row table is not a
#: million round trips, small enough not to build a multi-GB parameter list.
BATCH = 1_000


class MigrationError(RuntimeError):
    """The migration could not complete, and nothing was renamed."""


def _copy_table(source: StateStore, target: StateStore, table: str) -> int:
    rows = source.rows(f"SELECT * FROM {table}")
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ",".join("?" for _ in columns)
    statement = f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})"
    payload = [tuple(row[column] for column in columns) for row in rows]
    for start in range(0, len(payload), BATCH):
        target.backend.executemany(statement, payload[start : start + BATCH])
    target.conn.commit()
    return len(payload)


def _rebuild_fts(target: StateStore) -> int:
    """Regenerate ``chunks_fts`` from the copied tables, for the target dialect.

    The same projection ``_migrate_fts_titles`` uses, so ``title`` is the
    basename and not a second copy of the path — the distinction the
    2026-08-03 ranking work depends on.
    """

    target.conn.execute("DELETE FROM chunks_fts")
    target.conn.execute(
        """INSERT INTO chunks_fts(
               chunk_id, source_id, artifact_id, title, path, heading_path, text)
           SELECT chunks.id, chunks.source_id, chunks.artifact_id,
                  replace(
                      COALESCE(artifacts.relative_path, artifacts.path),
                      rtrim(COALESCE(artifacts.relative_path, artifacts.path),
                            replace(COALESCE(artifacts.relative_path, artifacts.path),
                                    '/', '')),
                      ''),
                  COALESCE(artifacts.relative_path, artifacts.path),
                  COALESCE(chunks.heading_path, ''),
                  chunks.text
           FROM chunks JOIN artifacts ON artifacts.id = chunks.artifact_id"""
    )
    target.conn.commit()
    return len(target.rows("SELECT chunk_id FROM chunks_fts"))


def migrate_sqlite_to_postgres(
    sqlite_path: str | Path,
    dsn: str,
    *,
    pool_size: int = 10,
    keep_original: bool = True,
) -> dict[str, Any]:
    """Copy a SQLite state database into Postgres and verify it landed.

    Returns a report: per-table row counts, the verification outcome, and where
    the original was parked.

    **Idempotent.** A target that already holds rows for a table is left alone
    rather than double-inserted, so an interrupted run can simply be re-run.
    """

    source_path = Path(sqlite_path)
    if not source_path.is_file():
        raise MigrationError(f"no SQLite state database at {source_path}")

    source = StateStore(backend=SqliteBackend(source_path))
    target = StateStore(backend=PostgresBackend(dsn, pool_size=pool_size))
    report: dict[str, Any] = {
        "source": str(source_path),
        "target": redact_dsn(dsn),
        "tables": {},
        "skipped": [],
    }
    try:
        target.migrate()
        for table in TABLE_ORDER:
            if not source.backend.table_columns(table):
                continue  # older state file that predates this table
            existing = target.rows(f"SELECT count(*) AS n FROM {table}")
            if existing and int(existing[0]["n"]) > 0:
                report["skipped"].append(table)
                continue
            report["tables"][table] = _copy_table(source, target, table)
        report["chunks_fts"] = _rebuild_fts(target)

        # Verify before renaming anything. A migration that silently dropped
        # half the artifacts and then moved the original aside would be the
        # worst possible outcome.
        mismatches = []
        for table, copied in report["tables"].items():
            landed = int(target.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"])
            if landed != copied:
                mismatches.append(f"{table}: copied {copied}, found {landed}")
        source_chunks = int(source.rows("SELECT count(*) AS n FROM chunks")[0]["n"])
        if report["chunks_fts"] != source_chunks:
            mismatches.append(
                f"chunks_fts: rebuilt {report['chunks_fts']} rows for {source_chunks} chunks"
            )
        if mismatches:
            report["verified"] = False
            report["mismatches"] = mismatches
            raise MigrationError(
                "migration did not verify; the original was left untouched: "
                + "; ".join(mismatches)
            )
        report["verified"] = True
    finally:
        source.close()
        target.close()

    if keep_original:
        parked = source_path.with_suffix(source_path.suffix + ".migrated")
        # Never overwrite an earlier parked copy — that would destroy the very
        # thing this is preserving.
        counter = 1
        while parked.exists():
            parked = source_path.with_suffix(f"{source_path.suffix}.migrated.{counter}")
            counter += 1
        source_path.rename(parked)
        report["original_renamed_to"] = str(parked)
        logger.info("SQLite state parked at %s", parked)
    return report
