# Graph Model

SyncSage uses a directed multi-graph model, compatible with `networkx.MultiDiGraph`, so multiple relationship types can connect the same pair of nodes.

## Node types

| Type | Purpose |
|---|---|
| `knowledge_base` | Root graph node for one SyncSage instance/config domain. |
| `source` | Configured source root. |
| `repository`, `branch`, `commit` | Git-aware repository context. |
| `directory`, `file`, `document`, `markdown_note` | Indexed filesystem artifacts. |
| `heading`, `chunk` | Retrieval and document structure units. |
| `symbol` | Code symbols, constants, and call targets extracted from language-aware passes. |
| `entity` | Named systems, products, people, or CamelCase/Title Case mentions. |
| `concept` | Normalized topic terms shared across artifacts for navigation and similarity. |
| `external_reference` | Imported modules, URLs, wiki links, document links, and citations. |
| `dependency`, `tag`, `topic`, `query`, `agent_action` | Optional classification, retrieval audit, and feedback-loop nodes. |

## Edge types

| Type | Purpose |
|---|---|
| `contains`, `indexes`, `has_chunk`, `has_heading` | Hierarchy and indexing relationships. |
| `mentions`, `derived_from` | Artifact-to-entity/concept/symbol links and reverse provenance. |
| `imports`, `calls` | Repository/code relationships. |
| `references` | Markdown links, URLs, wiki links, citations, and other external references. |
| `similar_to` | Lightweight semantic similarity between artifacts based on shared concepts. |
| `links_to`, `tagged_with` | Optional Markdown/document relationships. |
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

## Enrichment passes

SyncSage runs deterministic enrichment during sync:

- Code pass: extracts Python imports, classes, functions, constants, and call targets.
- Markdown/document pass: extracts headings, links, wiki links, URLs, citations, concepts, and named mentions.
- Semantic similarity pass: links artifacts with `similar_to` when their normalized concept sets overlap.

Graph-derived terms are also written to SQLite so hybrid search can surface multiple relevant files for a cross-file query, even when no single chunk contains every query term.
