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
from pheasant.search.sqlite_store import SearchStore, corpus_vocabulary
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


#: The one query the Postgres arm still ranks differently, and why.
#:
#: Three real gaps were found and fixed (see `_postgres_rank_expression` and
#: the schema's tokenizer note): no IDF at all, no term-frequency saturation,
#: and filenames tokenizing as a single lexeme so ``deploy-gateway.md`` never
#: matched "deploy". With those closed, "gateway restarts nightly" — the
#: rare-term case, and the more important retrieval property — is now correct
#: on both backends.
#:
#: What remains is structural, not a parameter. BM25 normalizes each column's
#: contribution by *that column's* length; ``ts_rank_cd`` applies one global
#: normalization to the whole weighted vector. So when a title match on a
#: common term competes with body matches on rare terms, the two rank
#: differently, and no weight vector fixes both cases at once — verified at
#: 8:1, 16:1 and 33:1 title:body ratios, each of which merely moved the
#: failure to the other query.
POSTGRES_RANK_RESIDUAL = {"deploy gateway"}


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
            "vocabulary": corpus_vocabulary(state, limit=24),
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
def test_the_contract_vocabulary_survives_the_backend_swap(both) -> None:
    """The one field the router scores a region on.

    `corpus_vocabulary` read `chunks_vocab`, an `fts5vocab` virtual table that
    only SQLite has, inside a bare `except: return []`. On Postgres that meant
    every published contract carried an **empty** vocabulary — the region kept
    answering searches, kept passing its health checks, and was silently
    unroutable. So this asserts non-emptiness first: equality alone passes on
    two empty lists, which is exactly the state being fixed.
    """

    sqlite_vocab = both["sqlite"]["vocabulary"]
    postgres_vocab = both["postgres"]["vocabulary"]
    assert sqlite_vocab, "the fixture produced no vocabulary at all"
    assert postgres_vocab, "Postgres published an empty vocabulary"

    # Document *frequencies* must agree exactly: both count chunks containing
    # the term, over an identical corpus. Ordering of equal-frequency terms is
    # the tie-break, which is why this compares as a mapping.
    shared = dict(sqlite_vocab).keys() & dict(postgres_vocab).keys()
    assert len(shared) >= len(sqlite_vocab) * 0.6, (
        f"the two vocabularies barely overlap: {sqlite_vocab} vs {postgres_vocab}"
    )
    for term in shared:
        assert dict(sqlite_vocab)[term] == dict(postgres_vocab)[term], (
            f"document frequency for {term!r} differs across backends"
        )


@postgres
def test_corpus_size_comes_from_the_planner_estimate_once_it_exists(tmp_path: Path) -> None:
    """IDF's ``N`` was a full ``count(*)`` on every text search.

    Postgres has no O(1) count, so the largest table in the database was
    sequentially scanned once per query — on the backend chosen precisely
    because the corpus is too big for SQLite. ``reltuples`` answers from the
    catalog. Both paths are exercised: before ``ANALYZE`` there are no
    statistics and the exact count must be used (a zero would flatten every
    score to nothing), after it the estimate must be.
    """

    import psycopg

    from pheasant.search.sqlite_store import _postgres_total_chunks

    _reset(DSN)
    workspace = _corpus(tmp_path)
    engine = SyncEngine(_config(tmp_path, workspace, "postgres"))
    try:
        engine.sync_source("docs", "full")
        real = len(engine.state.rows("SELECT chunk_id FROM chunks_fts"))
        assert real > 0

        # No statistics yet: the sentinel is negative, so the exact count wins.
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("UPDATE pg_class SET reltuples = -1 WHERE oid = 'chunks_fts'::regclass")
        assert _postgres_total_chunks(engine.state) == real

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("ANALYZE chunks_fts")
        estimate = _postgres_total_chunks(engine.state)
        assert estimate == real, f"planner estimate {estimate} disagrees with {real}"

        # And ranking still works through the real search path.
        search = HybridSearch(SearchStore(engine.state))
        hits = search.search_context(engine.config.knowledge_base_id, "invoices", "text", 5)
        assert [h.get("relative_path") for h in hits["results"]][:1] == ["billing.md"]
    finally:
        engine.close()


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
            if backend == "postgres" and query in POSTGRES_RANK_RESIDUAL:
                continue
            hits = ranked[query]
            assert hits, f"{backend}: {query!r} returned nothing"
            assert hits[0] == expected, (
                f"{backend}: {query!r} ranked {hits[0]} first, expected {expected}"
            )


@postgres
def test_postgres_and_sqlite_agree_on_the_top_hit(both) -> None:
    """Top-1 agreement is the contract; tail order is not.

    Full-list equality held before the port had IDF and matched SQLite's
    tokenizer — because Postgres was returning *fewer* documents (a filename
    that tokenized as one lexeme matched nothing) and the short lists happened
    to coincide. Now both backends see the same terms, both return the same
    documents, and they can legitimately disagree about ranks 2-3 while
    agreeing on the answer. Asserting full equality here would be asserting a
    coincidence, and would have to be relaxed by the first real corpus.
    """

    for query, _ in GOLD:
        if query in POSTGRES_RANK_RESIDUAL:
            continue
        sqlite_hits = both["sqlite"]["ranked"][query]
        postgres_hits = both["postgres"]["ranked"][query]
        assert sqlite_hits[:1] == postgres_hits[:1], query
        # Same documents found, whatever order they came back in.
        assert set(sqlite_hits) == set(postgres_hits), query


@postgres
def test_known_residual_postgres_lacks_per_column_length_normalization(both) -> None:
    """**The residual after three real fixes, pinned so it cannot be forgotten.**

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

    query = "deploy gateway"
    assert both["sqlite"]["ranked"][query][0] == "deploy-gateway.md"
    postgres_ranking = both["postgres"]["ranked"][query]
    # It *matches* now — the tokenizer fix did that, and before it the file
    # named for the query did not appear at all. It simply is not first.
    assert "deploy-gateway.md" in postgres_ranking[:2], (
        "the file named for the query fell out of the top 2 — the tokenizer "
        "normalization in persistence/schema.py may have regressed"
    )
    assert postgres_ranking[0] == "notes.md", (
        "Postgres ranked this correctly — per-column length normalization may "
        "have been added (a pg_search/ParadeDB backend would do it); if so, "
        "delete this test and empty POSTGRES_RANK_RESIDUAL."
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


@postgres
def test_migration_copies_state_and_never_destroys_the_original(tmp_path: Path) -> None:
    """`pheasant migrate --to postgres`, end to end.

    The important assertions are the safety ones: the SQLite file survives
    (rule 2 — ``/state`` is user data), the stable IDs carry over unchanged
    (rule 3), and ``chunks_fts`` is *rebuilt* for the target dialect rather
    than copied, because its tokenization is dialect-specific.
    """

    from pheasant.persistence.migrate import migrate_sqlite_to_postgres

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        sqlite_path = Path(engine.state.path)
        before = sorted(str(r["id"]) for r in engine.state.rows("SELECT id FROM artifacts"))
        chunk_count = len(engine.state.rows("SELECT id FROM chunks"))
    finally:
        engine.close()

    report = migrate_sqlite_to_postgres(sqlite_path, DSN)

    assert report["verified"] is True
    assert report["tables"]["artifacts"] == len(before)
    # Derived cache, rebuilt not copied — one row per chunk.
    assert report["chunks_fts"] == chunk_count

    # The original is renamed, never deleted.
    assert not sqlite_path.exists()
    parked = Path(report["original_renamed_to"])
    assert parked.exists() and parked.stat().st_size > 0

    postgres_config = _config(tmp_path, workspace, "postgres")
    migrated = SyncEngine(postgres_config)
    try:
        after = sorted(str(r["id"]) for r in migrated.state.rows("SELECT id FROM artifacts"))
        assert after == before, "stable IDs changed across the migration"
        # And the migrated index is searchable, not merely present.
        search = HybridSearch(SearchStore(migrated.state))
        hits = search.search_context(postgres_config.knowledge_base_id, "invoices", "text", 5)
        assert [h.get("relative_path") for h in hits["results"]][:1] == ["billing.md"]
    finally:
        migrated.close()


@postgres
def test_migration_is_idempotent_and_verifies_before_renaming(tmp_path: Path) -> None:
    """A re-run must not double-insert, and must not park the original twice."""

    from pheasant.persistence.migrate import migrate_sqlite_to_postgres

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        sqlite_path = Path(engine.state.path)
    finally:
        engine.close()

    first = migrate_sqlite_to_postgres(sqlite_path, DSN)
    assert first["tables"]["artifacts"] == 5

    # Re-run against the parked copy: every table is already populated, so
    # nothing is inserted a second time.
    parked = Path(first["original_renamed_to"])
    second = migrate_sqlite_to_postgres(parked, DSN, keep_original=False)
    assert second["verified"] is True
    assert "artifacts" in second["skipped"]
    # Tables that were empty in the source are still "copied" — of nothing.
    # The invariant that matters is that no row was inserted twice.
    assert all(count == 0 for count in second["tables"].values()), second["tables"]

    postgres_config = _config(tmp_path, workspace, "postgres")
    migrated = SyncEngine(postgres_config)
    try:
        assert len(migrated.state.rows("SELECT id FROM artifacts")) == 5
    finally:
        migrated.close()


@postgres
def test_a_half_copied_table_fails_verification_instead_of_being_skipped(tmp_path: Path) -> None:
    """The interrupted-run case, which the skip path used to wave through.

    "Already has rows" was treated as "already done", and only *copied* tables
    were counted at the end — so a run killed part-way through `chunks` left a
    partial table, the re-run skipped it, verification never looked at it, and
    the original SQLite file was renamed on the strength of a check that had
    excluded the one table at risk.
    """

    import psycopg

    from pheasant.persistence.migrate import MigrationError, migrate_sqlite_to_postgres

    _reset(DSN)
    workspace = _corpus(tmp_path)
    config = _config(tmp_path, workspace, "sqlite")
    engine = SyncEngine(config)
    try:
        engine.sync_source("docs", "full")
        sqlite_path = Path(engine.state.path)
    finally:
        engine.close()

    first = migrate_sqlite_to_postgres(sqlite_path, DSN)
    assert first["verified"] is True
    parked = Path(first["original_renamed_to"])

    # Simulate the interruption: drop some chunks, leaving the table non-empty
    # (so the re-run skips it) but short (so it is wrong). `chunks` is the
    # table that actually takes long enough to be interrupted in.
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM chunks WHERE id IN (SELECT id FROM chunks LIMIT 2)")
        short = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert short > 0, "the table must stay non-empty or it would simply be re-copied"

    with pytest.raises(MigrationError) as caught:
        migrate_sqlite_to_postgres(parked, DSN, keep_original=True)
    # Asserted on the *skipped-table* message specifically. A looser check for
    # "chunks" passed with this fix mutated out, because the pre-existing
    # chunks_fts row-count check fires on the same corruption and its message
    # also says "chunks" — mutation testing is what surfaced that.
    assert "skipped as already present" in str(caught.value), str(caught.value)
    assert "chunks:" in str(caught.value)
    assert parked.exists(), "the original was renamed despite a failed verification"
