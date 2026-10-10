#!/usr/bin/env python3
"""End-to-end tests for scripts/migrate-legacy-repo.py.

Builds throwaway fixture repositories (bare remotes) in a temp dir — a skills repo with a `dist`
branch, migrated HDI and technote templates with legacy history, and legacy consumer repos — then runs
the migration CLI against them with --push and checks the result, idempotence, dry-run and refusals.

    python3 scripts/tests/test_migrate.py [-v]

Standard library only; needs git. Never touches GitHub.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "migrate-legacy-repo.py"
BASE = HERE.parents[1]

spec = importlib.util.spec_from_file_location("migrate", SCRIPT)
migrate = importlib.util.module_from_spec(spec)
sys.modules["migrate"] = migrate
spec.loader.exec_module(migrate)

SKILLS_TREE = "https://github.com/miyako/skills/tree/main/4d-skills/skills/"
MIGRATION = {
    "version": 1,
    "instructions": {
        "css.instructions.md": {"status": "skill", "skill": "4dcss", "url": SKILLS_TREE + "4dcss"},
        "tahoe.css.instructions.md": {"status": "skill", "skill": "4dcss", "url": SKILLS_TREE + "4dcss"},
        "startup.instructions.md": {"status": "instruction", "skill": "4dstartup", "url": None,
                                    "path": ".github/instructions/hdi.startup.instructions.md",
                                    "templates": ["HDI_Template"]},
        "aikit.async.instructions.md": {"status": "skill", "skill": "4daikit", "url": SKILLS_TREE + "4daikit"},
        "deprecated.commands.instructions.md": {"status": "skill", "skill": "4dlang", "url": SKILLS_TREE + "4dlang"},
        "readme.usage.guidance.instructions.md": {"status": "deleted"},
        "readme.hdi.instructions.md": {"status": "deleted"},
        "readme.branches.instructions.md": {"status": "instruction", "templates": ["HDI_Template"]},
        "readme.structure.instructions.md": {"status": "instruction", "templates": ["HDI_Template"]},
        "technote-translation.instructions.md": {"status": "instruction",
                                                 "templates": ["4d-technote-localisation-template"]},
    },
    "skills": {
        "apply-glossary": {"skill": "technote-apply-glossary", "url": SKILLS_TREE + "technote-apply-glossary"},
        "review-figures": {"skill": "technote-review-figures", "url": SKILLS_TREE + "technote-review-figures"},
        "4d-docs": {"skill": "4dlang", "url": SKILLS_TREE + "4dlang"},
        # 4d-form-layout deliberately missing: removed as a known legacy skill, with a warning
    },
}

CSS_A = "---\napplyTo: '**/*.css'\n---\n# CSS\n\nUse automatic colours.\n"
CSS_UNKNOWN = "---\napplyTo: '**/*.css'\n---\n# CSS\n\nUse automatic colours.\nExtra lesson only in this repo.\n"
AIKIT = "".join(f"# AI Kit async\n\nStep {i}: convert call {i} to a callback with formula.\n" for i in range(30))
OLD_TN = ("# Copilot instructions: 4D technical note localisation\n\n## Non-negotiable rules\n\n"
          "1. Code is never translated.\n2. Never patch the PDF.\n\n## Workflow\n\n- Phase 0: setup\n"
          "- Skills in `.github/skills/`: `review-figures`, `apply-glossary`\n")
NEW_TN_AGENTS = ("# 4D technical note localisation\n\n## Rules\n\n1. Code is never translated.\n\n## Skills\n\n"
                 "Read `.agents/AGENTS.md`; skills in `.agents/skills/`.\n")
HDI_AGENTS = "# HDI demo project\n\nA 4D How Do I demo.\n"  # lacks the skills pointer: the tool must add it
OLD_SETUP = """name: Copilot setup steps
on: workflow_dispatch
jobs:
  copilot-setup-steps:   # this job name is required
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Python environment
        run: make setup
"""
TOKEN_BRANCHES = "---\napplyTo: README.md\n---\nbranches\n\n## Copilot Token Usage\n\nAdd a table.\n"
README_HDI = (
    "# HDI_Foo\r\n\r\nDemo.\r\n\r\n## Modernisation notes\r\n\r\n"
    "Each branch represents a distinct modernisation effort, guided by a corresponding Copilot instruction file.\r\n\r\n"
    "| Branch | Description | Instructions |\r\n|--------|-------------|--------------|\r\n"
    "| [`b1`](../../tree/b1) | Dark mode | [css.instructions.md](.github/instructions/css.instructions.md), "
    "[tahoe](./.github/instructions/tahoe.css.instructions.md#top) |\r\n"
    "| [`b2`](../../tree/b2) | Docs | [readme.usage.guidance.instructions.md](.github/instructions/readme.usage.guidance.instructions.md) |\r\n"
    "| [`b3`](../../tree/b3) | Branch table | [readme.branches.instructions.md](.github/instructions/readme.branches.instructions.md) |\r\n"
    "| [`b4`](../../tree/b4) | Old path | [`startup.instructions.md`](.github/copilot/instructions/startup.instructions.md) |\r\n"
    "| [`b5`](../../tree/b5) | README | [readme.hdi.instructions.md](.github/instructions/readme.hdi.instructions.md) |\r\n"
    "\r\nSee the [instructions folder](.github/instructions/).\r\n\r\n"
    "```markdown\r\n## Copilot Token Usage\r\n[x](.github/instructions/css.instructions.md)\r\n```\r\n\r\n"
    "## Copilot Token Usage\r\n\r\n| Session | Tokens |\r\n|---|--:|\r\n| a | 1 |\r\n\r\n"
    "## Model Selection Assessment\r\n\r\nText.\r\n\r\n### Sessions where Opus was likely overkill\r\n\r\nMore.\r\n\r\n"
    "## License\r\n\r\nMIT \u00e9\u00e8 \u65e5\u672c\u8a9e\r\n\r\n"
    "### Mode Selection Guidance\r\n\r\nInteractive was right.\r\n"
)
README_HDI_EXPECTED = (
    "# HDI_Foo\r\n\r\nDemo.\r\n\r\n## Modernisation notes\r\n\r\n"
    "Each branch represents a distinct modernisation effort, guided by a 4D skill or a repository instruction file.\r\n\r\n"
    "| Branch | Description | Guidance |\r\n|--------|-------------|----------|\r\n"
    f"| [`b1`](../../tree/b1) | Dark mode | [`4dcss`]({SKILLS_TREE}4dcss) |\r\n"
    "| [`b2`](../../tree/b2) | Docs | readme.usage.guidance.instructions.md |\r\n"
    "| [`b3`](../../tree/b3) | Branch table | [readme.branches.instructions.md](.github/instructions/readme.branches.instructions.md) |\r\n"
    f"| [`b4`](../../tree/b4) | Old path | [`4dstartup`]({SKILLS_TREE}4dstartup), "
    "[hdi.startup.instructions.md](.github/instructions/hdi.startup.instructions.md) |\r\n"
    "| [`b5`](../../tree/b5) | README | [readme.structure.instructions.md](.github/instructions/readme.structure.instructions.md) |\r\n"
    "\r\nSee the [instructions folder](.github/instructions/).\r\n\r\n"
    "```markdown\r\n## Copilot Token Usage\r\n[x](.github/instructions/css.instructions.md)\r\n```\r\n\r\n"
    "## License\r\n\r\nMIT \u00e9\u00e8 \u65e5\u672c\u8a9e\r\n"
)


def sh(*cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise AssertionError(f"{' '.join(map(str, cmd))} failed:\n{p.stdout.decode()}\n{p.stderr.decode()}")
    return p.stdout.decode("utf-8", "replace").strip()


def make_repo(root: Path, name: str, commits, branch="main") -> Path:
    """commits: list of {path: bytes|str|None}; None deletes. Returns bare repo path."""
    work = root / "work" / name
    work.mkdir(parents=True)
    sh("git", "init", "-q", "-b", branch, cwd=work)
    for i, files in enumerate(commits):
        for path, content in files.items():
            p = work / path
            if content is None:
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        sh("git", "add", "-A", cwd=work)
        sh("git", "commit", "-q", "--allow-empty", "-m", f"commit {i}", cwd=work)
    bare = root / "remote" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    sh("git", "clone", "-q", "--bare", str(work), str(bare))
    return bare


class MigrateE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="migrate-test-"))
        gcfg = cls.tmp / "gitconfig"
        gcfg.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
                        "[protocol \"file\"]\n\tallow = always\n[commit]\n\tgpgsign = false\n")
        os.environ.update(GIT_CONFIG_GLOBAL=str(gcfg), GIT_CONFIG_NOSYSTEM="1")
        r = cls.tmp
        (r / "MIGRATION.json").write_text(json.dumps(MIGRATION, indent=2))

        skills = make_repo(r, "skills", [{"4d-skills/MIGRATION.json": json.dumps(MIGRATION)}])
        dist = r / "work" / "skills"
        sh("git", "checkout", "-q", "--orphan", "dist", cwd=dist)
        sh("git", "rm", "-q", "-rf", ".", cwd=dist)
        (dist / "AGENTS.md").write_text("# skills\n")
        (dist / "skills/4dcss").mkdir(parents=True)
        (dist / "skills/4dcss/SKILL.md").write_text("---\nname: 4dcss\n---\n")
        sh("git", "add", "-A", cwd=dist)
        sh("git", "commit", "-q", "-m", "dist", cwd=dist)
        sh("git", "push", "-q", str(skills), "dist", cwd=dist)
        cls.skills = skills

        cls.hdi_tpl = make_repo(r, "HDI_Template", [
            {".github/instructions/css.instructions.md": CSS_A,
             ".github/instructions/readme.branches.instructions.md": "old branches rules\n",
             ".github/instructions/readme.usage.guidance.instructions.md": "usage\n",
             ".gitignore": "Data/\n"},
            {".github/instructions/css.instructions.md": None,
             ".github/instructions/readme.usage.guidance.instructions.md": None,
             ".github/instructions/readme.branches.instructions.md": "---\napplyTo: README.md\n---\nnew branches\n",
             ".github/instructions/readme.structure.instructions.md": "---\napplyTo: README.md\n---\nstructure\n",
             ".github/instructions/hdi.startup.instructions.md": "---\napplyTo: '**/*.4dm'\n---\nsplash\n",
             ".github/instructions/README.md": "template docs, not copied\n",
             "AGENTS.md": HDI_AGENTS, "CLAUDE.md": "@AGENTS.md\n"},
        ])
        cls.tn_tpl = make_repo(r, "4d-technote-localisation-template", [
            {".github/copilot-instructions.md": OLD_TN,
             ".github/instructions/technote-translation.instructions.md": "---\napplyTo: src/**\n---\npipeline v1\n",
             ".github/instructions/aikit.async.instructions.md": AIKIT,
             ".github/skills/apply-glossary/SKILL.md": "glossary\n",
             ".github/workflows/copilot-setup-steps.yml": OLD_SETUP, "technote.json": "{}\n"},
            {".github/copilot-instructions.md": None, ".github/skills": None,
             ".github/instructions/aikit.async.instructions.md": None,
             ".github/instructions/technote-translation.instructions.md": "---\napplyTo: src/**\n---\npipeline v2\n",
             "AGENTS.md": NEW_TN_AGENTS, "CLAUDE.md": "@AGENTS.md\n"},
        ])
        cls.legacy_tpl = make_repo(r, "LegacyTemplate", [{".github/copilot-instructions.md": OLD_TN}])

        def hdi_files():
            return {
                ".github/instructions/css.instructions.md": CSS_A,
                ".github/instructions/tahoe.css.instructions.md": CSS_UNKNOWN,
                ".github/instructions/startup.instructions.md": "startup\n",
                ".github/instructions/readme.branches.instructions.md": "old branches rules\n",
                ".github/instructions/readme.usage.guidance.instructions.md": "usage\n",
                ".github/instructions/readme.hdi.instructions.md": "hdi\n",
                ".github/instructions/deprecated.commands.instructions.md": "deprecated\n",
                ".github/instructions/custom.instructions.md": "---\napplyTo: x\n---\nmine\n",
                "README.md": README_HDI.encode("utf-8"),
                ".gitignore": b"Data/\r\n/tools/4dlsp/",
                "Project/Sources/Methods/m.4dm": "// code\n",
            }

        make_repo(r, "HDI_Foo", [hdi_files()])
        make_repo(r, "HDI_Dry", [hdi_files()])
        make_repo(r, "HDI_LegacyTpl", [hdi_files()])
        old_wf = sh("git", "-C", str(BASE), "show", "7b20c04:.github/workflows/update-skills.yml")
        make_repo(r, "HDI_OldWf", [dict(hdi_files(), **{".github/workflows/update-skills.yml": old_wf + "\n"})])
        make_repo(r, "HDI_CustomWf", [dict(hdi_files(), **{".github/workflows/update-skills.yml": "name: mine\n"})])
        tn_cop = OLD_TN.replace("2. Never patch the PDF.\n",
                                "2. Never patch the PDF.\n3. This document uses Shift-JIS figures; keep them.\n")
        make_repo(r, "TN_Bar", [{
            ".github/copilot-instructions.md": tn_cop,
            ".github/instructions/technote-translation.instructions.md": "---\napplyTo: src/**\n---\nmy learnings\n",
            ".github/instructions/aikit.async.instructions.md": AIKIT,
            ".github/instructions/startup.instructions.md": "startup\n",
            ".github/skills/apply-glossary/SKILL.md": "glossary\n",
            ".github/skills/review-figures/SKILL.md": "figures\n",
            ".github/workflows/copilot-setup-steps.yml": OLD_SETUP,
            ".github/workflows/check.yml": "name: check\n",
            "technote.json": "{}\n",
            "README.md": "# Bar\n\nNothing to change here.\n",
            ".gitignore": ".venv/\nbuild/\n",
        }])
        make_repo(r, "Custom_Demo", [{
            ".github/copilot-instructions.md": "# 4D Project — Global Instructions\n\nRead `.github/.instructions/`.\n"
                                               "Use skill `.github/skills/4d-docs` and `.github/skills/4d-form-layout`.\n",
            ".github/.instructions/4d.instructions.md": "external\n",
            ".github/skills/4d-docs/SKILL.md": "docs\n",
            ".github/skills/4d-form-layout/SKILL.md": "layout\n",
            ".github/skills/4d-form-layout/form_layout_calc.py": "print(1)\n",
            "README.md": "# Custom\n",
        }])
        make_repo(r, "SkillCopy_Fork", [{
            ".github/copilot-instructions.md": AIKIT.replace("Step 3:", "Step three:") + "Local tweak.\n",
            "ReadMe.md": "# Fork\n",
        }])
        make_repo(r, "Webinar_Gen", [{
            ".github/instructions/startup.instructions.md": "startup\n",
            ".github/instructions/readme.branches.instructions.md": TOKEN_BRANCHES,
            ".github/instructions/css.instructions.md": CSS_A,
            ".github/instructions/notes.instructions.md": "---\napplyTo: x\n---\nAdd a Model Selection table.\n",
            "README.md": "# Gen\n\nSee [startup](.github/instructions/startup.instructions.md) and "
                         "[css](.github/instructions/css.instructions.md).\n",
        }])
        make_repo(r, "4d-plugin-sodium", [{".github/instructions/plugin-dev.instructions.md": "x\n"}])

    @classmethod
    def tearDownClass(cls):
        if not os.environ.get("KEEP_TMP"):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers
    def run_cli(self, *repos, extra=(), expect=0):
        report = self.tmp / f"report-{len(list(self.tmp.glob('report-*')))}.json"
        cmd = [sys.executable, str(SCRIPT), *repos, "--workdir", str(self.tmp / "wd"),
               "--clone-url", str(self.tmp / "remote" / "{repo}.git"),
               "--migration", str(self.tmp / "MIGRATION.json"),
               "--template", f"hdi={self.hdi_tpl}", "--template", f"technote={self.tn_tpl}",
               "--skills-url", str(self.skills), "--base", str(BASE), "--report-json", str(report), *extra]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = p.stdout.decode() + p.stderr.decode()
        self.assertEqual(p.returncode, expect, out)
        return {r["repo"].split("/")[-1]: r for r in json.loads(report.read_text())}, out

    def checkout(self, name) -> Path:
        dest = self.tmp / "check" / name
        if dest.exists():
            shutil.rmtree(dest)
        sh("git", "-c", "protocol.file.allow=always", "clone", "-q", "--recurse-submodules",
           str(self.tmp / "remote" / f"{name}.git"), str(dest))
        return dest

    def head(self, name):
        return sh("git", "--git-dir", str(self.tmp / "remote" / f"{name}.git"), "rev-parse", "main")

    # -- tests (unittest runs them in alphabetical order; the idempotence test is last on purpose)
    def test_1_hdi(self):
        reps, out = self.run_cli("HDI_Foo", extra=["--push"])
        rep = reps["HDI_Foo"]
        self.assertEqual(rep["kind"], "hdi")
        self.assertTrue(rep["status"].startswith("pushed"), out)
        wc = self.checkout("HDI_Foo")
        idir = wc / ".github/instructions"
        self.assertEqual(sorted(p.name for p in idir.iterdir()),
                         ["custom.instructions.md", "hdi.startup.instructions.md",
                          "readme.branches.instructions.md", "readme.structure.instructions.md"])
        self.assertEqual((idir / "readme.branches.instructions.md").read_text(),
                         "---\napplyTo: README.md\n---\nnew branches\n")
        agents = (wc / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith(HDI_AGENTS + "\n## Skills\n"), agents)
        self.assertIn(".agents/AGENTS.md", agents)
        self.assertEqual((wc / "CLAUDE.md").read_text(), "@AGENTS.md\n")
        self.assertEqual((wc / "README.md").read_bytes(), README_HDI_EXPECTED.encode("utf-8"))
        gi = (wc / ".gitignore").read_bytes()
        self.assertTrue(gi.startswith(b"Data/\r\n/tools/4dlsp/\r\n\r\n# Tools"), gi)
        for d in ("/tools/4dcatalog/", "/tools/4dform/", "/tools/4dlang/", "/tools/4dcli/"):
            self.assertIn(d.encode() + b"\r\n", gi)
        self.assertEqual(gi.count(b"4dlsp"), 1)
        setup = (wc / ".github/workflows/copilot-setup-steps.yml").read_text()
        self.assertIn("copilot-setup-steps:", setup)
        self.assertIn("submodules: true", setup)
        self.assertTrue((wc / ".github/workflows/update-skills.yml").exists())
        self.assertEqual(sh("git", "config", "-f", ".gitmodules", "submodule..agents.branch", cwd=wc), "dist")
        self.assertTrue((wc / ".agents/skills/4dcss/SKILL.md").exists())
        msg = sh("git", "log", "-1", "--format=%B", cwd=wc)
        self.assertIn(migrate.TRAILER, msg)
        warnings = "\n".join(rep["warnings"])
        self.assertIn("tahoe.css.instructions.md: variant", warnings)   # unknown variant saved for review
        self.assertNotIn("css.instructions.md: variant", warnings.replace("tahoe.css", ""))  # known (template history)
        self.assertIn("custom.instructions.md: not in MIGRATION.json", warnings)
        self.assertIn("link .github/instructions/: placeholder", warnings)
        self.assertEqual(len(rep["sections"]), 3, rep["sections"])
        self.assertTrue((self.tmp / "wd/_unknown_variants/HDI_Foo__tahoe.css.instructions.md").exists())

    def test_2_technote(self):
        reps, out = self.run_cli("TN_Bar", extra=["--push"])
        rep = reps["TN_Bar"]
        self.assertEqual(rep["kind"], "technote")
        wc = self.checkout("TN_Bar")
        self.assertFalse((wc / ".github/copilot-instructions.md").exists())
        self.assertFalse((wc / ".github/skills").exists())
        self.assertEqual(sorted(p.name for p in (wc / ".github/instructions").iterdir()),
                         ["technote-translation.instructions.md"])
        self.assertIn("my learnings", (wc / ".github/instructions/technote-translation.instructions.md").read_text())
        agents = (wc / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith(NEW_TN_AGENTS.rstrip("\n")))
        self.assertIn("## Repository-specific notes", agents)
        self.assertIn("3. This document uses Shift-JIS figures; keep them.", agents)
        self.assertNotIn("Phase 0", agents.split("## Repository-specific notes")[1])
        self.assertEqual((wc / "README.md").read_text(), "# Bar\n\nNothing to change here.\n")
        setup = (wc / ".github/workflows/copilot-setup-steps.yml").read_text()
        self.assertEqual(setup, OLD_SETUP.replace("actions/checkout@v4\n",
                                                  "actions/checkout@v4\n        with:\n          submodules: true\n"))
        self.assertEqual((wc / ".github/workflows/check.yml").read_text(), "name: check\n")
        self.assertTrue((wc / ".gitignore").read_text().startswith(".venv/\nbuild/\n\n# Tools"))

    def test_3_custom(self):
        reps, out = self.run_cli("Custom_Demo", extra=["--push"])
        rep = reps["Custom_Demo"]
        self.assertEqual(rep["kind"], "generic")
        wc = self.checkout("Custom_Demo")
        agents = (wc / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith("# 4D Project — Global Instructions"))
        self.assertIn("`.agents/skills/4dlang`", agents)           # 4d-docs → 4dlang (MIGRATION.json)
        self.assertIn("`.agents/skills/4d-form-layout`", agents)   # unknown → same name
        self.assertIn("## Skills", agents)
        self.assertIn("git submodule update --init .agents", agents)
        self.assertNotIn("(keep as is)", agents)
        self.assertFalse((wc / ".github/skills").exists())
        self.assertTrue((wc / ".github/.instructions/4d.instructions.md").exists())
        self.assertIn(".github/.instructions/: vendored third-party content", "\n".join(rep["warnings"]))
        self.assertIn("4d-form-layout: not in MIGRATION.json", "\n".join(rep["warnings"]))

    def test_3b_generic_token_rules(self):
        reps, out = self.run_cli("Webinar_Gen", extra=["--push"])
        rep = reps["Webinar_Gen"]
        self.assertEqual(rep["kind"], "generic")
        wc = self.checkout("Webinar_Gen")
        idir = wc / ".github/instructions"
        self.assertEqual(sorted(p.name for p in idir.iterdir()),
                         ["notes.instructions.md", "readme.branches.instructions.md"])   # startup: HDI only
        self.assertEqual((idir / "readme.branches.instructions.md").read_text(),
                         "---\napplyTo: README.md\n---\nnew branches\n")              # token-free HDI version
        self.assertIn("replaced by the HDI_Template version", "\n".join(rep["modified"]))
        self.assertIn("notes.instructions.md: still contains Copilot token/model usage rules",
                      "\n".join(rep["warnings"]))
        self.assertEqual((wc / "README.md").read_text(),
                         f"# Gen\n\nSee [`4dstartup`]({SKILLS_TREE}4dstartup) and "
                         f"[`4dcss`]({SKILLS_TREE}4dcss).\n")

    def test_3c_update_skills_workflow(self):
        reps, out = self.run_cli("HDI_OldWf", "HDI_CustomWf", extra=["--push"])
        current = (BASE / ".github/workflows/update-skills.yml").read_text()
        self.assertIn("updated to the 4d-template version", "\n".join(reps["HDI_OldWf"]["modified"]))
        self.assertEqual((self.checkout("HDI_OldWf") / ".github/workflows/update-skills.yml").read_text(), current)
        self.assertIn("update-skills.yml: customised", "\n".join(reps["HDI_CustomWf"]["warnings"]))
        self.assertEqual((self.checkout("HDI_CustomWf") / ".github/workflows/update-skills.yml").read_text(),
                         "name: mine\n")
        reps, out = self.run_cli("HDI_OldWf", extra=["--push"])
        self.assertTrue(reps["HDI_OldWf"]["status"].startswith("no-op"), reps["HDI_OldWf"])

    def test_4_skill_copy(self):
        reps, out = self.run_cli("SkillCopy_Fork", extra=["--push"])
        rep = reps["SkillCopy_Fork"]
        wc = self.checkout("SkillCopy_Fork")
        agents = (wc / "AGENTS.md").read_text()
        self.assertTrue(agents.startswith("# SkillCopy_Fork\n"))
        self.assertNotIn("AI Kit async", agents)
        self.assertIn("aikit.async.instructions.md", "\n".join(rep["notes"]))
        self.assertTrue((self.tmp / "wd/_unknown_variants/SkillCopy_Fork__copilot-instructions.md").exists())
        self.assertEqual((wc / "ReadMe.md").read_text(), "# Fork\n")

    def test_5_refused_and_legacy_template(self):
        before = self.head("4d-plugin-sodium")
        reps, _ = self.run_cli("4d-plugin-sodium", extra=["--push"])
        self.assertEqual(reps["4d-plugin-sodium"]["status"], "refused")
        self.assertEqual(self.head("4d-plugin-sodium"), before)
        before = self.head("HDI_LegacyTpl")
        reps, out = self.run_cli("HDI_LegacyTpl", extra=["--push", "--template", f"hdi={self.legacy_tpl}"], expect=1)
        self.assertEqual(reps["HDI_LegacyTpl"]["status"], "FAILED")
        self.assertIn("not migrated yet", out)
        self.assertEqual(self.head("HDI_LegacyTpl"), before)

    def test_6_dry_run(self):
        before = self.head("HDI_Dry")
        reps, out = self.run_cli("HDI_Dry", extra=["--dry-run"])
        self.assertTrue(reps["HDI_Dry"]["status"].startswith("dry-run"))
        self.assertIn("AGENTS.md", reps["HDI_Dry"]["added"])
        self.assertEqual(self.head("HDI_Dry"), before)

    def test_7_idempotent(self):
        for name in ("HDI_Foo", "TN_Bar", "Custom_Demo", "SkillCopy_Fork", "Webinar_Gen"):
            if self.head(name) == sh("git", "--git-dir", str(self.tmp / "work" / name / ".git"), "rev-parse", "HEAD"):
                self.run_cli(name, extra=["--push"])  # make the test independent of ordering
        heads = {n: self.head(n) for n in ("HDI_Foo", "TN_Bar", "Custom_Demo", "SkillCopy_Fork", "Webinar_Gen")}
        reps, out = self.run_cli(*heads, extra=["--push"])
        for name, h in heads.items():
            self.assertTrue(reps[name]["status"].startswith("no-op"), (name, reps[name]))
            self.assertEqual(self.head(name), h)


class UnitTests(unittest.TestCase):
    def test_mapping_shapes(self):
        m = migrate.parse_mapping([{"old": ".github/instructions/css.instructions.md", "status": "moved",
                                    "new": SKILLS_TREE + "4dcss"}])
        self.assertEqual(m.instructions["css.instructions.md"].status, "skill")
        self.assertEqual(m.instructions["css.instructions.md"].skill, "4dcss")
        m = migrate.parse_mapping({"menu.instructions.md": SKILLS_TREE + "4dmenus/SKILL.md",
                                   "readme.hdi.instructions.md": "deleted"})
        self.assertEqual(m.instructions["menu.instructions.md"].skill, "4dmenus")
        self.assertEqual(m.instructions["readme.hdi.instructions.md"].status, "deleted")

    def test_real_mapping(self):
        real = BASE / ".agents/MIGRATION.json"   # the published file (dist branch of miyako/skills)
        if not real.exists():
            self.skipTest("run `git submodule update --init .agents` first")
        m = migrate.load_mapping(str(real))
        st = m.instructions["startup.instructions.md"]
        self.assertEqual((st.status, st.skill, st.url), ("instruction", "4dstartup", None))
        self.assertEqual(st.new_name("startup.instructions.md"), "hdi.startup.instructions.md")
        self.assertEqual(m.instructions["css.instructions.md"].skill, "4dcss")
        self.assertEqual(m.instructions["readme.usage.guidance.instructions.md"].status, "deleted")
        self.assertEqual(m.skills["apply-glossary"].skill, "technote-apply-glossary")
        for e in m.instructions.values():
            self.assertIn(e.status, ("skill", "instruction", "deleted"), e)
            if e.status == "skill":
                self.assertTrue((BASE / ".agents/skills" / e.skill / "SKILL.md").exists(), e)
        for e in m.skills.values():
            self.assertTrue((BASE / ".agents/skills" / e.skill / "SKILL.md").exists(), e)

    def test_inventory(self):
        d = Path(tempfile.mkdtemp(prefix="inv-"))
        try:
            (d / "blobs").mkdir()
            (d / "blobs/aaa").write_text("## Copilot Token Usage\n")
            (d / "scan.tsv").write_text("HDI_A\tfalse\nDemo_X\ttrue\nTN_C\tfalse\n4d-plugin-x\tfalse\n"
                                        "HDI_Template\tfalse\nEmpty\tfalse\n")
            (d / "files.tsv").write_text(
                "HDI_A\t.github/instructions/css.instructions.md\tc1\n"
                "HDI_A\t.github/instructions/readme.branches.instructions.md\taaa\n"
                "Demo_X\t.github/copilot-instructions.md\tzzz\n"
                "TN_C\t.github/instructions/technote-translation.instructions.md\tt1\n"
                "TN_C\t.github/skills/apply-glossary/SKILL.md\tg1\n"
                "4d-plugin-x\t.github/instructions/plugin-dev.instructions.md\tp1\n")
            md = migrate.inventory(d, migrate.parse_mapping(MIGRATION),
                                   {"HDI_A": {"status": "pushed", "commit": "abc1234", "warnings": ["w"]}},
                                   migrate.TEMPLATES)
            rows = {l.split("|")[1].strip().split("]")[0][1:]: [c.strip() for c in l.split("|")[2:-1]]
                    for l in md.splitlines() if l.startswith("| [")}
            self.assertEqual(rows["HDI_A"][:3], ["", "hdi", "migrate (hdi)"])
            self.assertIn("remove 1 instruction copies", rows["HDI_A"][3])
            self.assertIn("has token rules", rows["HDI_A"][3])
            self.assertEqual(rows["HDI_A"][4], "pushed (1 warnings)")
            self.assertIn("abc1234", rows["HDI_A"][5])
            self.assertEqual(rows["Demo_X"][:3], ["yes", "generic", "migrate (generic)"])
            self.assertEqual(rows["TN_C"][1], "technote")
            self.assertIn("keep own", rows["TN_C"][3])
            self.assertEqual(rows["4d-plugin-x"][2], "deferred (do not touch)")
            self.assertTrue(rows["HDI_Template"][2].startswith("core"))
            self.assertEqual(rows["Empty"][2], "nothing to migrate")
        finally:
            shutil.rmtree(d)

    def test_branches_table(self):
        text = ("## Modernisation notes\n\nConverted from the binary.\n\n| Branch | Description | Instructions |\n"
                "|--------|-------------|--------------|\n| a | b | c |\n\n```md\n| Branch | Description | Instructions |\n"
                "```\n\n## Other\n\n| Branch | Description | Instructions |\n")
        new, done, warns = migrate.normalise_branches_table(text)
        self.assertEqual(new, text.replace("| Instructions |\n|--------|-------------|--------------|",
                                           "| Guidance |\n|--------|-------------|----------|", 1))
        self.assertEqual(warns, ["branches table: intro sentence is not the standard one; kept as is"])
        self.assertEqual(migrate.normalise_branches_table(new), (new, [], []))   # idempotent

    def test_strip_sections_keeps_bytes(self):
        text = "# T\n\n## A\n\nx\n\n## Copilot Token Usage\n\n|a|\n\n### sub\n\ny\n\n## B\n\nz"
        out, st = migrate.strip_sections(text)
        self.assertEqual(out, "# T\n\n## A\n\nx\n\n## B\n\nz")
        self.assertEqual(st, ["## Copilot Token Usage"])
        same = "# T\n\n## Usage\n\n## Model\n\n```\n## Copilot Token Usage\n```\n"
        self.assertEqual(migrate.strip_sections(same), (same, []))

    def test_checkout_merge(self):
        y = "jobs:\n  copilot-setup-steps:\n    steps:\n      - name: c\n        uses: actions/checkout@v4\n" \
            "        with:\n          fetch-depth: 0\n      - run: x\n"
        out, msg = migrate.ensure_checkout_submodules(y)
        self.assertIn("        with:\n          submodules: true\n          fetch-depth: 0\n", out)
        self.assertEqual(migrate.ensure_checkout_submodules(out)[0], out)
        self.assertTrue(migrate.ensure_checkout_submodules("jobs:\n  build:\n")[1].startswith("warning"))
        noeol = "jobs:\n  copilot-setup-steps:\n    steps:\n      - name: c\n        uses: actions/checkout@v4"
        out, _ = migrate.ensure_checkout_submodules(noeol)
        self.assertEqual(out, noeol + "\n        with:\n          submodules: true\n")
        self.assertEqual(migrate.ensure_checkout_submodules(out)[0], out)
        inline = "jobs:\n  copilot-setup-steps:\n    steps:\n      - uses: actions/checkout@v4\n" \
                 "        with: { fetch-depth: 0 }\n"
        out, msg = migrate.ensure_checkout_submodules(inline)
        self.assertEqual(out, inline)
        self.assertTrue(msg.startswith("warning"))

    def test_keep_as_is_sections(self):
        s = migrate.keep_as_is_sections((BASE / "AGENTS.md").read_text())
        self.assertIn("## Skills\n", s)
        self.assertIn("## Setup\n", s)
        self.assertNotIn("TODO", s)
        self.assertNotIn("<!--", s)


if __name__ == "__main__":
    unittest.main()
