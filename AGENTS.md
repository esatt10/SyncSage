# AGENTS.md — pheasant

Entry point for coding agents that read `AGENTS.md` by convention (Codex
CLI, Gemini CLI, and others). This file is intentionally short — it exists
so those tools have *something* to auto-load; it is not a duplicate of the
real hand-off doc.

**Read `CLAUDE.md` first.** It is the dense, canonical context file for
this repository (what pheasant is, repo layout, canonical commands, rules,
current work queue). Everything below is a pointer, not a substitute.

## Building a knowledge-source config (`pheasant.yaml` + `.env`)

If the user wants to configure a new pheasant knowledge source — set up
`pheasant.yaml`, generate a matching `.env`, and get the exact commands to
start the instance — **read `agent/config_wizard_prompt.md` in full and
follow it exactly.** It is a self-contained, tool-agnostic operating
procedure: a guided Q&A that explains every config option before asking
for it, tracks progress in on-disk state so the user never loses their
place across sessions or tools, and ends with a complete config, a
complete `.env`, and the startup commands (including any dependency
installs) for the deployment target they chose.

Do not attempt to answer "how do I configure pheasant" questions from
memory instead of running the wizard — pheasant's config surface changes
often (see `CLAUDE.md`'s work-queue history), and the wizard's own first
rule is to re-read the live schema/docs rather than recite anything
memorized.

## Everything else

Ordinary code changes (bug fixes, features, tests) follow the rules in
`CLAUDE.md` §4, the canonical commands in §3, and — if the change touches
`src/pheasant/config/schema.py` — the freshness obligation described in
`.claude/skills/config-surface-sync/SKILL.md` and enforced by
`tests/test_config_wizard_freshness.py`.
