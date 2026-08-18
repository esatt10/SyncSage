"""Source type is surfaceable, and filterable, on every retrieval surface.

A knowledge base is built from *kinds* of source — a git repository, an
Obsidian vault, Notion, Slack — and until now a search hit said only which
source it came from, never which kind. That left every caller in the same
position: to ask "only what came out of our wikis" you first had to list every
source, read each one's type, and rebuild the question as a list of names.

So the property under test is two-sided. Every hit **reports** its source type
(`provenance.source_type`), and every surface can **filter** on it with the
same two criteria — MCP `search_context`, HTTP `POST /search`, the assistant,
and the Synapse contract that tells a router what a region is built from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.search.criteria import apply_retrieval_criteria, criteria_active, type_of

#: Two sources of *different types* holding the same vocabulary, so a query
#: matches both and only the type can separate them. Same-typed fixtures would
#: pass whether or not the filter did anything.
CORPUS = {
    "handbook": ("markdown_folder", "The deployment gateway rotates credentials nightly.\n"),
    "runbooks": ("document_folder", "The deployment gateway rotates credentials nightly.\n"),
}


def _config(tmp_path: Path, **overrides: Any) -> PheasantConfig:
    workspace = tmp_path / "workspace"
    sources = []
    for name, (source_type, body) in CORPUS.items():
        folder = workspace / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "note.md").write_text(f"# {name}\n\n{body}", encoding="utf-8")
        sources.append(
            {
                "name": name,
                "type": source_type,
                "path": str(folder),
                "include": ["**/*.md"],
            }
        )
    payload: dict[str, Any] = {
        "pheasant": {
            "name": "source-types",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(workspace),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
        "sources": sources,
    }
    payload.update(overrides)
    return PheasantConfig.model_validate(payload)


def _tools(tmp_path: Path) -> PheasantTools:
    tools = PheasantTools(_config(tmp_path))
    tools.sync_all(tools.config.knowledge_base_id, "full")
    return tools


# --------------------------------------------------------------------------
# Surfaced: every hit says what kind of source it came from
# --------------------------------------------------------------------------


def test_every_hit_reports_its_source_type(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    try:
        payload = tools.search_context(
            tools.config.knowledge_base_id, "gateway credentials", max_results=10
        )
        results = payload["results"]
        assert results, "the fixture matched nothing"
        for hit in results:
            assert hit["provenance"]["source_type"], f"no source_type on {hit['provenance']}"
        assert {hit["provenance"]["source_type"] for hit in results} == {
            "markdown_folder",
            "document_folder",
        }
    finally:
        tools.engine.close()


def test_the_type_is_the_configured_kind_not_the_node_type(tmp_path: Path) -> None:
    """`type` on a hit is the *node* type (chunk); the source type is separate."""

    tools = _tools(tmp_path)
    try:
        hit = tools.search_context(
            tools.config.knowledge_base_id, "gateway credentials", max_results=1
        )["results"][0]
        assert hit["type"] == "chunk"
        assert type_of(hit) in {"markdown_folder", "document_folder"}
    finally:
        tools.engine.close()


# --------------------------------------------------------------------------
# Filterable: MCP
# --------------------------------------------------------------------------


def test_mcp_search_scopes_to_a_source_type(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    try:
        kb = tools.config.knowledge_base_id
        payload = tools.search_context(
            kb, "gateway credentials", max_results=10, source_types=["document_folder"]
        )
        assert payload["results"], "the filter dropped everything"
        assert {type_of(hit) for hit in payload["results"]} == {"document_folder"}
        # The echo says what was applied, so a caller can see why it got this.
        assert payload["criteria"]["source_types"] == ["document_folder"]
    finally:
        tools.engine.close()


def test_mcp_search_excludes_a_source_type(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    try:
        payload = tools.search_context(
            tools.config.knowledge_base_id,
            "gateway credentials",
            max_results=10,
            exclude_source_types=["markdown_folder"],
        )
        assert payload["results"]
        assert {type_of(hit) for hit in payload["results"]} == {"document_folder"}
    finally:
        tools.engine.close()


def test_an_unmatched_type_returns_nothing_rather_than_everything(tmp_path: Path) -> None:
    """A filter that silently fails open is worse than one that refuses."""

    tools = _tools(tmp_path)
    try:
        payload = tools.search_context(
            tools.config.knowledge_base_id,
            "gateway credentials",
            max_results=10,
            source_types=["slack"],
        )
        assert payload["results"] == []
    finally:
        tools.engine.close()


# --------------------------------------------------------------------------
# Filterable: HTTP — the surface the router and every non-MCP client uses
# --------------------------------------------------------------------------


def test_http_search_takes_the_same_criteria(tmp_path: Path) -> None:
    config = _config(tmp_path)
    client = TestClient(create_app(config))
    client.post("/sync", json={"mode": "full", "wait": True})

    scoped = client.post(
        "/search",
        json={"query": "gateway credentials", "source_types": ["markdown_folder"]},
    ).json()
    assert scoped["results"]
    assert {type_of(hit) for hit in scoped["results"]} == {"markdown_folder"}
    assert scoped["criteria"]["source_types"] == ["markdown_folder"]

    everything = client.post("/search", json={"query": "gateway credentials"}).json()
    assert {type_of(hit) for hit in everything["results"]} == {
        "markdown_folder",
        "document_folder",
    }


def test_mcp_and_http_agree(tmp_path: Path) -> None:
    """One region must not answer differently depending on which protocol asks."""

    config = _config(tmp_path)
    client = TestClient(create_app(config))
    client.post("/sync", json={"mode": "full", "wait": True})
    over_http = client.post(
        "/search",
        json={"query": "gateway credentials", "source_types": ["document_folder"]},
    ).json()

    tools = PheasantTools(config)
    try:
        over_mcp = tools.search_context(
            config.knowledge_base_id,
            "gateway credentials",
            source_types=["document_folder"],
        )
    finally:
        tools.engine.close()

    assert [h["node_id"] for h in over_http["results"]] == [
        h["node_id"] for h in over_mcp["results"]
    ]


# --------------------------------------------------------------------------
# Discoverable: an agent can find out which types exist before filtering
# --------------------------------------------------------------------------


def test_describe_retrieval_lists_the_types_present_and_the_new_knobs(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    try:
        described = tools.describe_retrieval(tools.config.knowledge_base_id)
        assert described["source_types"] == ["document_folder", "markdown_folder"]
        tunable = described["tunable_per_call"]["search_context"]
        assert "source_types" in tunable and "exclude_source_types" in tunable
    finally:
        tools.engine.close()


# --------------------------------------------------------------------------
# A2A: the contract tells a router what this region is built from
# --------------------------------------------------------------------------


def test_the_contract_advertises_the_regions_source_types(tmp_path: Path) -> None:
    from pheasant.synapse.publisher import ContractPublisher

    tools = _tools(tmp_path)
    try:
        contract = ContractPublisher(tools.config, tools.state).build(
            generated_at="2026-06-21T00:00:00Z"
        )
        assert contract["capabilities"]["source_types"] == [
            "document_folder",
            "markdown_folder",
        ]
    finally:
        tools.engine.close()


def test_the_contract_still_validates_against_the_vendored_schema(tmp_path: Path) -> None:
    """Rule 6: additive wire data, no schema bump and no re-vendor."""

    import json

    import jsonschema

    from pheasant.synapse.publisher import ContractPublisher

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "contracts" / "semantic_contract.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    tools = _tools(tmp_path)
    try:
        contract = ContractPublisher(tools.config, tools.state).build(
            generated_at="2026-06-21T00:00:00Z"
        )
    finally:
        tools.engine.close()
    jsonschema.validate(contract, schema)


# --------------------------------------------------------------------------
# The criteria layer itself
# --------------------------------------------------------------------------


def test_criteria_are_post_filters_that_only_ever_remove() -> None:
    rows = [
        {"node_id": "a", "provenance": {"source_id": "x", "source_type": "slack"}},
        {"node_id": "b", "provenance": {"source_id": "y", "source_type": "repository"}},
    ]
    kept = apply_retrieval_criteria(rows, source_types=["slack"])
    assert [row["node_id"] for row in kept] == ["a"]
    # Order preserved, nothing re-scored, and the originals are untouched.
    assert apply_retrieval_criteria(rows) == rows


def test_a_hit_with_no_type_is_dropped_by_an_include_and_kept_by_an_exclude() -> None:
    """Asymmetric on purpose: 'only slack' cannot admit an unknown, but
    'anything except slack' has no reason to drop one."""

    rows = [{"node_id": "a", "provenance": {"source_id": "x"}}]
    assert apply_retrieval_criteria(rows, source_types=["slack"]) == []
    assert apply_retrieval_criteria(rows, exclude_source_types=["slack"]) == rows


def test_the_over_fetch_switch_notices_the_new_criteria() -> None:
    """Without this, `max_results` degrades to 'whatever survived the filter'."""

    assert criteria_active(None, None, None) is False
    assert criteria_active(None, None, None, ["slack"], None) is True
    assert criteria_active(None, None, None, None, ["slack"]) is True


# --------------------------------------------------------------------------
# The assistant: a scoped question must answer only from that kind of source
# --------------------------------------------------------------------------


def test_the_assistant_answers_only_from_the_scoped_source_type(tmp_path: Path) -> None:
    """The scope is held on the retriever, so it survives a multi-round loop.

    An answering loop issues many searches; a scope threaded per call would be
    honoured by whichever ones remembered to pass it. This asserts the whole
    answer — every citation — stayed inside the requested kind of source.
    """

    config = _config(tmp_path)
    client = TestClient(create_app(config))
    client.post("/sync", json={"mode": "full", "wait": True})

    def cited_types(body: dict) -> set[str]:
        return {str(c.get("source_type") or "") for c in body["citations"]}

    unscoped = client.post(
        "/assistant/chat", json={"question": "how are credentials rotated?"}
    ).json()
    assert unscoped["citations"], "the fixture answered nothing to scope"
    assert cited_types(unscoped) == {"markdown_folder", "document_folder"}

    scoped = client.post(
        "/assistant/chat",
        json={"question": "how are credentials rotated?", "source_types": ["document_folder"]},
    ).json()
    assert scoped["citations"], "the scope dropped every passage"
    assert cited_types(scoped) == {"document_folder"}


def test_the_assistant_scope_reaches_every_search_in_one_answer(tmp_path: Path) -> None:
    """Pinned at the retriever, not the route: `multi_search` fans out over
    (query x mode) pairs and each one has to see the same scope."""

    from pheasant.assistant.retrieval import PheasantRetriever

    tools = _tools(tmp_path)
    try:
        retriever = PheasantRetriever(
            search=tools.searcher,
            knowledge_base=tools.config.knowledge_base_id,
            graph=tools.engine.graph_builder.graph,
            state=tools.state,
            config=tools.config,
            source_types=["markdown_folder"],
        )
        passages = retriever.multi_search(
            ["gateway credentials", "rotation nightly"], modes=["text", "hybrid"], limit=10
        )
        assert passages, "the scope dropped everything"
        assert {p.source_id for p in passages} == {"handbook"}
    finally:
        tools.engine.close()
