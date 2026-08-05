# Agentic Workflows

pheasant is designed to keep agent context precise and fresh without loading whole repositories or document collections into prompts.

## Coding workflow

1. Agent calls `get_relevant_files` with the task.
2. Agent inspects returned paths, summaries, chunks, graph neighbors, and provenance.
3. Agent edits the smallest safe file set.
4. Agent runs tests/checks.
5. Agent commits changes or records the completed write action.
6. Agent calls `sync_source(mode=incremental)`.
7. Agent checks `get_sync_status`.
8. Next task uses the updated graph/search index.

## Research workflow

1. Agent calls `search_context` with a focused question.
2. Agent uses returned chunks with source paths and headings.
3. Agent calls `get_graph_neighbors` for referenced notes/files.
4. Agent summarizes only from retrieved evidence.
5. Agent can call `export_obsidian_notes` for human review.

## Multi-agent safety

- Do not revert edits made by other agents.
- Prefer explicit sync after commits rather than reacting to every save.
- Record branch, commit, and working tree state in provenance.
- If branch/commit changes during indexing, mark the sync stale and retry.
