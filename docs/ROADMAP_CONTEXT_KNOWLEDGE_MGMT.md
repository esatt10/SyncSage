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
