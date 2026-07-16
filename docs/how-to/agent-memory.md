# Give your agents durable memory

A SyncSage region can be an **agent memory store** (Product Framework Step
33.1): agents write facts through MCP or HTTP, and those facts become
ordinary indexed knowledge — searchable with the same `search_context` /
`/search` surface as everything else, provenance included. No separate
memory database, no second retrieval path.

## 1. Configure a memory source

One block in `syncsage.yaml`:

```yaml
sources:
  - name: agent-memory
    type: memory
    path: memory        # a directory, anchored to workspace_root
```

A `memory` source is a filesystem source: the watcher, scheduler, include
globs, backups, and the Obsidian projection all treat it like any folder
of Markdown.

## 2. Write memories

From an MCP agent (Claude Code, Cursor, …) via the `memory_write` tool, or
over HTTP:

```bash
curl -s localhost:8765/memory -X POST -H 'content-type: application/json' -d '{
  "text": "The staging cluster lives in us-east-2.",
  "scope": "org",
  "subject": "infra",
  "tags": ["deploy", "aws"]
}'
```

Scopes are `session`, `user`, or `org`; `subject` identifies whose memory
it is (a session id, a user, a team). With `sync: true` (the default) the
record is indexed before the call returns — **read-your-writes**: the very
next `search_context` finds it.

## 3. Recall is just search

```bash
curl -s localhost:8765/search -X POST -H 'content-type: application/json' \
  -d '{"query": "where does staging run?", "mode": "hybrid"}'
```

Memory records rank alongside (and link into) the rest of the knowledge
graph. `GET /memory?scope=org` lists raw records for inspection.

## What a record looks like on disk

Append-only Markdown, one file per record, deterministic id — greppable,
diffable, Obsidian-friendly, and versioned:

```markdown
---
schema_version: 1
record_id: mem-20260716T120000Z-9f2ab31c74d05e88
memory_scope: org
memory_subject: infra
asserted_at: 2026-07-16T12:00:00Z
tags: deploy, aws
---

The staging cluster lives in us-east-2.
```

Writing the identical fact again is a no-op (`created: false`, no
re-index). Re-asserting a fact later creates a new record — recency is
signal. The optional `supersedes: <record_id>` field marks corrections;
validity-window handling and consolidation land in Step 33.2.

## Fleet routing

A region with a memory source advertises `memory` in its contract's
`capabilities.modalities`, so a Synapse router can direct remember/recall
traffic to memory-capable regions with the existing `--modality memory`
filter — no wire-format change. The router-side `synapse_remember` tool is
Step 33.3.
