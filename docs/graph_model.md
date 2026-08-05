# Graph Model

pheasant uses a directed multi-graph model, compatible with `networkx.MultiDiGraph`, so multiple relationship types can connect the same pair of nodes.

## Node types

| Type | Purpose |
|---|---|
| `knowledge_base` | Root graph node for one pheasant instance/config domain. |
| `source` | Configured source root. |
| `repository`, `branch`, `commit` | Git-aware repository context. |
| `directory`, `file`, `document`, `markdown_note` | Indexed filesystem artifacts. |
| `heading`, `chunk` | Retrieval and document structure units. |
| `symbol` | Code symbols, constants, and call targets extracted from language-aware passes. |
| `entity` | Named systems, products, people, or CamelCase/Title Case mentions. |
| ~~`concept`~~ | **Retired 2026-08-03.** Concept extraction produced 87.2% of a 162k-node graph and 98.6% of its edges while contributing nothing measurable: the retrieval expansion path never fired, the facts panel filled every slot with terms like "limit" and "request info", and the similarity pass it fed emitted zero edges. See `graph.enrichment._add_concept`. |
| `external_reference` | Imported modules, URLs, wiki links, document links, and citations. |
| `dependency`, `tag`, `topic`, `query`, `agent_action` | Optional classification, retrieval audit, and feedback-loop nodes. |

## Edge types

| Type | Purpose |
|---|---|
| `contains`, `indexes`, `has_chunk`, `has_heading` | Hierarchy and indexing relationships. |
| `mentions`, `derived_from` | Artifact-to-entity/symbol links and reverse provenance. |
| `imports`, `calls` | Repository/code relationships. |
| `references` | Markdown links, URLs, wiki links, citations, and other external references. |
| ~~`similar_to`~~ | **Retired 2026-08-03** with the concept layer it keyed off. Not a silent loss: a live 2,132-file graph contained zero of these edges, so the pairwise pass that produced them was doing a full artifact-by-artifact scan every sync and emitting nothing. |
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
source:local-pheasant:pheasant-codebase
file:pheasant-codebase:src/pheasant/main.py:branch=main
chunk:pheasant-codebase:src/pheasant/main.py:sha256=abc123:chunk=0004
symbol:pheasant-codebase:src/pheasant/main.py:PheasantServer.start
commit:pheasant-codebase:6f2a9c1
```

## Required provenance

Nodes and search results should record source ID, knowledge base ID, relative path, content hash, indexed timestamp, branch, commit, and parser/rule provenance when available.

## Enrichment passes

pheasant runs deterministic enrichment during sync:

- Code pass: extracts Python imports, classes, functions, constants, and call targets.
- Markdown/document pass: extracts headings, links, wiki links, URLs, citations, concepts, and named mentions.
- Internal reference resolution: a post-sync pass that turns a file's imports
  and document links into edges pointing at **the file they resolve to**,
  by longest-suffix path match (`agent_framework._workflows._checkpoint` →
  `.../agent_framework/_workflows/_checkpoint.py`). Deterministic and
  rule-based — no LLM. Same-source targets carry
  `enrichment_pass: internal_resolution`; cross-source ones keep
  `cross_source_resolution` (Synapse 21.6B).

  This is the file-to-file connectivity the graph advertised and did not have.
  Resolution used to skip same-source targets on the belief that intra-source
  enrichment already covered them; it did not, so a single-source knowledge
  base — the common case — had **zero** file→file import edges while carrying
  1,871 `imports` edges that all terminated at a bare module name.

Graph-derived terms are also written to SQLite so hybrid search can surface multiple relevant files for a cross-file query, even when no single chunk contains every query term.

Search runs in three modes. `text` queries the SQLite full-text index over chunk content and paths. `graph` matches directly against the live graph — node labels, types and attribute values, plus relationship types and endpoint labels — so enrichment outputs (symbols, entities, external references) and the relationships between them are first-class search targets, not just the chunk text they were derived from. `hybrid` (the default) runs both and merges the results, de-duplicating by node and re-ranking by score. The retrieval mode and the maximum result count are both caller-adjustable.
