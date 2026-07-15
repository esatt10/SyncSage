# Context & Knowledge Management Roadmap — Region-Side Companion

**Status:** companion document (2026-07-14). The **canonical roadmap** —
landscape survey, gap analysis, positioning, and the full Phase 30–36 plan —
lives in the sibling repo:
`subjective-retrieval/docs/ROADMAP_CONTEXT_KNOWLEDGE_MGMT.md` (ADR 2026-07-14
in `…/docs/DECISIONS.md`). This file records only what lands **in this repo**
and the invariants each phase must respect here. Cross-repo rules are
unchanged: contract JSON + HTTP only, identical branch names, fixture parity,
both suites green.

---

## 1. Why SyncSage is the load-bearing half

The 2026 landscape research (sourced in the canonical doc) converged on three
findings that all point at the region:

1. **Knowledge-base MCP is the largest unmet demand in the MCP ecosystem**
   (10,000+ public servers, no established KB answer). SyncSage *is* a KB MCP
   server — the gap is packaging and distribution, not capability.
2. **Permission-aware retrieval is the #1 enterprise blocker** for GenAI
   rollouts, and the consensus is that enforcement must live in the retrieval
   layer, not the app layer. In a federated system, *the region is the
   retrieval layer* — ACL capture and filtering land here.
3. **Federated context with strong contracts** is the architecture analysts
   prescribe over central catalogs. Regions keep the data; only the ≤256 KB
   contract travels. Every enterprise-search competitor (Glean, Onyx) is
   structurally centralized and cannot cheaply retrofit this.

SyncSage's design pillars — deterministic parsing (no LLM in the indexing
path), idempotent/incremental sync, local-first persistence split — are the
*differentiators* against LLM-graph rivals (GraphRAG/LightRAG), not
limitations. They stay inviolate through every phase below.

## 2. Region-side workstreams by phase

| Phase | Region-side work (this repo) | Key invariants to preserve |
|---|---|---|
| **30 — Packaging & first-run** | 30.1 `syncsage up` personal quickstart (vault/folder autodetect → config → index → UI, no YAML on the happy path); 30.3 published GHCR images [x-repo]; 30.5 "attach your KB to a coding agent in 5 min" MCP packaging + registry listings | Standalone mode is the product here — no router required anywhere in the personal path |
| **31 — Connector SDK + connectors** | 31.1 entry-point connector SDK (checkpoint API, manifest integration, idempotency harness — the four pillars enforced by contract); 31.2–31.6 Notion, Google Drive, Slack, Confluence/Jira, IMAP/email — each with incremental cursors and **ACL-capture fields reserved** for Phase 32; 31.7 conformance suite + template | No LLM in path; `tests/test_sync_idempotency.py` grows per connector; recorded-fixture offline tests |
| **32 — Permission-aware federation** [x-repo] | 32.1 per-artifact ACL metadata at ingest (SQLite alongside chunks; stable-ID grammar unchanged); 32.2 principal-context filtering in self-search *before* scoring/return; 32.4 ACL/group sync loop with a documented staleness SLA; 32.6 leak-test suite (permanent gate) | Contracts stay ACL-free (Tier-1 untouched); defaults off — a standalone/ACL-less region is byte-identical to today |
| **33 — Agent memory as a region** | 33.1 memory region type: `memory_write` MCP tool + HTTP append of schema-versioned memory records flowing through the normal chunk→embed→graph path; 33.2 temporal validity (asserted-at/superseded-at) + consolidation/decay on the 21.1 scheduler | Content arrives via API but indexing stays deterministic; one-shot idempotent state migrations for the new artifact |
| **34 — Subjective relevance** | No region code change expected (SR-side UX + calibration); regions may expose feedback capture hooks in the UI | — |
| **35 — Open contract spec + adapters** [x-repo] | Schema re-vendor **only if** SR bumps the wire format (bilateral law + PARITY.json); A2A alignment if the region grows its own card | Never hand-edit `contracts/`; vendored-fixture parity |
| **36 — Enterprise ops** | Fleet observability hooks (region health/sync metrics), backup/restore drill participation (21.6 snapshots already ship) | `/state` is user data — migrations preserve originals |

## 2b. Phase 30 — region-side step contracts (execution started 2026-07-15)

Canonical contracts live in
`subjective-retrieval/docs/PRODUCT_FRAMEWORK.md` §2; this section mirrors
only the steps that land **here**.

### Step 30.1 — `syncsage up` personal quickstart — **landed 2026-07-15**

One command takes a directory (default `.`) from nothing to an indexed,
queryable knowledge base: **detect** (`.obsidian/` → `obsidian_vault`,
`.git/` → `repository`, else `document_folder`) → **generate** a
laptop-shaped config if absent (quickstart profile, one source with the
detected type + absolute path, name slugged from the dir, state anchored
under `./.syncsage/{state,vault,exports}`, `workspace_root` = target;
an existing config is **reused unchanged**, never overwritten) →
**index** via the normal `SyncEngine.sync_all("incremental")` →
**serve** as `syncsage start` does (`--no-serve` stops after sync,
`--port` sets the generated port). Acceptance: fixture-workspace run
indexes > 0 artifacts with state under `./.syncsage/state`; second run is
byte-stable config + zero re-index (idempotency spine); detection tests
for all three types; fully offline; no synapse config emitted (standalone
mode untouched).

### Step 30.3 — published images — **landed 2026-07-15 (defaults alignment)**

This repo's half pre-existed: `.github/workflows/container.yml` publishes
`ghcr.io/<owner>/syncsage:<semver>` on every merged release. The SR side
aligned its Helm/compose/docs defaults to the published-image path (its
`docs/PRODUCT_FRAMEWORK.md` §2 has the full contract). No change here.

### Step 30.5 — KB-MCP wedge — **landed 2026-07-15**

`syncsage client-config claude-code|cursor [--mode local|docker-exec|
docker-run]`: new `src/syncsage/mcp_client/agents.py` emits the shared
`mcpServers` config shape (project `.mcp.json` / `.cursor/mcp.json`).
`local` mode runs the pip-installed binary over stdio — the zero-docker
path that pairs with `syncsage up` (whose ready-message now prints the
attach command); docker modes reuse the VS Code arg vectors. Guide:
`docs/how-to/attach-to-coding-agent.md`. MCP tool surface unchanged
(additive-only rule respected). Acceptance: agent-config cases in
`tests/test_mcp_client_config.py`. External MCP registry/directory
listings deferred (release-channel work, tracked in the SR framework doc).

**Phase 30 is complete (2026-07-15).**

## 2c. Phase 31 — region-side step contracts (execution started 2026-07-15)

Canonical contracts: `subjective-retrieval/docs/PRODUCT_FRAMEWORK.md` §3.

### Step 31.1 — Connector SDK — **landed 2026-07-15**

Third parties add a source type without forking: connector classes resolve
by `sources[].type` name through `importlib.metadata` entry points (group
`syncsage.connectors`, new `sync/connector_registry.py`) or programmatic
`register_connector_class`; loaded objects must subclass
`SourceConnector`. Config accepts unknown type strings as
`PluginSourceType` (a `str` with a `.value` property — every existing
`source.type.value` call site untouched; no workspace-root anchoring for
plugin types); resolution happens at dispatch, and a missing plugin fails
the sync naming the type + installed plugins. Built-ins stay hardcoded in
`connector_for_source` — the zero-plugin path is byte-identical. The
public quality bar is `syncsage.testing.ConnectorConformance` (declared
type, healthy validate, deterministic unique identities, stable payloads
or `ItemNotModified`, JSON-serializable checkpoint round-trip, full-mode
bypass) — **FilesystemConnector passes the same harness**. Canonical
third-party shape: `tests/fixtures/syncsage-connector-example/`
(`StaticDirConnector`), driven end-to-end through `SyncEngine` with an
idempotent second sync in `tests/test_connector_sdk.py`; conformance for
both connectors in `tests/test_connector_conformance.py`. Docs:
`docs/reference/connector-sdk.md`. Steps 31.2–31.6 (Notion, Google Drive,
Slack, Confluence/Jira, IMAP) build on this SDK, one per session, each
with recorded-fixture offline tests + ACL-capture fields reserved for
Phase 32; 31.7 publishes the conformance suite as the public bar.

## 3. Sequencing note for this repo

The enterprise track runs **30 → 31 → 32 → 36** and is region-heavy; the
personal track runs **30 → 33** and is also region-heavy. Expect this repo to
carry most of the roadmap's implementation weight, with the SR repo carrying
routing/identity/spec/eval work. Each phase gets per-step contracts (in the
style of `docs/SYNAPSE_INTEGRATION.md` §2) when scheduled; sessions stay
scoped to one step, with run summaries per the framework conventions.

## 4. Pointers

- Canonical roadmap + landscape sources:
  `subjective-retrieval/docs/ROADMAP_CONTEXT_KNOWLEDGE_MGMT.md`
- Region-side Synapse spec: `docs/SYNAPSE_INTEGRATION.md`
- Design pillars + rules: `CLAUDE.md` §1, §4
