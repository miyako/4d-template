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
from typing import Dict, Iterable, List, Optional, Set, Tuple

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
# README links to a deleted file that has a repository-local successor (HDI_Template readme.branches rules).
LINK_REPLACEMENTS = {"readme.hdi.instructions.md": "readme.structure.instructions.md"}
SKILLS_TREE_URL = "https://github.com/miyako/skills/tree/main/4d-skills/skills/"
TOOL_DIRS = ["/tools/4dcatalog/", "/tools/4dform/", "/tools/4dlsp/", "/tools/4dlang/", "/tools/4dcli/"]
# Repositories this tool must not touch (DESIGN.md "Do NOT touch" + core repos). Override with --force.
DENYLIST = {
    "4d-plugin-tutorial", "4d-plugin-4d-for-oci", "4d-plugin-factur-x-qpdf", "4d-plugin-factur-x-podofo",
    "4d-plugin-sodium", "4d-form-tutorial", "4d-catalog-tutorial", "4d-classic-to-oop", "bonsai",
    "4d-static-docs", "4d-skills-test-claude", "4d-skills-test-gemini", "4d-catalog-diagram",
    "skills", "4d-template", "hdi_template", "4d-technote-localisation-template",
}
DENY_PREFIXES = ("4d-plugin-", "4d-skills-test-")


def is_denied(repo: str) -> bool:
    r = repo.lower()
    return r in DENYLIST or r.startswith(DENY_PREFIXES)


# README headings whose whole section is removed (Copilot model / mode / token / session content).
STRIP_HEADING_PATTERNS = [
    r"\bcopilot\b.*\b(token|tokens|usage|model|models|mode|modes|session|sessions)\b",
    r"\btoken\s+usage\b",
    r"\bmodel\s+(selection|choice|assessment)\b",
    r"\bmode\s+(selection|choice|guidance)\b",
    r"\busage\s+guidance\b",
    r"copilot.*(トークン|モデル|モード|セッション)",
]
# Kept instruction files must not carry these rules any more (user decision: no Copilot usage in READMEs).
USAGE_RULE_PATTERNS = [r"copilot\s+token\s+usage", r"\btoken\s+usage\b", r"model\s+selection", r"mode\s+selection",
                       r"usage\s+guidance"]
CORE_REPOS = {"skills", "4d-template", "hdi_template", "4d-technote-localisation-template"}
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
    path: Optional[str] = None   # status instruction: repository-relative path in the templates (may be a rename)

    def new_name(self, old: str) -> str:
        if self.path and self.path.startswith(".github/instructions/"):
            return self.path.rsplit("/", 1)[-1]
        return old


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
    return Entry(name=name, status=status, url=url, skill=skill, templates=templates, path=raw.get("path"))


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

def file_history(repo: Optional[Path], rel: str) -> set:
    """Blob shas of every committed version of `rel` in the git repository `repo`."""
    if not repo or not (repo / ".git").exists():
        return set()
    out = run(["git", "-C", str(repo), "log", "--format=", "--raw", "--no-abbrev", "--all", "--", rel], check=False)
    shas = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith(":"):
            shas.update(p for p in parts[2:4] if not set(p) <= {"0"})
    return shas


def rewrite_technote_refs(text: str, skills: Dict[str, str], bare_names: bool) -> str:
    """`.github/copilot-instructions.md` → `AGENTS.md`; `.github/skills/<old>[/SKILL.md]` →
    `.agents/skills/<new>/SKILL.md` (other files in the folder keep their relative path); with bare_names,
    also `<old>` → `<new>` as a whole word. Nothing else changes; idempotent."""
    text = re.sub(r"(?<![\w/.-])\.github/copilot-instructions\.md\b", "AGENTS.md", text)
    if not skills:
        return text
    names = "|".join(re.escape(n) for n in sorted(skills, key=len, reverse=True))

    def path(m: re.Match) -> str:
        rest = m.group(2) or ""
        if rest in ("", "/", "/SKILL.md"):
            rest = "/SKILL.md"
        return f".agents/skills/{skills[m.group(1)]}{rest}"
    text = re.sub(r"\.github/skills/(" + names + r")(?![\w-])(/[\w./-]*)?", path, text)
    if bare_names:
        text = re.sub(r"(?<![\w/.-])(" + names + r")(?![\w-]|\.\w)", lambda m: skills[m.group(1)], text)
    return text


def merge_renamed_rows(text: str, names: Set[str]) -> str:
    """Merge adjacent 2-column table rows whose first cell became the same renamed skill (`| `x` | a |` and
    `| `x` | b |` → `| `x` | a; b |`)."""
    out: List[str] = []
    row = re.compile(r"^\|\s*`?([\w.-]+)`?\s*\|([^|]*)\|\s*$")
    for line in text.split("\n"):
        m = row.match(line.rstrip("\r"))
        prev = row.match(out[-1].rstrip("\r")) if out else None
        if m and prev and m.group(1) == prev.group(1) and m.group(1) in names:
            cr = "\r" if out[-1].endswith("\r") else ""
            merged = prev.group(2).rstrip() + "; " + m.group(2).strip()
            head = out[-1][:prev.start(2)]
            out[-1] = f"{head}{merged} |{cr}"
            continue
        out.append(line)
    return "\n".join(out)


def kind_for(exists, repo: str) -> str:
    """exists(relpath) -> bool. Used on a checkout and on survey path lists (--list)."""
    if any(exists(p) for p in ("technote.json", ".github/instructions/technote-translation.instructions.md",
                               ".github/skills/release-technote", ".github/templates/README.technote.md")):
        return "technote"
    if repo.lower().startswith("hdi_") or any(exists(p) for p in (
            ".github/instructions/readme.structure.instructions.md", ".github/instructions/readme.hdi.instructions.md")):
        return "hdi"
    return "generic"


def mentions_usage_rules(text: str) -> bool:
    return any(re.search(p, text, re.I) for p in USAGE_RULE_PATTERNS)


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

    def template_name(self, kind: str) -> str:
        """Repository name of the template (for a local --template path: the name of its origin, if any)."""
        url = self.o.templates.get(kind, "")
        if url and is_local(url):
            origin = run(["git", "-C", url, "remote", "get-url", "origin"], check=False)
            if origin.returncode == 0 and origin.stdout.strip():
                url = origin.stdout.decode("utf-8", "replace").strip()
        return _strip_git(url).rstrip("/").split("/")[-1]

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
        if is_denied(repo) and not self.o.force:
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
        if kind == "technote":
            self.step_technote(wc, kind, tpl, rep)
        self.step_skills(wc, rep)
        self.step_agents(wc, repo, kind, tpl, rep)
        self.step_claude(wc, rep)
        self.step_gitignore(wc, rep)
        self.step_setup_steps(wc, kind, tpl, rep)
        self.step_update_skills(wc, kind, tpl, rep)
        self.step_readme(wc, kind, tpl, rep)
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
        return kind_for(lambda rel: (wc / rel).exists(), repo)

    # --- steps
    def step_instructions(self, wc: Path, kind: str, tpl: Optional[Path], rep: Report) -> None:
        idir = wc / ".github/instructions"
        if (wc / ".github/.instructions").is_dir():
            rep.warnings.append(".github/.instructions/: vendored third-party content (not loaded by hosts); "
                                "left untouched")
        tdir = tpl / ".github/instructions" if tpl else None
        tpl_files = {p.name: p for p in tdir.iterdir()
                     if p.is_file() and p.name.endswith(".instructions.md")
                     and p.name not in TEMPLATE_ONLY_INSTRUCTIONS} if tdir and tdir.is_dir() else {}
        tpl_name = self.template_name(kind)
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
                own = bool(tpl) and any(t.lower() == tpl_name.lower() for t in entry.templates or [])
                new_name = entry.new_name(path.name)
                if entry.templates and not own and (tpl or new_name != path.name):
                    # e.g. startup.instructions.md → hdi.startup.instructions.md exists in HDI repos only
                    path.unlink()
                    shipped = f" as {new_name}" if new_name != path.name else ""
                    rep.removed.append(f"{rel} (only {', '.join(entry.templates)} repositories keep it{shipped}"
                                       f"{'; generic part: skill ' + entry.skill if entry.skill else ''})")
                    continue
                if own and new_name != path.name:
                    if new_name in tpl_files:
                        path.unlink()  # the template's version is added below under the new name
                        rep.removed.append(f"{rel} (replaced by {new_name} from {tpl_name}"
                                           f"{'; generic part: skill ' + entry.skill if entry.skill else ''})")
                    elif (idir / new_name).exists():
                        path.unlink()
                        rep.removed.append(f"{rel} (replaced by the existing {new_name})")
                    else:
                        path.rename(idir / new_name)
                        rep.added.append(f".github/instructions/{new_name} (renamed from {path.name})")
                        rep.warnings.append(f"{rel}: renamed to {new_name}, but {tpl_name} does not ship it yet; "
                                            f"content kept as is")
                    continue
                other = self._template_copy(path.name, entry, rep)
                if other:
                    src, src_name = other
                    if path.read_bytes() != src.read_bytes():
                        path.write_bytes(src.read_bytes())
                        rep.modified.append(f"{rel} (replaced by the {src_name} version)")
                    continue
                rep.kept.append(rel)
                if tpl:
                    rep.warnings.append(f"{rel}: repo-specific but absent from {tpl_name}; kept as is")
            else:
                rep.kept.append(rel)
                rep.warnings.append(f"{rel}: not in MIGRATION.json; kept, review by hand")
            if mentions_usage_rules(read_text(path)):
                rep.warnings.append(f"{rel}: still contains Copilot token/model usage rules and no token-free "
                                    f"template version was found; edit it by hand")
        for name, src in sorted(tpl_files.items()):
            dest = idir / name
            if not dest.exists():
                write_text(dest, read_text(src))
                rep.added.append(f".github/instructions/{name} (from {tpl_name})")
        if idir.is_dir() and not any(idir.iterdir()):
            idir.rmdir()

    def _template_copy(self, name: str, entry: Entry, rep: Report) -> Optional[Tuple[Path, str]]:
        """The file `name` from a (migrated) template listed in entry.templates, else from any template."""
        kinds = list(self.o.templates)
        if entry.templates:
            wanted = {t.lower() for t in entry.templates}
            kinds = [k for k in kinds if self.template_name(k).lower() in wanted]
        for k in kinds:
            try:
                t = self.template(k)
            except MigrationError as exc:
                rep.warnings.append(f".github/instructions/{name}: cannot use template '{k}' ({exc.args[0][:80]}…)")
                continue
            if t and (t / ".github/instructions" / name).is_file():
                return t / ".github/instructions" / name, self.template_name(k)
        return None

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
        self._moved_skills: Dict[str, str] = {}
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
                if e and e.skill:
                    self._moved_skills[d.name] = e.skill
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
        tpl_names = {f"survey:{self.template_name(k)}" for k in self.o.templates}
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
        self._carried_renames: List[str] = []
        old_path = wc / ".github/copilot-instructions.md"
        agents = wc / "AGENTS.md"
        old = read_text(old_path) if old_path.exists() else None
        if tpl and (tpl / "AGENTS.md").exists():
            base = read_text(tpl / "AGENTS.md").replace("{{REPO_NAME}}", repo)
            base_origin = self.template_name(kind)
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
                        self._carry(body).strip("\n") + "\n"
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
                    text = base.rstrip("\n") + "\n\n" + self._carry(self._hunks_text(var.text, old))
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
                    text = self._carry(old)
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
        if self._carried_renames:
            rep.notes.append("AGENTS.md: skill references renamed in the carried-over content: "
                             + ", ".join(self._carried_renames))

    def _carry(self, text: str) -> str:
        """Repository content carried into AGENTS.md: moved instruction paths rewritten, and the names / paths
        of the skills this repository had under .github/skills renamed to their miyako/skills skill."""
        text = rewrite_paths(text, self.mapping)
        moved = {k: v for k, v in getattr(self, "_moved_skills", {}).items() if k != v}
        if moved:
            new = merge_renamed_rows(rewrite_technote_refs(text, moved, bare_names=True), set(moved.values()))
            if new != text:
                self._carried_renames = sorted(f"{k} → {v}" for k, v in moved.items())
            text = new
        return text

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
        add += "# Tools provisioned by the 4D skills (see the 4dtools skill)" + eol
        add += "".join(d + eol for d in missing)
        write_text(p, text + add)
        (rep.modified if text else rep.added).append(f".gitignore (+ {' '.join(missing)})")

    def step_setup_steps(self, wc: Path, kind: str, tpl: Optional[Path], rep: Report) -> None:
        rel = ".github/workflows/copilot-setup-steps.yml"
        p = wc / rel
        if not p.exists():
            src = tpl / rel if tpl and (tpl / rel).exists() else None
            write_text(p, read_text(src) if src else self.base_setup)
            rep.added.append(f"{rel} (from {self.template_name(kind) if src else '4d-template'})")
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

    def step_update_skills(self, wc: Path, kind: str, tpl: Optional[Path], rep: Report) -> None:
        rel = ".github/workflows/update-skills.yml"
        p = wc / rel
        src, origin = self.base_update, "4d-template"
        if tpl and (tpl / rel).is_file():
            src, origin = read_text(tpl / rel), self.template_name(kind)
        if not p.exists():
            write_text(p, src)
            rep.added.append(f"{rel} (from {origin})")
            return
        cur = read_text(p)
        if cur == src:
            return
        # Earlier versions shipped by 4d-template or the template are upgraded; anything else is the repo's own.
        known = file_history(self.o.base, rel) | (file_history(tpl, rel) if tpl else set())
        if blob_sha(cur.encode("utf-8", "surrogateescape")) in known:
            write_text(p, src)
            rep.modified.append(f"{rel} (updated to the {origin} version)")
        else:
            rep.warnings.append(f"{rel}: customised (not a version shipped by {origin}); left as is")

    def _technote_skills(self, kind: str) -> Dict[str, str]:
        """Legacy skill folder name → new skill name, for the skills that belong to the technote template."""
        tname = self.template_name(kind).lower()
        return {old: e.skill for old, e in self.mapping.skills.items()
                if e.skill and (any(t.lower() == tname for t in e.templates or [])
                                or e.skill.startswith("technote-"))}

    def step_technote(self, wc: Path, kind: str, tpl: Optional[Path], rep: Report) -> None:
        # The repository's own pipeline notes are kept; only the pointers to moved files are updated.
        rel = ".github/instructions/technote-translation.instructions.md"
        p = wc / rel
        if p.is_file():
            text = read_text(p)
            new = rewrite_technote_refs(text, self._technote_skills(kind), bare_names=True)
            if new != text:
                write_text(p, new)
                rep.modified.append(f"{rel} (references to AGENTS.md / technote-* skills updated)")
        rel = ".github/ISSUE_TEMPLATE/localisation-request.yml"
        if tpl and (wc / rel).is_file() and (tpl / rel).is_file():
            if (wc / rel).read_bytes() != (tpl / rel).read_bytes():
                (wc / rel).write_bytes((tpl / rel).read_bytes())
                rep.modified.append(f"{rel} (synced with {self.template_name(kind)})")

    def step_readme(self, wc: Path, kind: str, tpl: Optional[Path], rep: Report) -> None:
        candidates = [p for p in wc.iterdir() if p.is_file() and p.name.lower() == "readme.md"]
        if not candidates:
            return
        p = candidates[0]
        text = read_text(p)
        if kind == "technote" and tpl and blob_sha(p.read_bytes()) in file_history(tpl, "README.md"):
            # still the template's usage guide (the technote has not been released yet)
            guide = rewrite_technote_refs(text, self._technote_skills(kind), bare_names=False)
            if guide != text:
                rep.links.append(f"{p.name}: template usage guide: references to AGENTS.md / technote-* skills updated")
                text_for_strip = guide
            else:
                text_for_strip = text
        else:
            text_for_strip = text
        new, stripped = strip_sections(text_for_strip)
        rep.sections.extend(f"{p.name}: {h}" for h in stripped)
        new, links, warns = rewrite_readme_links(new, self.mapping, wc)
        rep.links.extend(f"{p.name}: {l}" for l in links)
        rep.warnings.extend(f"{p.name}: {w}" for w in warns)
        new, done, warns = normalise_branches_table(new)
        rep.links.extend(f"{p.name}: {d}" for d in done)
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


OLD_BRANCHES_INTRO = ("Each branch represents a distinct modernisation effort, guided by a corresponding Copilot "
                      "instruction file.")
NEW_BRANCHES_INTRO = ("Each branch represents a distinct modernisation effort, guided by a 4D skill or a repository "
                      "instruction file.")
_OLD_HEADER = re.compile(r"^(\|\s*Branch\s*\|\s*Description\s*\|\s*)Instructions(\s*\|\s*)$")
_OLD_SEPARATOR = "|--------|-------------|--------------|"
_NEW_SEPARATOR = "|--------|-------------|----------|"


def normalise_branches_table(text: str) -> Tuple[str, List[str], List[str]]:
    """Header `Instructions` → `Guidance` (and the standard intro sentence) in the branches table under
    `## Modernisation notes` / `## Branches`, as in HDI_Template's readme.branches.instructions.md."""
    lines = split_lines(text)
    mask = _fence_mask(lines)
    done: List[str] = []
    warns: List[str] = []
    section = ""
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        if mask[i]:
            continue
        h = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", body)
        if h:
            section = h.group(1).strip().lower()
            continue
        m = _OLD_HEADER.match(body)
        if not m or section not in ("modernisation notes", "modernization notes", "branches"):
            continue
        lines[i] = m.group(1) + "Guidance" + m.group(2) + eol
        done.append("branches table: header Instructions → Guidance")
        if i + 1 < len(lines) and lines[i + 1].rstrip("\r\n") == _OLD_SEPARATOR:
            lines[i + 1] = _NEW_SEPARATOR + lines[i + 1][len(_OLD_SEPARATOR):]
        j = i - 1   # the paragraph just above the table (skip blank lines)
        while j >= 0 and not lines[j].strip():
            j -= 1
        above = lines[j].rstrip("\r\n") if j >= 0 else ""
        if above == OLD_BRANCHES_INTRO:
            lines[j] = NEW_BRANCHES_INTRO + lines[j][len(above):]
            done.append("branches table: standard intro sentence updated")
        elif above.startswith("#"):
            warns.append("branches table: no intro sentence above the table; left as is")
        elif above != NEW_BRANCHES_INTRO:
            warns.append("branches table: intro sentence is not the standard one; kept as is")
    return "".join(lines), done, warns


def rewrite_readme_links(text: str, mapping: Mapping, wc: Path) -> Tuple[str, List[str], List[str]]:
    lines = split_lines(text)
    mask = _fence_mask(lines)
    done: List[str] = []
    warns: List[str] = []

    def resolve(target: str, fname: str, anchor: Optional[str]) -> Tuple[Optional[str], str]:
        """→ (new target, or None to unlink, or "" to leave as is; description). Labels: see label_for()."""
        if not fname or "{" in fname:
            return "", "placeholder/directory link left as is"
        if (wc / ".github/instructions" / fname).exists():
            if "/copilot/instructions/" in target:
                return f".github/instructions/{fname}{anchor or ''}", "still in the repository"
            return target, "still in the repository"
        e = mapping.instructions.get(fname)
        if e and e.url:
            return e.url, f"→ {e.url}"
        repl = LINK_REPLACEMENTS.get(fname)
        if repl and (wc / ".github/instructions" / repl).exists():
            prefix = target[:target.index(".github/")]
            return f"{prefix}.github/instructions/{repl}{anchor or ''}", "renamed"
        if e and e.status == STATUS_INSTRUCTION:
            new_name = e.new_name(fname)
            if new_name != fname and (wc / ".github/instructions" / new_name).exists():
                prefix = target[:target.index(".github/")]
                return f"{prefix}.github/instructions/{new_name}{anchor or ''}", "renamed"
            if e.skill:  # removed from this repository; its generic part lives in a skill
                url = SKILLS_TREE_URL + e.skill
                return url, f"→ {url}"
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
        e = mapping.instructions.get(fname)
        if e and e.skill and new in (e.url, SKILLS_TREE_URL + e.skill):
            return f"[`{e.skill}`]({new}{title})"
        if why == "renamed":
            new_name = new.split("#")[0].rsplit("/", 1)[-1]
            if label.strip("`") == fname:
                label = new_name
            if e and e.skill:  # e.g. startup → skill 4dstartup + hdi.startup.instructions.md
                return f"[`{e.skill}`]({SKILLS_TREE_URL}{e.skill}), [{label}]({new}{title})"
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
    link = r"\[[^\[\]]+\]\([^()\s]+\)"
    run_re = re.compile(link + r"(?:\s*,\s*" + link + r")+")

    def dedup(m: re.Match) -> str:   # e.g. css + tahoe.css both became [`4dcss`](…): keep the first
        items = re.findall(link, m.group(0))
        if len(set(items)) == len(items):
            return m.group(0)
        return ", ".join(dict.fromkeys(items))
    out = []
    for i, line in enumerate(lines):
        if not mask[i] and ".github/" in line:
            new = href_re.sub(href, ref_re.sub(refdef, inline_re.sub(inline, line)))
            if new != line:
                new = run_re.sub(dedup, new)
            line = new
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


# --------------------------------------------------------------------------------------- inventory

# Facts the survey cannot show (it only lists agent files), reported in the inventory.
SURVEY_NOTES = {
    "4D-Demo-AI-Automation": "`.github/.instructions/` (vendored 4d-depot content, MIT) left untouched; "
                             "custom AGENTS.md: skill names `4d-docs`/`4d-form-layout` renamed to `4dform`",
    "TechNote_26-01-AIKitInModernApp": "copilot-instructions is AIManager-specific: moved to AGENTS.md verbatim",
}


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def inventory(survey: Path, mapping: Optional[Mapping], results: Dict[str, dict], templates: Dict[str, str]) -> str:
    """Markdown table of every surveyed repository: detected kind, planned action, and run results."""
    forks: Dict[str, bool] = {}
    for line in (survey / "scan.tsv").read_text("utf-8").splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            forks[parts[0]] = len(parts) > 1 and parts[1] == "true"
    files: Dict[str, List[Tuple[str, str]]] = {}
    for line in (survey / "files.tsv").read_text("utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            files.setdefault(parts[0], []).append((parts[1], parts[2]))
            forks.setdefault(parts[0], False)
    tpl_names = {_strip_git(u).split("/")[-1] for u in templates.values()}
    tpl_copilot = {sha for repo, rows in files.items() if repo in tpl_names
                   for path, sha in rows if path == ".github/copilot-instructions.md"}

    def blob_text(sha: str) -> str:
        b = survey / "blobs" / sha
        return b.read_bytes().decode("utf-8", "replace") if b.exists() else ""

    rows = []
    for repo in sorted(forks, key=str.lower):
        paths = files.get(repo, [])
        names = {p for p, _ in paths}
        exists = lambda rel: any(n == rel or n.startswith(rel + "/") for n in names)
        instr = [(p.split("/")[-1], sha) for p, sha in paths if p.startswith(".github/instructions/")]
        skills = sorted({p.split("/")[2] for p, _ in paths if p.startswith(".github/skills/")})
        copilot = next((sha for p, sha in paths if p == ".github/copilot-instructions.md"), None)
        kind = kind_for(exists, repo)
        notes: List[str] = []
        if repo.lower() in CORE_REPOS:
            action = "core — migrated by its own session"
        elif is_denied(repo):
            action = "deferred (do not touch)"
        elif not (instr or skills or copilot):
            action = "nothing to migrate"
        else:
            action = f"migrate ({kind})"
            if mapping is None:
                notes.append("MIGRATION.json not available yet")
            else:
                removed = [n for n, _ in instr if n in mapping.instructions
                           and mapping.instructions[n].status in (STATUS_SKILL, STATUS_DELETED)
                           and n not in KEEP_OWN.get(kind, set())]
                kept = [n for n, _ in instr if n not in removed]
                if removed:
                    notes.append(f"remove {len(removed)} instruction copies")
                for n, sha in instr:
                    if n not in kept:
                        continue
                    e = mapping.instructions.get(n)
                    if n in KEEP_OWN.get(kind, set()):
                        notes.append(f"keep own `{n}`")
                    elif e is None:
                        notes.append(f"`{n}` not in MIGRATION.json (kept)")
                    elif e.templates and (templates.get(kind) or e.new_name(n) != n) and not any(
                            t.lower() == re.sub(r"\.git$", "", templates.get(kind, "").rstrip("/")).rsplit("/", 1)[-1].lower()
                            for t in e.templates):
                        notes.append(f"remove `{n}` (only {', '.join(e.templates)} keeps it)")
                    elif mentions_usage_rules(blob_text(sha)):
                        notes.append(f"`{n}` has token rules → template version")
                    elif e.new_name(n) != n:
                        notes.append(f"`{n}` → template `{e.new_name(n)}`")
                    else:
                        notes.append(f"keep/sync `{n}`")
            if skills:
                notes.append("remove skills: " + ", ".join(f"`{x}`" for x in skills))
            if copilot:
                if copilot in tpl_copilot:
                    notes.append("copilot-instructions = template copy → template AGENTS.md")
                else:
                    notes.append(f"copilot-instructions {copilot[:7]} custom/modified → AGENTS.md (review)")
        if repo in SURVEY_NOTES:
            notes.append(SURVEY_NOTES[repo])
        if exists(".gitmodules"):
            notes.append("has other submodules")
        r = results.get(repo, {})
        status = r.get("status", "")
        if r.get("warnings"):
            status += f" ({len(r['warnings'])} warnings)"
        rows.append((repo, "yes" if forks.get(repo) else "", kind if action.startswith("migrate") else "",
                     action, "; ".join(notes), status, r.get("commit", "")))

    counts: Dict[str, int] = {}
    for row in rows:
        counts[row[3]] = counts.get(row[3], 0) + 1
    out = ["# Migration status", "",
           "Repositories found by the survey of agent files (`scan.tsv` / `files.tsv`), the layout family detected "
           "by `scripts/migrate-legacy-repo.py`, the planned action and the result of the run.",
           "Generated with `python3 scripts/migrate-legacy-repo.py --list --survey <dir> [--results report.json]`.",
           "", "| Action | Repositories |", "|---|--:|"]
    out += [f"| {a} | {n} |" for a, n in sorted(counts.items())]
    out += ["", "| Repository | Fork | Kind | Action | Plan / notes | Result | Commit |", "|---|---|---|---|---|---|---|"]
    order = lambda r: (not r[3].startswith("migrate"), r[3], r[0].lower())
    for repo, fork, kind, action, notes, status, commit in sorted(rows, key=order):
        link = f"[{repo}](https://github.com/{DEFAULT_OWNER}/{repo})"
        c = f"[`{commit}`](https://github.com/{DEFAULT_OWNER}/{repo}/commit/{commit})" if commit else ""
        out.append(f"| {link} | {fork} | {kind} | {_md(action)} | {_md(notes)} | {_md(status)} | {c} |")
    return "\n".join(out) + "\n"


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
    ap.add_argument("repos", nargs="*", metavar="REPO", help="repository name (owner defaults to --owner) or owner/name")
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
    inv = ap.add_argument_group("inventory (no repository is cloned or changed)")
    inv.add_argument("--list", action="store_true",
                     help="write a Markdown table of every surveyed repository with kind and planned action "
                          "(needs --survey)")
    inv.add_argument("--results", type=Path, action="append", default=[],
                     help="report JSON(s) from earlier runs (--report-json) to fill the Result/Commit columns")
    inv.add_argument("--list-output", type=Path, help="write the table to this file instead of stdout")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        if not args.survey:
            parser.error("--list needs --survey")
        try:
            mapping = load_mapping(args.migration)
        except MigrationError:
            mapping = None
        results: Dict[str, dict] = {}
        for f in args.results:
            for r in json.loads(f.read_text("utf-8")):
                results[r["repo"].split("/")[-1]] = r  # later files win
        text = inventory(args.survey, mapping, results, TEMPLATES)
        if args.list_output:
            args.list_output.parent.mkdir(parents=True, exist_ok=True)
            args.list_output.write_text(text, "utf-8")
        else:
            sys.stdout.write(text)
        return 0
    if not args.repos:
        parser.error("give at least one REPO (or --list)")
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
