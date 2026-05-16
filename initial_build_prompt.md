# SyncSage Build & Specification Document

**Version:** 0.1.0 draft  
**Date:** 2026-05-15  
**Project type:** Docker-first MCP server, lightweight knowledge graph indexer, Obsidian-compatible knowledge vault, and agentic retrieval layer  
**Primary implementation language:** Python 3.11+  
**Primary runtime:** Docker container, with optional Docker Desktop Kubernetes and enterprise Kubernetes deployment

---

## 1. Executive Summary

SyncSage is a lightweight, Docker-first Model Context Protocol server that creates and maintains local knowledge graphs over repositories, Markdown files, web-derived content, documents, and watched folders. Its purpose is to give agentic workflows a precise, low-token, tool-callable context layer without requiring a heavy enterprise search platform such as Azure AI Search, a hosted vector database, or a large application stack.

SyncSage operates as a local or enterprise-deployable server that:

1. Registers knowledge sources through a YAML configuration file or API/tool call.
2. Indexes each source into a persistent graph and search store.
3. Watches local directories and repositories for changes.
4. Re-indexes idempotently when files change, commits occur, or an agent explicitly requests a refresh.
5. Exposes MCP tools, resources, and prompts so downstream agents can ask for targeted context.
6. Optionally publishes human-readable Markdown notes into an Obsidian vault so the user can inspect and navigate the knowledge base visually.
7. Supports a simple deployment path from one local Docker command to local Kubernetes namespaces to enterprise Kubernetes namespaces.

The core product idea is: **a small, portable knowledge scaffolding service that lets agents retrieve the right files, chunks, dependencies, and relationships without loading entire repositories or document sets into the prompt.**

---

## 2. Product Purpose

### 2.1 Problem Statement

Agentic coding and knowledge workflows frequently need context from large local resources:

- Code repositories
- README files
- Architecture documents
- Markdown notes
- PDFs and Word documents
- HTML/XML files
- Web pages or exported articles
- Local project folders
- Obsidian vaults

Without an index, agents tend to either:

- Load too much context, increasing token usage and latency.
- Miss important files because retrieval is ad hoc.
- Repeatedly rediscover the same project structure.
- Lose accuracy after new commits because their context becomes stale.
- Require a heavy external search system that is overkill for local or early-stage workflows.

SyncSage solves this by maintaining a local graph + search index that can be referenced by name.

### 2.2 Desired User Experience

A user should be able to define a YAML file such as:

```yaml
syncsage:
  name: personal-dev-knowledge
  vault_path: /vault
  state_path: /state

sources:
  - name: syncsage-repo
    type: repository
    path: /workspace/syncsage
    branch_policy: current
    sync:
      on_startup: true
      on_file_change: debounce
      on_git_commit: true
      interval_seconds: 900

  - name: architecture-notes
    type: markdown_folder
    path: /workspace/notes/architecture
    sync:
      on_startup: true
      on_file_change: debounce

  - name: product-documents
    type: document_folder
    path: /workspace/docs
    include:
      - "**/*.pdf"
      - "**/*.docx"
      - "**/*.md"
    exclude:
      - "**/.git/**"
      - "**/node_modules/**"
```

Then start SyncSage with a single command:

```bash
docker run --rm \
  --name syncsage \
  -p 8765:8765 \
  -v "$PWD/syncsage.yaml:/config/syncsage.yaml:ro" \
  -v "$HOME/projects:/workspace" \
  -v "$HOME/SyncSageVault:/vault" \
  -v syncsage-state:/state \
  ghcr.io/<org>/syncsage:latest
```

After startup, an agentic workflow should be able to call MCP tools such as:

- `list_knowledge_bases`
- `register_source`
- `sync_source`
- `search_context`
- `get_relevant_files`
- `get_graph_neighbors`
- `get_file_summary`
- `get_repo_map`
- `explain_node`
- `export_obsidian_notes`

The agent should not need to know the full file system layout. It should call SyncSage by knowledge base name and intent.

---

## 3. Non-Goals

SyncSage should remain lightweight. The initial build should avoid becoming a full enterprise search platform.

Out of scope for the first implementation:

- Multi-tenant cloud SaaS
- Large-scale distributed indexing across thousands of repositories
- Fine-grained role-based access control beyond local or namespace-level isolation
- Full replacement for Azure AI Search, Elastic, OpenSearch, or enterprise document management systems
- Full replacement for Obsidian
- Full replacement for Git hosting platforms
- Deep semantic code understanding equal to language servers or commercial code intelligence systems
- Automatic execution of arbitrary agent code from untrusted prompts

SyncSage should be designed so these features can be added later, but the first product should stay small, inspectable, and easy to deploy.

---

## 4. Core Design Principles

1. **Docker-first portability**  
   One container image should support local, Docker Desktop Kubernetes, and enterprise Kubernetes deployment.

2. **Config-first operation**  
   A YAML file should define knowledge bases, watched paths, sync policies, search settings, and Obsidian output rules.

3. **Human-readable persistence where useful**  
   The Obsidian vault should remain Markdown-first, with YAML frontmatter and backlinks.

4. **Machine-efficient persistence where necessary**  
   Graph state, search state, sync manifests, hashes, embeddings, and operational metadata should live in a state directory or SQLite database, not only in Markdown.

5. **Idempotent indexing**  
   Re-running indexing on the same source state must not duplicate nodes, edges, chunks, or notes.

6. **Incremental updates by default**  
   Cold start should validate the full state, but normal operation should re-index changed files or changed commits only.

7. **Branch-aware repository indexing**  
   Git branch, commit SHA, worktree state, and source path must be part of the index identity.

8. **Agent-friendly retrieval**  
   Retrieval should return precise files, chunks, graph neighborhoods, and summaries with enough provenance for an agent to act safely.

9. **User-facing graph visibility**  
   Users should be able to inspect the graph through Obsidian-compatible Markdown, JSON graph exports, and optionally a simple web UI with a force-directed graph.

10. **No unnecessary orchestration**  
   Multiple SyncSage instances can run separately with their own volumes and namespaces. A central coordinator is optional, not required.

---

## 5. Reference Architecture

### 5.1 Logical Components

```text
+--------------------------------------------------------------+
|                        Agentic Workflow                       |
|      Claude / Cursor / Codex / LangGraph / custom agent        |
+----------------------------+---------------------------------+
                             |
                             | MCP tools/resources/prompts
                             v
+--------------------------------------------------------------+
|                           SyncSage                            |
|                                                              |
|  +-------------------+    +-------------------+               |
|  | MCP Server Layer  |    | REST/Admin API    |               |
|  +-------------------+    +-------------------+               |
|             |                       |                          |
|             v                       v                          |
|  +--------------------------------------------------------+    |
|  |                Source Registry & Config Loader          |    |
|  +--------------------------------------------------------+    |
|             |                       |                          |
|             v                       v                          |
|  +-------------------+    +-------------------+               |
|  | Sync Engine       |    | File/Git Watchers |               |
|  +-------------------+    +-------------------+               |
|             |                                                  |
|             v                                                  |
|  +--------------------------------------------------------+    |
|  |                 Ingestion & Parsing Pipeline            |    |
|  | repo parser | markdown parser | doc parser | web parser     |
|  +--------------------------------------------------------+    |
|             |                                                  |
|             v                                                  |
|  +-------------------+    +-------------------+               |
|  | Graph Store       |    | Search Store      |               |
|  | NetworkX + JSON   |    | SQLite FTS / vec  |               |
|  +-------------------+    +-------------------+               |
|             |                       |                          |
|             v                       v                          |
|  +--------------------------------------------------------+    |
|  |       Obsidian Exporter + Graph JSON Visualization       |    |
|  +--------------------------------------------------------+    |
+--------------------------------------------------------------+
                             |
                             v
+--------------------------------------------------------------+
|                 Mounted Volumes / Persistent Storage           |
|   /workspace  /config  /state  /vault  /exports  /logs         |
+--------------------------------------------------------------+
```

### 5.2 Runtime Components

| Component | Responsibility |
|---|---|
| MCP server | Exposes SyncSage as tools/resources/prompts for agentic workflows. |
| Admin API | Provides HTTP endpoints for config inspection, health checks, sync triggers, graph exports, and UI access. |
| Source registry | Stores configured sources and runtime registration metadata. |
| Sync engine | Orchestrates startup scans, file watcher events, git commit events, scheduled refreshes, and manual refreshes. |
| Watcher service | Watches up to roughly 100 configured local folders or repositories. |
| Parser pipeline | Converts source files into documents, chunks, code symbols, metadata, and graph relationships. |
| Graph builder | Creates and updates NetworkX graph nodes and edges. |
| Search indexer | Updates SQLite FTS, optional embeddings, and optional vector index. |
| Persistence service | Writes graph, manifest, database, and Obsidian notes to durable storage. |
| Visualization exporter | Produces graph JSON and optional static or live force-directed visualization payloads. |
| Obsidian bridge | Writes Markdown notes, frontmatter, backlinks, and index notes into the mounted vault directory. |

---

## 6. Recommended Technology Stack

### 6.1 Required Core Packages

| Purpose | Package | Notes |
|---|---|---|
| MCP server | `mcp` / official MCP Python SDK | Expose resources, prompts, and tools. |
| API server | `fastapi` | Admin API, health endpoints, optional graph UI backend. |
| ASGI runtime | `uvicorn` | Runs FastAPI. |
| Data validation | `pydantic` | Config, API contracts, graph models. |
| YAML config | `pyyaml` or `ruamel.yaml` | Load and validate `syncsage.yaml`. |
| Graph modeling | `networkx` | In-memory graph creation, traversal, centrality, exports. |
| File watching | `watchdog` | Event-driven watching of folders/files. |
| SQLite persistence | `sqlite3` stdlib or `apsw` | Metadata, manifests, FTS search, idempotency. |
| Git inspection | `GitPython` or subprocess `git` | Detect branch, commit SHA, changed files. |
| Markdown parsing | `markdown-it-py` | Parse Markdown structure and headings. |
| HTML/XML parsing | `beautifulsoup4`, `lxml` | Parse local HTML/XML and web-derived artifacts. |
| PDF parsing | `pymupdf` | Extract text, tables, and metadata from PDF files. |
| DOCX parsing | `python-docx` | Extract text from Word documents. |
| CLI | `typer` | Local commands such as `syncsage init`, `syncsage validate`, `syncsage sync`. |
| Logging | `structlog` or stdlib `logging` | Structured logs for sync and retrieval activity. |
| Tests | `pytest`, `pytest-asyncio` | Unit and integration tests. |

### 6.2 Optional Packages

| Purpose | Package | Use Case |
|---|---|---|
| Code parsing | `tree-sitter`, language grammars | Symbol-level code graph extraction. |
| Token counting | `tiktoken` or provider-specific tokenizers | Estimate token savings and chunk sizes. |
| Local embeddings | `sentence-transformers` | Optional semantic retrieval without external APIs. |
| Vector search | `chromadb`, `lancedb`, or `qdrant-client` | Optional vector retrieval for larger or semantic-heavy use cases. |
| Web crawling | `httpx`, `trafilatura`, `readability-lxml` | Ingest web-based sources. |
| Visualization | `cytoscape.js`, `d3-force`, `react-force-graph` | Optional frontend for graph browsing. |
| Metrics | `prometheus-client` | Metrics endpoint for enterprise deployments. |
| Scheduling | `apscheduler` | Fallback scheduled sync jobs. |
| Task queue | `rq`, `dramatiq`, or `celery` | Optional for heavier enterprise-scale sync workloads. |

### 6.3 Recommended Default Choices

For the first build, use:

- `mcp` official Python SDK for MCP compatibility.
- `FastAPI` + `uvicorn` for local admin endpoints.
- `NetworkX` for graph modeling.
- `SQLite` for durable metadata, FTS, and idempotency manifests.
- `watchdog` for file watchers.
- `GitPython` or `git` subprocess for repository state.
- `markdown-it-py`, `beautifulsoup4`, `pymupdf`, `python-docx` for parsing.
- Obsidian vault compatibility through Markdown files and YAML frontmatter, not by running the Obsidian desktop app inside the container.

The recommended architecture is to treat Obsidian as the **user experience layer** over a mounted vault folder. SyncSage writes Markdown notes into that vault. The user can open the vault in the desktop Obsidian app, while the container keeps the vault files updated.

Running the Obsidian desktop GUI inside the same Docker container is not recommended for v0.1 because it adds unnecessary GUI/VNC complexity and weakens the one-line local workflow.

---

## 7. Repository Structure

Recommended open-source repository layout:

```text
syncsage/
  README.md
  LICENSE
  pyproject.toml
  uv.lock or poetry.lock
  Dockerfile
  docker-compose.yml
  syncsage.example.yaml
  .env.example
  .gitignore

  src/
    syncsage/
      __init__.py
      main.py
      cli.py

      config/
        loader.py
        schema.py
        defaults.py

      mcp_server/
        server.py
        tools.py
        resources.py
        prompts.py
        contracts.py

      api/
        app.py
        routes_health.py
        routes_sources.py
        routes_graph.py
        routes_search.py
        routes_sync.py

      registry/
        source_registry.py
        knowledge_base_registry.py

      sync/
        engine.py
        watcher.py
        git_monitor.py
        scheduler.py
        event_queue.py
        debounce.py
        locks.py

      ingestion/
        pipeline.py
        chunking.py
        content_types.py
        repository_parser.py
        markdown_parser.py
        document_parser.py
        web_parser.py
        code_parser.py
        ignore_rules.py

      graph/
        model.py
        builder.py
        algorithms.py
        diff.py
        exporter.py
        serializer.py

      search/
        sqlite_store.py
        fts.py
        hybrid.py
        embeddings.py
        vector_store.py
        ranking.py

      persistence/
        paths.py
        manifest.py
        graph_store.py
        state_store.py
        migrations.py
        snapshots.py

      obsidian/
        exporter.py
        frontmatter.py
        note_templates.py
        backlinks.py
        canvas_export.py

      visualization/
        graph_json.py
        static_assets/
        web.py

      security/
        path_policy.py
        allowlist.py
        secrets.py
        sandbox.py

      telemetry/
        logging.py
        metrics.py
        audit.py

  tests/
    unit/
    integration/
    fixtures/
      sample_repo/
      sample_vault/
      sample_docs/

  deploy/
    docker/
    compose/
    kubernetes/
      namespace.yaml
      configmap.yaml
      deployment.yaml
      service.yaml
      pvc.yaml
    helm/
      Chart.yaml
      values.yaml
      templates/

  docs/
    architecture.md
    configuration.md
    graph_model.md
    mcp_tools.md
    deployment.md
    agentic_workflows.md
    obsidian_integration.md
    security.md
    troubleshooting.md

  agent/
    build_agent_config.yaml
    product_owner_prompt.md
    architect_prompt.md
    backend_engineer_prompt.md
    graph_engineer_prompt.md
    ingestion_engineer_prompt.md
    devops_engineer_prompt.md
    qa_engineer_prompt.md
    security_reviewer_prompt.md
```

---

## 8. Configuration Specification

### 8.1 Primary Config File

Default path inside container:

```text
/config/syncsage.yaml
```

### 8.2 Example Full Config

```yaml
syncsage:
  name: local-syncsage
  description: Lightweight MCP knowledge graph and retrieval server
  environment: local
  log_level: INFO
  state_path: /state
  vault_path: /vault
  workspace_root: /workspace
  exports_path: /exports

server:
  host: 0.0.0.0
  port: 8765
  mcp:
    enabled: true
    transports:
      stdio: true
      streamable_http: true
      sse: false
  api:
    enabled: true
    openapi: true
  ui:
    enabled: true
    graph_visualization: true

storage:
  graph_format: node_link_json
  graph_snapshot_interval_seconds: 900
  sqlite_path: /state/syncsage.db
  graph_path: /state/graphs
  manifest_path: /state/manifests
  max_state_size_gb: 10
  compression:
    enabled: true
    algorithm: zstd
  retention:
    keep_snapshots: 10
    keep_event_days: 30

search:
  default_mode: hybrid
  keyword:
    enabled: true
    engine: sqlite_fts5
  embeddings:
    enabled: false
    provider: local
    model: sentence-transformers/all-MiniLM-L6-v2
  vector_store:
    enabled: false
    engine: chroma
    path: /state/vector
  ranking:
    prefer_exact_path_matches: true
    prefer_recent_commits: true
    graph_neighbor_boost: true
    max_results_default: 10

sync:
  startup:
    full_validation: true
    repair_missing_indexes: true
  watcher:
    enabled: true
    max_watch_paths: 100
    debounce_ms: 1500
    batch_window_ms: 5000
  git:
    enabled: true
    detect_commit_changes: true
    detect_branch_switch: true
    reindex_on_commit: true
    reindex_on_branch_switch: validate_only
  scheduler:
    enabled: true
    interval_seconds: 900
  idempotency:
    hash_algorithm: sha256
    compare_size_mtime_hash: true
    skip_unchanged_files: true
  concurrency:
    max_parallel_sources: 4
    max_parallel_files: 8
    lock_timeout_seconds: 120

obsidian:
  enabled: true
  write_mode: upsert
  note_root: SyncSage
  create_index_notes: true
  create_source_notes: true
  create_file_notes: true
  create_chunk_notes: false
  create_canvas: true
  frontmatter:
    include_source_id: true
    include_hash: true
    include_last_indexed: true
    include_graph_node_id: true
  backlinks:
    enabled: true
    style: wikilink
  tags:
    base:
      - syncsage
    by_source_type: true

sources:
  - name: syncsage-codebase
    type: repository
    path: /workspace/syncsage
    description: SyncSage project repository
    enabled: true
    include:
      - "**/*.py"
      - "**/*.md"
      - "**/*.yaml"
      - "**/*.yml"
      - "**/*.toml"
      - "**/Dockerfile"
    exclude:
      - "**/.git/**"
      - "**/__pycache__/**"
      - "**/.venv/**"
      - "**/node_modules/**"
      - "**/dist/**"
      - "**/build/**"
    repo:
      branch_policy: current
      include_uncommitted: true
      commit_trigger: true
      dependency_graph:
        enabled: true
        languages:
          python: true
          javascript: false
          typescript: false
    chunking:
      strategy: semantic
      max_chars: 4000
      overlap_chars: 400
    sync:
      on_startup: true
      on_file_change: debounce
      on_git_commit: true
      interval_seconds: 900

  - name: architecture-vault
    type: obsidian_vault
    path: /vault
    enabled: true
    include:
      - "**/*.md"
      - "**/*.canvas"
    exclude:
      - "**/.obsidian/**"
      - "**/.trash/**"
    sync:
      on_startup: true
      on_file_change: debounce

  - name: documents
    type: document_folder
    path: /workspace/documents
    enabled: true
    include:
      - "**/*.pdf"
      - "**/*.docx"
      - "**/*.txt"
      - "**/*.md"
    exclude:
      - "**/~$*"
    chunking:
      strategy: heading_or_page
      max_chars: 5000
      overlap_chars: 500
    sync:
      on_startup: true
      on_file_change: debounce
      interval_seconds: 3600
```

### 8.3 Source Types

| Type | Description |
|---|---|
| `repository` | Git or non-Git source code repository. Includes file graph, optional dependency graph, commit metadata. |
| `markdown_folder` | Folder of Markdown files. Extracts headings, links, frontmatter, tags. |
| `obsidian_vault` | Markdown folder with Obsidian conventions such as wikilinks, tags, canvas files, and `.obsidian` settings excluded by default. |
| `document_folder` | Folder containing PDFs, DOCX, TXT, Markdown, HTML, XML. |
| `web_collection` | Set of URLs or locally cached web pages. Optional for v0.1. |
| `single_file` | A single file artifact to index. |

---

## 9. Persistence Model

### 9.1 Storage Layout

Inside mounted state volume:

```text
/state/
  syncsage.db
  graphs/
    <knowledge_base_id>/
      graph.latest.json
      graph.<timestamp>.json.zst
  manifests/
    <source_id>.manifest.json
  snapshots/
    <knowledge_base_id>/
  vector/
  cache/
  locks/
  logs/
```

Inside mounted Obsidian vault:

```text
/vault/
  SyncSage/
    Index.md
    Sources/
      syncsage-codebase.md
      documents.md
    Repositories/
      syncsage-codebase/
        Repo Map.md
        Files/
          src__syncsage__main.py.md
    Documents/
      product-documents/
        Some PDF.md
    Graphs/
      syncsage-codebase.canvas
    Queries/
      recent-searches.md
```

### 9.2 Recommended Persistence Formats

| Data | Format | Reason |
|---|---|---|
| Graph | NetworkX node-link JSON | Portable, easy to reload, compatible with graph visualization. |
| Operational metadata | SQLite | Durable, queryable, simple local file persistence. |
| Full-text search | SQLite FTS5 | Lightweight keyword search without external service. |
| File manifests | JSON | Easy diffing, troubleshooting, repair. |
| Obsidian notes | Markdown + YAML frontmatter | Human-readable and Git-friendly. |
| Large snapshots | Compressed JSON | Keeps storage below target thresholds. |
| Optional vectors | Chroma/LanceDB/Qdrant/local files | Enable semantic retrieval only when needed. |

### 9.3 Why Not Store Everything in Obsidian?

Obsidian is excellent as a human-readable vault and graph UI, but it should not be the only persistence layer for SyncSage because:

- Raw graph state can grow large.
- Search metadata and manifests require fast machine queries.
- Chunk-level data can create thousands of notes and clutter the vault.
- Idempotency requires stable hashes and source manifests.
- Obsidian is best used as the UX and reflection layer, not the only database.

Recommended split:

- **SQLite / JSON graph:** operational source of truth.
- **Obsidian Markdown:** user-facing projection of the most useful graph information.

---

## 10. Knowledge Base Meta Model

### 10.1 Core Entities

```text
KnowledgeBase
  âââ Source
        âââ Artifact
              âââ Chunk
              âââ Symbol
              âââ Metadata
```

### 10.2 Entity Definitions

#### KnowledgeBase

A named logical collection of sources.

Attributes:

- `knowledge_base_id`
- `name`
- `description`
- `created_at`
- `updated_at`
- `config_hash`
- `state_version`

#### Source

A configured root input such as a repository, document folder, Markdown folder, or vault.

Attributes:

- `source_id`
- `knowledge_base_id`
- `name`
- `type`
- `path`
- `enabled`
- `include_patterns`
- `exclude_patterns`
- `sync_policy`
- `last_indexed_at`
- `last_status`

#### Artifact

A file, URL, document, repository module, or other indexable object.

Attributes:

- `artifact_id`
- `source_id`
- `type`
- `path`
- `relative_path`
- `uri`
- `mime_type`
- `extension`
- `size_bytes`
- `sha256`
- `mtime`
- `git_branch`
- `git_commit`
- `last_indexed_at`

#### Chunk

A retrievable section of text or code.

Attributes:

- `chunk_id`
- `artifact_id`
- `source_id`
- `chunk_index`
- `heading_path`
- `start_line`
- `end_line`
- `start_char`
- `end_char`
- `text_hash`
- `summary`
- `token_estimate`

#### Symbol

A code-level entity, such as a function, class, method, import, module, or configuration key.

Attributes:

- `symbol_id`
- `artifact_id`
- `source_id`
- `language`
- `symbol_type`
- `name`
- `qualified_name`
- `start_line`
- `end_line`
- `signature`
- `docstring_summary`

---

## 11. Graph Model

### 11.1 Graph Type

Use a directed multi-graph where useful:

```python
networkx.MultiDiGraph
```

A `MultiDiGraph` allows multiple relationship types between the same pair of nodes. For example, a Markdown file can both `links_to` and `references` another node.

### 11.2 Node Types

| Node Type | Purpose |
|---|---|
| `knowledge_base` | Root graph node for a named knowledge base. |
| `source` | Configured source root. |
| `repository` | Git repository root. |
| `branch` | Git branch context. |
| `commit` | Git commit snapshot. |
| `directory` | Folder path. |
| `file` | File artifact. |
| `document` | Document artifact such as PDF or DOCX. |
| `markdown_note` | Markdown or Obsidian note. |
| `heading` | Heading inside Markdown or document. |
| `chunk` | Searchable text/code chunk. |
| `symbol` | Code symbol such as class/function/import. |
| `dependency` | Package or module dependency. |
| `tag` | Obsidian or extracted tag. |
| `topic` | Optional inferred or configured topic. |
| `query` | Saved query or retrieval pattern. |
| `agent_action` | Optional recorded agent-triggered event. |

### 11.3 Edge Types

| Edge Type | From | To | Purpose |
|---|---|---|---|
| `contains` | knowledge_base/source/directory/file | child node | Hierarchy. |
| `indexes` | source | artifact | Source indexing relationship. |
| `has_chunk` | artifact | chunk | Retrieval unit. |
| `has_heading` | artifact | heading | Document structure. |
| `defines_symbol` | file | symbol | Code definitions. |
| `imports` | file/symbol | dependency/symbol | Code dependency. |
| `calls` | symbol | symbol | Optional code call graph. |
| `links_to` | markdown_note | markdown_note | Obsidian/Markdown link. |
| `references` | chunk/symbol | artifact/chunk | Citation or reference. |
| `tagged_with` | artifact/chunk/note | tag | Classification. |
| `belongs_to_branch` | artifact/commit | branch | Git branch context. |
| `at_commit` | artifact | commit | Git snapshot. |
| `supersedes` | artifact/chunk/snapshot | older node | Version lineage. |
| `generated_note` | graph node | markdown_note | Obsidian projection. |
| `retrieved_by` | chunk/file | query | Retrieval audit. |
| `modified_by` | artifact | agent_action | Optional agent feedback loop. |

### 11.4 Stable Node IDs

Stable IDs are critical for idempotency.

Recommended ID format:

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

### 11.5 Node Attributes

Every node should include:

```json
{
  "id": "file:syncsage-codebase:src/syncsage/main.py",
  "type": "file",
  "label": "main.py",
  "source_id": "syncsage-codebase",
  "knowledge_base_id": "local-syncsage",
  "created_at": "2026-05-15T20:00:00Z",
  "updated_at": "2026-05-15T20:05:00Z",
  "hash": "sha256:...",
  "provenance": {
    "path": "/workspace/syncsage/src/syncsage/main.py",
    "relative_path": "src/syncsage/main.py",
    "git_branch": "main",
    "git_commit": "6f2a9c1"
  }
}
```

### 11.6 Edge Attributes

Every edge should include:

```json
{
  "type": "contains",
  "source_id": "syncsage-codebase",
  "created_at": "2026-05-15T20:00:00Z",
  "confidence": 1.0,
  "provenance": {
    "parser": "repository_parser",
    "rule": "directory_hierarchy"
  }
}
```

---

## 12. Search and Retrieval Design

### 12.1 Retrieval Goals

SyncSage retrieval should reduce token usage by returning:

- Relevant files instead of whole repositories.
- Relevant chunks instead of whole files.
- Graph neighborhoods instead of flat search results.
- Summaries plus source references.
- Path, line, and commit provenance.

### 12.2 Search Modes

| Mode | Description | Default? |
|---|---|---|
| `keyword` | SQLite FTS5 keyword search. | Yes |
| `path` | Search by path, filename, extension, folder. | Yes |
| `graph` | Traverse neighbors from known nodes. | Yes |
| `hybrid` | Keyword + path + graph ranking. | Yes |
| `semantic` | Optional embeddings/vector similarity. | No for v0.1 |
| `symbol` | Code symbol search. | Yes for repositories |

### 12.3 Ranking Signals

Rank results using:

1. Exact filename/path match.
2. Keyword score from FTS.
3. Graph proximity to known source, file, or topic.
4. Recency from git commit or file modification time.
5. Source type priority.
6. Chunk heading match.
7. Symbol name match.
8. Optional semantic similarity score.
9. Agent-provided intent.

### 12.4 Retrieval Response Contract

```json
{
  "query": "where is the sync engine implemented?",
  "knowledge_base": "local-syncsage",
  "mode": "hybrid",
  "results": [
    {
      "rank": 1,
      "node_id": "file:syncsage-codebase:src/syncsage/sync/engine.py",
      "type": "file",
      "title": "sync/engine.py",
      "path": "/workspace/syncsage/src/syncsage/sync/engine.py",
      "relative_path": "src/syncsage/sync/engine.py",
      "score": 0.93,
      "reason": "Exact symbol and keyword match for sync engine",
      "summary": "Coordinates startup scans, watcher events, git events, and scheduled sync jobs.",
      "chunks": [
        {
          "chunk_id": "chunk:...",
          "start_line": 25,
          "end_line": 110,
          "text_preview": "class SyncEngine: ...",
          "token_estimate": 850
        }
      ],
      "graph_neighbors": {
        "imports": ["watcher.py", "git_monitor.py", "manifest.py"],
        "contains": ["SyncEngine", "SyncEvent"]
      },
      "provenance": {
        "source_id": "syncsage-codebase",
        "git_branch": "main",
        "git_commit": "6f2a9c1",
        "indexed_at": "2026-05-15T20:05:00Z"
      }
    }
  ]
}
```

---

## 13. MCP Interface Specification

### 13.1 MCP Tools

#### `list_knowledge_bases`

Returns registered knowledge bases and status.

Input:

```json
{}
```

Output:

```json
{
  "knowledge_bases": [
    {
      "id": "local-syncsage",
      "name": "local-syncsage",
      "source_count": 3,
      "last_indexed_at": "2026-05-15T20:05:00Z",
      "status": "healthy"
    }
  ]
}
```

#### `register_source`

Registers a new source at runtime.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "source": {
    "name": "new-repo",
    "type": "repository",
    "path": "/workspace/new-repo",
    "sync": {
      "on_startup": true,
      "on_file_change": "debounce",
      "on_git_commit": true
    }
  }
}
```

Behavior:

- Validate path is under allowed workspace roots.
- Validate include/exclude patterns.
- Add to source registry.
- Optionally trigger initial sync.

#### `sync_source`

Triggers a sync for one source.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "source_name": "syncsage-codebase",
  "mode": "incremental"
}
```

Modes:

- `incremental`
- `full`
- `validate_only`
- `repair`

#### `sync_all`

Triggers sync for all enabled sources.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "mode": "incremental"
}
```

#### `search_context`

Searches context across graph and search store.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "query": "where does repository sync happen?",
  "mode": "hybrid",
  "max_results": 10,
  "include_chunks": true,
  "include_graph_neighbors": true
}
```

#### `get_relevant_files`

Returns likely files for a coding task.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "task": "Add git commit event detection to the sync engine",
  "source_name": "syncsage-codebase",
  "max_files": 8
}
```

#### `get_graph_neighbors`

Returns graph neighborhood around a node.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "node_id": "file:syncsage-codebase:src/syncsage/sync/engine.py",
  "depth": 2,
  "edge_types": ["imports", "defines_symbol", "contains"]
}
```

#### `get_file_summary`

Returns a compact summary of a file.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "path": "src/syncsage/sync/engine.py",
  "source_name": "syncsage-codebase"
}
```

#### `get_repo_map`

Returns repository structure, important modules, and dependency summary.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "source_name": "syncsage-codebase",
  "depth": 3
}
```

#### `explain_node`

Explains what a node represents and why it matters.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "node_id": "symbol:syncsage-codebase:src/syncsage/sync/engine.py:SyncEngine"
}
```

#### `export_obsidian_notes`

Writes/updates Obsidian Markdown notes for the selected knowledge base or source.

Input:

```json
{
  "knowledge_base": "local-syncsage",
  "source_name": "syncsage-codebase",
  "scope": "source"
}
```

#### `get_sync_status`

Returns current sync state, queue, errors, and last successful index.

Input:

```json
{
  "knowledge_base": "local-syncsage"
}
```

### 13.2 MCP Resources

Expose resources such as:

```text
syncsage://knowledge-bases
syncsage://knowledge-bases/{kb_id}/sources
syncsage://knowledge-bases/{kb_id}/graph
syncsage://knowledge-bases/{kb_id}/sources/{source_id}/manifest
syncsage://knowledge-bases/{kb_id}/sources/{source_id}/repo-map
syncsage://knowledge-bases/{kb_id}/nodes/{node_id}
```

### 13.3 MCP Prompts

Include reusable prompts:

#### `use_syncsage_for_coding_task`

Purpose: Instruct a coding agent to call SyncSage before editing.

```markdown
You are working in a repository indexed by SyncSage. Before making changes:

1. Call `get_relevant_files` with the user's task.
2. Inspect the returned files and chunks.
3. Make the smallest safe change.
4. After committing or saving changes, call `sync_source` with mode `incremental`.
5. Use `get_sync_status` to confirm the index is fresh.
6. Continue the next task using the updated index.
```

#### `use_syncsage_for_document_research`

```markdown
You are researching a document collection indexed by SyncSage. Use `search_context` first. Prefer chunks with explicit provenance, headings, and source paths. Do not summarize beyond retrieved evidence. Ask SyncSage for graph neighbors when a source references related material.
```

---

## 14. Admin API Specification

MCP is the primary agent interface, but an HTTP API is useful for health checks, local UI, and Kubernetes probes.

### 14.1 Health

```http
GET /health
GET /ready
GET /metrics
```

### 14.2 Sources

```http
GET /sources
POST /sources
GET /sources/{source_id}
PATCH /sources/{source_id}
DELETE /sources/{source_id}
```

### 14.3 Sync

```http
POST /sync
POST /sync/{source_id}
GET /sync/status
GET /sync/events
```

### 14.4 Search

```http
POST /search
POST /relevant-files
GET /files/{source_id}/{path:path}/summary
```

### 14.5 Graph

```http
GET /graph
GET /graph/nodes/{node_id}
GET /graph/nodes/{node_id}/neighbors
GET /graph/export/node-link-json
GET /graph/export/cytoscape-json
```

### 14.6 Obsidian

```http
POST /obsidian/export
GET /obsidian/status
```

---

## 15. Sync Methods

SyncSage should support multiple syncing methods. These should be composable and idempotent.

### 15.1 Startup Full Validation

On container cold start:

1. Load YAML config.
2. Validate all configured paths.
3. Load previous source manifests.
4. For each enabled source:
   - Check whether graph exists.
   - Check whether SQLite records exist.
   - Compare source state to manifest.
   - Re-index missing or changed artifacts.
5. Repair missing graph/search records if manifest indicates they should exist.
6. Start watchers and scheduler.

This protects reliability after crashes, container recreation, or volume migration.

### 15.2 File Watcher Events

Use file system events to detect:

- Created files
- Modified files
- Deleted files
- Moved files
- Directory changes

Required behavior:

- Debounce noisy events.
- Batch multiple rapid changes.
- Ignore excluded patterns.
- Avoid indexing temporary files.
- Re-index only changed artifacts.

### 15.3 Git Commit Trigger

For repositories, SyncSage should monitor `.git` state and commit changes locally.

Possible implementation options:

1. Watch `.git/HEAD` and relevant refs files.
2. Periodically compare current `HEAD` SHA to last indexed SHA.
3. Allow an agent to call `sync_source` after commit.
4. Optionally install a local post-commit hook in a future version.

Recommended v0.1 approach:

- Do not mutate repositories by installing hooks automatically.
- Use watcher + periodic git state check.
- Provide an optional documented hook users can install manually.
- Encourage agents to call `sync_source` after commits.

### 15.4 Agent-Initiated Sync

Agents should explicitly call SyncSage after meaningful changes:

```text
edit files -> run tests -> commit -> call sync_source(source, incremental)
```

This makes the feedback loop deterministic and avoids overreacting to every file save.

### 15.5 Scheduled Fallback Sync

A periodic sync catches missed events.

Default:

```yaml
sync:
  scheduler:
    enabled: true
    interval_seconds: 900
```

### 15.6 Manual CLI Sync

CLI examples:

```bash
syncsage sync --all
syncsage sync --source syncsage-codebase --mode incremental
syncsage sync --source syncsage-codebase --mode full
syncsage validate
syncsage repair
```

---

## 16. Idempotency and Reliability Requirements

### 16.1 Idempotency Rules

Every index operation must be safe to run repeatedly.

For each artifact:

1. Compute stable artifact ID.
2. Compute content hash.
3. Compare against manifest.
4. If unchanged, skip parsing.
5. If changed, remove or supersede old chunk/symbol nodes.
6. Upsert new nodes and edges.
7. Update search records in a transaction.
8. Update manifest only after successful graph and search write.

### 16.2 Transaction Boundaries

Use transactions for:

- Source manifest updates
- Search index updates
- File/chunk/symbol metadata updates
- Sync event logs

Graph JSON updates should use atomic writes:

1. Write to temporary file.
2. Flush and fsync if feasible.
3. Rename temp file to target.

### 16.3 Locking

Use source-level locks:

```text
/state/locks/<source_id>.lock
```

Rules:

- Only one sync per source at a time.
- Different sources can sync concurrently.
- Lock timeout prevents deadlocks.
- On startup, stale locks are detected and cleared.

### 16.4 Error Handling

Errors should be recorded but should not crash the entire server unless configuration is invalid.

Error categories:

- `CONFIG_ERROR`
- `PATH_NOT_FOUND`
- `PERMISSION_DENIED`
- `PARSER_ERROR`
- `GIT_ERROR`
- `GRAPH_WRITE_ERROR`
- `SEARCH_WRITE_ERROR`
- `OBSIDIAN_EXPORT_ERROR`

### 16.5 Recovery

Provide:

```bash
syncsage validate
syncsage repair
syncsage rebuild --source <source>
syncsage rebuild --all
```

Repair should:

- Rebuild missing graph nodes from SQLite and manifests where possible.
- Rebuild search index from graph/chunks.
- Rebuild Obsidian notes from graph.
- Fall back to full re-index if state is inconsistent.

---

## 17. Branching and Multi-Agent Repository Safety

### 17.1 Risks

Multi-agent coding can break or confuse sync if:

- Agents edit the same files concurrently.
- Branch switches occur while indexing is running.
- Rebase/merge changes rewrite commits quickly.
- The watcher indexes half-written files.
- Multiple SyncSage instances point to the same state volume.
- A repository is deleted or moved while watchers are active.

### 17.2 Required Protections

1. **Branch context in artifact identity**  
   Store branch and commit metadata with repository artifacts.

2. **Repository sync lock**  
   Only one indexing operation per repository at a time.

3. **Debounced indexing**  
   Wait for file changes to settle.

4. **Commit-aware refresh**  
   Prefer indexing after commit events or explicit agent sync calls.

5. **Working tree state tracking**  
   Record whether indexed content came from committed state or working tree state.

6. **Conflict detection**  
   If branch or commit changes during indexing, mark the sync stale and retry.

7. **No shared write volume across independent instances**  
   Multiple SyncSage containers should not write to the same `/state` unless an explicit coordination mode exists.

### 17.3 Recommended Agent Workflow

```text
1. Agent calls get_relevant_files.
2. Agent edits files.
3. Agent runs tests/checks.
4. Agent commits changes or records completed write action.
5. Agent calls sync_source(mode=incremental).
6. Agent checks get_sync_status.
7. Next task uses updated graph/search index.
```

---

## 18. Obsidian Integration

### 18.1 Role of Obsidian

Obsidian should act as:

- User-facing knowledge graph layer.
- Human-readable note surface.
- Markdown-based audit and navigation layer.
- Optional prompt/instruction authoring environment.

Obsidian should not be required for MCP functionality. SyncSage should run without Obsidian enabled.

### 18.2 Obsidian Output Format

Each generated note should include frontmatter:

```markdown
---
syncsage: true
node_id: file:syncsage-codebase:src/syncsage/sync/engine.py
source_id: syncsage-codebase
source_type: repository
relative_path: src/syncsage/sync/engine.py
content_hash: sha256:abc123
last_indexed_at: 2026-05-15T20:05:00Z
git_branch: main
git_commit: 6f2a9c1
tags:
  - syncsage
  - syncsage/repository
---

# sync/engine.py

## Summary

Coordinates startup scans, watcher events, git events, and scheduled sync jobs.

## Relationships

- Source: [[syncsage-codebase]]
- Imports: [[watcher.py]], [[git_monitor.py]], [[manifest.py]]
- Defines: `SyncEngine`, `SyncEvent`

## Retrieval Notes

This file is often relevant for tasks involving indexing, re-indexing, event handling, idempotency, and source synchronization.
```

### 18.3 Obsidian Note Types

| Note Type | Default? | Purpose |
|---|---|---|
| Knowledge base index | Yes | Entry point into generated knowledge. |
| Source note | Yes | Summary of each configured source. |
| Repository map | Yes | Directory and module overview. |
| File note | Yes | Summary and relationships for key files. |
| Chunk note | No | Too noisy by default; optional. |
| Topic note | Optional | Inferred or configured topical clusters. |
| Query note | Optional | Saved search results and agent workflows. |
| Canvas graph | Optional | Visual graph representation. |

### 18.4 Git Friendliness

To avoid making Obsidian too large for Git:

- Do not export every chunk as a note by default.
- Keep file notes concise.
- Store bulky graph state in `/state`, not `/vault`.
- Use stable note names so updates are diffs, not new files.
- Add generated folders to Git selectively.
- Consider `.gitignore` for generated graph exports if they become large.

---

## 19. Visualization

### 19.1 Graph JSON Export

SyncSage should export graph data in:

- NetworkX node-link JSON
- Cytoscape JSON
- Optional D3 force-directed JSON

Endpoint:

```http
GET /graph/export/cytoscape-json?source_id=syncsage-codebase&depth=2
```

### 19.2 UI Requirements

A minimal local UI should support:

- List knowledge bases.
- List sources.
- Show source sync health.
- Search context.
- Render force-directed graph.
- Filter by node type.
- Filter by edge type.
- Click node to view metadata.
- Open file path or Obsidian note reference.

Recommended first UI stack:

- FastAPI serves static assets.
- Simple HTML + Cytoscape.js.
- No full React app required for v0.1.

---

## 20. Deployment Specification

### 20.1 Deployment Method 1: Single Local Docker Container

Use case:

- Individual laptop.
- Local agent workflows.
- Simple one-line startup.
- One SyncSage instance managing one or more local source folders.

Command:

```bash
docker run --rm \
  --name syncsage \
  -p 8765:8765 \
  -v "$PWD/syncsage.yaml:/config/syncsage.yaml:ro" \
  -v "$HOME/projects:/workspace" \
  -v "$HOME/SyncSageVault:/vault" \
  -v syncsage-state:/state \
  ghcr.io/<org>/syncsage:latest
```

### 20.2 Deployment Method 2: Docker Compose

Use case:

- Slightly more repeatable local setup.
- Optional separate vector store or UI service later.

```yaml
services:
  syncsage:
    image: ghcr.io/<org>/syncsage:latest
    container_name: syncsage
    ports:
      - "8765:8765"
    volumes:
      - ./syncsage.yaml:/config/syncsage.yaml:ro
      - ~/projects:/workspace
      - ~/SyncSageVault:/vault
      - syncsage-state:/state
    environment:
      - SYNCSAGE_CONFIG=/config/syncsage.yaml

volumes:
  syncsage-state:
```

### 20.3 Deployment Method 3: Local Kubernetes via Docker Desktop

Use case:

- Simulate enterprise deployments locally.
- Multiple isolated SyncSage instances.
- One namespace per SyncSage instance.

Pattern:

```text
namespace: syncsage-project-a
  deployment: syncsage
  configmap: syncsage-config
  pvc: syncsage-state
  service: syncsage

namespace: syncsage-project-b
  deployment: syncsage
  configmap: syncsage-config
  pvc: syncsage-state
  service: syncsage
```

### 20.4 Deployment Method 4: Enterprise Kubernetes Namespace

Use case:

- Enterprise deployment.
- One SyncSage instance per team/project/namespace.
- Persistent volume claim for state.
- ConfigMap or mounted Secret for config.
- Optional ingress for UI/API.

Required Kubernetes resources:

- Namespace
- ConfigMap
- PersistentVolumeClaim
- Deployment
- Service
- Optional Ingress
- Optional NetworkPolicy
- Optional ServiceAccount/RBAC
- Optional ResourceQuota

### 20.5 Kubernetes Manifest Sketch

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: syncsage-project-a
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: syncsage-config
  namespace: syncsage-project-a
data:
  syncsage.yaml: |
    syncsage:
      name: project-a
      state_path: /state
      vault_path: /vault
      workspace_root: /workspace
    sources: []
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: syncsage-state
  namespace: syncsage-project-a
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: syncsage
  namespace: syncsage-project-a
spec:
  replicas: 1
  selector:
    matchLabels:
      app: syncsage
  template:
    metadata:
      labels:
        app: syncsage
    spec:
      containers:
        - name: syncsage
          image: ghcr.io/<org>/syncsage:latest
          ports:
            - containerPort: 8765
          env:
            - name: SYNCSAGE_CONFIG
              value: /config/syncsage.yaml
          volumeMounts:
            - name: config
              mountPath: /config
              readOnly: true
            - name: state
              mountPath: /state
          readinessProbe:
            httpGet:
              path: /ready
              port: 8765
          livenessProbe:
            httpGet:
              path: /health
              port: 8765
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
            limits:
              cpu: "2"
              memory: "4Gi"
      volumes:
        - name: config
          configMap:
            name: syncsage-config
        - name: state
          persistentVolumeClaim:
            claimName: syncsage-state
```

---

## 21. Docker Image Specification

### 21.1 Dockerfile Sketch

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SYNCSAGE_CONFIG=/config/syncsage.yaml

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip && \
    pip install .

RUN mkdir -p /config /state /workspace /vault /exports

EXPOSE 8765

CMD ["syncsage", "serve", "--config", "/config/syncsage.yaml"]
```

### 21.2 Image Requirements

- Runs as non-root where possible.
- Uses read-only config mount.
- Writes only to `/state`, `/vault`, `/exports`, and configured workspace paths if explicitly allowed.
- Includes `git` CLI for repository inspection.
- Avoids bundling Obsidian desktop app by default.

---

## 22. Compatibility Requirements

### 22.1 Local OS Compatibility

Target:

- macOS with Docker Desktop
- Windows with Docker Desktop + WSL2 backend
- Linux with Docker Engine

Important note:

- File watcher behavior differs by host OS and Docker mount type. Scheduled fallback sync is required for reliability.

### 22.2 Python Compatibility

Recommended:

- Python 3.11+
- Prefer Python 3.12 for initial Docker image.

### 22.3 Kubernetes Compatibility

Target:

- Docker Desktop Kubernetes
- Kubernetes 1.29+
- Enterprise clusters with standard PVC support

### 22.4 Obsidian Compatibility

SyncSage should write standard Markdown files with YAML frontmatter and wikilinks. Users can open the mounted vault in Obsidian desktop. SyncSage should not require an Obsidian plugin for v0.1.

---

## 23. Security Model

### 23.1 Threats

- Prompt injection inside indexed documents.
- Path traversal through source registration.
- Accidental indexing of secrets.
- MCP tool misuse by agents.
- Unsafe execution of repository code.
- Exposing local file paths through remote API.
- Overbroad host volume mounts.

### 23.2 Required Controls

1. **Path allowlisting**  
   Only index paths under configured workspace roots.

2. **Default excludes**  
   Exclude `.git`, `.env`, secrets, private keys, node_modules, binary build outputs, caches.

3. **No arbitrary command execution from indexed content**  
   Parsers inspect files; they do not run project code.

4. **Safe MCP tools**  
   Initial MCP tools retrieve, sync, and export. They should not run shell commands.

5. **Read-only source mode option**  
   For enterprise, allow all watched sources to be read-only.

6. **Secret scanning hooks**  
   Add optional lightweight detection for high-risk files or patterns.

7. **API binding defaults**  
   For local use, bind to localhost unless container networking requires otherwise. For Kubernetes, secure with network policies/ingress.

### 23.3 Default Exclude Patterns

```yaml
exclude:
  - "**/.git/**"
  - "**/.env"
  - "**/.env.*"
  - "**/*id_rsa*"
  - "**/*id_ed25519*"
  - "**/*.pem"
  - "**/*.key"
  - "**/node_modules/**"
  - "**/__pycache__/**"
  - "**/.venv/**"
  - "**/venv/**"
  - "**/dist/**"
  - "**/build/**"
  - "**/.mypy_cache/**"
  - "**/.pytest_cache/**"
```

---

## 24. Performance and Storage Targets

### 24.1 Target Scale

Initial target:

- Up to 100 watched paths.
- Up to 10 GB persistent state per instance before pruning/compaction warnings.
- Up to low hundreds of thousands of graph nodes per instance.
- Single container per knowledge base or project domain.

### 24.2 Storage Controls

- Hash and skip unchanged files.
- Store summaries and metadata in Obsidian, not every chunk.
- Compress graph snapshots.
- Keep only latest graph plus limited snapshots.
- Exclude large binaries by default.
- Record size metrics per source.
- Warn when state exceeds configurable thresholds.

### 24.3 Performance Controls

- Batch watcher events.
- Use incremental parsing.
- Use source-level locks.
- Use SQLite indexes.
- Avoid recomputing embeddings unless content hash changed.
- Avoid full graph rewrite after every small change if possible; snapshot periodically.

---

## 25. Implementation Phases

### Phase 0: Project Bootstrap

Deliverables:

- Repository scaffold.
- `pyproject.toml`.
- Basic CLI.
- Basic Dockerfile.
- Example config.
- Development README.
- Unit test setup.

Acceptance criteria:

- `syncsage --help` works.
- `syncsage validate --config syncsage.example.yaml` works.
- Docker image builds.

### Phase 1: Config, Registry, and Persistence

Deliverables:

- Pydantic config schema.
- Source registry.
- SQLite schema and migrations.
- State path manager.
- Manifest read/write.

Acceptance criteria:

- Config loads and validates.
- Sources are persisted.
- Manifests are written to `/state`.

### Phase 2: Ingestion Pipeline

Deliverables:

- File discovery with include/exclude.
- Markdown parser.
- Text parser.
- PDF parser.
- DOCX parser.
- Basic repository parser.
- Chunker.

Acceptance criteria:

- Sample repo and docs generate artifacts and chunks.
- Excluded files are skipped.
- Parser errors are recorded but do not stop full sync.

### Phase 3: Graph Builder

Deliverables:

- NetworkX graph model.
- Node/edge upsert.
- Stable IDs.
- Graph serializer.
- Graph export endpoint.

Acceptance criteria:

- Full sync produces graph JSON.
- Re-running sync does not duplicate nodes.
- File changes update relevant nodes/chunks.

### Phase 4: Search Layer

Deliverables:

- SQLite FTS tables.
- Path search.
- Hybrid ranker.
- Retrieval contracts.

Acceptance criteria:

- Query returns relevant files/chunks.
- Results include path, source, line/chunk provenance.
- Retrieval is usable by coding agents.

### Phase 5: MCP Server

Deliverables:

- MCP tools.
- MCP resources.
- MCP prompts.
- Local MCP client test.

Acceptance criteria:

- Agent can list knowledge bases.
- Agent can search context.
- Agent can trigger sync.
- Agent can retrieve graph neighbors.

### Phase 6: Sync Engine and Watchers

Deliverables:

- Startup validation.
- Watchdog service.
- Git monitor.
- Scheduled fallback sync.
- Debounce and event queue.
- Source locks.

Acceptance criteria:

- File change triggers incremental update.
- Commit SHA change triggers repository validation/re-index.
- Cold start repairs missing state.
- Rapid changes do not create duplicate records.

### Phase 7: Obsidian Export

Deliverables:

- Markdown note exporter.
- Frontmatter writer.
- Backlink generator.
- Source and repository map notes.
- Optional Canvas export.

Acceptance criteria:

- Obsidian vault shows generated SyncSage notes.
- Re-export updates notes instead of creating duplicates.
- Vault stays Git-friendly.

### Phase 8: Visualization UI

Deliverables:

- Graph JSON endpoint.
- Simple browser UI.
- Force-directed graph rendering.
- Filters and node detail panel.

Acceptance criteria:

- User can open local UI and inspect graph.
- User can filter by source and node type.

### Phase 9: Docker and Kubernetes Deployment

Deliverables:

- Production Dockerfile.
- Docker Compose file.
- Kubernetes manifests.
- Helm chart skeleton.
- Health/readiness probes.

Acceptance criteria:

- Runs locally with one command.
- Runs in Docker Desktop Kubernetes.
- Runs in isolated namespace with PVC.

### Phase 10: Hardening and Documentation

Deliverables:

- Security defaults.
- Troubleshooting guide.
- Metrics endpoint.
- Storage warning thresholds.
- End-to-end tests.
- Open-source README.

Acceptance criteria:

- New user can start from README.
- Agentic coding workflow can use the MCP server reliably.
- Known limitations are documented.

---

## 26. Database Sketch

### 26.1 Core Tables

```sql
CREATE TABLE knowledge_bases (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  config_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  knowledge_base_id TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  path TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  config_json TEXT NOT NULL,
  last_indexed_at TEXT,
  last_status TEXT,
  FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)
);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  type TEXT NOT NULL,
  path TEXT NOT NULL,
  relative_path TEXT,
  mime_type TEXT,
  size_bytes INTEGER,
  sha256 TEXT,
  mtime TEXT,
  git_branch TEXT,
  git_commit TEXT,
  last_indexed_at TEXT,
  status TEXT,
  FOREIGN KEY (source_id) REFERENCES sources(id)
);

CREATE TABLE chunks (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  heading_path TEXT,
  start_line INTEGER,
  end_line INTEGER,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  summary TEXT,
  token_estimate INTEGER,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
  FOREIGN KEY (source_id) REFERENCES sources(id)
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  chunk_id UNINDEXED,
  source_id UNINDEXED,
  artifact_id UNINDEXED,
  title,
  path,
  heading_path,
  text
);

CREATE TABLE symbols (
  id TEXT PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  language TEXT,
  symbol_type TEXT,
  name TEXT,
  qualified_name TEXT,
  start_line INTEGER,
  end_line INTEGER,
  signature TEXT,
  docstring_summary TEXT,
  FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE sync_events (
  id TEXT PRIMARY KEY,
  source_id TEXT,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  details_json TEXT,
  error_json TEXT
);
```

---

## 27. Agentic Build Configuration

This section is intended to guide a coding agent or multi-agent development workflow.

### 27.1 Global Agent Instructions

Create `agent/build_agent_config.yaml`:

```yaml
project: SyncSage
mission: Build a lightweight Docker-first MCP server for graph-based local knowledge indexing and agentic retrieval.

principles:
  - Keep the first version simple and local-first.
  - Prefer deterministic indexing over LLM-dependent indexing.
  - Use stable IDs and content hashes for idempotency.
  - Do not execute code from indexed repositories.
  - Keep Obsidian optional and Markdown-compatible.
  - Every change must preserve Docker-first operation.
  - All MCP tools must return provenance.

coding_standards:
  language: Python
  python_version: "3.11+"
  typing: strict where practical
  formatting: ruff_format
  linting: ruff
  tests: pytest

architecture_constraints:
  - FastAPI may be used for admin API and UI backend.
  - NetworkX is the graph engine for v0.1.
  - SQLite is the default durable metadata/search store.
  - Watchdog is the default file watcher.
  - Obsidian is a vault format integration, not a required runtime dependency.
  - Docker image must remain the main packaging unit.

required_acceptance:
  - docker build succeeds
  - syncsage validate works
  - syncsage serve starts API and MCP server
  - sample repository can be indexed
  - search_context returns relevant chunks with provenance
  - repeated sync is idempotent
  - file changes trigger incremental sync
  - Obsidian export writes stable Markdown notes
```

### 27.2 Agent Roles

#### Product Architect Agent

Responsibilities:

- Maintain scope and product intent.
- Protect lightweight design.
- Update architecture docs.
- Review public API and MCP contracts.

Prompt:

```markdown
You are the Product Architect for SyncSage. Your job is to preserve the lightweight MCP + graph indexing architecture. Review changes for scope creep, unclear abstractions, missing provenance, and violations of Docker-first deployment. Prefer simple local-first choices unless the spec explicitly requires enterprise scale.
```

#### Backend Engineer Agent

Responsibilities:

- Implement config loader.
- Implement FastAPI app.
- Implement CLI.
- Implement persistence services.

Prompt:

```markdown
You are the Backend Engineer for SyncSage. Implement typed Python modules with clear boundaries. Use Pydantic for contracts, SQLite for persistence, and FastAPI for admin APIs. Do not mix parsing, persistence, and API logic in the same module. Add tests for every public service method.
```

#### MCP Engineer Agent

Responsibilities:

- Implement MCP tools/resources/prompts.
- Ensure tool outputs are compact and provenance-rich.
- Test with MCP-compatible clients.

Prompt:

```markdown
You are the MCP Engineer for SyncSage. Expose SyncSage through MCP tools, resources, and prompts. Every tool response must include source IDs, paths, timestamps, and confidence/reason fields where relevant. Avoid tools that execute arbitrary shell commands. Retrieval tools should minimize tokens by returning concise summaries and targeted chunks.
```

#### Graph Engineer Agent

Responsibilities:

- Implement NetworkX graph model.
- Define stable IDs.
- Build graph upsert/diff/serialization.
- Implement graph traversal and export.

Prompt:

```markdown
You are the Graph Engineer for SyncSage. Build a deterministic graph model using NetworkX. Use stable node IDs and typed edge relationships. Repeated indexing must not duplicate nodes or edges. Provide graph exports for node-link JSON and Cytoscape JSON. Keep graph algorithms simple and explainable.
```

#### Ingestion Engineer Agent

Responsibilities:

- Implement source discovery.
- Implement parsers.
- Implement chunking.
- Implement code/document metadata extraction.

Prompt:

```markdown
You are the Ingestion Engineer for SyncSage. Build deterministic parsers for repositories, Markdown, PDFs, DOCX, HTML, XML, and text. Respect include/exclude patterns. Never execute indexed source code. Parser failures should be recorded and isolated so one bad file does not stop an entire source sync.
```

#### Sync/Reliability Agent

Responsibilities:

- Implement file watchers.
- Implement git monitor.
- Implement event queue, debounce, locks.
- Implement cold start validation and repair.

Prompt:

```markdown
You are the Sync/Reliability Engineer for SyncSage. Your job is to make indexing reliable under file changes, git commits, branch switches, cold starts, and crashes. All sync operations must be idempotent. Use source-level locks, event debouncing, startup validation, and transactional state updates.
```

#### Obsidian Integration Agent

Responsibilities:

- Implement Markdown note export.
- Implement frontmatter and backlinks.
- Keep vault output clean.

Prompt:

```markdown
You are the Obsidian Integration Engineer for SyncSage. Treat Obsidian as a Markdown vault and UX layer. Write stable, concise notes with YAML frontmatter, backlinks, and tags. Do not export every chunk by default. Generated notes must update in place and remain Git-friendly.
```

#### DevOps Agent

Responsibilities:

- Dockerfile.
- Compose file.
- Kubernetes manifests.
- Helm chart.
- Health probes.

Prompt:

```markdown
You are the DevOps Engineer for SyncSage. Make SyncSage easy to run with one Docker command, Docker Compose, Docker Desktop Kubernetes, and enterprise Kubernetes. Use persistent volumes for state. Keep the image portable and secure. Include health/readiness probes and resource limits.
```

#### QA Agent

Responsibilities:

- Unit tests.
- Integration tests.
- Fixture repositories.
- Idempotency and sync stress tests.

Prompt:

```markdown
You are the QA Engineer for SyncSage. Test idempotency, incremental sync, parser failure isolation, file watcher debounce, git branch changes, Obsidian export stability, and MCP tool contracts. Build fixtures that simulate real repositories and document folders.
```

#### Security Reviewer Agent

Responsibilities:

- Review path handling.
- Review prompt injection risks.
- Review secret exposure.
- Review container security.

Prompt:

```markdown
You are the Security Reviewer for SyncSage. Ensure SyncSage does not execute indexed code, does not follow unsafe paths outside allowed roots, excludes secrets by default, and does not expose sensitive local paths unnecessarily. MCP tools must be safe and scoped.
```

---

## 28. Agentic Development Workflow

Recommended build sequence for a coding agent:

```text
1. Read this specification.
2. Create repository scaffold.
3. Implement config schema and validation.
4. Implement persistence and manifests.
5. Implement basic ingestion for Markdown and text.
6. Implement graph upsert and serialization.
7. Implement SQLite FTS search.
8. Implement FastAPI health/search/sync endpoints.
9. Implement MCP tools.
10. Implement repository parser and Git metadata.
11. Implement watcher/sync engine.
12. Implement Obsidian export.
13. Add Dockerfile and compose file.
14. Add Kubernetes manifests.
15. Add tests and documentation.
```

After every substantial repository change, the build agent should:

```text
1. Run tests.
2. Commit changes.
3. Call SyncSage sync_source if SyncSage is already running.
4. Use SyncSage get_relevant_files for the next task.
```

---

## 29. Open-Source Positioning

### 29.1 Name

Project name: **SyncSage**

Meaning:

- **Sync**: keeps repositories, documents, vaults, and indexes up to date.
- **Sage**: gives agents and users wise, context-aware guidance.

### 29.2 Suggested Tagline

> SyncSage is a lightweight MCP knowledge graph server that keeps local repositories, documents, and Obsidian vaults indexed for agentic workflows.

### 29.3 Suggested GitHub Description

> Docker-first MCP server for lightweight graph indexing, watched sync, Obsidian-compatible knowledge projection, and low-token agentic retrieval over local repositories and documents.

### 29.4 Suggested License

Consider Apache-2.0 if the goal is permissive open source with explicit patent grant language. Confirm license choice with legal counsel if patent strategy matters.

---

## 30. Minimum Viable Product Definition

The v0.1 MVP is complete when:

1. A user can run SyncSage with one Docker command.
2. SyncSage can load a YAML config.
3. SyncSage can index at least:
   - One local repository.
   - One Markdown folder.
   - One document folder with PDF or DOCX.
4. SyncSage creates a persistent graph and search index.
5. SyncSage exposes MCP tools for search and sync.
6. SyncSage supports idempotent re-indexing.
7. SyncSage detects file changes with debounce.
8. SyncSage can detect Git commit/branch state changes.
9. SyncSage writes useful Obsidian-compatible notes.
10. SyncSage has Docker and Kubernetes deployment examples.

---

## 31. Key Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Watcher events are unreliable across Docker mounts | Scheduled fallback sync and startup validation. |
| Graph grows too large | Export summaries to Obsidian, compress snapshots, avoid chunk notes by default. |
| Multi-agent edits create stale indexes | Source locks, branch/commit metadata, explicit agent sync after commits. |
| Obsidian vault becomes noisy | Export source/file summaries by default, chunks optional. |
| MCP tool exposes too much context | Return concise ranked results with token estimates. |
| Secrets get indexed | Default excludes, optional secret scanner, path allowlisting. |
| Branch switches during indexing | Detect branch/HEAD changes and retry or mark stale. |
| Heavy parsing slows container | Incremental indexing, include/exclude filters, parser timeouts. |
| Enterprise deployments need isolation | One namespace/PVC per SyncSage instance. |

---

## 32. Initial Backlog

### Epic 1: Foundation

- Create Python package.
- Create CLI.
- Create config schema.
- Create state paths.
- Create Dockerfile.

### Epic 2: Indexing

- Implement file discovery.
- Implement Markdown parser.
- Implement text parser.
- Implement PDF parser.
- Implement DOCX parser.
- Implement repository metadata parser.

### Epic 3: Graph

- Implement node/edge model.
- Implement stable IDs.
- Implement graph upsert.
- Implement graph serialization.
- Implement graph JSON export.

### Epic 4: Search

- Implement SQLite schema.
- Implement FTS indexing.
- Implement hybrid ranker.
- Implement search API.

### Epic 5: MCP

- Implement MCP server.
- Implement core tools.
- Implement resources.
- Implement prompts.

### Epic 6: Sync

- Implement startup validation.
- Implement file watcher.
- Implement git monitor.
- Implement scheduled sync.
- Implement locks and debounce.

### Epic 7: Obsidian

- Implement note templates.
- Implement frontmatter.
- Implement backlinks.
- Implement source/repo notes.
- Implement optional canvas export.

### Epic 8: Deployment

- Docker Compose.
- Kubernetes manifests.
- Helm skeleton.
- Health/readiness probes.

### Epic 9: Quality

- Unit tests.
- Integration tests.
- E2E Docker test.
- Idempotency tests.
- Watcher tests.

---

## 33. Build Acceptance Tests

### 33.1 Config Test

```text
Given syncsage.example.yaml
When syncsage validate is run
Then config loads successfully and all paths are validated or clearly reported
```

### 33.2 Idempotent Sync Test

```text
Given a sample repository
When sync_source(full) is run twice
Then node count, edge count, artifact count, and chunk count remain stable
```

### 33.3 Incremental File Change Test

```text
Given an indexed Markdown file
When the file content changes
Then only that artifact and related chunks are updated
```

### 33.4 Git Commit Test

```text
Given an indexed git repository
When a file is changed and committed
Then SyncSage detects the new commit SHA and updates repository metadata
```

### 33.5 Search Test

```text
Given an indexed source with a sync engine file
When search_context("sync engine") is called
Then the relevant file and chunks are returned with provenance
```

### 33.6 Obsidian Export Test

```text
Given an indexed source
When export_obsidian_notes is called twice
Then generated notes update in place and do not duplicate
```

### 33.7 Docker Test

```text
Given the Docker image
When it starts with mounted config/state/workspace/vault
Then /health and /ready pass and MCP tools are available
```

---

## 34. Critical Implementation Notes

1. Do not make Obsidian a hard runtime dependency.
2. Do not index every file type by default.
3. Do not create one Markdown note per chunk by default.
4. Do not install git hooks automatically in v0.1.
5. Do not share one writable `/state` volume across multiple independent containers.
6. Do not let agents register arbitrary host paths outside configured roots.
7. Do not execute indexed repository code.
8. Do include source provenance in every retrieval response.
9. Do make every sync operation idempotent.
10. Do make cold start validation a first-class reliability feature.

---

## 35. Future Enhancements

- Optional semantic embeddings.
- Optional Qdrant, Chroma, or LanceDB vector backend.
- Language-server-based code intelligence.
- Tree-sitter symbol extraction for more languages.
- Agent action audit graph.
- Obsidian plugin for live SyncSage status.
- GitHub app integration.
- Remote source connectors.
- Cross-instance federation.
- Namespace-level enterprise policy controls.
- Web UI with richer graph exploration.
- RBAC and multi-user authentication.
- Incremental graph diff streaming.

---

## 36. Final Build Instruction to Agentic Workflow

Build SyncSage as a small, reliable, Docker-first MCP knowledge graph server. Start with deterministic local indexing, durable graph/search state, and clean MCP retrieval tools. Keep Obsidian as a Markdown-compatible projection layer, not the source of operational truth. Prioritize idempotency, provenance, cold-start recovery, and simple deployment. Only add semantic/vector complexity after the basic graph + SQLite FTS system works end to end.

The successful first version should feel like this:

```text
Point SyncSage at my folders.
Run one container.
Let it index and watch.
Let my agents ask it where to look.
Let Obsidian show me what it knows.
Keep everything fresh after changes.
```

That is the core product.

---

## 37. Reference Notes Used for Technology Choices

- MCP Python SDK: official SDK supports building MCP servers exposing tools, resources, and prompts, with transports such as stdio and Streamable HTTP.
- NetworkX: Python package for creating, manipulating, and studying graph/network structures.
- Watchdog: Python library and utilities for monitoring file system events.
- FastAPI: Python API framework based on standard Python type hints.
- Docker volumes: durable data persistence mechanism for containers.
- Kubernetes namespaces and persistent volumes: deployment isolation and durable storage primitives.
- Obsidian vaults: folders of notes/subfolders suitable for Markdown-based knowledge projection.
- SQLite FTS5: built-in SQLite full-text search extension.
- Markdown-it-py, Beautiful Soup, PyMuPDF, and Tree-sitter: parsing options for Markdown, HTML/XML, PDFs, and source code.