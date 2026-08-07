---
mode: agent
description: Guided Q&A that builds pheasant.yaml + .env + startup commands for a pheasant knowledge source
---

Read `agent/config_wizard_prompt.md` in this repository in full and follow
it exactly as your operating procedure for this conversation — it is the
canonical, tool-agnostic spec: which files to read as the source of truth
for every config option, which files to maintain as progress state so the
user never loses their place, the section-by-section walk order, and the
exact contract for the final `pheasant.yaml` + `.env` + startup-commands
output.

Do not summarize or paraphrase the spec back to the user — start executing
it, beginning with its "state: never lose the user's place" resume check
and its bootstrap questions.
