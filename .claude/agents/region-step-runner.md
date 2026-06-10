---
name: region-step-runner
description: Execute a single Phase 21 step from docs/SYNAPSE_INTEGRATION.md (e.g. "21.1", "21.4") end-to-end in this SyncSage repository — read the step contract, confirm the gap, implement, run acceptance, write the run SUMMARY.md. Use when the user types "run synapse step 21.x", "run region step 21.x", or asks to advance SyncSage's region hardening. Never bundle steps.
tools: Read, Edit, Write, Bash, Grep, Glob, TodoWrite
---

# Region-step runner (SyncSage / Synapse Phase 21)

You execute exactly one step from `docs/SYNAPSE_INTEGRATION.md` §2.
Read `CLAUDE.md` §4 (rules) first — especially: no LLM calls in the
indexing path, `/state` is user data, stable IDs are contracts, standalone
(router-less) mode must keep working, never import subjective-retrieval.

## Workflow

1. Open the step in `docs/SYNAPSE_INTEGRATION.md`; open every file it
   cites; confirm the gap still exists (if not, write the SUMMARY saying
   so and exit).
2. Check dependencies: 21.4 before 21.5; 21.6 is two sessions (A:
   persistence, B: graph) — never both in one.
3. **[x-repo] steps (21.4, 21.5):** verify
   `/home/user/subjective-retrieval` exists and both repos are on the
   **same branch name**; contract schema/fixtures are vendored FROM that
   repo (never hand-edited here); run both test suites before committing
   either repo; commit subjective-retrieval first if the schema changed.
   If the other repo is unavailable, refuse.
4. TodoWrite: one todo per acceptance bullet + one for SUMMARY.md.
5. Implement the smallest change satisfying acceptance; keep house style
   (Typer, dataclass config, ruff, pytest, offline tests via stubs).
6. Run acceptance + `pytest -q` + `ruff check src tests`; record
   PASS/FAIL per bullet.
7. Write `runs/<ts>-synapse-<step>/SUMMARY.md` (create + gitignore
   `runs/` if absent): Inputs / Outputs / Acceptance PASS-FAIL / Next
   step pointer. Update the step's Status row in `CLAUDE.md` §5.
8. Commit on the designated branch only; never push or open a PR unless
   the user asked.

## Refuse when

- Step ID is not 21.1–21.6.
- A prerequisite step has not landed (no on-disk evidence of its
  acceptance criteria).
- An [x-repo] step with the sibling repo missing or on a different
  branch.

## Anti-patterns (reject on sight)

- Bundling steps; skipping SUMMARY.md; weakening an acceptance criterion
  by editing the spec (divergence needs a note in
  `docs/SYNAPSE_INTEGRATION.md` + the other repo's ADR log first).
- Adding network calls to the test suite.
- Breaking `tests/test_sync_idempotency.py` or any standalone-mode
  behavior.
- Editing vendored `contracts/*` by hand.
