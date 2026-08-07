# Setup simplification — plan

**Date:** 2026-08-07 · **Branch:** `claude/codex-setup-simplification-wgx4as`

## Why

The 2026-08-06 "startup overhaul" (#42) tried to solve onboarding by handing
the job to a coding agent: a 449-line operating procedure
(`agent/config_wizard_prompt.md`) plus four per-tool adapter files (Claude,
Codex, Gemini, VS Code) that all say "read that file and follow it."

It does not work well, for reasons that are structural rather than fixable:

- **It is not deterministic.** The same answers produce different YAML
  depending on which agent ran it and what it remembered. Everything else in
  this repo is deterministic on purpose (CLAUDE.md §4 rule 1).
- **It needs an agent to run at all.** A user with Docker and a terminal —
  the target user of a "Docker-first, local-first" tool — cannot use it.
- **It duplicates the schema in prose**, which is why it needed a CI
  freshness test (`tests/test_config_wizard_freshness.py`) to stop it rotting.
  A wizard that reads the schema cannot rot.
- **The hard parts were never the questions.** They are: getting a host
  directory visible inside a container, knowing a sync is progressing,
  changing your mind after setup, and getting secrets in without pasting them
  into YAML. Prose cannot do any of those.

So: remove the agentic surface, and make the product easy to set up **itself**.

## Phase 0 — Remove the agentic setup surface

Delete `agent/config_wizard_prompt.md`, `agent/codex_prompts/`,
`.claude/commands/config-wizard.md`, `.gemini/`, `.github/prompts/`,
`.claude/skills/config-surface-sync/`, `docs/how-to/config-wizard.md`, and
`tests/test_config_wizard_freshness.py`. Trim `AGENTS.md` back to a pointer at
`CLAUDE.md`; update `CLAUDE.md` rule 11, `README.md` and `mkdocs.yml`.

The freshness test's *mechanical* value is kept, not dropped: it is replaced
by `tests/test_config_surface_freshness.py`, which asserts every dataclass
section in `config/schema.py` is documented in `docs/configuration.md` **and**
reachable from the new `pheasant setup` — a check against live code instead of
against prose.

## Phase 1 — `pheasant setup`: a real interactive terminal wizard

`src/pheasant/setup_wizard.py`. Deterministic, offline, no LLM. Walks the
config surface in **sections**, each of which explains what it does before
asking, and every question carries a working default so Enter is always a
valid answer.

- Sections are **generated from the schema**, so a new field cannot be
  forgotten (this is what makes the CI freshness test cheap and true).
- Secrets: the wizard writes the *env var name* into `pheasant.yaml` and the
  *value* into `.env` with mode `0600`, never echoing it, never storing it in
  YAML or `/state`. An existing `.env` is merged, never clobbered; the
  wizard checks `.gitignore` covers it and fixes it if not.
- Resumable: progress is checkpointed to `.pheasant-setup.json`.
- `--answers FILE` / `--accept-defaults` make it scriptable and testable.

## Phase 2 — Load documents through the UI

`POST /sources/upload` (multipart). Files land under
`<state>/uploads/<source>/`, are registered as a real `document_folder`
source, and flow through the ordinary connector → chunk → graph pipeline —
no second ingest path. UI: a drop zone on the empty state and in quick-add.

## Phase 3 — Any local directory, not just a container subdirectory

`src/pheasant/deployment/mounts.py` reads the container's own mount table and
answers the question the user actually has: *"I typed `/Users/me/notes` and it
says the path does not exist."*

- `GET /fs/host-path?path=…` → `visible` (with the container path) or
  `not_mounted` (with an exact `docker compose` / `docker run` remedy).
- `pheasant mount <hostpath>` writes the bind mount into
  `docker-compose.override.yml` and adds the container path to
  `security.allow_workspace_roots`.
- Registering a source at an unmounted path returns the remedy instead of a
  bare 400.

## Phase 4 — Retrieval tuning as first-class config

`assistant.retrieval` — a typed home for the knobs that were only reachable as
an untyped `workflow_options` dict: rounds (turns), search depth, expansion
depth, per-query results, context passages, modes, grading, citation
verification. Merged *under* `workflow_options` so existing configs and
per-request overrides keep winning. `GET`/`PUT /assistant/retrieval` reads and
persists it; the UI gets a Retrieval section.

## Phase 5 — One universal image

Multi-stage `Dockerfile`: build `ui/dist`, then a runtime that installs **all**
extras by default (`mcp,agent,vector,wasm,a2a`) and serves API + MCP + UI from
one container. An entrypoint generates a working config when `/config` is
empty, so `docker run -v $PWD:/workspace ghcr.io/esatt10/pheasant` works with
no config file at all — and any config the user *does* supply still works,
because every optional code path is installed.

## Phase 6 — Modify the live knowledge base from the UI

`PATCH /config/section/{section}`: validate one section, persist it, apply it
live where that is safe, and report honestly (`applied` vs `restart_required`)
where it is not. `PUT /knowledge-base` edits identity (description freely;
renaming reports the re-index it implies rather than silently orphaning the
graph). The UI gets a Knowledge base panel driven by the same routes.

## Phase 7 — Background job progress

`src/pheasant/jobs.py`: an in-process job registry. `SyncEngine.sync_source`
grows an `on_progress` hook; `pheasant sync --progress` emits NDJSON progress
lines; `sync/worker.py` streams the child's stdout instead of buffering it, so
the parent sees progress live. `GET /jobs`, `GET /jobs/{id}`,
`GET /jobs/stream` (SSE). UI: an expandable jobs tray in the top bar showing
every running job with a real progress bar.

## Phase 8 — Fullscreen graph workspace

A `/graph` route with the canvas at full viewport, plus the diagnostics the
side panel has no room for: node search, degree/hub ranking, orphan and
component counts, an edge-type histogram, and a shortest-path finder between
two nodes. API: `GET /graph/diagnostics`, `GET /graph/path`.

## Phase 9 — MCP: retrieval criteria an agent can set per call

Additive only (CLAUDE.md §4 rule 8). `search_context` gains the retrieval
criteria that were previously static config; two new tools —
`describe_retrieval` (what this region's retrieval is configured to do, and
what is tunable) and `preview_retrieval` (run criteria and report what they
would return, and how that differs from the static config) — let an agent test
a configuration before anyone writes it into YAML.

## Acceptance

`pytest -q` green, `ruff check`/`ruff format --check` clean, `tsc -b && vite
build` clean, and every phase carries its own tests.
