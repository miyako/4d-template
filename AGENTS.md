<!--
  TEMPLATE AUTHORS: this file is the agent instruction entry point of every repository created from
  this template. GitHub Copilot (CLI, VS Code, cloud agent), Codex and other agents read it; Claude Code
  reads it through CLAUDE.md (`@AGENTS.md`).

  Replace every `TODO:` placeholder, delete the guidance comments, and keep the sections marked
  "keep as is". Keep this file short and specific to the repository: reusable 4D know-how belongs in
  miyako/skills (see "Instructions vs skills" below), not here.
-->

# TODO: repository name

<!-- One or two sentences: what this repository contains and what it is for. -->
TODO: what this repository is (e.g. "A 4D project demonstrating …", "A pipeline that …").

<!-- Optional: the main folders an agent should know about. Delete if obvious. -->
- `TODO/`: TODO

## Repository rules

<!--
  Rules that only make sense for THIS repository (or for repositories created from this template):
  naming, file layout, what must never be edited by hand, what to commit and what not, review gates…
  Number them when order or priority matters. Rules that apply only to some paths go into
  `.github/instructions/<topic>.instructions.md` with an `applyTo` glob instead (see that folder's README).
-->
1. TODO: a repository-specific rule.
2. TODO: another one.

## Workflow and checkpoints

<!--
  Optional. Describe the phases of the typical task in this repository and where the agent must STOP
  and wait for the user (checkpoints). Delete this section if the repository has no fixed workflow.
-->
1. TODO: phase 1. **STOP:** summarise what changed and list what the user should review.
2. TODO: phase 2.

## Skills (keep as is)

Reusable 4D knowledge lives in the `.agents` submodule (`miyako/skills`, branch `dist`):

- Each skill is at `.agents/skills/<name>/SKILL.md`. Copilot, Codex and Claude Code discover them automatically.
- Read `.agents/AGENTS.md` first: it explains which skill to use for which 4D artifact
  (`.4DProject`, `.4DForm`, `.4DCatalog`, `.4DSettings`, `.4dm`, XLIFF …).
- Prefer the most specific skill over general knowledge of 4D.
- Never edit files under `.agents/`: they are overwritten by the weekly skills update.

## Setup (keep as is)

```sh
git submodule update --init .agents
```

If `.agents/` is empty, run the command above before doing anything else. `--recursive` is not needed.
Tools provisioned by the skills go to `tools/4dcatalog/`, `tools/4dform/`, `tools/4dlsp/` and `tools/4dlang/`;
they are ignored by `.gitignore` and must not be committed.

## Instructions vs skills (keep as is)

- **Repository-specific rules** go in this file, or in `.github/instructions/*.instructions.md` with an
  `applyTo` glob when they only apply to some paths.
- **Reusable know-how** (4D language, forms, CSS, XLIFF, project settings …) goes to
  [miyako/skills](https://github.com/miyako/skills). Never copy a skill or its content into this repository;
  improve the skill there instead, and mention what you learnt to the user so it can be upstreamed.

## README (keep as is)

`README.md` is for developers. Never put Copilot model, mode, token or session content (usage tables,
model selection advice, session logs) in `README.md`.
