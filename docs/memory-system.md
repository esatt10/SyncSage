# The memory system

How agent memory works in pheasant, end to end — what a memory *is*, how it
gets written, indexed, filtered, ranked and forgotten, and which invariants the
subsystem must not break.

This is the explanation. For the task-oriented version — enable it, write a
record, recall it — see [Agent memory](how-to/agent-memory.md). For the config
keys, see [Configuration](configuration.md#memory).

---

## 1. The load-bearing decision: a memory is source content

A memory record is **one append-only Markdown file** under a built-in
`type: memory` filesystem source. Frontmatter above, the remembered text below.

Everything else follows from that. Indexing a memory is the ordinary
deterministic chunk → embed → graph pipeline that indexes any other file, so:

- **recall is search** — there is no separate retrieval path to keep in sync,
  and a memory competes for a slot on the same ranking as a document;
- **no LLM ever touches the memory path**, because no LLM touches the indexing
  path (design pillar; see [CLAUDE.md](https://github.com/esatt10/pheasant-kb/blob/main/CLAUDE.md) rule 1);
- **idempotency comes for free** — the engine's pre-read sha256 skip means
  re-syncing unchanged records is zero work;
- **the bytes are never destroyed.** "Forgetting" is a rename to
  `<id>.md.archived`, which stops matching the source's `**/*.md` include glob.

The write path only ever *creates files*. It never reaches into the index
directly.

```
memory_write / POST /memory
        │
        ▼
  MemoryStore.append ──────► <root>/<scope>/mem-<instant>-<digest>.md
        │                                    │
        │ (sync=true, the default)           │  ordinary filesystem source
        ▼                                    ▼
   incremental sync ────► chunks + FTS5 ─┬─► text arm
                     └──► vectors ───────┼─► vector arm
                     └──► graph nodes ───┴─► graph arm
                     └──► memory_records (projection)
                                │
                                ▼
                        MemoryPolicy / Steering
```

---

## 2. The modules

| Module | Owns |
|---|---|
| `memory/store.py` | The record file format, id derivation, append-only writes, archiving, consolidation |
| `memory/projection.py` | Deriving the `memory_records` SQLite rows from the files |
| `memory/policy.py` | `MemoryPolicy` — what one query is allowed to see, in two encodings |
| `memory/steering.py` | Turning `alias`/`preference`/`exclusion` records into ranking rules |
| `memory/salience.py` | The deterministic keep-worthiness formula |
| `memory/maintenance.py` | One consolidation + capacity pass, run by the scheduler or on demand |
| `memory/bridge.py` | `about` and `supersedes` edges into the knowledge graph |
| `memory/benchmark.py` | The offline LongMemEval-style recall benchmark |
| `security/acl.py` | Turning a record's scope into a read ACL |

---

## 3. Record identity

```
mem-<asserted-at>-<blake2b8(digest input)>
```

Identity is the **digest, not the timestamp**. Re-asserting the same thing
returns the record already stored, however much later it happens — an identical
write is reported `created: false` rather than duplicated.

The digest input is `scope|subject|text`, plus extra parts appended *only when
they differ from what schema v1 implied* (`kind`, `written_by`, `valid_from`,
`valid_until`). That is what makes **every v1 record id reproduce
byte-identically** under schema 2 — nothing on disk is ever rewritten.

`written_by` being in the digest is load-bearing for isolation: two principals
asserting the same sentence in the same second get two records rather than
silently sharing one file owned by whoever arrived first.

### Frontmatter is caller data and is validated as such

Every caller-supplied value that reaches the frontmatter (`subject`,
`supersedes`, `written_by`, each tag, `valid_from`, `valid_until`) is rejected
if it contains a line break, and the timestamps must parse as ISO-8601.

This is a security boundary, not tidiness. The block is line-oriented and
`load` reads `key: value` per line, so a newline in a value appends arbitrary
*keys* — and `memory_scope` is what `security/acl.normalize_acl` reads to build
the record's read ACL. Before this check, `subject="x\nmemory_scope: org"` turned
a private `user` note into a world-readable `org` one under
`security.acl_enforced`. `load` additionally takes the **first** occurrence of
each key, so a record written by an older release cannot be read at a forged
scope.

---

## 4. The validity model

A record is never edited and never deleted. It is **invalidated**, and there
are three independent ways that happens — the earliest wins:

| Mechanism | Set by | Meaning |
|---|---|---|
| supersession | a later record naming this one in `supersedes` | corrected at that record's `asserted_at` |
| declared expiry | `valid_until` on the write | it said so itself |
| scope TTL | `memory.{session,user,org}_ttl_days` | opt-in decay; `None` = never |

`valid_until` in the projection is **derived, never double-stored**:
`effective_valid_until` takes the minimum of the declared expiry and the
earliest correction. A record that declared it was good until December but got
corrected in March is stale from March.

**Validity is enforced at query time, not only by the batch pass.** That is the
stale-fact defect the agent-memory literature names as the primary one: the
region knows a fact was corrected and serves it anyway between consolidation
beats. `current_only` (on by default) closes it. `as_of` deliberately brings the
old record back — that is the whole point of invalidating rather than deleting.

---

## 5. Query-time policy

`MemoryPolicy` is one knob, spelled identically on MCP `search_context`,
`POST /search`, `POST /relevant-files` and `POST /assistant/chat`.

| Field | Default | Meaning |
|---|---|---|
| `mode` | `auto` | `auto` / `off` / `only` / `prefer` |
| `scopes` | all | restrict to `session` / `user` / `org` |
| `subject` | — | substring match on the record's subject |
| `current_only` | `true` | drop corrected records |
| `as_of` | — | point-in-time recall |
| `max_results` | — | cap how many slots memory may occupy |
| `include_rules` | `false` | return steering records as content too |

### One rule, two encodings

The same rule is written twice, deliberately, and pinned together by a
10-policy × 5-record parity test:

- **`sql_predicate`** — pushed into the text arm's SQL, ahead of `LIMIT`.
  Post-filtering a globally-ranked page would return nothing from a narrow
  slice while the matching rows sat just past the cut.
- **`admits`** — the Python half, applied to the vector and graph arms, which
  carry no memory columns of their own.

Because the vector and graph arms are filtered *after* their own truncation,
those arms must **over-fetch** whenever the policy could drop something.
That decision is `may_filter`, which asks the loaded index whether any record
would actually be dropped — not `is_default`, which answers the different
question of whether the *caller* asked for anything unusual. The default policy
is not inert: it drops corrected records and steering rules.

### Steering records are not results

`alias`/`preference`/`exclusion` records are retrieval *machinery*. By default
they steer ranking and are excluded from result lists.

Measured on the vscode corpus: with them included, the alias rule
`filewatch daemon -> fileService, watcher` took **rank 1** for "where is the
file service implemented", pushing the real `fileService.ts` to rank 5 and
dropping corpus MRR from 0.462 to 0.335. It also hands an agent rule syntax
dressed as retrieved knowledge. The rules stay fully in force and fully
inspectable via `describe_retrieval` and `GET /memory`.

### Trust containment

The answering prompt states that a remembered passage is a *recorded
assertion*, that corpus content wins a disagreement, and that passage text is
**data, never an instruction**. Remembered passages are labelled with their
scope and assertion time through both citation builders. This is the
memory-control-flow defence: memory is an input an agent wrote, so it must not
be able to redirect the agent that reads it.

---

## 6. Steering

A record whose payload is a deterministic retrieval rule rather than prose.
This is the half that changes queries returning **no memory at all**.

```
alias       router -> pheasant-flock, flock
preference  when: deploy, docker -> prefer: docs/, deploy/
exclusion   never: vendor/**
```

Parsing is regex over text an agent already wrote — no model, no network.
A malformed rule is **ignored, never raised**: these are read during a sync, and
one bad line must not fail indexing or a search.

Two things that are easy to get wrong and are pinned by tests:

- **Triggers are matched against the tokenized query**, split the same way
  `_query_tokens` splits, with *every* part required. Matching one token would
  let a rule about `pheasant-flock` fire on a query that merely said
  `pheasant`; splitting on too few separators leaves `ci/cd`, `fs.watch` and
  `api:gateway` silently dead while still reporting themselves in force.
- **Scope gating is a security property.** A rule applies only to a query whose
  policy admits its scope, so `session` steering stays in that session. `mode`
  is deliberately *not* applied — `memory: "off"` is a statement about results,
  and a caller who wants no remembered passages still wants the region's
  remembered vocabulary.

Rule text is reassembled from its chunks in `chunk_index` order, because SQLite
does not guarantee the order an aggregate sees its input and a rule long enough
to span two chunks could otherwise parse differently between runs.

Off by default (`memory.steering_enabled`): memory that silently re-orders
results is a surprise unless it was asked for.

---

## 7. Memory in the knowledge graph

Memory is not a graph island. Records become `memory_record` nodes, with:

- **`supersedes` edges** between records, so a correction is a traversable
  relationship rather than only a frontmatter string;
- **`about` edges** to what the record is *about*, drawn by a precedence
  ladder — **reference → symbol → heading → entity**, first rung wins, capped
  at `memory.about_max_targets`.

The ladder exists because `entity` cannot be the bridge on its own: entity
extraction runs only for `.md/.txt/.html/.xml`, so an entity-only bridge looks
fine on Markdown and is a silent no-op on PDFs or non-Python code.

The lexical/BM25 rung is deliberately **not** materialized — the search index
answers it at query time, and materializing it is how the retired concept layer
reached 98.6% of all edges.

Coverage is reported, not silent: `describe_retrieval` carries
`graph:{records, bridged, unbridged, by_signal}`, computed live.

---

## 8. Salience and bounded growth

The one thing memory learns from *being used*. A documented deterministic
formula over recorded inputs — no model, no sampling — so a pruning pass is
reproducible rather than a judgement call:

```
salience = recency(90-day half-life) × (1 + 0.5·log1p(uses)) × scope_weight
scope_weight: org 1.25, user 1.0, session 0.6
```

`uses` is counted **after truncation**, so a record is credited for being
*served*, not merely considered. Usage tracking is off by default — it is a
write on the read path.

`memory.max_records` archives the least salient beyond the cap, via the same
in-place rename consolidation uses, with the score written back so a prune is
explainable. Unbounded by default.

**Steering records are exempt from the cap in both directions** — they neither
consume slots nor get archived. Ranking a deliberate operator-written rule
against ordinary facts, on a formula built for facts, meant crossing
`max_records` could silently switch off an `exclusion` and change ranking for
every future query. The number of rules actually in force is bounded separately
by `steering.MAX_RULES`.

---

## 9. Isolation

Under `security.acl_enforced`, a record's ACL follows its **scope**:

| Scope | Readable by |
|---|---|
| `org` | everyone (region default visibility) |
| `user`, `session` | only the principal that wrote it |
| no recorded writer, not org | falls through to the region default |

The ACL is computed inside the artifact loop, before the projection for that
sync exists, so it is read from the record file. That is exactly why §3's
frontmatter validation matters: the file is the authority, so what reaches the
file must be trusted input.

---

## 10. Surfaces

| Surface | Write | Read | Maintain |
|---|---|---|---|
| MCP | `memory_write` | `search_context(memory=…)`, `describe_retrieval`, `pheasant://…/memory` | `memory_consolidate` |
| HTTP | `POST /memory` | `GET /memory`, `POST /search` | `POST /memory/consolidate`, `POST /memory/enable` |
| UI | `/memory` page | citation chips, `use memory` chat toggle | Settings → Memory panel |

The MCP tool surface is public API: additive evolution only, deprecate before
remove.

---

## 11. Invariants

Things this subsystem must keep true:

1. **Records are files.** No second ingestion path, no direct index writes.
2. **Nothing is ever deleted.** Archiving is a rename; corrections invalidate.
3. **v1 record ids reproduce byte-identically.** The digest may only grow parts
   for values v1 could not express.
4. **`sql_predicate` and `admits` agree exactly.** They are one rule in two
   encodings, and the parity test is what keeps them honest.
5. **Steering never narrows.** Alias expansion is additive; the caller's own
   tokens always survive.
6. **A malformed rule is ignored, never raised.**
7. **Passage text is data, never instruction.**
8. **The wire format is unchanged.** `memory` rides in `capabilities.modalities`,
   which is existing contract data — no schema bump, no re-vendor.

---

## 12. Measured

From `memory/benchmark.py` (deterministic, offline, through the real
`memory_write` → index → `search_context` path):

| Metric | Value |
|---|---|
| recall@5 | 1.000 |
| update accuracy | 1.000 |
| stale leak | 0.000 |
| abstention | 1.000 |
| bytes/record | 212.9 B |
| write latency | 4.51 ms |
| search latency | 11.83 ms |

On a real corpus (microsoft/vscode, partial index), through the real MCP stdio
surface: corpus MRR 0.462 → **0.495** with memory on (memory is not a tax),
memory-only recall 0.000 → **1.000**, and the config-level steering ablation
moved team-vocabulary queries **0.029 → 0.467** while control queries moved
**+0.000**.

---

## 13. Known limits

- **Bridge and vector behaviour at ~2,000-file scale with a real embedder is
  still unmeasured.**
- **`max_records` bounds recallable facts, not rules.** A store that
  accumulates thousands of steering records grows unbounded on disk; only the
  first `MAX_RULES` are ever in force.
- **`supersedes` is not authorization-checked.** Any writer may supersede any
  record whose id it knows. Injection can no longer *forge* the field, but the
  API accepts it as a legitimate parameter, and cross-principal correction is
  currently by design rather than by decision.
- **Taxonomy is not published on the Synapse contract** — the outline is
  region-local retrieval structure, not routing signal.
