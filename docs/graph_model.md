# Graph Model

SyncSage uses a directed multi-graph model, compatible with `networkx.MultiDiGraph`, so multiple relationship types can connect the same pair of nodes.

## Node types

| Type | Purpose |
|---|---|
| `knowledge_base` | Root graph node for one SyncSage instance/config domain. |
| `source` | Configured source root. |
| `repository`, `branch`, `commit` | Git-aware repository context. |
| `directory`, `file`, `document`, `markdown_note` | Indexed filesystem artifacts. |
| `heading`, `chunk`, `symbol` | Retrieval and code/document structure units. |
| `dependency`, `tag`, `topic`, `query`, `agent_action` | Relationships, classification, retrieval audit, and optional feedback loops. |

## Edge types

| Type | Purpose |
|---|---|
| `contains`, `indexes`, `has_chunk`, `has_heading` | Hierarchy and indexing relationships. |
| `defines_symbol`, `imports`, `calls` | Repository/code relationships. |
| `links_to`, `references`, `tagged_with` | Markdown/document relationships. |
| `belongs_to_branch`, `at_commit`, `supersedes` | Git and version lineage. |
| `generated_note`, `retrieved_by`, `modified_by` | Obsidian projection and agent audit. |

## Stable IDs

Stable IDs must include source identity and enough path/hash/context to support idempotent upserts:

```text
<node_type>:<source_id>:<stable_path_or_hash>:<optional_context>
```

Examples:

```text
source:local-syncsage:syncsage-codebase
file:syncsage-codebase:src/syncsage/main.py:branch=main
chunk:syncsage-codebase:src/syncsage/main.py:sha256=abc123:chunk=0004
symbol:syncsage-codebase:src/syncsage/main.py:SyncSageServer.start
commit:syncsage-codebase:6f2a9c1
```

## Required provenance

Nodes and search results should record source ID, knowledge base ID, relative path, content hash, indexed timestamp, branch, commit, and parser/rule provenance when available.
