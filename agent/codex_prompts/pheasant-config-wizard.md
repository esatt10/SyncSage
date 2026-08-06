<!--
Codex CLI reads custom slash-command prompts from ~/.codex/prompts/*.md
(user-level only — Codex has no project-scoped custom-command directory as
of this writing). Copy or symlink this file there to get a /pheasant-
config-wizard slash command in any repo:

  macOS/Linux:  cp agent/codex_prompts/pheasant-config-wizard.md ~/.codex/prompts/
  Windows:      copy agent\codex_prompts\pheasant-config-wizard.md %USERPROFILE%\.codex\prompts\

No copy needed if you'd rather just paste the two-line instruction below
directly into a Codex session — see docs/how-to/config-wizard.md.
-->

Read `agent/config_wizard_prompt.md` in this repository in full and follow
it exactly as your operating procedure for this conversation. It is the
canonical, tool-agnostic spec for the pheasant configuration wizard: which
files to read as the source of truth for every config option, which files
to maintain as progress state so the user never loses their place, the
section-by-section walk order, and the exact contract for the final
`pheasant.yaml` + `.env` + startup-commands output.

Do not summarize or paraphrase the spec back to the user — start executing
it, beginning with its "state: never lose the user's place" resume check
and its bootstrap questions.
