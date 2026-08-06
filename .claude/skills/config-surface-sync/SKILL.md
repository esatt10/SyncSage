---
name: config-surface-sync
description: Keep the config wizard and its docs in sync whenever a config-schema-touching change lands. Triggers whenever you add, rename, remove, or change the default of a field in src/pheasant/config/schema.py (PheasantConfig or any nested *Settings dataclass), add a new env var pheasant reads, add a new pyproject.toml optional-dependency extra, or add a new sources[].type / connector. Also triggers on any Synapse/Phase step whose CLAUDE.md write-up you're drafting if it touched config.
---

# Keep the config wizard in sync

pheasant has a guided configuration wizard —
[`agent/config_wizard_prompt.md`](../../../agent/config_wizard_prompt.md) —
that walks a user through every config section with a live agent, backed
by [`docs/configuration.md`](../../../docs/configuration.md) as the
human-readable reference. Both are hand-maintained prose describing a
config surface (`src/pheasant/config/schema.py`) that changes often.
`tests/test_config_wizard_freshness.py` mechanically checks *that a
mention exists*, but it cannot check that the mention is *accurate* or
*complete* — that's this skill's job, and it's why it's a skill rather
than only a test.

## When this applies

You are doing work that touches any of:

- `src/pheasant/config/schema.py` — a new field, a new nested `*Settings`
  dataclass, a renamed field, a changed default.
- `.env.example` — a new env var, a new provider/connector credential.
- `pyproject.toml`'s `[project.optional-dependencies]` — a new extra.
- A new `sources[].type` (built-in `SourceType` or a new first-party
  connector under `src/pheasant/connectors/`).
- `pheasant.example.yaml` — a new top-level block or example source shape.

If none of the above, this skill does not apply — most feature work
(retrieval logic, graph enrichment internals, UI) never touches the
config surface and needs no action here.

## What to do

1. **Update `docs/configuration.md` first.** Add or edit the `##`
   section / table row for what you changed, in the same style as the
   surrounding sections (Key | Type | Default | Notes table). This is
   the document a human reads directly and the one the wizard treats as
   its own source of truth — get it right here before touching the
   wizard file.
2. **Update `agent/config_wizard_prompt.md`.**
   - New top-level `PheasantConfig` field → add it to the §5 walk-order
     list, in the same field-declaration position as `schema.py` (the
     freshness test checks the order matches).
   - New nested settings block (something like `search.embeddings` or
     `ingestion.captioner`) → make sure its field name is mentioned by
     name somewhere in the wizard file — either the generic per-section
     walk already covers it, or add a short note under "A few sections
     need more than the generic walk" the way `search`/`ingestion`/
     `sync`/`security`/`synapse`/`assistant` already do.
   - New env var → add it to §7a's trigger table if it's conditional on
     a config choice (most are).
   - New pyproject extra → add it to §7b's extras-mapping table.
   - New connector/source type → make sure §6's type list still points
     at the authoritative enumeration (`SourceType` /
     `pheasant.connectors` entry points) rather than hardcoding a stale
     list — the wizard file is written to re-check this at run time, so
     you may not need an edit at all unless the type needs a *new
     question* (e.g. a new required credential).
3. **Run the freshness gate:**
   ```bash
   python -m pytest tests/test_config_wizard_freshness.py -q
   ```
   Fix whatever it names before moving on. It only checks presence/order
   of section names, not prose quality — reread your docs/configuration.md
   and agent/config_wizard_prompt.md edits for accuracy too, the test is a
   floor, not a substitute for review.
4. **Full suite before finishing the session**, as usual (`pytest -q`,
   `ruff check src tests`) — the freshness test is one file among many,
   not a replacement for the ordinary acceptance bar in `CLAUDE.md` §4.

## Anti-patterns (reject on sight)

- Adding a config field and moving on without opening
  `docs/configuration.md` or `agent/config_wizard_prompt.md` at all.
- Satisfying the freshness test by adding the field name somewhere
  irrelevant (a code comment, an unrelated sentence) instead of a real,
  accurate explanation a user would actually read.
- Duplicating wizard content into a tool-specific adapter
  (`.claude/commands/config-wizard.md`,
  `.github/prompts/pheasant-config-wizard.prompt.md`,
  `.gemini/commands/config-wizard.toml`,
  `agent/codex_prompts/pheasant-config-wizard.md`, `AGENTS.md`) instead
  of editing the one canonical `agent/config_wizard_prompt.md` — those
  files must stay thin pointers (also enforced by the freshness test).
