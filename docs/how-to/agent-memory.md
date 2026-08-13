# Give your agents durable memory

A pheasant region can be an **agent memory store** (Product Framework Step
33.1): agents write facts through MCP or HTTP, and those facts become
ordinary indexed knowledge — searchable with the same `search_context` /
`/search` surface as everything else, provenance included. No separate
memory database, no second retrieval path.

> For how the whole subsystem fits together — the validity model, the two
> encodings of the query policy, the graph bridge, salience and the invariants
> — see [The memory system](../memory-system.md).

## 1. Configure a memory source

One block in `pheasant.yaml`:

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

**Field rules.** `subject`, `supersedes`, `written_by` and each tag must not
contain a line break, and `valid_from` / `valid_until` must be ISO-8601
instants; a write that breaks either rule is rejected with a `400`. The
frontmatter block is line-oriented and a record's `memory_scope` is what
decides its read ACL, so a newline in a field value would otherwise let a
caller forge frontmatter and escalate a private note to `org`. Everything else
— colons, unicode, punctuation — round-trips unchanged.

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
signal.

## Corrections, decay, and consolidation

Pass `supersedes: <record_id>` when a memory corrects an earlier one. The
old record is then no longer *current* — `GET /memory?current_only=true`
filters it immediately — and the next **consolidation pass** archives it:
the file is renamed `<id>.md.archived` in place (bytes preserved forever,
nothing deleted) and a full re-sync of the memory source drops it from the
index, so search stops surfacing the stale fact while the audit trail
remains on disk.

Consolidation runs automatically on the scheduler beat, or on demand via
the `memory_consolidate` MCP tool / `POST /memory/consolidate`. Per-scope
decay is opt-in:

```yaml
memory:
  consolidation_enabled: true   # default — supersedes chains get archived
  session_ttl_days: 14          # opt-in: scratch session memories decay
  user_ttl_days: null           # null = the scope never expires (default)
  org_ttl_days: null
```

## Fleet routing

A region with a memory source advertises `memory` in its contract's
`capabilities.modalities`, so a Synapse router directs remember/recall
traffic with the existing `--modality memory` filter — no wire-format
change. Router-side, the `synapse_remember` MCP tool (Step 33.3) routes a
write to the fleet's memory-capable region and recall stays ordinary
`synapse_search`.

## Controlling memory at query time

Every retrieval surface takes a `memory` argument — MCP `search_context`,
`POST /search`, `POST /relevant-files` and `POST /assistant/chat`. One word
covers the common cases:

```jsonc
{"query": "rollout password", "memory": "off"}    // no memory in the results
{"query": "rollout password", "memory": "only"}   // memory and nothing else
{"query": "rollout password", "memory": "prefer"} // memory keeps a share of the slots
```

or an object for the rest:

```json
{
  "query": "rollout password",
  "memory": {
    "scopes": ["user"],
    "subject": "deploy",
    "as_of": "2026-01-01T00:00:00Z",
    "current_only": false
  }
}
```

**A corrected record is never returned by default.** Supersession is enforced
at query time, so you do not have to wait for a consolidation pass to stop
seeing a fact the region already knows was replaced. `as_of` deliberately
brings it back — that is the point of invalidating rather than deleting, and it
is how you ask what was believed at a past instant.

Results that came from memory carry a `memory` block:

```json
{"record_id": "mem-…", "scope": "user", "subject": "deploy", "kind": "fact",
 "asserted_at": "2026-07-16T12:00:00Z"}
```

`describe_retrieval` reports the memory source's name, its scopes and record
counts, how many records are wired into the graph, and any steering in force —
so an agent never has to guess.

## Steering — memory that improves every query {#steering}

Set `memory.steering_enabled: true` and a record's `kind` becomes a retrieval
rule rather than an assertion to recall. This is the one part of memory that
changes queries returning **no memory at all**.

| kind | Write | Effect |
|---|---|---|
| `alias` | `router -> pheasant-flock, flock` | Expands the query, so asking about "the router" finds documents that only say "pheasant-flock" |
| `preference` | `when: deploy, docker -> prefer: docs/, deploy/` | Adds a path prior alongside the built-in depth/tests/samples ones |
| `exclusion` | `never: vendor/**` | Suppresses matching paths |

```bash
curl -X POST localhost:8765/memory -H 'content-type: application/json' \
  -d '{"text": "router -> pheasant-flock, flock", "kind": "alias", "scope": "org"}'
```

Paths are matched against a source-relative `relative_path`, so a source rooted
at `docs/` uses `vendor/`, not `docs/vendor/`.

**Scope decides reach, and that is a security property.** A rule applies only
where its scope does: `session` steering is confined to that session, `user` to
that principal, `org` fleet-wide. A corrected or expired rule steers nothing —
the same validity predicate governs steering and retrieval, so the two cannot
disagree. Note that `memory: "off"` suppresses memory *content* but still
honours steering: not wanting remembered passages is not the same as not
wanting the region's remembered vocabulary.

**Rules steer; they are not results.** A steering record is retrieval
machinery, so it is excluded from result lists by default — measured live, an
alias rule was taking rank 1 for the very query it was written to improve,
pushing the real answer down five places. It stays fully in force and fully
inspectable via `describe_retrieval` and `GET /memory`. Pass
`"memory": {"include_rules": true}` to see rules in results anyway.

**Triggers match the tokenized query.** Write them however reads naturally —
`filewatch daemon`, `pheasant-flock`, `ci/cd`, `fs.watch` all work — and every
part of the trigger must be present for the rule to fire, so a rule about
`pheasant-flock` does not fire on a query that merely said `pheasant`.

## Salience and bounded growth

With `memory.usage_tracking: true`, retrieval counts which records it actually
returned. That feeds a deterministic salience score — recency (90-day half
life) × use (with diminishing returns) × scope weight — which decides what goes
first when `memory.max_records` is set. Pruning archives with the same in-place
`.md.archived` rename consolidation uses: bytes are preserved, never deleted.

Both are off by default. Usage tracking is a write on the read path, and
bounded growth should be a decision, not a surprise.

`max_records` bounds **recallable facts**. Steering records are exempt in both
directions — they neither consume slots nor get archived — because ranking a
deliberate rule against ordinary facts, on a formula built for facts, meant
crossing the cap could silently switch off an `exclusion` and change ranking
for every future query.

## In the UI

The **Memory** tab lists what has been recorded, grouped by subject, with the
scope, when it was asserted and who wrote it. Corrections are made by
*superseding* — never by editing — so the history `as_of` reads stays intact;
the "Correct" button pre-fills a superseding write. Consolidation runs from the
same page.

In chat, a **use memory** switch on the composer sends the same `memory` field
MCP and the router send, and any passage that came from memory is chipped with
its scope so an answer never silently passes off a remembered assertion as a
document.

## Isolation

When `security.acl_enforced` is on, a record's ACL follows its scope: `org` is
shared, while `user` and `session` records are readable only by the principal
that wrote them (`memory_write(..., principal="user:alice")`). Without a
recorded writer nothing is asserted and the region default applies.
