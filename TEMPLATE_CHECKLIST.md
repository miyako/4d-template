# Checklist for authors of a derived template

Use this list when you create a template from `miyako/4d-template` (or update an existing derived template).

## Structure

- [ ] Created with **Use this template** from `miyako/4d-template`, under its final name.
- [ ] `.gitmodules` declares `.agents` → `https://github.com/miyako/skills.git`, `branch = dist`.
- [ ] `git submodule status .agents` shows a commit (not `-`), and `.agents/AGENTS.md` exists after cloning
      with `--recurse-submodules`.
- [ ] `CLAUDE.md` contains `@AGENTS.md`.
- [ ] No `.github/copilot-instructions.md` (its content belongs in `AGENTS.md`).
- [ ] No `.github/skills/` and no copies of skills or skill content anywhere in the repository.
- [ ] **Settings → General → Template repository** is ticked.

## AGENTS.md

- [ ] Every `TODO` is replaced; the HTML guidance comments are removed.
- [ ] Says what the template is for and what a repository created from it contains.
- [ ] Repository rules are specific to this template; generic 4D advice links to a skill instead.
- [ ] Workflow / checkpoints are described if the template has a fixed process.
- [ ] The "Skills", "Setup", "Instructions vs skills" and "README" sections are kept.
- [ ] Written so that it is correct **in a repository created from the template** too (don't hard-code the
      template's own name where the derived repository's name is meant).

## Path-scoped instructions

- [ ] Each `.github/instructions/*.instructions.md` has `description` and `applyTo` frontmatter.
- [ ] Each one is repository-specific; nothing that would be useful in an unrelated 4D repository.
- [ ] `example.instructions.md` is deleted or replaced.

## Workflows

- [ ] `copilot-setup-steps.yml`: job named `copilot-setup-steps`; checkout has `submodules: true`;
      template-specific steps (tools, fonts, Python …) come after the checkout.
- [ ] `update-skills.yml` is present, unchanged unless needed (weekly + manual, `contents: write`).
- [ ] Any other workflow that needs the skills checks out with `submodules: true`.

## .gitignore

- [ ] Ignores `/tools/4dcatalog/`, `/tools/4dform/`, `/tools/4dlsp/`, `/tools/4dlang/`, `/tools/4dcli/` — **not** the whole `tools/`.
- [ ] 4D entries adjusted to the template's layout (e.g. `demo/*/Data/` when projects live in sub-folders);
      decide explicitly whether `Settings/` is tracked.

## README.md

- [ ] Rewritten for the template's users (developers): purpose, how to create a repository from it, how to clone
      (`--recurse-submodules` / `git submodule update --init .agents`).
- [ ] No Copilot model, mode, token or session content.
- [ ] The template is listed in the table of derived templates in `miyako/4d-template`'s README.
