# Build your config with the guided wizard

pheasant ships a coding-agent-driven Q&A wizard that builds a complete
`pheasant.yaml`, a matching `.env`, and the exact commands to start your
instance — explaining every config option and its trade-offs as it goes,
rather than handing you a wall of YAML to figure out alone.

It's one file, [`agent/config_wizard_prompt.md`](https://github.com/esatt10/pheasant-kb/blob/main/agent/config_wizard_prompt.md),
written to be read and followed by **any** capable coding agent — the
short entry points below just point each tool at it.

## Invoke it

=== "Claude Code"

    ```
    /config-wizard
    ```

    (`.claude/commands/config-wizard.md`)

=== "GitHub Copilot"

    In VS Code Copilot Chat, switch to **agent mode** and run:

    ```
    /pheasant-config-wizard
    ```

    (`.github/prompts/pheasant-config-wizard.prompt.md` — a
    [prompt file](https://docs.github.com/en/copilot/tutorials/customization-library/prompt-files))

=== "Gemini CLI"

    ```
    /config-wizard
    ```

    (`.gemini/commands/config-wizard.toml`)

=== "Codex CLI"

    Codex only discovers custom prompts from `~/.codex/prompts/` (no
    project-scoped equivalent), so either copy the convenience file in
    once —

    ```bash
    cp agent/codex_prompts/pheasant-config-wizard.md ~/.codex/prompts/
    ```

    then run `/pheasant-config-wizard` in any Codex session — or skip the
    copy and just paste this into a Codex session in this repo:

    ```
    Read agent/config_wizard_prompt.md in full and follow it exactly.
    ```

=== "Any other agent"

    Paste the same instruction as the Codex fallback above — the wizard
    file is self-contained and assumes nothing about the tool running it
    beyond the ability to read/write files in the repo.

`AGENTS.md` at the repo root also points at the wizard file, so a tool
that only auto-loads that convention file (rather than a slash command)
still discovers it.

## What it produces

- A complete `pheasant.yaml` in the repo root (already gitignored).
- A complete `.env` in the repo root (already gitignored) with only the
  secrets your choices actually need, most left as clearly-marked
  placeholders for you to fill in.
- The exact `docker compose` or `pip install` + `pheasant` commands to
  install dependencies and start the instance, computed from which
  optional features you turned on (vector search, the agentic assistant
  workflow, WASM acceleration, Synapse signing, …) — see
  `pyproject.toml`'s `[project.optional-dependencies]` for what each
  extra buys.

## Never lose your place

The wizard writes its progress to `.pheasant/wizard-progress.md`
(gitignored) after every answered question, alongside the growing
`pheasant.yaml` draft. If your session is interrupted — closed terminal,
context reset, or you just want to finish in a different tool — start the
wizard again and it resumes from the last unanswered question instead of
starting over.

## Keeping the wizard itself current

The wizard's own first rule is to read `docs/configuration.md`,
`pheasant.example.yaml`, `.env.example`, and
`src/pheasant/config/schema.py` live rather than recite anything
memorized, so it mostly stays accurate on its own. The parts of it that
*are* hand-maintained (the section walk order, the source-of-truth file
list, the extras-mapping table) are checked by
`tests/test_config_wizard_freshness.py`, which fails CI if
`schema.py` gains a new top-level or nested settings block that isn't
mentioned in both `docs/configuration.md` and
`agent/config_wizard_prompt.md`. See
[`.claude/skills/config-surface-sync`](https://github.com/esatt10/pheasant-kb/blob/main/.claude/skills/config-surface-sync/SKILL.md)
for the contributor-facing side of that obligation.
