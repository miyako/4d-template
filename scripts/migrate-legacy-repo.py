#!/usr/bin/env python3
"""Migrate a repository from the legacy agent layout to the miyako/4d-template layout.

Legacy layout (copied into every repository):
    .github/copilot-instructions.md
    .github/instructions/*.instructions.md   (copies of generic know-how + a few repo rules)
    .github/skills/<name>/                   (copies of generic skills)

Target layout (see README.md of miyako/4d-template):
    AGENTS.md, CLAUDE.md, .agents (submodule miyako/skills@dist), .gitmodules,
    .github/instructions/ (repo-specific, applyTo only), copilot-setup-steps.yml (submodules: true),
    update-skills.yml, .gitignore with the provisioned tool folders.

The mapping of legacy instruction files comes from `4d-skills/MIGRATION.json` in miyako/skills.
Repository-specific instruction files and AGENTS.md come from the relevant template
(HDI_Template or 4d-technote-localisation-template), fetched at run time.

Python 3.8+, standard library only. Needs `git` (and `gh` only as a fallback to fetch MIGRATION.json).
Run with --help for usage. Tests: scripts/tests/test_migrate.py
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

TRAILER = "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
DEFAULT_OWNER = "miyako"
DEFAULT_CLONE_URL = "https://github.com/{owner}/{repo}.git"
DEFAULT_MIGRATION = "https://raw.githubusercontent.com/miyako/skills/main/4d-skills/MIGRATION.json"
DEFAULT_SKILLS_URL = "https://github.com/miyako/skills.git"
DEFAULT_SKILLS_BRANCH = "dist"
SUBMODULE_PATH = ".agents"
TEMPLATES = {
    "hdi": "https://github.com/miyako/HDI_Template.git",
    "technote": "https://github.com/miyako/4d-technote-localisation-template.git",
}
# Instruction files a derived repository keeps as its own (never replaced by the template version).
KEEP_OWN = {
    "technote": {"technote-translation.instructions.md"},
    "hdi": set(),
    "generic": set(),
}
# Files of .github/instructions/ that are template documentation, never copied into derived repos.
TEMPLATE_ONLY_INSTRUCTIONS = {"README.md", "example.instructions.md"}
# Legacy .github/skills/<name> known to have moved to miyako/skills (used when MIGRATION.json has no entry).
KNOWN_MOVED_SKILLS = {
    "apply-glossary", "review-figures", "localise-demo-data", "localise-4d-project", "release-technote",
    "4d-docs", "4d-form-layout",
}
TOOL_DIRS = ["/tools/4dcatalog/", "/tools/4dform/", "/tools/4dlsp/", "/tools/4dlang/"]
# Repositories this tool must not touch (DESIGN.md "Do NOT touch" + core repos). Override with --force.
DENYLIST = {
    "4d-plugin-tutorial", "4d-plugin-4d-for-oci", "4d-plugin-factur-x-qpdf", "4d-plugin-factur-x-podofo",
    "4d-plugin-sodium", "4d-form-tutorial", "4d-catalog-tutorial", "4d-classic-to-oop", "bonsai",
    "4d-static-docs", "4d-skills-test-claude", "4d-skills-test-gemini", "4d-catalog-diagram",
    "skills", "4d-template", "hdi_template", "4d-technote-localisation-template",
}
# README headings whose whole section is removed (Copilot model / mode / token / session content).
STRIP_HEADING_PATTERNS = [
    r"\bcopilot\b.*\b(token|tokens|usage|model|models|mode|modes|session|sessions)\b",
    r"\btoken\s+usage\b",
    r"\bmodel\s+(selection|choice|assessment)\b",
    r"\bmode\s+(selection|choice|guidance)\b",
    r"\busage\s+guidance\b",
    r"copilot.*(トークン|モデル|モード|セッション)",
]
SIMILARITY_THRESHOLD = 0.6

STATUS_SKILL, STATUS_DELETED, STATUS_INSTRUCTION, STATUS_UNKNOWN = "skill", "deleted", "instruction", "unknown"
_STATUS_ALIASES = {
    STATUS_SKILL: {"skill", "skills", "moved", "merged", "folded"},
    STATUS_DELETED: {"deleted", "delete", "removed", "remove", "obsolete", "drop", "dropped"},
    STATUS_INSTRUCTION: {"instruction", "instructions", "repo", "repo-specific", "kept", "keep", "stays",
                         "template", "local"},
}


class MigrationError(Exception):
    pass


# --------------------------------------------------------------------------------------------- utils

def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True, capture: bool = True,
        input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, input=input_bytes,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        raise MigrationError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{err}")
    return proc


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", *args], cwd=repo, check=check).stdout.decode("utf-8", "surrogateescape").strip()


def is_local(url: str) -> bool:
    return url.startswith("file://") or not re.match(r"^[a-z][a-z0-9+.-]*://|^[^/]+@[^/]+:", url)


def git_url_args(url: str) -> List[str]:
    # Local fixtures (tests) need file transport for submodules.
    return ["-c", "protocol.file.allow=always"] if is_local(url) else []


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", "surrogateescape")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8", "surrogateescape"))


def split_lines(text: str) -> List[str]:
    """Split on \\n only, keeping line endings (str.splitlines would also split on \\x0c, \\u2028…)."""
    if not text:
        return []
    parts = re.split(r"(?<=\n)", text)
    return [p for p in parts if p != ""]


def eol_of(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def normalise(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip() + "\n"


def line_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalise(a).split("\n"), normalise(b).split("\n"),
                                   autojunk=False).ratio()


def _strip_git(url: str) -> str:
    url = url.strip().rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def blob_sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ------------------------------------------------------------------------------------------ mapping

@dataclass
class Entry:
    name: str
    status: str
    url: Optional[str] = None
    skill: Optional[str] = None
    templates: Optional[List[str]] = None


@dataclass
class Mapping:
    instructions: Dict[str, Entry] = field(default_factory=dict)
    skills: Dict[str, Entry] = field(default_factory=dict)
    variants: Dict[str, str] = field(default_factory=dict)  # blob sha -> filename (optional)


def _norm_status(value) -> str:
    v = str(value or "").strip().lower()
    for status, aliases in _STATUS_ALIASES.items():
        if v in aliases:
            return status
    return STATUS_UNKNOWN


def _entries(obj, name_keys=("old", "file", "from", "source", "name", "path")) -> Iterable[Tuple[str, dict]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                yield k, v
            elif isinstance(v, str):  # "file": "url"  or "file": "deleted"
                yield k, ({"status": v} if _norm_status(v) != STATUS_UNKNOWN else {"status": "skill", "url": v})
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict):
                key = next((v[k] for k in name_keys if k in v and isinstance(v[k], str)), None)
                if key:
                    yield key, v


def _make_entry(key: str, raw: dict) -> Entry:
    name = key.rstrip("/").split("/")[-1]
    url = next((raw[k] for k in ("url", "new", "to", "target", "location", "href") if raw.get(k)), None)
    skill = raw.get("skill")
    if not skill and url:
        m = re.search(r"/skills/([^/#?]+)/?(?:SKILL\.md)?(?:[#?].*)?$", url)
        if m:
            skill = m.group(1)
    status = _norm_status(raw.get("status"))
    if status == STATUS_UNKNOWN and skill:
        status = STATUS_SKILL
    templates = raw.get("templates") or raw.get("template")
    if isinstance(templates, str):
        templates = [templates]
    return Entry(name=name, status=status, url=url, skill=skill, templates=templates)


def parse_mapping(data) -> Mapping:
    """Accepts several shapes so the producer of MIGRATION.json is not constrained:
    {"instructions": {...} | [...], "skills": {...} | [...]}, a flat {filename: entry}, or a list of entries.
    Entries: {"status": "skill|deleted|instruction", "url": "...", "skill": "4dcss", "templates": ["HDI_Template"]}.
    """
    mapping = Mapping()
    if isinstance(data, dict) and any(k in data for k in ("instructions", "skills", "files", "mapping")):
        instr = data.get("instructions", data.get("files", data.get("mapping", {})))
        skills = data.get("skills", {})
        for sha, fname in (data.get("variants") or {}).items():
            mapping.variants[sha] = fname
    else:
        instr, skills = data, {}
    for key, raw in _entries(instr):
        e = _make_entry(key, raw)
        if "skills/" in key and not key.endswith(".md"):
            mapping.skills[e.name] = e
        else:
            mapping.instructions[e.name] = e
        for sha in raw.get("variants", []) or []:
            mapping.variants[sha] = e.name
    for key, raw in _entries(skills):
        e = _make_entry(key, raw)
        if e.status == STATUS_UNKNOWN:
            e.status = STATUS_SKILL
        mapping.skills[e.name] = e
    return mapping


def load_mapping(source: str) -> Mapping:
    if os.path.exists(source):
        return parse_mapping(json.loads(Path(source).read_text("utf-8")))
    try:
        with urllib.request.urlopen(source, timeout=30) as resp:
            return parse_mapping(json.loads(resp.read().decode("utf-8")))
    except (urllib.error.URLError, ValueError) as exc:
        m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", source)
        if not m or not shutil.which("gh"):
            raise MigrationError(f"cannot load MIGRATION.json from {source}: {exc}")
        owner, repo, ref, path = m.groups()
        proc = run(["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}",
                    "-H", "Accept: application/vnd.github.raw"], check=False)
        if proc.returncode != 0:
            raise MigrationError(f"cannot load MIGRATION.json from {source} (not published yet?)")
        return parse_mapping(json.loads(proc.stdout.decode("utf-8")))


# ----------------------------------------------------------------------------------------- variants

@dataclass
class Variant:
    name: str     # basename of the file (copilot-instructions.md, css.instructions.md …)
    sha: str
    text: str
    origin: str


class VariantPool:
    """Known historical contents of legacy files (from template history and the optional survey)."""

    def __init__(self):
        self.items: Dict[str, Variant] = {}

    def add(self, name: str, data: bytes, origin: str) -> None:
        sha = blob_sha(data)
        if sha not in self.items:
            self.items[sha] = Variant(name, sha, data.decode("utf-8", "surrogateescape"), origin)

    def by_name(self, name: str) -> List[Variant]:
        return [v for v in self.items.values() if v.name == name]

    def knows(self, sha: str) -> bool:
        return sha in self.items

    def add_survey(self, survey: Path) -> None:
        tsv = survey / "uniq_blobs.tsv"
        if not tsv.exists():
            raise MigrationError(f"--survey: {tsv} not found")
        for line in tsv.read_text("utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                blob = survey / "blobs" / parts[2]
                if blob.exists():
                    self.add(parts[1].split("/")[-1], blob.read_bytes(), f"survey:{parts[0]}")

    def add_git_history(self, repo: Path, origin: str) -> None:
        """All historical blobs of .github/copilot-instructions.md and .github/instructions/* in repo."""
        log = git(repo, "log", "--all", "--format=%H", "--", ".github/copilot-instructions.md",
                  ".github/instructions", ".github/skills")
        seen = set()
        for commit in log.split():
            tree = git(repo, "ls-tree", "-r", commit, "--", ".github/copilot-instructions.md",
                       ".github/instructions", ".github/skills")
            for row in tree.splitlines():
                meta, path = row.split("\t", 1)
                sha = meta.split()[2]
                if sha in seen:
                    continue
                seen.add(sha)
                data = run(["git", "cat-file", "blob", sha], cwd=repo).stdout
                name = path.split("/")[-1]
                if path.startswith(".github/skills/"):
                    name = "skill:" + path.split("/")[2] + "/" + name
                self.add(name, data, f"{origin}@{commit[:7]}")


# ------------------------------------------------------------------------------------------- report

@dataclass
class Report:
    repo: str
    kind: str = ""
    removed: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    sections: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    status: str = ""
    commit: str = ""

    def as_text(self) -> str:
        out = [f"== {self.repo}  (kind: {self.kind})  → {self.status}" + (f" {self.commit}" if self.commit else "")]
        for title, items in (("removed", self.removed), ("added", self.added), ("modified", self.modified),
                             ("kept", self.kept), ("README links rewritten", self.links),
                             ("README sections stripped", self.sections), ("notes", self.notes),
                             ("WARNINGS", self.warnings)):
            if items:
                out.append(f"  {title}:")
                out.extend(f"    - {i}" for i in items)
        return "\n".join(out)


# ------------------------------------------------------------------------------------------ helpers

def keep_as_is_sections(agents_md: str) -> str:
    """The '(keep as is)' sections of 4d-template's AGENTS.md, marker removed, comments stripped."""
    text = re.sub(r"<!--.*?-->\n?", "", agents_md, flags=re.S)
    lines = split_lines(text)
    out: List[str] = []
    keep = False
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if m and len(m.group(1)) == 2:
            keep = "(keep as is)" in m.group(2)
            if keep:
                line = line.replace(" (keep as is)", "")
        elif m and len(m.group(1)) == 1:
            keep = False
        if keep:
            out.append(line)
    return "".join(out).strip("\n") + "\n"


def mentions_skills(text: str) -> bool:
    return ".agents/AGENTS.md" in text


def rewrite_paths(text: str, mapping: Mapping) -> str:
    def skill_repl(m):
        name = m.group(1)
        e = mapping.skills.get(name)
        return f".agents/skills/{(e.skill if e and e.skill else name)}"
    text = re.sub(r"\.github/skills/([A-Za-z0-9_.-]+)", skill_repl, text)

    def instr_repl(m):
        e = mapping.instructions.get(m.group(2))
        if e and e.status == STATUS_SKILL and e.skill:
            return f".agents/skills/{e.skill}/SKILL.md"
        return m.group(0)
    return re.sub(r"\.github/(copilot/)?instructions/([A-Za-z0-9_.-]+\.md)", instr_repl, text)


def added_hunks(base: str, repo: str) -> List[Tuple[str, List[str]]]:
    """Lines of `repo` added or changed relative to `base`, each with the nearest preceding heading."""
    a = normalise(base).split("\n")
    b = normalise(repo).split("\n")
    hunks = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag not in ("insert", "replace"):
            continue
        chunk = b[j1:j2]
        if not any(line.strip() for line in chunk):
            continue
        heading = next((b[k] for k in range(j1 - 1, -1, -1) if re.match(r"^#{1,6}\s", b[k])), "")
        hunks.append((heading, chunk))
    return hunks


# --------------------------------------------------------------------------------------- the migrator

@dataclass
class Options:
    owner: str = DEFAULT_OWNER
    kind: str = "auto"
    dry_run: bool = False
    push: bool = False
    workdir: Path = Path(tempfile.gettempdir()) / "migrate-legacy-repo"
    clone_url: str = DEFAULT_CLONE_URL
    migration: str = DEFAULT_MIGRATION
    templates: Dict[str, str] = field(default_factory=lambda: dict(TEMPLATES))
    skills_url: str = DEFAULT_SKILLS_URL
    skills_branch: str = DEFAULT_SKILLS_BRANCH
    base: Path = Path(__file__).resolve().parents[1]
    survey: Optional[Path] = None
    force: bool = False
    allow_legacy_template: bool = False
    no_submodule: bool = False


class Migrator:
    def __init__(self, opts: Options):
        self.o = opts
        self.o.workdir.mkdir(parents=True, exist_ok=True)
        self._mapping: Optional[Mapping] = None
        self._templates: Dict[str, Path] = {}
        self._pool: Optional[VariantPool] = None
        self.base_agents = read_text(self.o.base / "AGENTS.md")
        self.base_claude = read_text(self.o.base / "CLAUDE.md")
        self.base_setup = read_text(self.o.base / ".github/workflows/copilot-setup-steps.yml")
        self.base_update = read_text(self.o.base / ".github/workflows/update-skills.yml")

    # --- lazily loaded shared inputs
    @property
    def mapping(self) -> Mapping:
        if self._mapping is None:
            self._mapping = load_mapping(self.o.migration)
        return self._mapping

    def template(self, kind: str) -> Optional[Path]:
        if kind not in self.o.templates:
            return None
        if kind not in self._templates:
            url = self.o.templates[kind]
            dest = self.o.workdir / "_templates" / kind
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            args = ["git", "clone", "-q"]
            if not is_local(url):
                args.append("--filter=blob:none")
            run(args + [url, str(dest)])
            if not self.o.allow_legacy_template:
                if not (dest / "AGENTS.md").exists() or (dest / ".github/copilot-instructions.md").exists():
                    raise MigrationError(
                        f"template for '{kind}' ({url}) is not migrated yet (needs AGENTS.md and no "
                        f".github/copilot-instructions.md). Migrate the template first, or pass "
                        f"--allow-legacy-template.")
            self._templates[kind] = dest
        return self._templates[kind]

    @property
    def pool(self) -> VariantPool:
        if self._pool is None:
            pool = VariantPool()
            for kind in self.o.templates:
                try:
                    t = self.template(kind)
                except MigrationError:
                    continue
                if t:
                    pool.add_git_history(t, kind)
            if self.o.survey:
                pool.add_survey(self.o.survey)
            self._pool = pool
        return self._pool

    # --- entry point
    def migrate(self, name: str) -> Report:
        owner, repo = (name.split("/", 1) if "/" in name else (self.o.owner, name))
        rep = Report(repo=f"{owner}/{repo}")
        if repo.lower() in DENYLIST and not self.o.force:
            rep.status = "refused"
            rep.warnings.append("repository is on the do-not-touch list (use --force to override)")
            return rep
        wc = self.o.workdir / repo
        if wc.exists():
            shutil.rmtree(wc)
        url = self.o.clone_url.format(owner=owner, repo=repo)
        run(["git", "clone", "-q", "--depth", "1", url, str(wc)])
        branch = git(wc, "rev-parse", "--abbrev-ref", "HEAD")

        kind = self.detect_kind(wc, repo) if self.o.kind == "auto" else self.o.kind
        rep.kind = kind
        tpl = self.template(kind) if kind in ("hdi", "technote") else None

        self.step_instructions(wc, kind, tpl, rep)
        self.step_skills(wc, rep)
        self.step_agents(wc, repo, kind, tpl, rep)
        self.step_claude(wc, rep)
        self.step_gitignore(wc, rep)
        self.step_setup_steps(wc, tpl, rep)
        self.step_update_skills(wc, rep)
        self.step_readme(wc, rep)
        self.step_submodule(wc, rep)

        git(wc, "add", "-A")
        if not git(wc, "status", "--porcelain"):
            rep.status = "no-op (already migrated)"
            return rep
        if self.o.dry_run:
            rep.status = "dry-run (changes not committed)"
            rep.notes.append("diff --stat:\n      " +
                             git(wc, "diff", "--cached", "--stat").replace("\n", "\n      "))
            return rep
        message = self.commit_message(rep)
        run(["git", "commit", "-q", "-F", "-"], cwd=wc, input_bytes=message.encode("utf-8"))
        rep.commit = git(wc, "rev-parse", "--short", "HEAD")
        if self.o.push:
            run(["git", "push", "-q", "origin", f"HEAD:{branch}"], cwd=wc)
            rep.status = f"pushed to {branch}"
        else:
            rep.status = f"committed locally in {wc} (not pushed; use --push)"
        return rep

    @staticmethod
    def detect_kind(wc: Path, repo: str) -> str:
        gh = wc / ".github"
        if ((wc / "technote.json").exists() or (gh / "instructions/technote-translation.instructions.md").exists()
                or (gh / "skills/release-technote").exists() or (gh / "templates/README.technote.md").exists()):
            return "technote"
        if (repo.lower().startswith("hdi_") or (gh / "instructions/readme.structure.instructions.md").exists()
                or (gh / "instructions/readme.hdi.instructions.md").exists()):
            return "hdi"
        return "generic"

    # --- steps
    def step_instructions(self, wc: Path, kind: str, tpl: Optional[Path], rep: Report) -> None:
        idir = wc / ".github/instructions"
        tdir = tpl / ".github/instructions" if tpl else None
        tpl_files = {p.name: p for p in tdir.iterdir()
                     if p.is_file() and p.name.endswith(".instructions.md")
                     and p.name not in TEMPLATE_ONLY_INSTRUCTIONS} if tdir and tdir.is_dir() else {}
        tpl_name = Path(self.o.templates.get(kind, "")).name.replace(".git", "")
        present = sorted(p for p in idir.iterdir() if p.is_file()) if idir.is_dir() else []
        for path in present:
            rel = f".github/instructions/{path.name}"
            if not path.name.endswith(".md") or path.name == "README.md":
                continue
            entry = self.mapping.instructions.get(path.name)
            if path.name in KEEP_OWN.get(kind, set()):
                rep.kept.append(f"{rel} (repository's own version)")
                continue
            if path.name in tpl_files:  # the template ships it: keep the derived repo in sync with it
                new = tpl_files[path.name].read_bytes()
                if path.read_bytes() != new:
                    path.write_bytes(new)
                    rep.modified.append(f"{rel} (replaced by the {tpl_name} version)")
                continue
            if entry and entry.status in (STATUS_SKILL, STATUS_DELETED):
                if entry.status == STATUS_SKILL:
                    self._check_variant(path, rep)
                path.unlink()
                where = entry.skill or entry.url or "miyako/skills"
                rep.removed.append(f"{rel} ({'moved to ' + where if entry.status == STATUS_SKILL else 'deleted'})")
                continue
            if entry and entry.status == STATUS_INSTRUCTION:
                if entry.templates and tpl and not any(t.lower() == tpl_name.lower() for t in entry.templates):
                    path.unlink()
                    rep.removed.append(f"{rel} (repo-specific to {', '.join(entry.templates)} only)")
                    continue
                rep.kept.append(rel)
                if tpl:
                    rep.warnings.append(f"{rel}: repo-specific but absent from {tpl_name}; kept as is")
                continue
            rep.kept.append(rel)
            rep.warnings.append(f"{rel}: not in MIGRATION.json; kept, review by hand")
        for name, src in sorted(tpl_files.items()):
            dest = idir / name
            if not dest.exists():
                write_text(dest, read_text(src))
                rep.added.append(f".github/instructions/{name} (from {tpl_name})")
        if idir.is_dir() and not any(idir.iterdir()):
            idir.rmdir()

    def _check_variant(self, path: Path, rep: Report) -> None:
        data = path.read_bytes()
        sha = blob_sha(data)
        if self.pool.knows(sha) or sha in self.mapping.variants:
            return
        keep = self.o.workdir / "_unknown_variants" / f"{rep.repo.split('/')[-1]}__{path.name}"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(data)
        rep.warnings.append(f".github/instructions/{path.name}: variant {sha[:7]} unknown to the survey/templates; "
                            f"copy saved to {keep} — check it for lessons missing from the skill")

    def step_skills(self, wc: Path, rep: Report) -> None:
        sdir = wc / ".github/skills"
        if not sdir.is_dir():
            return
        for d in sorted(p for p in sdir.iterdir() if p.is_dir()):
            e = self.mapping.skills.get(d.name)
            if e and e.status == STATUS_INSTRUCTION:
                rep.kept.append(f".github/skills/{d.name}")
                continue
            if e or d.name in KNOWN_MOVED_SKILLS:
                shutil.rmtree(d)
                target = (e.skill or e.url) if e else None
                rep.removed.append(f".github/skills/{d.name}/ (moved to {target or 'miyako/skills'})")
                if not e:
                    rep.warnings.append(f".github/skills/{d.name}: not in MIGRATION.json; removed as a known "
                                        f"legacy skill")
            else:
                rep.kept.append(f".github/skills/{d.name}")
                rep.warnings.append(f".github/skills/{d.name}: unknown skill; kept, move it to miyako/skills")
        if not any(sdir.iterdir()):
            sdir.rmdir()

    def _classify_old(self, old: str) -> Tuple[str, Optional[Variant], float]:
        """pristine | modified | skill-copy | custom, with the closest variant."""
        tpl_names = {f"survey:{_strip_git(u).split('/')[-1]}" for u in self.o.templates.values()}
        # Only variants that come from a template (its history, or the survey of the template repo itself)
        # count as "the template the repository was created from".
        tpl_variants = [v for v in self.pool.by_name("copilot-instructions.md")
                        if v.origin.split("@")[0] in self.o.templates or v.origin in tpl_names]
        sha = blob_sha(old.encode("utf-8", "surrogateescape"))
        for v in tpl_variants:
            if v.sha == sha or normalise(v.text) == normalise(old):
                return "pristine", v, 1.0
        best: Tuple[float, Optional[Variant]] = (0.0, None)
        for v in tpl_variants:
            r = line_ratio(v.text, old)
            if r > best[0]:
                best = (r, v)
        if best[1] is not None and best[0] >= SIMILARITY_THRESHOLD:
            return "modified", best[1], best[0]
        skill_names = {n for n, e in self.mapping.instructions.items() if e.status in (STATUS_SKILL, STATUS_DELETED)}
        best = (0.0, None)
        for v in self.pool.items.values():
            if v.name in skill_names:
                r = line_ratio(v.text, old)
                if r > best[0]:
                    best = (r, v)
        if best[1] is not None and best[0] >= SIMILARITY_THRESHOLD:
            return "skill-copy", best[1], best[0]
        return "custom", None, 0.0

    def step_agents(self, wc: Path, repo: str, kind: str, tpl: Optional[Path], rep: Report) -> None:
        old_path = wc / ".github/copilot-instructions.md"
        agents = wc / "AGENTS.md"
        old = read_text(old_path) if old_path.exists() else None
        if tpl and (tpl / "AGENTS.md").exists():
            base = read_text(tpl / "AGENTS.md").replace("{{REPO_NAME}}", repo)
            base_origin = _strip_git(self.o.templates[kind]).split("/")[-1]
        else:
            base = (f"# {repo}\n\n<!-- Describe what this repository is and its repository-specific rules here. -->\n\n"
                    + keep_as_is_sections(self.base_agents))
            base_origin = "generic"
        skills_block = keep_as_is_sections(self.base_agents)

        if agents.exists():
            text = read_text(agents)
            new = text
            if old is not None:
                cls, var, ratio = self._classify_old(old)
                if cls in ("custom", "modified"):
                    body = old if cls == "custom" else self._hunks_text(var.text, old)
                    new = new.rstrip("\n") + "\n\n## Migrated from .github/copilot-instructions.md\n\n" + \
                        rewrite_paths(body, self.mapping).strip("\n") + "\n"
                    rep.notes.append(f"copilot-instructions.md ({cls}) appended to the existing AGENTS.md")
                else:
                    rep.notes.append(f"copilot-instructions.md was {cls} ({var.origin if var else ''}); dropped")
            if not mentions_skills(new):
                new = new.rstrip("\n") + "\n\n" + skills_block
                rep.notes.append("AGENTS.md: appended the Skills / Setup / Instructions vs skills / README sections")
            if new != text:
                write_text(agents, new)
                rep.modified.append("AGENTS.md")
        else:
            if old is None:
                text = base
                rep.notes.append(f"AGENTS.md from {base_origin}")
            else:
                cls, var, ratio = self._classify_old(old)
                if cls == "modified" and base_origin == "generic":
                    cls = "custom"  # no template AGENTS.md to merge the differences into
                if cls == "pristine":
                    text = base
                    rep.notes.append(f"copilot-instructions.md was the unmodified template ({var.origin}); "
                                     f"AGENTS.md from {base_origin}")
                elif cls == "modified":
                    text = base.rstrip("\n") + "\n\n" + self._hunks_text(var.text, old)
                    rep.notes.append(f"copilot-instructions.md differed from the template ({var.origin}, "
                                     f"similarity {ratio:.2f}); repository additions kept at the end of AGENTS.md")
                    rep.warnings.append("AGENTS.md: review the 'Repository-specific notes' section and merge it")
                elif cls == "skill-copy":
                    text = base
                    saved = self.o.workdir / "_unknown_variants" / f"{repo}__copilot-instructions.md"
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    saved.write_bytes(old.encode("utf-8", "surrogateescape"))
                    rep.notes.append(f"copilot-instructions.md was a copy of {var.name} ({var.origin}, similarity "
                                     f"{ratio:.2f}) which moved to a skill; AGENTS.md from {base_origin}")
                    rep.warnings.append(f"copilot-instructions.md dropped as a skill copy; saved to {saved} — "
                                        f"check it for lessons missing from the skill")
                else:
                    text = rewrite_paths(old, self.mapping)
                    if not mentions_skills(text):
                        text = text.rstrip("\n") + "\n\n" + skills_block
                    rep.notes.append("copilot-instructions.md is repository-specific; moved to AGENTS.md "
                                     "(paths rewritten, skills sections appended)")
                    rep.warnings.append("AGENTS.md: custom content carried over; check that it still matches "
                                        "the repository")
            if not mentions_skills(text):
                text = text.rstrip("\n") + "\n\n" + skills_block
                rep.warnings.append(f"AGENTS.md from {base_origin} does not mention .agents/AGENTS.md; "
                                    f"standard Skills / Setup sections appended (fix the template)")
            write_text(agents, text)
            rep.added.append("AGENTS.md")
        if old is not None:
            old_path.unlink()
            rep.removed.append(".github/copilot-instructions.md (folded into AGENTS.md)")

    def _hunks_text(self, base_old: str, repo_old: str) -> str:
        hunks = added_hunks(base_old, repo_old)
        out = ["## Repository-specific notes", "",
               "<!-- Migrated from .github/copilot-instructions.md: lines this repository added or changed "
               "compared with the template version. Merge them into the sections above, then delete this "
               "comment. -->", ""]
        for heading, chunk in hunks:
            if heading:
                out.append(f"<!-- was under: {heading.strip()} -->")
            out.extend(chunk)
            out.append("")
        return rewrite_paths("\n".join(out).rstrip("\n") + "\n", self.mapping)

    def step_claude(self, wc: Path, rep: Report) -> None:
        p = wc / "CLAUDE.md"
        if not p.exists():
            write_text(p, self.base_claude)
            rep.added.append("CLAUDE.md")
            return
        text = read_text(p)
        if "@AGENTS.md" not in text:
            write_text(p, "@AGENTS.md" + eol_of(text) * 2 + text)
            rep.modified.append("CLAUDE.md (@AGENTS.md added)")

    def step_gitignore(self, wc: Path, rep: Report) -> None:
        p = wc / ".gitignore"
        text = read_text(p) if p.exists() else ""
        have = {line.strip().rstrip("/").lstrip("/") for line in text.replace("\r\n", "\n").split("\n")}
        missing = [d for d in TOOL_DIRS if d.strip("/") not in have]
        if not missing:
            return
        eol = eol_of(text) if text else "\n"
        add = ""
        if text and not text.endswith("\n"):
            add += eol
        if text:
            add += eol
        add += "# Tools provisioned by the skills in .agents (do not ignore the whole tools/ folder)" + eol
        add += "".join(d + eol for d in missing)
        write_text(p, text + add)
        (rep.modified if text else rep.added).append(f".gitignore (+ {' '.join(missing)})")

    def step_setup_steps(self, wc: Path, tpl: Optional[Path], rep: Report) -> None:
        rel = ".github/workflows/copilot-setup-steps.yml"
        p = wc / rel
        if not p.exists():
            src = tpl / rel if tpl and (tpl / rel).exists() else None
            write_text(p, read_text(src) if src else self.base_setup)
            rep.added.append(f"{rel} (from {tpl.name if src else '4d-template'})")
            text = read_text(p)
            new, msg = ensure_checkout_submodules(text)
            if new != text:
                write_text(p, new)
            return
        text = read_text(p)
        new, msg = ensure_checkout_submodules(text)
        if msg.startswith("warning"):
            rep.warnings.append(f"{rel}: {msg}")
        if new != text:
            write_text(p, new)
            rep.modified.append(f"{rel} ({msg})")

    def step_update_skills(self, wc: Path, rep: Report) -> None:
        rel = ".github/workflows/update-skills.yml"
        p = wc / rel
        if not p.exists():
            write_text(p, self.base_update)
            rep.added.append(rel)
        elif read_text(p) != self.base_update:
            rep.notes.append(f"{rel} exists and differs from 4d-template; left as is")

    def step_readme(self, wc: Path, rep: Report) -> None:
        candidates = [p for p in wc.iterdir() if p.is_file() and p.name.lower() == "readme.md"]
        if not candidates:
            return
        p = candidates[0]
        text = read_text(p)
        new, stripped = strip_sections(text)
        rep.sections.extend(f"{p.name}: {h}" for h in stripped)
        new, links, warns = rewrite_readme_links(new, self.mapping, wc)
        rep.links.extend(f"{p.name}: {l}" for l in links)
        rep.warnings.extend(f"{p.name}: {w}" for w in warns)
        if new != text:
            write_text(p, new)
            rep.modified.append(p.name)

    def step_submodule(self, wc: Path, rep: Report) -> None:
        gm = wc / ".gitmodules"
        if gm.exists():
            names = git(wc, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$", check=False)
            for row in names.splitlines():
                key, path = row.split(" ", 1)
                if path.strip() == SUBMODULE_PATH:
                    sect = key[: -len(".path")]
                    url = git(wc, "config", "-f", ".gitmodules", "--get", f"{sect}.url", check=False)
                    br = git(wc, "config", "-f", ".gitmodules", "--get", f"{sect}.branch", check=False)
                    if _strip_git(url) != _strip_git(self.o.skills_url):
                        rep.warnings.append(f".gitmodules: {SUBMODULE_PATH} points to {url}, expected "
                                            f"{self.o.skills_url}; left as is")
                    if br != self.o.skills_branch:
                        git(wc, "config", "-f", ".gitmodules", f"{sect}.branch", self.o.skills_branch)
                        rep.modified.append(f".gitmodules ({SUBMODULE_PATH} branch → {self.o.skills_branch})")
                    return
        if (wc / SUBMODULE_PATH).exists():
            raise MigrationError(f"{SUBMODULE_PATH}/ exists but is not a submodule; resolve by hand")
        if self.o.no_submodule:
            rep.warnings.append(f"--no-submodule: {SUBMODULE_PATH} submodule NOT added")
            return
        run(["git", *git_url_args(self.o.skills_url), "submodule", "add", "-q", "-b", self.o.skills_branch,
             self.o.skills_url, SUBMODULE_PATH], cwd=wc)
        sha = git(wc, "-C", SUBMODULE_PATH, "rev-parse", "--short", "HEAD")
        rep.added.append(f"{SUBMODULE_PATH} (submodule {self.o.skills_url} @ {self.o.skills_branch}, {sha}) + .gitmodules")

    @staticmethod
    def commit_message(rep: Report) -> str:
        lines = ["Migrate agent setup to AGENTS.md + .agents skills submodule", "",
                 "Converted from the legacy layout (copied .github/instructions, .github/skills,",
                 ".github/copilot-instructions.md) to the miyako/4d-template layout. Generic",
                 "know-how now comes from miyako/skills (branch dist) mounted at .agents/.", ""]
        for title, items in (("Removed", rep.removed), ("Added", rep.added), ("Modified", rep.modified),
                             ("README links rewritten", rep.links), ("README sections stripped", rep.sections)):
            if items:
                lines.append(f"{title}:")
                lines.extend(f"- {i}" for i in items)
                lines.append("")
        lines.append(TRAILER)
        return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------------- text transforms

def _fence_mask(lines: List[str]) -> List[bool]:
    """True for lines inside (or delimiting) fenced code blocks."""
    mask, fence = [], None
    for line in lines:
        s = line.lstrip(" ")
        m = re.match(r"^(`{3,}|~{3,})", s) if len(line) - len(s) < 4 else None
        if fence is None and m:
            fence = m.group(1)
            mask.append(True)
        elif fence is not None:
            mask.append(True)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not s[len(m.group(1)):].strip():
                fence = None
        else:
            mask.append(False)
    return mask


def _heading(line: str) -> Optional[Tuple[int, str]]:
    m = re.match(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*\r?\n?$", line)
    return (len(m.group(1)), m.group(2)) if m else None


def strip_sections(text: str) -> Tuple[str, List[str]]:
    lines = split_lines(text)
    mask = _fence_mask(lines)
    out: List[str] = []
    stripped: List[str] = []
    skip_level = 0
    for i, line in enumerate(lines):
        h = None if mask[i] else _heading(line)
        if skip_level:
            if h and h[0] <= skip_level:
                skip_level = 0
            else:
                continue
        if h and any(re.search(p, h[1], re.I) for p in STRIP_HEADING_PATTERNS):
            skip_level = h[0]
            stripped.append("#" * h[0] + " " + h[1])
            continue
        out.append(line)
    if not stripped:
        return text, []
    result = "".join(out)
    if skip_level:  # stripped through end of file: drop trailing blank lines, keep one final EOL
        eol = eol_of(text)
        result = result.rstrip("\r\n") + (eol if text.endswith("\n") else "")
    return result, stripped


_LINK_TARGET = r"(?:\./)?\.github/(?:copilot/)?instructions/([^)\s#\"'>]*)(#[^)\s\"'>]*)?"


def rewrite_readme_links(text: str, mapping: Mapping, wc: Path) -> Tuple[str, List[str], List[str]]:
    lines = split_lines(text)
    mask = _fence_mask(lines)
    done: List[str] = []
    warns: List[str] = []

    def resolve(target: str, fname: str, anchor: Optional[str]) -> Tuple[Optional[str], str]:
        """→ (new target, or None to unlink, or "" to leave as is; description)."""
        if not fname or "{" in fname:
            return "", "placeholder/directory link left as is"
        if (wc / ".github/instructions" / fname).exists():
            if "/copilot/instructions/" in target:
                return f".github/instructions/{fname}{anchor or ''}", "still in the repository"
            return target, "still in the repository"
        e = mapping.instructions.get(fname)
        if e and e.url:
            return e.url, f"→ {e.url}"
        if e and e.status == STATUS_DELETED:
            return None, "deleted; link removed (text kept)"
        return "", "no mapping; left as is"

    def inline(m: re.Match) -> str:
        label, target, fname, anchor, title = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5) or ""
        new, why = resolve(target, fname, anchor)
        if new == "":
            warns.append(f"link {target}: {why}")
            return m.group(0)
        if new is None:
            done.append(f"{target}: {why}")
            return label
        if new == target:
            return m.group(0)
        done.append(f"{target} {why}" if why.startswith("→") else f"{target} → {new}")
        return f"[{label}]({new}{title})"

    def refdef(m: re.Match) -> str:
        target, fname, anchor = m.group(2), m.group(3), m.group(4)
        new, why = resolve(target, fname, anchor)
        if not new or new == target:
            if new == "" or new is None:
                warns.append(f"reference link {target}: {why if new == '' else 'deleted, no URL; left as is'}")
            return m.group(0)
        done.append(f"{target} {why}" if why.startswith("→") else f"{target} → {new}")
        return m.group(1) + new + m.group(5)

    def href(m: re.Match) -> str:
        target, fname, anchor = m.group(2), m.group(3), m.group(4)
        new, why = resolve(target, fname, anchor)
        if not new or new == target:
            if new == "" or new is None:
                warns.append(f"href {target}: {why if new == '' else 'deleted, no URL; left as is'}")
            return m.group(0)
        done.append(f"{target} {why}" if why.startswith("→") else f"{target} → {new}")
        return m.group(1) + new + m.group(5)

    inline_re = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*(" + _LINK_TARGET + r")(\s+\"[^\"]*\")?\s*\)")
    ref_re = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s*<?)(" + _LINK_TARGET + r")(>?(?:\s+\"[^\"]*\")?\s*\r?\n?)$")
    href_re = re.compile(r"(href=[\"'])(" + _LINK_TARGET + r")([\"'])")
    out = []
    for i, line in enumerate(lines):
        if not mask[i] and ".github/" in line:
            line = inline_re.sub(inline, line)
            line = ref_re.sub(refdef, line)
            line = href_re.sub(href, line)
        out.append(line)
    return "".join(out), done, warns


def ensure_checkout_submodules(text: str) -> Tuple[str, str]:
    """Make the actions/checkout step of job `copilot-setup-steps` check out submodules.
    Line-based edit that preserves everything else (comments, order, EOLs)."""
    eol = eol_of(text) if text else "\n"
    original = text
    if text and not text.endswith("\n"):
        text += eol  # so inserted lines never get glued to an unterminated last line
    lines = split_lines(text)

    def ind(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def significant(s: str) -> bool:
        return bool(s.strip()) and not s.lstrip().startswith("#")

    def body(s: str) -> str:
        return s.rstrip("\r\n")

    job = next((i for i, l in enumerate(lines) if re.match(r"^\s+copilot-setup-steps:\s*(#.*)?$", body(l))), None)
    if job is None:
        return original, "warning: no job named copilot-setup-steps; left as is"
    end = next((i for i in range(job + 1, len(lines)) if significant(lines[i]) and ind(lines[i]) <= ind(lines[job])),
               len(lines))
    for i in range(job + 1, end):
        m = re.match(r"^(\s*)(-\s+)?uses:\s*['\"]?actions/checkout@", lines[i])
        if not m:
            continue
        if m.group(2):  # "- uses: actions/checkout@v4"
            start, key_ind = i, len(m.group(1)) + len(m.group(2))
        else:           # "- name: …" then "  uses: actions/checkout@v4"
            key_ind = len(m.group(1))
            start = next((k for k in range(i - 1, job, -1)
                          if re.match(r"^\s*-\s", lines[k]) and ind(lines[k]) < key_ind), i)
        dash_ind = ind(lines[start])
        stop = next((k for k in range(start + 1, end) if significant(lines[k]) and ind(lines[k]) <= dash_ind), end)
        if any(re.match(r"^\s*(-\s+)?with:\s*[^\s#]", body(lines[k])) for k in range(start, stop)):
            return original, "warning: checkout uses an inline 'with:' mapping; add submodules: true by hand"
        with_i = next((k for k in range(start, stop)
                       if re.match(r"^\s*(-\s+)?with:\s*(#.*)?$", body(lines[k]))
                       and (ind(lines[k]) == key_ind or k == start)), None)
        if with_i is None:
            lines[i + 1:i + 1] = [" " * key_ind + "with:" + eol, " " * (key_ind + 2) + "submodules: true" + eol]
            return "".join(lines), "checkout: added submodules: true"
        child_ind = None
        k = with_i + 1
        while k < stop and (not significant(lines[k]) or ind(lines[k]) > key_ind):
            if significant(lines[k]):
                child_ind = child_ind or ind(lines[k])
                sm = re.match(r"^(\s*)submodules:\s*(['\"]?)([^'\"#\s]*)\2(\s*(#.*)?)$", body(lines[k]))
                if sm:
                    if sm.group(3).lower() in ("true", "recursive"):
                        return original, "checkout already has submodules"
                    le = lines[k][len(body(lines[k])):]
                    lines[k] = f"{sm.group(1)}submodules: true{sm.group(4)}{le or eol}"
                    return "".join(lines), "checkout: submodules set to true"
            k += 1
        lines.insert(with_i + 1, " " * (child_ind or key_ind + 2) + "submodules: true" + eol)
        return "".join(lines), "checkout: added submodules: true"
    steps = next((i for i in range(job + 1, end) if re.match(r"^\s*steps:\s*(#.*)?$", body(lines[i]))), None)
    if steps is None:
        return original, "warning: copilot-setup-steps job has no steps; left as is"
    first = next((i for i in range(steps + 1, end) if re.match(r"^\s*-\s", lines[i])), None)
    dash = ind(lines[first]) if first is not None else ind(lines[steps]) + 2
    new = [" " * dash + "- uses: actions/checkout@v4" + eol, " " * (dash + 2) + "with:" + eol,
           " " * (dash + 4) + "submodules: true" + eol]
    at = first if first is not None else steps + 1
    lines[at:at] = new + ([eol] if first is not None else [])
    return "".join(lines), "checkout step with submodules added"


# ---------------------------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Convert repositories from the legacy agent layout (.github/instructions copies, "
                    ".github/skills, copilot-instructions.md) to the miyako/4d-template layout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  %(prog)s HDI_ForEach --dry-run\n"
               "  %(prog)s HDI_ForEach HDI_FormData --push\n"
               "  %(prog)s --kind technote ErrorManagement --survey ~/survey --push\n"
               "Without --push, changes are committed in WORKDIR/<repo> only. Re-running on a migrated "
               "repository is a no-op.")
    ap.add_argument("repos", nargs="+", metavar="REPO", help="repository name (owner defaults to --owner) or owner/name")
    ap.add_argument("--kind", choices=["auto", "hdi", "technote", "generic"], default="auto",
                    help="template family (default: auto-detect; generic = no template, keep repo files)")
    ap.add_argument("--dry-run", action="store_true", help="apply in the clone and report; do not commit or push")
    ap.add_argument("--push", action="store_true", help="push the migration commit to the default branch")
    ap.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()) / "migrate-legacy-repo",
                    help="where clones are made (default: %(default)s)")
    ap.add_argument("--owner", default=DEFAULT_OWNER)
    ap.add_argument("--clone-url", default=DEFAULT_CLONE_URL, help="clone URL pattern with {owner} and {repo}")
    ap.add_argument("--migration", default=DEFAULT_MIGRATION, help="MIGRATION.json path or URL")
    ap.add_argument("--template", action="append", default=[], metavar="KIND=URL",
                    help="override a template repository (hdi=…, technote=…); path or git URL")
    ap.add_argument("--skills-url", default=DEFAULT_SKILLS_URL)
    ap.add_argument("--skills-branch", default=DEFAULT_SKILLS_BRANCH)
    ap.add_argument("--base", type=Path, default=Path(__file__).resolve().parents[1],
                    help="checkout of miyako/4d-template providing AGENTS.md, CLAUDE.md and workflows")
    ap.add_argument("--survey", type=Path, help="survey folder (uniq_blobs.tsv + blobs/) of known legacy variants")
    ap.add_argument("--report-json", type=Path, help="also write the reports as JSON")
    ap.add_argument("--force", action="store_true", help="allow repositories on the do-not-touch list")
    ap.add_argument("--allow-legacy-template", action="store_true",
                    help="use a template that has not been migrated itself (testing only)")
    ap.add_argument("--no-submodule", action="store_true", help="do not add the .agents submodule (offline tests)")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and args.push:
        print("--dry-run and --push are exclusive", file=sys.stderr)
        return 2
    templates = dict(TEMPLATES)
    for t in args.template:
        k, _, v = t.partition("=")
        if k not in TEMPLATES or not v:
            print(f"bad --template {t!r} (expected hdi=URL or technote=URL)", file=sys.stderr)
            return 2
        templates[k] = v
    opts = Options(kind=args.kind, dry_run=args.dry_run, push=args.push, workdir=args.workdir.resolve(),
                   owner=args.owner, clone_url=args.clone_url, migration=args.migration, templates=templates,
                   skills_url=args.skills_url, skills_branch=args.skills_branch, base=args.base.resolve(),
                   survey=args.survey, force=args.force, allow_legacy_template=args.allow_legacy_template,
                   no_submodule=args.no_submodule)
    migrator = Migrator(opts)
    reports, failed = [], 0
    for name in args.repos:
        try:
            rep = migrator.migrate(name)
        except MigrationError as exc:
            rep = Report(repo=name, status="FAILED")
            rep.warnings.append(str(exc))
            failed += 1
        reports.append(rep)
        print(rep.as_text(), flush=True)
    if args.report_json:
        args.report_json.write_text(json.dumps([r.__dict__ for r in reports], indent=2, ensure_ascii=False), "utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
