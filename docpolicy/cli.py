"""CLI: docpolicy {init,check,audit,brief,fix,triage,llm}."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from docpolicy import __version__, checks
from docpolicy.triage import DEFAULT_LLM_BASE, DEFAULT_LLM_MODEL
from docpolicy import frontmatter as fm
from docpolicy.policy import GENERIC_TEMPLATE, POLICY_FILENAME, Policy, PolicyError, load

PRE_COMMIT = """\
#!/bin/sh
# docpolicy: block commits that violate the doc policy.
repo="$(git rev-parse --show-toplevel)" || exit 1
PYTHONPATH="$HOME/src/docpolicy${PYTHONPATH:+:$PYTHONPATH}" \\
  python3 -m docpolicy check --staged --repo "$repo" || {
  echo "docpolicy: commit blocked — fix the findings above or use --no-verify (discouraged)." >&2
  exit 1
}
"""


def _resolve_repo(args) -> Path:
    raw = getattr(args, "repo", None)
    if isinstance(raw, list):          # check/audit accept repeats; this is the 1st
        raw = raw[0] if raw else None
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def _resolve_repos(args) -> list:
    """Repos from --repo (repeatable) plus positional paths; default cwd.

    Order is preserved and duplicates are dropped, so `--repo . work` and
    `work --repo .` both mean the same thing."""
    given = list(getattr(args, "repo", None) or []) + list(getattr(args, "repos", None) or [])
    if not given:
        return [Path.cwd().resolve()]
    out = []
    for raw in given:
        p = Path(raw).expanduser().resolve()
        if p not in out:
            out.append(p)
    return out


def _load(args) -> tuple[Path, Policy]:
    repo = _resolve_repo(args)
    return repo, load(repo)


def _staged_files(repo: Path) -> set:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip().endswith(".md")}


def _emit(findings, as_json: bool, advisory: bool):
    summary = checks.summarize(findings)
    if as_json:
        print(json.dumps({
            "findings": [f.as_dict() for f in findings],
            "summary": summary,
        }, indent=2))
    else:
        for f in findings:
            loc = f"{f.path}: " if f.path else ""
            print(f"{f.severity.upper():5} [{f.code}] {loc}{f.message}")
        tag = "warnings" if advisory else "errors"
        print(f"— {summary['total']} findings "
              f"({summary['errors']} errors, {summary['warnings']} warnings)"
              + (" (advisory mode: exit 0)" if advisory else ""))
    blocked = (not advisory) and summary["errors"] > 0
    return 1 if blocked else 0


def _run_many(repos, args, mode: str) -> int:
    """check/audit over several repos. Mirrors docpolicy-all output; exit 1 if
    any repo has error findings, 2 if any repo has no usable policy."""
    results = []
    rc = 0
    for repo in repos:
        try:
            policy = load(repo)
        except PolicyError as e:
            print(f"docpolicy: {e}", file=sys.stderr)
            rc = 2
            continue
        if mode == "check":
            findings = checks.check(repo, policy, advisory=args.advisory)
        else:
            findings = checks.audit(repo, policy, advisory=args.advisory)
        results.append((repo, findings))
        if args.json:
            continue
        print(f"== {repo}")
        rc = max(rc, _emit(findings, False, args.advisory))

    if args.json:
        payload = [{
            "repo": str(repo),
            "findings": [f.as_dict() for f in findings],
            "summary": checks.summarize(findings),
        } for repo, findings in results]
        print(json.dumps({"repos": payload}, indent=2))
        if not args.advisory and any(p["summary"]["errors"] for p in payload):
            rc = max(rc, 1)
    return rc


def cmd_check(args) -> int:
    repos = _resolve_repos(args)
    if len(repos) > 1:
        if getattr(args, "stdin_file", None) or args.files:
            print("docpolicy: --files / --stdin-file apply to a single repo",
                  file=sys.stderr)
            return 2
        return _run_many(repos, args, "check")
    repo = repos[0]
    try:
        policy = load(repo)
    except PolicyError as e:
        print(f"docpolicy: {e}", file=sys.stderr)
        return 2
    only = None
    if args.staged:
        only = _staged_files(repo)
        if not only:
            return 0
    if args.files:
        try:
            only = {Path(f).resolve().relative_to(repo).as_posix() if Path(f).is_absolute()
                    else str(f) for f in args.files}
        except ValueError:
            print(f"docpolicy: --files path outside repo root {repo}", file=sys.stderr)
            return 2
    stdin_file = stdin_text = None
    if getattr(args, "stdin_file", None):
        stdin_file = str(args.stdin_file)
        stdin_text = sys.stdin.read()
    findings = checks.check(repo, policy, only=only, advisory=args.advisory,
                            stdin_file=stdin_file, stdin_text=stdin_text)
    return _emit(findings, args.json, args.advisory)


def cmd_audit(args) -> int:
    repos = _resolve_repos(args)
    if len(repos) > 1:
        return _run_many(repos, args, "audit")
    repo = repos[0]
    try:
        policy = load(repo)
    except PolicyError as e:
        print(f"docpolicy: {e}", file=sys.stderr)
        return 2
    findings = checks.audit(repo, policy, advisory=args.advisory)
    return _emit(findings, args.json, args.advisory)


def cmd_brief(args) -> int:
    try:
        repo, policy = _load(args)
    except PolicyError as e:
        print(f"docpolicy: {e}", file=sys.stderr)
        return 2
    lines = [f"doc policy — {repo}"]
    if args.md:
        lines.append("")
    for c in policy.classes:
        if not c["paths"]:
            continue
        extra = ""
        if c["mode"] == "ephemeral":
            extra = f", max_age={c['max_age_days']}d, pointer_required={c['require_pointer']}"
        lines.append(f"  {c['mode']:11} {c['id']}: {', '.join(c['paths'])}{extra}")
    lines += [
        "",
        "  invariant: every doc has frontmatter subject + status; for each subject",
        "  exactly ONE doc is status: active; any other copy is status: superseded",
        "  with superseded_by pointing at the active doc.",
        "  When updating a doc: edit the active one. To replace it: new doc gets",
        "  subject+active, old doc gets status: superseded + superseded_by: <new path>.",
    ]
    print("\n".join(lines))
    return 0


def cmd_init(args) -> int:
    repo = _resolve_repo(args)
    path = repo / POLICY_FILENAME
    if path.exists() and not args.force:
        print(f"docpolicy: {path} already exists (use --force to overwrite)")
        scaffold_inbox(repo, load(repo))
        return 2
    path.write_text(GENERIC_TEMPLATE, encoding="utf-8")
    print(f"docpolicy: wrote {path} — now edit the classes to match this repo")
    hooks = repo / ".githooks" / "pre-commit"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text(PRE_COMMIT, encoding="utf-8")
    hooks.chmod(0o755)
    r = subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo)
    if r.returncode == 0:
        print(f"docpolicy: installed {hooks} and set core.hooksPath=.githooks")
    scaffold_inbox(repo, load(repo))
    return r.returncode


def scaffold_inbox(repo: Path, policy: Policy) -> int:
    """Init completeness: create each inbox class's root directory. Git does not
    track empty dirs, so drop a .gitkeep in each fresh one (harmless once real
    captures land; triage promotes them out, the keep just stays)."""
    roots = []
    for c in policy.classes:
        if c["mode"] != "inbox":
            continue
        for pat in c["paths"]:
            head = pat.split("/")[0]
            if head and "*" not in head and head not in roots:
                roots.append(head)
    for head in sorted(roots):
        d = repo / head
        d.mkdir(parents=True, exist_ok=True)
        if not any(d.iterdir()):
            (d / ".gitkeep").write_text("", encoding="utf-8")
            print(f"docpolicy: scaffolded inbox dir {head}/ (kept with .gitkeep)")
    return 0


def cmd_fix(args) -> int:
    """Mechanical conformance: propose (and with --apply write) frontmatter for
    canonical docs that lack subject/status. Subject defaults to the filename stem."""
    try:
        repo, policy = _load(args)
    except PolicyError as e:
        print(f"docpolicy: {e}", file=sys.stderr)
        return 2
    docs = checks.collect(repo, policy)
    changed = 0
    for d in docs:
        if "canonical" not in {policy.class_mode(c) for c in d.classes}:
            continue
        if d.fm is not None and d.fm.get("subject") and d.fm.get("status"):
            continue
        stem = Path(d.rel).stem.lower().replace("_", "-")
        subject = d.fm.get("subject") or stem
        status = d.fm.get("status") or fm.STATUS_ACTIVE
        text = (repo / d.rel).read_text(encoding="utf-8")
        if d.had_fm and d.fm_ok:
            new = text.replace("---\n", f"---\nsubject: {subject}\nstatus: {status}\n", 1)
        else:
            new = f"---\nsubject: {subject}\nstatus: {status}\n---\n\n" + text
        if args.apply:
            (repo / d.rel).write_text(new, encoding="utf-8")
            print(f"fixed  {d.rel}  (subject: {subject}, status: {status})")
        else:
            print(f"would fix  {d.rel}  (subject: {subject}, status: {status})")
        changed += 1
    print(f"— {changed} doc(s) {'written' if args.apply else 'proposed'}"
          + ("" if args.apply else "  (re-run with --apply)"))
    return 0


def cmd_triage(args) -> int:
    """Inbox triage: propose promotions, and with --apply write them.

    Default is propose-only. Items carrying `needs` flags are never promoted
    without --force; with --queue they become tasks in the configured store."""
    from docpolicy import triage

    try:
        repo, policy = _load(args)
    except PolicyError as e:
        print(f"docpolicy: {e}", file=sys.stderr)
        return 2
    items = triage.scan(repo, policy)
    profiles = triage.corpus(repo, policy)
    proposals = [triage.propose(i, profiles, policy) for i in items]
    if args.llm and not args.no_llm:
        cfg = triage.llm_config()
        for p in proposals:
            triage.llm_refine(p, profiles, cfg)
    elif not args.no_llm:
        cfg = triage.llm_config()
        if cfg.get("key"):
            for p in proposals:
                triage.llm_refine(p, profiles, cfg)
        else:
            for p in proposals:
                p.llm = "no-key"

    promoted, queued = [], []
    if args.apply or args.force:
        for p in proposals:
            if p.needs and not args.force:
                continue
            res = triage.promote(repo, p, apply=True)
            p.promoted_to = res["to"]
            promoted.append(res)
    if args.queue:
        project = (args.project or policy.project or "").strip()
        db = policy.sources.get("issues")
        if not project:
            print("docpolicy: --queue needs a project code (pass --project or set "
                  "'project:' in .docpolicy.yaml)", file=sys.stderr)
        else:
            for p in proposals:
                if p.needs:
                    tid = triage.queue_task(db, project, p)
                    if tid:
                        queued.append((p.item.rel, tid))

    if args.json:
        payload = json.loads(triage.report(proposals, as_json=True))
        payload["promoted"] = promoted
        payload["queued"] = [{"item": i, "task_id": t} for i, t in queued]
        print(json.dumps(payload, indent=2))
    else:
        for res in promoted:
            print(f"promoted  {res['from']} -> {res['to']}  (subject: {res['subject']})")
        for rel, tid in queued:
            print(f"queued    {rel} -> task #{tid}")
        print(triage.report(proposals))
    return 0


def cmd_llm(args) -> int:
    """Show or set the optional model-layer config (~/.hermes/docpolicy/llm.json).

    The key is read from stdin (never an argv list: shell history and process
    listings leak argv). Model and base can be set without a key."""
    import getpass

    from docpolicy import triage

    if args.clear:
        try:
            triage.llm_config_path().unlink()
            print(f"docpolicy: removed {triage.llm_config_path()}")
        except FileNotFoundError:
            print(f"docpolicy: no config at {triage.llm_config_path()}")
        except OSError as e:
            print(f"docpolicy: cannot remove config: {e}", file=sys.stderr)
            return 2
        return 0

    if args.set_key:
        try:
            key = getpass.getpass("DOCPOLICY_LLM_KEY (input hidden, empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("docpolicy: cancelled", file=sys.stderr)
            return 1
        if not key:
            print("docpolicy: empty key — nothing written")
            return 1
        path = triage.llm_config_save({"key": key})
        print(f"docpolicy: key written to {path} (mode 0600)")

    if args.set_model or args.set_base:
        values = {}
        if args.set_model:
            values["model"] = args.set_model.strip()
        if args.set_base:
            values["base"] = args.set_base.strip()
        path = triage.llm_config_save(values)
        print(f"docpolicy: updated {path} ({', '.join(values)})")

    cfg = triage.llm_config()
    source = triage.llm_config_source()
    if args.json:
        print(json.dumps({"config_path": str(triage.llm_config_path()),
                          "source": source, "model": cfg["model"], "base": cfg["base"],
                          "key_set": bool(cfg["key"]), "env_model": bool(os.environ.get("DOCPOLICY_LLM_MODEL")),
                          "env_key": bool(os.environ.get("DOCPOLICY_LLM_KEY")
                                          or os.environ.get("OPENROUTER_API_KEY"))}, indent=2))
        return 0
    print(f"config    {triage.llm_config_path()}")
    print(f"key       {'set' if cfg['key'] else 'MISSING'}  (source: {source})")
    print(f"model     {cfg['model']}")
    print(f"base      {cfg['base']}")
    print(f"effect    {'model labels enabled (--llm, or default when a key is present)' if cfg['key'] else 'deterministic labels only'}")
    if cfg["key"] and (os.environ.get("DOCPOLICY_LLM_MODEL") or os.environ.get("DOCPOLICY_LLM_BASE")):
        print("note      environment variables override the config file")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="docpolicy", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--repo", action="append", metavar="PATH",
                        help="repo root; repeatable (default: cwd)")
        sp.add_argument("repos", nargs="*", metavar="PATH",
                        help="repo roots as positional paths (same as --repo)")
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.add_argument("--advisory", action="store_true",
                        help="report everything as warnings, always exit 0")

    c = sub.add_parser("check", help="fast check (C1-C3, frontmatter, archive)")
    c.add_argument("--staged", action="store_true", help="only staged files (pre-commit)")
    c.add_argument("--files", nargs="*", help="only these files (per-call mode)")
    c.add_argument("--stdin-file", metavar="RELPATH",
                   help="evaluate content from stdin as this repo-relative doc "
                        "(pre-write check; target = the virtual doc)")
    common(c)
    c.set_defaults(fn=cmd_check)

    a = sub.add_parser("audit", help="full audit (adds C4-C8)")
    common(a)
    a.set_defaults(fn=cmd_audit)

    b = sub.add_parser("brief", help="print the policy table (for agent context)")
    b.add_argument("--repo")
    b.add_argument("--md", action="store_true")
    b.set_defaults(fn=cmd_brief)

    i = sub.add_parser("init", help="write policy template + install git pre-commit")
    i.add_argument("--repo")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)

    f = sub.add_parser("fix", help="propose/apply mechanical frontmatter fixes")
    f.add_argument("--repo")
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--apply", action="store_true")
    f.set_defaults(fn=cmd_fix)

    t = sub.add_parser("triage", help="propose (with --apply: promote) inbox captures")
    t.add_argument("--repo")
    t.add_argument("--json", action="store_true", help="machine-readable output")
    t.add_argument("--apply", action="store_true",
                   help="promote items with no open questions (never a flagged item)")
    t.add_argument("--force", action="store_true",
                   help="promote every item, including flagged ones")
    t.add_argument("--queue", action="store_true",
                   help="file a task per flagged item in the configured store")
    t.add_argument("--project", help="project code for --queue (default: policy 'project:')")
    t.add_argument("--llm", action="store_true",
                   help="use the optional model layer for labels (needs DOCPOLICY_LLM_KEY)")
    t.add_argument("--no-llm", action="store_true",
                   help="deterministic labels only")
    t.set_defaults(fn=cmd_triage)

    l = sub.add_parser("llm", help="show/set the optional model-layer config")
    l.add_argument("--repo")
    l.add_argument("--json", action="store_true")
    l.add_argument("--set-key", action="store_true",
                   help="read the API key from stdin (hidden) and store it 0600")
    l.add_argument("--set-model", metavar="MODEL",
                   help=f"set the model (default {DEFAULT_LLM_MODEL})")
    l.add_argument("--set-base", metavar="URL",
                   help=f"set the API base URL (default {DEFAULT_LLM_BASE})")
    l.add_argument("--clear", action="store_true", help="remove the config file")
    l.set_defaults(fn=cmd_llm)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
