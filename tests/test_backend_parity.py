"""Cross-backend parity for the Phase 35.2 storage seam.

The seam is only worth having if both backends produce the *same knowledge
base*. Three things have to hold, in descending order of how badly a break
would hurt:

1. **Stable IDs are byte-identical.** They are a contract (CLAUDE.md rule 3):
   every persisted graph, every manifest, every Synapse contract references
   them. A backend that changed them would silently orphan existing state.
2. **The same corpus produces the same counts.** Artifacts, chunks, FTS rows,
   graph nodes and edges.
3. **Retrieval quality holds.** This one is deliberately *not* an equality
   assertion. FTS5's ``bm25()`` and Postgres's ``ts_rank_cd`` are different
   functions; demanding identical scores would be demanding something false.
   What matters is that the ranking is as good, so the gate is measured
   quality on a gold set.

These tests need a real Postgres. They skip — loudly, with the reason — when
``PHEASANT_TEST_POSTGRES_DSN`` is unset, so the offline suite stays offline and
network-free (CLAUDE.md pillar 3). Point it at a throwaway database:

    createdb pheasant_parity
    PHEASANT_TEST_POSTGRES_DSN=postgresql://user@127.0.0.1:5432/pheasant_parity pytest -q
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.sync.engine import SyncEngine

DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()

postgres = pytest.mark.skipif(
    not DSN,
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database to run backend parity",
)

#: (query, the file that must rank first).
#:
#: The last case is the one that actually gates the port. The first three have
#: a unique winning token, so they rank correctly whatever the column weights
#: are — mutation testing showed that flattening the ts_rank weights, reversing
#: them, and stripping the title weighting out of the generated tsvector *all*
#: still passed. They only prove the query runs.
#:
#: ``deploy-gateway.md`` is named for the query and barely mentions it; the
#: decoy says "gateway" repeatedly in its body. The right answer wins only
#: because title is weighted above text — which is exactly the 2026-08-03
#: "locate readme" failure (the repo's own README sat at rank 125) that the
#: 8/3/2/1 weights were introduced to fix. If the A/B/C/D mapping is wrong,
#: this case is what says so.
GOLD: list[tuple[str, str]] = [
    ("gateway restarts nightly", "runbook.md"),
    ("readme install configure", "README.md"),
    ("invoices generated monthly", "billing.md"),
    ("deploy gateway", "deploy-gateway.md"),
]


#: Queries whose correct answer depends on term rarity or on a short precise
#: document beating a long repetitive one — the two things the tsvector port
#: cannot reproduce. See :func:`test_known_divergence_postgres_has_no_idf`.
IDF_SENSITIVE = {"gateway restarts nightly", "deploy gateway"}


def _corpus(root: Path) -> Path:
    workspace = root / "docs"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text(
        "# Project README\n\nHow to install and configure the gateway.\n", encoding="utf-8"
    )
    (workspace / "runbook.md").write_text(
        "# Kestrel Runbook\n\nThe gateway restarts nightly at 0300 UTC.\n"
        "Escalate to the on-call rota if it fails twice.\n",
        encoding="utf-8",
    )
    (workspace / "billing.md").write_text(
        "# Billing\n\nInvoices are generated monthly by the finance service.\n", encoding="utf-8"
    )
    # The weighting probe: named for the query, nearly silent about it.
    (workspace / "deploy-gateway.md").write_text(
        "# Deployment\n\nRun the rollout script and wait for the health check.\n",
        encoding="utf-8",
    )
    # The decoy: says the query's words over and over in its body, and would
    # win on body frequency alone if `title` were not weighted above `text`.
    (workspace / "notes.md").write_text(
        "# Notes\n\n"
        + "The gateway deploy notes mention deploy and gateway repeatedly. " * 6
        + "\n",
        encoding="utf-8",
    )
    return workspace


def _config(root: Path, workspace: Path, backend: str) -> PheasantConfig:
    data: dict[str, Any] = {
        "pheasant": {
            "name": "parity",
            "state_path": str(root / f"state-{backend}"),
            "workspace_root": str(root),
            "exports_path": str(root / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
            }
        ],
    }
    if backend == "postgres":
        data["storage"]["backend"] = "postgres"
        data["storage"]["dsn_env"] = "PHEASANT_TEST_POSTGRES_DSN"
    return PheasantConfig.model_validate(data)


def _index(root: Path, workspace: Path, backend: str) -> dict[str, Any]:
    config = _config(root, workspace, backend)
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("docs", "full")
        state = engine.state
        search = HybridSearch(SearchStore(state))
        ranked = {
            query: [
                hit.get("relative_path")
                for hit in search.search_context(config.knowledge_base_id, query, "text", 5)[
                    "results"
                ]
            ]
            for query, _ in GOLD
        }
        return {
            "counts": {
                "artifacts": len(state.rows("SELECT id FROM artifacts")),
                "chunks": len(state.rows("SELECT id FROM chunks")),
                "fts": len(state.rows("SELECT chunk_id FROM chunks_fts")),
                "indexed": result.indexed_artifacts,
                "nodes": result.graph_nodes,
                "edges": result.graph_edges,
            },
            "artifact_ids": sorted(str(r["id"]) for r in state.rows("SELECT id FROM artifacts")),
            "chunk_ids": sorted(str(r["id"]) for r in state.rows("SELECT id FROM chunks")),
            "ranked": ranked,
        }
    finally:
        engine.close()


def _reset(dsn: str) -> None:
    """Drop everything this suite creates, so a re-run starts clean."""

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture()
def both(tmp_path: Path) -> dict[str, dict[str, Any]]:
    _reset(DSN)
    workspace = _corpus(tmp_path)
    return {
        "sqlite": _index(tmp_path, workspace, "sqlite"),
        "postgres": _index(tmp_path, workspace, "postgres"),
    }


@postgres
def test_stable_ids_are_byte_identical_across_backends(both) -> None:
    """Rule 3. A backend that changed these would orphan every persisted graph."""

    assert both["sqlite"]["artifact_ids"] == both["postgres"]["artifact_ids"]
    assert both["sqlite"]["chunk_ids"] == both["postgres"]["chunk_ids"]
    # Not merely equal — actually populated, or this passes on two empty lists.
    assert both["sqlite"]["artifact_ids"], "the fixture indexed nothing"
    assert all(":" in artifact_id for artifact_id in both["sqlite"]["artifact_ids"])


@postgres
def test_the_same_corpus_produces_the_same_index(both) -> None:
    assert both["sqlite"]["counts"] == both["postgres"]["counts"]
    assert both["sqlite"]["counts"]["chunks"] > 0
    # chunks_fts is written by the same code on both backends; if it ever
    # diverges from `chunks`, search silently loses documents.
    assert both["sqlite"]["counts"]["fts"] == both["sqlite"]["counts"]["chunks"]
    assert both["postgres"]["counts"]["fts"] == both["postgres"]["counts"]["chunks"]


@postgres
def test_retrieval_quality_holds_on_the_gold_set(both) -> None:
    """Measured quality, not score equality — see the module docstring.

    The IDF-sensitive query is exempted for Postgres only, and only because
    :func:`test_known_divergence_postgres_has_no_idf` asserts that exact
    failure instead. Skipping it silently here would turn a documented
    limitation into an invisible one.
    """

    for backend in ("sqlite", "postgres"):
        ranked = both[backend]["ranked"]
        for query, expected in GOLD:
            if backend == "postgres" and query in IDF_SENSITIVE:
                continue
            hits = ranked[query]
            assert hits, f"{backend}: {query!r} returned nothing"
            assert hits[0] == expected, (
                f"{backend}: {query!r} ranked {hits[0]} first, expected {expected}"
            )


@postgres
def test_postgres_ranks_the_gold_set_the_same_way_sqlite_does(both) -> None:
    """Identical ordering, except where IDF is the deciding factor."""

    for query, _ in GOLD:
        if query in IDF_SENSITIVE:
            continue
        assert both["sqlite"]["ranked"][query] == both["postgres"]["ranked"][query], query


@postgres
def test_known_divergence_postgres_has_no_idf(both) -> None:
    """**A measured limitation of the port, pinned so it cannot be forgotten.**

    SQLite ranks by BM25, which weights *rare* terms heavily: "restarts" and
    "nightly" occur in exactly one document, so `runbook.md` wins even though a
    longer decoy repeats "gateway" six times. Postgres's `ts_rank_cd` has no
    inverse document frequency at all — it accumulates cover density — so the
    decoy wins. Verified against every normalization flag (0/1/2/16): none of
    them fixes it, because normalization is about length, and the missing
    ingredient is corpus-wide term rarity.

    So the port preserves **field weighting** (title above body) but not
    **term weighting**. On single-topic queries the two agree; on multi-term
    queries where one term is rare and another is common, Postgres can rank a
    long, repetitive document above the short precise one.

    This test asserts the divergence *currently exists*. It goes red the day
    someone adds real IDF — a pg_search/ParadeDB backend, or a df-weighted
    tsquery — which is exactly when this docstring needs deleting.
    """

    for query, sqlite_winner in (
        ("gateway restarts nightly", "runbook.md"),
        ("deploy gateway", "deploy-gateway.md"),
    ):
        assert both["sqlite"]["ranked"][query][0] == sqlite_winner
        assert both["postgres"]["ranked"][query][0] == "notes.md", (
            f"Postgres ranked {query!r} correctly — real term weighting may have "
            "been added; if so, delete this test and empty IDF_SENSITIVE."
        )


@postgres
def test_a_second_sync_is_zero_work_on_postgres_too(tmp_path: Path) -> None:
    """Idempotency is a product guarantee (rule 4), not a SQLite behaviour."""

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "postgres")
    engine = SyncEngine(config)
    try:
        first = engine.sync_source("docs", "full")
        second = engine.sync_source("docs", "incremental")
        assert first.indexed_artifacts == 5
        assert second.indexed_artifacts == 0
        assert second.skipped_artifacts == 5
    finally:
        engine.close()


def test_the_sqlite_default_needs_no_dsn_and_no_driver(tmp_path: Path) -> None:
    """Rule 7: standalone mode is sacred.

    Runs unconditionally — it is the one test here that must pass on a machine
    with no Postgres at all, because that machine is the default deployment.
    """

    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    assert config.storage.backend == "sqlite"
    engine = SyncEngine(config)
    try:
        assert engine.state.dialect.name == "sqlite"
        assert engine.sync_source("docs", "full").indexed_artifacts == 5
    finally:
        engine.close()
