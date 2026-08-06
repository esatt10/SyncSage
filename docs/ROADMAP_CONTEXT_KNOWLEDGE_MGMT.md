# Context & Knowledge Management Roadmap — Region-Side Companion

**Status:** companion document (2026-07-14). The **canonical roadmap** —
landscape survey, gap analysis, positioning, and the full Phase 30–36 plan —
lives in the sibling repo:
`pheasant-flock/docs/ROADMAP_CONTEXT_KNOWLEDGE_MGMT.md` (ADR 2026-07-14
in `…/docs/DECISIONS.md`). This file records only what lands **in this repo**
and the invariants each phase must respect here. Cross-repo rules are
unchanged: contract JSON + HTTP only, identical branch names, fixture parity,
both suites green.

---

## 1. Why pheasant is the load-bearing half

The 2026 landscape research (sourced in the canonical doc) converged on three
findings that all point at the region:

1. **Knowledge-base MCP is the largest unmet demand in the MCP ecosystem**
   (10,000+ public servers, no established KB answer). pheasant *is* a KB MCP
   server — the gap is packaging and distribution, not capability.
2. **Permission-aware retrieval is the #1 enterprise blocker** for GenAI
   rollouts, and the consensus is that enforcement must live in the retrieval
   layer, not the app layer. In a federated system, *the region is the
   retrieval layer* — ACL capture and filtering land here.
3. **Federated context with strong contracts** is the architecture analysts
   prescribe over central catalogs. Regions keep the data; only the ≤256 KB
   contract travels. Every enterprise-search competitor (Glean, Onyx) is
   structurally centralized and cannot cheaply retrofit this.

pheasant's design pillars — deterministic parsing (no LLM in the indexing
path), idempotent/incremental sync, local-first persistence split — are the
*differentiators* against LLM-graph rivals (GraphRAG/LightRAG), not
limitations. They stay inviolate through every phase below.

## 2. Region-side workstreams by phase

| Phase | Region-side work (this repo) | Key invariants to preserve |
|---|---|---|
| **30 — Packaging & first-run** | 30.1 `pheasant up` personal quickstart (vault/folder autodetect → config → index → UI, no YAML on the happy path); 30.3 published GHCR images [x-repo]; 30.5 "attach your KB to a coding agent in 5 min" MCP packaging + registry listings | Standalone mode is the product here — no router required anywhere in the personal path |
| **31 — Connector SDK + connectors** | 31.1 entry-point connector SDK (checkpoint API, manifest integration, idempotency harness — the four pillars enforced by contract); 31.2–31.6 Notion, Google Drive, Slack, Confluence/Jira, IMAP/email — each with incremental cursors and **ACL-capture fields reserved** for Phase 32; 31.7 conformance suite + template | No LLM in path; `tests/test_sync_idempotency.py` grows per connector; recorded-fixture offline tests |
| **32 — Permission-aware federation** [x-repo] | 32.1 per-artifact ACL metadata at ingest (SQLite alongside chunks; stable-ID grammar unchanged); 32.2 principal-context filtering in self-search *before* scoring/return; 32.4 ACL/group sync loop with a documented staleness SLA; 32.6 leak-test suite (permanent gate) | Contracts stay ACL-free (Tier-1 untouched); defaults off — a standalone/ACL-less region is byte-identical to today |
| **33 — Agent memory as a region** | 33.1 memory region type: `memory_write` MCP tool + HTTP append of schema-versioned memory records flowing through the normal chunk→embed→graph path; 33.2 temporal validity (asserted-at/superseded-at) + consolidation/decay on the 21.1 scheduler | Content arrives via API but indexing stays deterministic; one-shot idempotent state migrations for the new artifact |
| **34 — Subjective relevance** | No region code change expected (Flock-side UX + calibration); regions may expose feedback capture hooks in the UI | — |
| **35 — Open contract spec + adapters** [x-repo] | Schema re-vendor **only if** Flock bumps the wire format (bilateral law + PARITY.json); A2A alignment if the region grows its own card | Never hand-edit `contracts/`; vendored-fixture parity |
| **36 — Enterprise ops** | Fleet observability hooks (region health/sync metrics), backup/restore drill participation (21.6 snapshots already ship) | `/state` is user data — migrations preserve originals |

## 2b. Phase 30 — region-side step contracts (execution started 2026-07-15)

Canonical contracts live in
`pheasant-flock/docs/PRODUCT_FRAMEWORK.md` §2; this section mirrors
only the steps that land **here**.

### Step 30.1 — `pheasant up` personal quickstart — **landed 2026-07-15**

One command takes a directory (default `.`) from nothing to an indexed,
queryable knowledge base: **detect** (`.obsidian/` → `obsidian_vault`,
`.git/` → `repository`, else `document_folder`) → **generate** a
laptop-shaped config if absent (quickstart profile, one source with the
detected type + absolute path, name slugged from the dir, state anchored
under `./.pheasant/{state,vault,exports}`, `workspace_root` = target;
an existing config is **reused unchanged**, never overwritten) →
**index** via the normal `SyncEngine.sync_all("incremental")` →
**serve** as `pheasant start` does (`--no-serve` stops after sync,
`--port` sets the generated port). Acceptance: fixture-workspace run
indexes > 0 artifacts with state under `./.pheasant/state`; second run is
byte-stable config + zero re-index (idempotency spine); detection tests
for all three types; fully offline; no synapse config emitted (standalone
mode untouched).

### Step 30.3 — published images — **landed 2026-07-15 (defaults alignment)**

This repo's half pre-existed: `.github/workflows/container.yml` publishes
`ghcr.io/<owner>/pheasant:<semver>` on every merged release. The Flock side
aligned its Helm/compose/docs defaults to the published-image path (its
`docs/PRODUCT_FRAMEWORK.md` §2 has the full contract). No change here.

### Step 30.5 — KB-MCP wedge — **landed 2026-07-15**

`pheasant client-config claude-code|cursor [--mode local|docker-exec|
docker-run]`: new `src/pheasant/mcp_client/agents.py` emits the shared
`mcpServers` config shape (project `.mcp.json` / `.cursor/mcp.json`).
`local` mode runs the pip-installed binary over stdio — the zero-docker
path that pairs with `pheasant up` (whose ready-message now prints the
attach command); docker modes reuse the VS Code arg vectors. Guide:
`docs/how-to/attach-to-coding-agent.md`. MCP tool surface unchanged
(additive-only rule respected). Acceptance: agent-config cases in
`tests/test_mcp_client_config.py`. External MCP registry/directory
listings deferred (release-channel work, tracked in the Flock framework doc).

**Phase 30 is complete (2026-07-15).**

## 2c. Phase 31 — region-side step contracts (execution started 2026-07-15)

Canonical contracts: `pheasant-flock/docs/PRODUCT_FRAMEWORK.md` §3.

### Step 31.1 — Connector SDK — **landed 2026-07-15**

Third parties add a source type without forking: connector classes resolve
by `sources[].type` name through `importlib.metadata` entry points (group
`pheasant.connectors`, new `sync/connector_registry.py`) or programmatic
`register_connector_class`; loaded objects must subclass
`SourceConnector`. Config accepts unknown type strings as
`PluginSourceType` (a `str` with a `.value` property — every existing
`source.type.value` call site untouched; no workspace-root anchoring for
plugin types); resolution happens at dispatch, and a missing plugin fails
the sync naming the type + installed plugins. Built-ins stay hardcoded in
`connector_for_source` — the zero-plugin path is byte-identical. The
public quality bar is `pheasant.testing.ConnectorConformance` (declared
type, healthy validate, deterministic unique identities, stable payloads
or `ItemNotModified`, JSON-serializable checkpoint round-trip, full-mode
bypass) — **FilesystemConnector passes the same harness**. Canonical
third-party shape: `tests/fixtures/pheasant-connector-example/`
(`StaticDirConnector`), driven end-to-end through `SyncEngine` with an
idempotent second sync in `tests/test_connector_sdk.py`; conformance for
both connectors in `tests/test_connector_conformance.py`. Docs:
`docs/reference/connector-sdk.md`.

### Step 31.2 — Notion connector — **landed 2026-07-16**

The first SaaS connector, built as a **first-party SDK plugin**
(dogfooding 31.1: `src/pheasant/connectors/notion.py`, declared under the
`pheasant.connectors` entry-point group in this repo's `pyproject.toml`,
config `type: notion`). Pages via paginated `POST /v1/search`; content =
each page's block tree rendered to **deterministic Markdown** (headings /
paragraphs / lists / to-dos / quotes / callouts / code / dividers, nested
children to depth 3, no LLM). Pillars: `item.sha256` =
`(page_id, last_edited_time)` version proxy → engine pre-read skip before
any block fetch; checkpoint cursor stores per-page edit times →
`read_item` raises `ItemNotModified` on incremental for unchanged pages;
`full` ignores the checkpoint; token from
`sources[].connector.api_key_env` (new generic field, default
`NOTION_TOKEN`) — never stored. **ACL capture reserved for Phase 32**:
`metadata["acl"]` carries `created_by`/`last_edited_by` principal ids.
Acceptance: `tests/test_notion_connector.py` (12) against recorded
fixtures — pagination, deterministic rendering, actionable token error,
incremental skip, engine e2e (idempotent second sync with zero block
fetches; edit-time-only bump refetches without re-index; real content
edit re-indexes exactly one page), conformance pass, entry-point guard.

### Steps 31.3–31.7 — GDrive, Slack, Confluence, IMAP + certification — **landed 2026-07-16 (Phase 31 complete)**

All four ride the 31.2 pattern (full contracts: Flock `PRODUCT_FRAMEWORK.md`
§3): entry-point SDK plugins in `src/pheasant/connectors/`, one
monkeypatch-friendly network touchpoint each (stdlib urllib / imaplib —
zero new deps), deterministic rendering, version-proxy `item.sha256`,
per-item incremental cursors (`ItemNotModified`), secrets via
`connector.api_key_env`, Phase-32 ACL capture in `metadata["acl"]`.
Highlights: gdrive exports Google-native docs as text and filters
non-text mimes; slack renders ts-ordered channel transcripts (subtypes
filtered, user ids as-is) with a `(channel, latest_ts)` proxy; confluence
expands storage bodies into the listing (network-free reads) and reduces
XHTML via BeautifulSoup; imap exploits message immutability for an exact
UID high-watermark (a second sync lists *nothing*). 31.7 publishes the
bar: certified-connectors table + recipe in
`docs/reference/connector-sdk.md`, certification test shipped inside the
canonical example package (fixture suites excluded from the host run via
`norecursedirs`); standalone PyPI/cookiecutter template = release-channel
work. Acceptance: `tests/test_saas_connectors.py` (34, fully offline)
incl. per-connector conformance + engine e2e + the five-entry-point
guard. Suite: **248 passed**.

## 2d. Phase 33 — region-side step contracts (execution started 2026-07-16)

Canonical contracts: `pheasant-flock/docs/PRODUCT_FRAMEWORK.md` §3c.

### Step 33.1 — Memory region type + write path — **landed 2026-07-16**

**Memory records are source content** — the write path only creates files;
indexing stays the ordinary deterministic pipeline. New
`src/pheasant/memory/store.py`: append-only `MemoryStore` (one Markdown
file per record under `<memory-source>/<scope>/<record_id>.md`,
frontmatter with `schema_version: 1` / scope / subject / `asserted_at` /
optional `supersedes` + `tags`; deterministic id
`mem-<instant>-<blake2b8(scope|subject|text)>`; identical write →
`created: false`, nothing ever overwritten). `SourceType.memory` is a
built-in filesystem source type (watcher/scheduler/globs/anchoring as any
folder). Write surfaces (additive): MCP `memory_write` on `PheasantTools`
+ `POST /memory` / `GET /memory` on the API — `sync=true` default gives
read-your-writes via ordinary `search_context`; no memory source →
actionable error. The contract publisher advertises `"memory"` in
`capabilities.modalities` (25.4 image/audio precedent — existing wire
data, no schema bump, parity green). Acceptance:
`tests/test_memory_region.py` (store determinism/round-trip/validation,
MCP write→search e2e + zero-work re-write, HTTP round-trip + 400s,
publisher capability). 33.3/33.4 (routing + benchmark) land in Flock.

### Step 33.2 — Temporal validity + consolidation — **landed 2026-07-16**

A record is **superseded** once any record names it in `supersedes`
(chains resolved across scopes); `list_records(current_only=True)` /
`GET /memory?current_only=true` filter live. **Consolidation** is a pure
content operation: superseded + per-scope-TTL-expired records are archived
(file renamed `<id>.md.archived` in place — bytes preserved, never
deleted — so it stops matching the `**/*.md` include glob), then a
**full** re-sync of the small memory source drops them from
index/graph/vectors through the ordinary pipeline (incremental never
prunes). Runs on the 21.1 scheduler beat
(`memory/maintenance.run_memory_maintenance`) and on demand
(`memory_consolidate` MCP tool, `POST /memory/consolidate`). Config
`memory.{consolidation_enabled, session_ttl_days, user_ttl_days,
org_ttl_days}` — TTLs opt-in, consolidation on by default. Deterministic
in `now`, idempotent second pass. Acceptance: +6 cases in
`tests/test_memory_region.py` (chain filtering, deterministic+idempotent
archive, search-forgets-after-maintenance, TTL-None, disabled/no-source
no-ops, HTTP round-trip).

### Step 33.4 — Memory-recall benchmark — **landed 2026-07-16 (Phase 33 complete)**

`memory/benchmark.py` (`python -m pheasant.memory.benchmark`):
LongMemEval-style, deterministic, offline — seeded cases through the
**real** `memory_write` → batch index → `search_context` (hybrid) path;
categories: single-hop recall, multi-session interference, knowledge
updates (supersedes + consolidation), abstention. Recorded (30 facts /
120 distractors / 10 updates / 10 abstain, k=5): **recall@5 1.000,
update_accuracy 1.000, stale_leak 0.000, abstention 1.000** — canonical
numbers + methodology in Flock `docs/RESULTS.md` §9d. **The bench exposed
and drove two real self-search fixes** (`search/sqlite_store.py`): NL
questions no longer zero out on FTS5 implicit-AND (MATCH is now an OR of
sanitized tokens ranked by BM25), and the bm25→relevance mapping is now
monotone (the old `1/(1+|bm25|)` inverted hybrid-merge ordering). Gate:
`tests/test_memory_benchmark.py` (thresholds ≥0.9/≥0.9/0.0/≥0.9 +
NL-question ranking regression + generator determinism).

## 3. Sequencing note for this repo

The enterprise track runs **30 → 31 → 32 → 36** and is region-heavy; the
personal track runs **30 → 33** and is also region-heavy. Expect this repo to
carry most of the roadmap's implementation weight, with the Flock repo carrying
routing/identity/spec/eval work. Each phase gets per-step contracts (in the
style of `docs/SYNAPSE_INTEGRATION.md` §2) when scheduled; sessions stay
scoped to one step, with run summaries per the framework conventions.

## 4. Pointers

- Canonical roadmap + landscape sources:
  `pheasant-flock/docs/ROADMAP_CONTEXT_KNOWLEDGE_MGMT.md`
- Region-side Synapse spec: `docs/SYNAPSE_INTEGRATION.md`
- Design pillars + rules: `CLAUDE.md` §1, §4
