"""A concept node earns its place by linking documents.

On microsoft/agent-framework, concepts were 96% of a 577k-node graph and 74.5%
of them were mentioned by exactly one document — three quarters of the graph
existed to connect nothing. Reconciliation keeps concepts that link at least
``graph.concept_min_documents`` documents and drops the rest, which costs no
information: the term stays on the artifact, in ``artifact_terms``, and in the
artifact's searchable text.

These tests assert against the term table rather than against invented words:
which phrases the extractor produces is its business, and a test that guesses
them tests the wrong thing.
"""

from __future__ import annotations

from pathlib import Path

from syncsage.config.loader import load_config
from syncsage.search.graph_search import search_graph
from syncsage.sync.engine import SyncEngine
from tests.conftest import CONFIG_TEMPLATE

SOURCE = "architecture-notes"


def _engine(tmp_path: Path, name: str, workspace: Path) -> SyncEngine:
    base = tmp_path / name
    for sub in ("state", "vault", "exports"):
        (base / sub).mkdir(parents=True)
    rendered = CONFIG_TEMPLATE.read_text(encoding="utf-8").format(
        workspace_path=workspace.as_posix(),
        state_path=(base / "state").as_posix(),
        vault_path=(base / "vault").as_posix(),
        exports_path=(base / "exports").as_posix(),
    )
    config_file = base / "syncsage.yaml"
    config_file.write_text(rendered, encoding="utf-8")
    return SyncEngine(load_config(config_file))


def _write(workspace: Path, name: str, text: str) -> None:
    """Add a note to the folder the ``architecture-notes`` source indexes."""

    target = workspace / "notes" / "architecture" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _concept_nodes(engine: SyncEngine) -> set[str]:
    graph = engine.graph_builder.graph
    with graph.reading():
        return {
            node_id for node_id, attrs in graph.iter_nodes() if attrs.get("type") == "concept"
        }


def _documents_per_concept(engine: SyncEngine) -> dict[str, int]:
    """How many distinct documents mention each concept, per the term table."""

    return {
        str(row["node_id"]): int(row["documents"])
        for row in engine.state.rows(
            """SELECT node_id, COUNT(DISTINCT artifact_id) AS documents
               FROM artifact_terms WHERE node_type='concept' GROUP BY node_id"""
        )
    }


def test_every_kept_concept_links_at_least_two_documents(tmp_path, workspace_copy) -> None:
    """The invariant, over a real corpus rather than a made-up term.

    Two of the notes deliberately share wording, so the corpus contains both
    concepts that qualify and concepts that do not — otherwise "nothing was
    kept" would pass the invariant while proving nothing.
    """

    shared = "# Ledger\n\nThe append-only ledger keeps a durable write path.\n"
    _write(workspace_copy, "shared_a.md", shared)
    _write(workspace_copy, "shared_b.md", shared.replace("Ledger", "Ledger notes"))

    engine = _engine(tmp_path, "prune", workspace_copy)
    try:
        engine.sync_source(SOURCE, "full")
        counts = _documents_per_concept(engine)
        kept = _concept_nodes(engine)

        assert kept, "the corpus produced no concept nodes at all"
        thin = {node_id for node_id, n in counts.items() if n < 2}
        assert thin, "fixture has no single-document concepts, so nothing is proved here"
        assert not (kept & thin), "concepts linking a single document were kept as nodes"
        assert all(counts.get(node_id, 0) >= 2 for node_id in kept)
    finally:
        engine.close()


def test_a_pruned_term_is_still_recorded_and_still_findable(tmp_path, workspace_copy) -> None:
    """Dropping the node must not drop the knowledge."""

    engine = _engine(tmp_path, "keepterm", workspace_copy)
    try:
        engine.sync_source(SOURCE, "full")
        counts = _documents_per_concept(engine)
        pruned = next(node_id for node_id, n in counts.items() if n < 2)

        rows = engine.state.rows(
            "SELECT artifact_id, normalized_term FROM artifact_terms WHERE node_id=?",
            (pruned,),
        )
        assert rows, "pruning a node must not delete the term itself"

        term = str(rows[0]["normalized_term"])
        # The document that mentions it is still findable by that term, because
        # the term is part of the artifact node's searchable text.
        assert search_graph(engine.graph_builder.graph, term), f"{term!r} became unfindable"
    finally:
        engine.close()


def test_a_concept_is_restored_when_a_second_document_uses_it(tmp_path, workspace_copy) -> None:
    """The case degree-based pruning gets wrong.

    Once a lone concept's node and edge are gone, the graph no longer records
    that the first document ever used the term — so counting edges could never
    bring it back. Reconciliation reads ``artifact_terms``, which still
    remembers, and relinks *both* documents.
    """

    text = "# Ledger\n\nThe append-only ledger keeps a durable write path.\n"
    _write(workspace_copy, "first.md", text)

    engine = _engine(tmp_path, "restore", workspace_copy)
    try:
        engine.sync_source(SOURCE, "full")
        counts = _documents_per_concept(engine)
        solo = {node_id for node_id, n in counts.items() if n < 2}
        assert solo, "expected the new note to contribute single-document concepts"
        assert not (solo & _concept_nodes(engine)), "single-document concepts were kept"

        # A second document repeats the same wording, so those terms now link
        # two documents and must come back as nodes.
        _write(workspace_copy, "second.md", text.replace("Ledger", "Ledger again"))
        engine.sync_source(SOURCE, "incremental")

        restored = solo & _concept_nodes(engine)
        assert restored, "concepts that crossed the threshold were not restored"

        # ...and each restored concept links both documents, including the one
        # indexed while it was still below the line.
        graph = engine.graph_builder.graph
        with graph.reading():
            mentions: dict[str, set[str]] = {}
            for (source, target), edge_map in graph.iter_edges():
                if target in restored and any(
                    d.get("type") == "mentions" for d in edge_map.values()
                ):
                    mentions.setdefault(target, set()).add(source)
        assert mentions, "restored concepts have no mentions edges"
        assert all(len(docs) >= 2 for docs in mentions.values()), mentions
    finally:
        engine.close()


def test_reconciliation_is_idempotent(tmp_path, workspace_copy) -> None:
    engine = _engine(tmp_path, "idem", workspace_copy)
    try:
        engine.sync_source(SOURCE, "full")
        before = (
            engine.graph_builder.graph.number_of_nodes(),
            engine.graph_builder.graph.number_of_edges(),
        )
        report = engine.graph_builder.reconcile_concepts(engine.state, min_documents=2)
        assert report["removed"] == 0 and report["restored"] == 0, report
        assert (
            engine.graph_builder.graph.number_of_nodes(),
            engine.graph_builder.graph.number_of_edges(),
        ) == before
    finally:
        engine.close()


def test_threshold_of_one_keeps_every_concept(tmp_path, workspace_copy) -> None:
    """The escape hatch: ``graph.concept_min_documents: 1`` is the old behavior."""

    engine = _engine(tmp_path, "keepall", workspace_copy)
    try:
        engine.config.graph.concept_min_documents = 1
        engine.sync_source(SOURCE, "full")
        counts = _documents_per_concept(engine)
        kept = _concept_nodes(engine)
        assert set(counts) == kept, "threshold 1 must keep a node for every extracted term"
    finally:
        engine.close()
