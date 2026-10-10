# Path-scoped instructions

Files named `*.instructions.md` in this folder are repository-specific rules that the agent applies only
to the paths matched by their `applyTo` glob. GitHub Copilot (CLI, VS Code, cloud agent) loads them
automatically; other agents follow the pointer in `AGENTS.md`.

## When to add one

Add an instruction file only when **all** of these are true:

- The rule is **specific to this repository** (or to repositories created from this template).
  Reusable 4D know-how — language, forms, CSS, XLIFF, menus, project settings, tooling — belongs in
  [miyako/skills](https://github.com/miyako/skills) and reaches this repository through the `.agents`
  submodule. Never copy a skill, or part of one, into this folder.
- The rule applies to **a subset of paths** (e.g. `README.md`, `demo/**`, `Project/Sources/Forms/**`).
  Rules that apply to the whole repository go in `AGENTS.md`.
- The rule is **stable**: something the agent must do every time, not a one-off task description.

## Format

```markdown
---
description: "One sentence: what the rules are about and when they apply."
applyTo: "glob/**/pattern, another/glob/*.md"
---

# Title

Rules, short and concrete. Link to skills (`.agents/skills/<name>/SKILL.md`) instead of repeating them.
```

- `applyTo` is required. Use a comma-separated list for several globs.
- One topic per file; name it `<topic>.instructions.md`.
- `example.instructions.md` shows the format. Delete it (or replace it with a real file) in derived repositories.
- This `README.md` is guidance for template authors; derived templates may keep or delete it.
