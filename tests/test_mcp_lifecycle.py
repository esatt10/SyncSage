from __future__ import annotations

from pathlib import Path

from syncsage.mcp_server.tools import SyncSageTools


def test_runtime_source_lifecycle_sync_query_and_promote(
    loaded_config: object,
    vault_path: Path,
) -> None:
    runtime_root = vault_path / "runtime-context"
    runtime_root.mkdir()
    (runtime_root / "context.md").write_text(
        "# Runtime Context\n\nRuntime promoted source contains durable configuration notes.\n",
        encoding="utf-8",
    )
    tools = SyncSageTools(loaded_config)
    knowledge_base = loaded_config.knowledge_base_id

    registered = tools.register_source(
        knowledge_base,
        name="runtime-context",
        source_type="markdown_folder",
        path=str(runtime_root),
        include=["**/*.md"],
        actor="pytest",
        client_id="mcp-test",
    )
    synced = tools.sync_source(knowledge_base, "runtime-context", "full")
    search = tools.search_context(knowledge_base, "durable configuration", max_results=5)
    listed = tools.list_sources(knowledge_base, source_type="markdown_folder", limit=10)
    promoted = tools.promote_runtime_source_to_config(knowledge_base, "runtime-context")
    disabled = tools.disable_source(knowledge_base, "runtime-context")
    removed = tools.remove_source(knowledge_base, "runtime-context")
    history = tools.get_sync_history(knowledge_base, "runtime-context")

    assert registered["status"] == "registered"
    assert registered["provenance"]["registered_by"] == "pytest"
    assert synced["indexed_artifacts"] == 1
    assert search["results"]
    assert any(source["name"] == "runtime-context" for source in listed["sources"])
    assert "name: runtime-context" in promoted["yaml_patch"]
    assert "type: markdown_folder" in promoted["yaml_patch"]
    assert disabled["status"] == "disabled"
    assert removed["status"] == "removed"
    assert any(event["action"] == "register_source" for event in history["events"])
    assert any(event["action"] == "promote_runtime_source_to_config" for event in history["events"])
