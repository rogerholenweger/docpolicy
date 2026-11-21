"""Checks C1-C12. check() = fast subset (per-call safe), audit() = full pass."""

import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from docpolicy import frontmatter as fm
from docpolicy.policy import SKIP_DIRS, Policy, PolicyError

MD_GLOB = "**/*.md"
ISSUE_REF_FALLBACK = re.compile(r"\b(?:[A-Z]{2,4})-(?P<num>\d+)\b")
MD_LINK = re.compile(r"\]\(([^)#\s]+?)(#[^)\s]*)?\)")
H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)

CODE_TITLES = {
    "C1": "classification", "C2": "subject-uniqueness", "C3": "supersession",
    "C4": "generated-freshness", "C5": "links", "C6": "issue-refs",
    "C7": "ephemeral", "C8": "title-collision", "C9": "archive-hygiene",
    "C10": "tag-collision", "C11": "inbox-age", "C12": "assertion-drift",
    "FM": "frontmatter",
}


@dataclass
class Finding:
    code: str
    severity: str
    path: str  # repo-relative posix, "" for repo-level findings
    message: str

    def as_dict(self):
        return {"code": self.code, "severity": self.severity,
                "path": self.path, "message": self.message}


@dataclass
class DocInfo:
    rel: str
    classes: list
    fm: dict
    body: str
    mtime: float
    had_fm: bool
    fm_ok: bool


def iter_markdown(repo: Path):
    for p in sorted(repo.rglob("*.md")):
        if any(part in SKIP_DIRS for part in p.relative_to(repo).parts):
            continue
        yield p


def collect(repo: Path, policy: Policy, only: set | None = None) -> list[DocInfo]:
    docs = []
    for p in iter_markdown(repo):
        rel = p.relative_to(repo).as_posix()
        if only is not None and rel not in only:
            continue
        classes = policy.class_for(rel)
        # Exempt docs (secrets, legal, generated-adjacent): classify the PATH only,
        # never read content — covers .env files, key material, licence texts.
        if classes and policy.class_mode(classes[0]) == "exempt":
            docs.append(DocInfo(rel, classes, {}, "", 0.0, False, False))
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            docs.append(DocInfo(rel, classes, {}, "", 0.0, False, False))
            continue
        fmd, body, had = fm.parse(text)
        ok = fmd is not None
        docs.append(DocInfo(rel, classes, fmd if ok else {}, body,
                            p.stat().st_mtime, had, ok))
    return docs


def _sev(policy: Policy, code: str, advisory: bool) -> str:
    if advisory:
        return "warn"
    return policy.severity.get(code, "warn")


def _add(out: list, policy: Policy, code: str, path: str, msg: str, advisory: bool):
    sev = _sev(policy, code, advisory)
    if sev == "off":
        return
    out.append(Finding(code, sev, path, msg))


def check_frontmatter(d: DocInfo, policy: Policy, out: list, advisory: bool):
    """Frontmatter requirements for canonical-mode docs (FM + C9 helpers)."""
    modes = {policy.class_mode(c) for c in d.classes}
    if "canonical" not in modes:
        return
    if (d.classes and not d.had_fm) or (d.had_fm and not d.fm_ok):
        _add(out, policy, "FM", d.rel,
             "malformed or missing frontmatter (need '---' block with subject + status)",
             advisory)
        return
    subject = (d.fm.get("subject") or "").strip()
    status = (d.fm.get("status") or "").strip()
    if not subject:
        _add(out, policy, "FM", d.rel, "missing frontmatter field: subject", advisory)
    if not status:
        _add(out, policy, "FM", d.rel, "missing frontmatter field: status", advisory)
    elif status not in fm.KNOWN_STATUSES:
        _add(out, policy, "FM", d.rel,
             f"bad status {status!r} (allowed: {sorted(fm.KNOWN_STATUSES)})", advisory)


def check_supersession(docs: list[DocInfo], policy: Policy, out: list, advisory: bool,
                       restrict: set | None = None):
    """C2 (one active per subject) + C3 (superseded_by integrity).

    restrict: when set (per-file / pre-write mode), only emit findings whose
    path is in the target set — a pre-existing collision elsewhere in the repo
    must not block an unrelated write."""
    by_subject: dict[str, list[DocInfo]] = {}
    by_rel = {d.rel: d for d in docs}
    for d in docs:
        if "canonical" not in {policy.class_mode(c) for c in d.classes}:
            continue
        status = (d.fm.get("status") or "").strip()
        subject = (d.fm.get("subject") or "").strip()
        if not subject or status not in fm.KNOWN_STATUSES:
            continue  # FM findings already cover this
        if status == fm.STATUS_ACTIVE:
            by_subject.setdefault(subject, []).append(d)
        if status == fm.STATUS_SUPERSEDED:
            if restrict is not None and d.rel not in restrict:
                continue
            target = (d.fm.get("superseded_by") or "").strip()
            if not target:
                _add(out, policy, "C3", d.rel,
                     f"status superseded but no superseded_by (subject {subject!r})",
                     advisory)
            elif target not in by_rel:
                _add(out, policy, "C3", d.rel,
                     f"superseded_by target not found: {target}", advisory)
            else:
                tstatus = (by_rel[target].fm.get("status") or "").strip()
                tclasses = by_rel[target].classes
                tmodes = {policy.class_mode(c) for c in tclasses}
                if tstatus != fm.STATUS_ACTIVE and not (tmodes & {"ephemeral", "exempt", "append-only"}):
                    _add(out, policy, "C3", d.rel,
                         f"superseded_by target {target} is not active (status {tstatus!r})",
                         advisory)
    for subject, actives in sorted(by_subject.items()):
        if len(actives) > 1:
            if restrict is not None and not any(d.rel in restrict for d in actives):
                continue
            _add(out, policy, "C2", actives[0].rel,
                 f"subject {subject!r} has {len(actives)} active docs: "
                 + ", ".join(d.rel for d in actives)
                 + " — exactly one must be active; set the rest superseded+superseded_by",
                 advisory)


def _class_facts_path(repo: Path, cls: dict):
    """Resolve a generated class's `facts:` file (repo-relative unless absolute)."""
    rel = cls.get("facts")
    if not rel:
        return None
    p = Path(os.path.expanduser(str(rel)))
    return p if p.is_absolute() else repo / p


def _restore_file(path, before):
    """Put a file back exactly as it was; before=None means it did not exist."""
    if path is None:
        return
    try:
        if before is None:
            if path.is_file():
                path.unlink()
        elif not path.is_file() or path.read_bytes() != before:
            path.write_bytes(before)
    except OSError:
        pass


def check_generated(repo: Path, policy: Policy, out: list, advisory: bool):
    """C4: re-run each declared generator; restore original bytes afterwards.

    The generator typically writes two things: the generated doc (matched by
    `paths`) and, when `facts:` is declared, a side-car JSON file. Both are
    snapshotted and restored, so an audit never mutates the repo.
    """
    for c in policy.classes:
        if c["mode"] != "generated" or not c["paths"] or not c.get("generator"):
            continue
        for pat in c["paths"]:
            for p in sorted(repo.glob(pat)):
                # `dir/**` matches the directory itself and its subdirectories, not just the
                # files inside; only files have bytes to diff (a directory raised IsADirectoryError).
                if not p.is_file():
                    continue
                rel = p.relative_to(repo).as_posix()
                before = p.read_bytes()
                h_before = hashlib.sha256(before).hexdigest()
                facts_path = _class_facts_path(repo, c)
                facts_before = (facts_path.read_bytes()
                                if facts_path is not None and facts_path.is_file() else None)
                # No shell: the generator line is repo-owner config (same trust as a
                # Makefile entry), but we still shlex-split it and expand ~/$VARS per
                # token instead of handing it to a shell. Complex pipelines belong in
                # a script; the policy points at the script.
                try:
                    argv = [
                        os.path.expandvars(os.path.expanduser(tok))
                        for tok in shlex.split(c["generator"])
                    ]
                except ValueError as e:
                    _add(out, policy, "C4", rel,
                         f"cannot parse generator {c['generator']!r}: {e}", advisory)
                    continue
                try:
                    try:
                        subprocess.run(argv, cwd=repo, timeout=120, check=False,
                                       capture_output=True)
                    except subprocess.TimeoutExpired:
                        _add(out, policy, "C4", rel,
                             f"generator timed out: {c['generator']!r}", advisory)
                        p.write_bytes(before)
                        continue
                    except OSError as e:
                        _add(out, policy, "C4", rel,
                             f"cannot run generator {c['generator']!r}: {e}", advisory)
                        continue
                    after = p.read_bytes() if p.exists() else b""
                    if hashlib.sha256(after).hexdigest() != h_before:
                        _add(out, policy, "C4", rel,
                             "generated file is stale — re-run: " + c["generator"], advisory)
                        p.write_bytes(before)  # generated doc: restored when stale
                finally:
                    _restore_file(facts_path, facts_before)  # `facts:` side-car too


def check_links(d: DocInfo, repo: Path, policy: Policy, out: list, advisory: bool):
    """C5: relative markdown links must resolve."""
    for m in MD_LINK.finditer(d.body):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#", "/")):
            continue
        resolved = (repo / d.rel).parent / target
        if not resolved.exists():
            _add(out, policy, "C5", d.rel, f"broken relative link: {target}", advisory)


def check_issue_refs(docs: list[DocInfo], repo: Path, policy: Policy,
                     out: list, advisory: bool):
    """C6: <PROJECT>-NN references must exist in the configured task store.
    Project codes come FROM the store (tasks.project), so 'SHA-256' or 'AVX-512'
    cannot false-positive unless a project is literally named SHA or AVX."""
    db = policy.sources.get("issues")
    if db is None or not Path(db).is_file():
        return
    known: set[int] = set()
    codes: set[str] = set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        known = {r[0] for r in con.execute("SELECT id FROM tasks")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        if "project" in cols:
            codes = {str(r[0]).upper() for r in con.execute("SELECT DISTINCT project FROM tasks")
                     if r[0]}
        con.close()
    except sqlite3.Error as e:
        _add(out, policy, "C6", "", f"cannot read issue store {db}: {e}", advisory)
        return
    if codes:
        ref = re.compile(r"\b(?:" + "|".join(sorted(codes, key=len, reverse=True)) + r")-(?P<num>\d+)\b")
    else:
        ref = ISSUE_REF_FALLBACK
    for d in docs:
        for m in ref.finditer(d.body):
            n = int(m.group("num"))
            if n not in known:
                _add(out, policy, "C6", d.rel,
                     f"issue reference {m.group(0)} not found in task store", advisory)


def check_ephemeral(d: DocInfo, repo: Path, policy: Policy, out: list, advisory: bool):
    """C7: ephemeral docs must be fresh and point at a canonical doc."""
    for cid in d.classes:
        c = policy.classes_by_id.get(cid, {})
        if c.get("mode") != "ephemeral":
            continue
        max_age = c.get("max_age_days")
        if max_age and d.mtime:
            age_days = (time.time() - d.mtime) / 86400
            if age_days > max_age:
                _add(out, policy, "C7", d.rel,
                     f"ephemeral doc is {age_days:.0f}d old (max {max_age}) — promote to the "
                     "canonical doc or tombstone it to a pointer", advisory)
        if c.get("require_pointer"):
            has_pointer = any(
                not t.startswith(("http", "mailto:")) for t in MD_LINK.findall(d.body)
                if isinstance(t, str)
            ) or bool(re.search(r"\]\([^)#\s]+\.md", d.body))
            if not has_pointer:
                _add(out, policy, "C7", d.rel,
                     "ephemeral doc lacks a pointer to a canonical doc", advisory)


def check_titles(docs: list[DocInfo], policy: Policy, out: list, advisory: bool):
    """C8: duplicate H1 across canonical docs (advisory)."""
    seen: dict[str, list[str]] = {}
    for d in docs:
        if "canonical" not in {policy.class_mode(c) for c in d.classes}:
            continue
        m = H1.search(d.body)
        if m:
            seen.setdefault(m.group(1).strip().lower(), []).append(d.rel)
    for title, rels in sorted(seen.items()):
        if len(rels) > 1:
            _add(out, policy, "C8", rels[0],
                 f"duplicate H1 {title!r} in: {', '.join(rels)} — one subject, one doc?",
                 advisory)


def check_archive(d: DocInfo, policy: Policy, out: list, advisory: bool):
    """C9: archive-class docs must not be active."""
    for cid in d.classes:
        if policy.class_mode(cid) == "archive":
            status = (d.fm.get("status") or "").strip()
            if status and status not in (fm.STATUS_SUPERSEDED, fm.STATUS_ARCHIVED, fm.STATUS_DRAFT):
                _add(out, policy, "C9", d.rel,
                     f"archive doc has status {status!r} — must be superseded or archived",
                     advisory)


def check_inbox(d: DocInfo, policy: Policy, out: list, advisory: bool):
    """C11: captures must not sit in the inbox forever (triaged or not, they age).

    Inbox docs are deliberately exempt from FM/C2/C9 — zero friction at capture
    time is the point. The cost is surfaced here instead: an old capture is a
    finding, and the fix is `docpolicy triage --apply` (or delete it)."""
    for cid in d.classes:
        c = policy.classes_by_id.get(cid, {})
        if c.get("mode") != "inbox":
            continue
        max_age = c.get("max_age_days")
        if max_age and d.mtime:
            age_days = (time.time() - d.mtime) / 86400
            if age_days > max_age:
                _add(out, policy, "C11", d.rel,
                     f"inbox capture is {age_days:.0f}d old (max {max_age}) — "
                     "run: docpolicy triage --repo . (or delete it)", advisory)


def check_tag_collisions(docs: list[DocInfo], policy: Policy, out: list, advisory: bool):
    """C10: two ACTIVE canonical docs sharing 2+ tags claim one topic (advisory).

    Subject is document identity (one active per subject, C2), so topic overlap
    is invisible to C2 — tags are where it shows. One shared tag is adjacency
    (promotion deliberately links related docs); two or more is the merge
    signal, and merging needs judgement, so it warns rather than errors.
    """
    actives = []
    for d in docs:
        if "canonical" not in {policy.class_mode(c) for c in d.classes}:
            continue
        if (d.fm.get("status") or "").strip() != fm.STATUS_ACTIVE:
            continue
        tags = d.fm.get("tags")
        if isinstance(tags, str):
            t = tags.strip()
            tags = [x.strip().strip("'\"") for x in t[1:-1].split(",")] if t.startswith("[") else [t]
        if not isinstance(tags, list):
            continue
        tagset = {str(t).strip().lower() for t in tags if str(t).strip()}
        if tagset:
            actives.append((d.rel, tagset))
    for i, (rel_a, tags_a) in enumerate(actives):
        for rel_b, tags_b in actives[i + 1:]:
            shared = sorted(tags_a & tags_b)
            if len(shared) >= 2:
                _add(out, policy, "C10", rel_a,
                     f"shares {len(shared)} tags with {rel_b} ({', '.join(shared)}) — "
                     "same topic? merge into one doc, or drop the shared tags",
                     advisory)


def check(repo: Path, policy: Policy, only: set | None = None,
          advisory: bool = False, stdin_file: str | None = None,
          stdin_text: str | None = None) -> list[Finding]:
    """Fast pass: C1/FM/C9/C7 on the target set, C2/C3 across the whole repo.

    stdin_file + stdin_text evaluate a VIRTUAL doc (the about-to-be-written
    content) instead of the on-disk state — the pre-write hook path. C2/C3
    still scan every doc so cross-doc collisions are caught.
    """
    out: list[Finding] = []
    all_docs = collect(repo, policy)
    if stdin_file is not None and stdin_text is not None:
        fmd, body, had = fm.parse(stdin_text)
        virtual = DocInfo(rel=stdin_file, classes=policy.class_for(stdin_file),
                          fm=fmd if fmd is not None else {}, body=body, mtime=0.0,
                          had_fm=had, fm_ok=fmd is not None)
        by_rel = {d.rel: d for d in all_docs}
        by_rel[stdin_file] = virtual
        docs = list(by_rel.values())
        targets = [virtual]
        restrict = {stdin_file}
    else:
        docs = all_docs
        targets = all_docs if only is None else [d for d in docs if d.rel in only]
        restrict = None if only is None else set(only)
    for d in targets:
        if not d.classes and not policy.severity.get("C1") == "off":
            _add(out, policy, "C1", d.rel, "unclassified: no class pattern matches",
                 advisory)
        check_frontmatter(d, policy, out, advisory)
        check_archive(d, policy, out, advisory)
        check_ephemeral(d, repo, policy, out, advisory)
        check_inbox(d, policy, out, advisory)
    check_supersession(docs, policy, out, advisory, restrict=restrict)
    return out


def _norm_assert(v) -> str:
    """Normalize an assertion value for comparison (numbers cross-type equal)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) and abs(f) < 1e15 else repr(f)
    return str(v).strip()


def _assert_match(reality, expected) -> bool:
    """Scalar: numeric-then-string equality. List expected: subset semantics —
    every asserted item must exist in reality (reality may know more)."""
    if isinstance(expected, list):
        if not isinstance(reality, list):
            return False
        real = {_norm_assert(x) for x in reality}
        return all(_norm_assert(x) in real for x in expected)
    if isinstance(reality, list):
        return _norm_assert(reality[0]) == _norm_assert(expected) if reality else False
    ea, ra = _norm_assert(expected), _norm_assert(reality)
    if ea == ra:
        return True
    try:
        return float(ea) == float(ra)
    except ValueError:
        return False


def check_assertions(docs: list[DocInfo], repo: Path, policy: Policy, out: list,
                     advisory: bool):
    """C12: `assert:` frontmatter on canonical docs must match machine facts.

    The generated class declares `facts:` (a JSON file its generator writes).
    Every canonical doc may carry an `assert:` mapping; each key must either be
    absent from facts (never asserted about) or equal reality. This is the
    loop-closer: a spec that contradicts what the repo actually is becomes a
    finding, not a silent prose drift."""
    facts_rel = None
    facts_path = None
    for c in policy.classes:
        if c["mode"] == "generated" and c.get("facts"):
            facts_rel = str(c["facts"])
            facts_path = _class_facts_path(repo, c)
            break
    if facts_rel is None or facts_path is None:
        return
    facts: dict = {}
    if not facts_path.is_file():
        _add(out, policy, "C12", facts_rel,
             "facts file missing — run the generator (see the generated class)", advisory)
        return
    try:
        loaded = json.loads(facts_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            facts = loaded
        else:
            raise ValueError("facts root must be a JSON object")
    except (OSError, ValueError) as e:
        _add(out, policy, "C12", facts_rel, f"facts file unreadable: {e}", advisory)
        return
    for d in docs:
        if "canonical" not in {policy.class_mode(c) for c in d.classes}:
            continue
        assert_map = d.fm.get("assert") if isinstance(d.fm, dict) else None
        if not isinstance(assert_map, dict):
            continue
        for key in sorted(assert_map):
            expected = assert_map[key]
            if key not in facts:
                _add(out, policy, "C12", d.rel,
                     f"assertion {key!r}: not present in {facts_rel} — the generator "
                     "does not extract this fact yet", advisory)
                continue
            if not _assert_match(facts[key], expected):
                _add(out, policy, "C12", d.rel,
                     f"assertion {key!r}: doc expects {expected!r}, repo reality is "
                     f"{facts[key]!r}", advisory)


def _dedupe(out: list[Finding]) -> list[Finding]:
    seen = set()
    unique = []
    for f in out:
        key = (f.code, f.path, f.message)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def audit(repo: Path, policy: Policy, advisory: bool = False) -> list[Finding]:
    """Full pass: everything in check() plus C4-C8, C10, C12. Side-effect free."""
    out = check(repo, policy, advisory=advisory)
    docs = collect(repo, policy)
    check_generated(repo, policy, out, advisory)
    for d in docs:
        check_links(d, repo, policy, out, advisory)
    check_issue_refs(docs, repo, policy, out, advisory)
    check_titles(docs, policy, out, advisory)
    check_tag_collisions(docs, policy, out, advisory)
    check_assertions(docs, repo, policy, out, advisory)
    return _dedupe(out)


def summarize(findings: list[Finding]) -> dict:
    by_sev = {"error": 0, "warn": 0}
    by_code: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_code[f.code] = by_code.get(f.code, 0) + 1
    return {"total": len(findings), "errors": by_sev["error"],
            "warnings": by_sev["warn"], "by_code": by_code}
