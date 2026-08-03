from __future__ import annotations

from pathlib import Path

from syncsage.mcp_server.tools import SyncSageTools
from tests.conftest import result_items, run_sync


def test_full_sync_creates_enriched_graph_nodes_and_edges(
    workspace_copy: Path,
    loaded_config: object,
    sync_engine: object,
) -> None:
    readme = workspace_copy / "syncsage-repo" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nSee [API docs](https://example.com/syncsage/api) and [@syncsage-paper].\n",
        encoding="utf-8",
    )
    (workspace_copy / "syncsage-repo" / "syncsage" / "cross_file.py").write_text(
        "import json\n\n"
        "HEALTH_PATH = '/health'\n\n"
        "class SearchAgent:\n"
        "    def render(self) -> str:\n"
        "        return json.dumps({'path': HEALTH_PATH})\n",
        encoding="utf-8",
    )

    run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    graph = sync_engine.graph_builder.graph
    node_types = {node["type"] for node in graph.to_node_link()["nodes"]}
    edge_types = {edge["type"] for edge in graph.to_node_link()["links"]}

    # `concept` is deliberately absent: concept extraction was retired after it
    # measured as 87% of the graph while contributing nothing to retrieval,
    # graph facts or similarity (graph.enrichment._add_concept).
    assert {"directory", "symbol", "entity", "external_reference"} <= node_types
    assert "concept" not in node_types
    assert any(
        node["type"] == "directory" and node.get("relative_path") == "syncsage"
        for node in graph.to_node_link()["nodes"]
    )
    expected_edges = {
        "contains",
        "imports",
        "calls",
        "references",
        "mentions",
    }
    assert expected_edges <= edge_types
    # `similar_to` went with concept extraction: the similarity pass scored
    # artifacts by shared `concept_terms`, and with no concepts there is
    # nothing for it to compare. This is not a silent loss — it never worked.
    # The live 2,132-file graph contained ZERO similar_to edges before the
    # removal, so the pass was already producing nothing while costing a
    # pairwise scan of every artifact against every other on each sync.
    assert "similar_to" not in edge_types


def test_graph_neighbors_honor_depth_and_edge_filters(
    loaded_config: object,
    sync_engine: object,
) -> None:
    run_sync(sync_engine, source_name="syncsage-repo", mode="full")
    tools = SyncSageTools(loaded_config)
    result = tools.get_graph_neighbors(
        loaded_config.knowledge_base_id,
        "file:syncsage-repo:syncsage/sync_engine.py:branch=none",
        depth=2,
        edge_types=["mentions", "derived_from"],
    )

    # Two hops still bridge documents, but through a *symbol* or *entity*
    # rather than a concept: "these two files both use SyncEngine" instead of
    # "these two files both contain the word 'limit'". Concept extraction was
    # retired (graph.enrichment._add_concept), and this is the connectivity
    # that replaced it — narrower, and worth traversing.
    assert any(neighbor["depth"] == 2 for neighbor in result["neighbors"])
    bridges = {
        neighbor["node"].get("type")
        for neighbor in result["neighbors"]
        if neighbor["depth"] == 1
    }
    assert bridges <= {"symbol", "entity", "external_reference"}, bridges
    assert not any(
        neighbor["node"].get("type") == "concept" for neighbor in result["neighbors"]
    )


def test_graph_terms_improve_cross_file_search(
    sync_engine: object,
) -> None:
    run_sync(sync_engine, source_name="syncsage-repo", mode="full")

    search_result = sync_engine.search_context("SyncEngine HEALTH_PATH", max_results=5)
    paths = {
        item["relative_path"]
        for item in result_items(search_result)
        if isinstance(item, dict)
    }

    assert "syncsage/sync_engine.py" in paths
    assert "syncsage/api.py" in paths


def test_similarity_index_matches_all_pairs_exactly() -> None:
    """The term index is an optimization, so it must emit the same edges.

    Two artifacts sharing no concept term score zero and are dropped by the
    threshold, so restricting candidates to "shares at least one term" cannot
    change the outcome — only the cost. This asserts that against a literal
    all-pairs implementation.
    """

    import random

    from syncsage.graph.enrichment import SemanticSimilarityPass

    def all_pairs(artifacts):
        edges = []
        for index, (left_id, left) in enumerate(artifacts):
            left_terms = set(left.get("concept_terms") or [])
            if len(left_terms) < 2:
                continue
            for right_id, right in artifacts[index + 1 :]:
                right_terms = set(right.get("concept_terms") or [])
                if len(right_terms) < 2:
                    continue
                shared = left_terms & right_terms
                score = len(shared) / len(left_terms | right_terms)
                if len(shared) < 2 and score < 0.25:
                    continue
                edges.append((left_id, right_id, round(min(1.0, score), 3)))
                edges.append((right_id, left_id, round(min(1.0, score), 3)))
        return sorted(edges)

    random.seed(11)
    vocabulary = [f"term{i}" for i in range(40)]
    artifacts = []
    for i in range(120):
        terms = random.sample(vocabulary, random.randint(0, 6))
        artifacts.append((f"file:src:{i}.py", {"concept_terms": terms}))

    produced = sorted(
        (edge.source, edge.target, edge.attrs["confidence"])
        for edge in SemanticSimilarityPass().run(artifacts)
    )
    assert produced == all_pairs(artifacts)


def test_similarity_can_be_limited_to_what_changed() -> None:
    """An incremental sync only needs pairs touching a rewritten artifact."""

    from syncsage.graph.enrichment import SemanticSimilarityPass

    artifacts = [
        ("a", {"concept_terms": ["alpha", "beta", "gamma"]}),
        ("b", {"concept_terms": ["alpha", "beta", "delta"]}),
        ("c", {"concept_terms": ["alpha", "beta", "epsilon"]}),
    ]
    every = SemanticSimilarityPass().run(artifacts)
    assert {(edge.source, edge.target) for edge in every} == {
        ("a", "b"), ("b", "a"), ("a", "c"), ("c", "a"), ("b", "c"), ("c", "b")
    }

    only_a = SemanticSimilarityPass().run(artifacts, changed_ids={"a"})
    touched = {(edge.source, edge.target) for edge in only_a}
    assert touched == {("a", "b"), ("b", "a"), ("a", "c"), ("c", "a")}
    assert ("b", "c") not in touched, "untouched pair was re-derived"
