"""docpolicy Hermes plugin — enforcement surface for the docpolicy checker.

Contains NO rules of its own: every decision comes from the docpolicy checker
subprocess (python3 -m docpolicy, stdlib-only). All hook paths are fail-open:
on any internal error they log and return None so a broken plugin can never
brick the agent.
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_NAME = "docpolicy"
POLICY_FILENAME = ".docpolicy.yaml"
# PYTHONPATH for the checker subprocess: the repo root that contains this
# plugin package (resolved through the install symlink), overridable for
# layouts that keep the checker somewhere else.
DOCPOLICY_SRC = os.environ.get("DOCPOLICY_SRC") or str(Path(__file__).resolve().parent.parent)
CHECKER_TIMEOUT_S = 5.0
# Deterministic triage scans the corpus (fast). With the LLM label layer the
# model costs ~60s per doc and its own HTTP timeout is 45s — so any triage that
# may apply LLM labels needs a much larger budget (see tool_docpolicy_triage).
TRIAGE_TIMEOUT_S = 8.0
TRIAGE_LLM_TIMEOUT_S = 120.0
INBOX_NOTICE_TTL_S = 120.0    # pre_llm_call fires every turn: keep the sweep cached

# Only these tool names are intercepted; only *.md paths are checked.
WATCHED_TOOLS = {"write_file", "patch", "create_file"}
PATH_KEYS = ("path", "file_path", "filepath", "target_path")

# Error-severity findings with these codes block the tool call.
BLOCK_CODES = {"FM", "C2", "C7", "C9"}

DEFAULT_CONFIG = {"advisory": False, "repos": []}


# ---------------------------------------------------------------------------
# Paths, config, log
# ---------------------------------------------------------------------------

def plugin_home() -> Path:
    """Runtime state dir (config.json, advisory.log) — env-overridable for tests.

    Deliberately NOT under ~/.hermes/plugins/<id>/: that path is the install
    location (symlink to the repo); state must not live inside the repo."""
    override = os.environ.get("DOCPOLICY_PLUGIN_HOME")
    if override:
        return Path(override)
    return Path.home() / ".hermes" / "docpolicy"


def load_config():
    """Load config.json, creating the default file on first run. Never raises."""
    home = plugin_home()
    path = home / "config.json"
    try:
        if not path.is_file():
            home.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
            return dict(DEFAULT_CONFIG)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return dict(DEFAULT_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        if not isinstance(merged.get("repos"), list):
            merged["repos"] = []
        merged["advisory"] = bool(merged.get("advisory"))
        return merged
    except Exception as e:  # fail open
        logger.warning("docpolicy plugin: config load failed (%s); using defaults", e)
        return dict(DEFAULT_CONFIG)


def log_line(text: str, session_id: str = "") -> None:
    """Append one line to the advisory log. Never raises."""
    try:
        home = plugin_home()
        home.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sid = f" session={session_id}" if session_id else ""
        with (home / "advisory.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{ts}{sid} {text}\n")
    except Exception:
        pass


def log_findings(prefix: str, findings: list, session_id: str = "") -> None:
    """One advisory.log line per finding."""
    for f in findings:
        if isinstance(f, dict):
            log_line(f"{prefix} [{f.get('code', '?')}] {f.get('severity', '?')} "
                     f"{f.get('path', '')}: {f.get('message', '')}", session_id)
        else:
            log_line(f"{prefix} {f}", session_id)


# ---------------------------------------------------------------------------
# Pure decision logic (no hermes, no subprocess — unit-testable)
# ---------------------------------------------------------------------------

def is_watched_tool(tool_name) -> bool:
    return tool_name in WATCHED_TOOLS


def extract_file_path(args) -> str | None:
    """Pull the target file path out of tool args (defensive about key names)."""
    if not isinstance(args, dict):
        return None
    for key in PATH_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def is_markdown(path_str: str) -> bool:
    return path_str.lower().endswith(".md")


def find_repo_root(path_str: str):
    """Walk up from path (file or dir) to the nearest dir with .docpolicy.yaml."""
    try:
        p = Path(path_str).expanduser().resolve()
        if p.is_file():
            p = p.parent
        for candidate in [p, *p.parents]:
            if (candidate / POLICY_FILENAME).is_file():
                return candidate
    except Exception:
        pass
    return None


def _findings_for_target(findings: list, rel: str) -> list:
    out = []
    for f in findings:
        if isinstance(f, dict) and f.get("path", "") == rel:
            out.append(f)
    return out


def blocking_findings(findings: list, rel: str) -> list:
    """Error-severity BLOCK_CODES findings on the target file."""
    return [f for f in _findings_for_target(findings, rel)
            if f.get("severity") == "error" and f.get("code") in BLOCK_CODES]


def non_blocking_findings(findings: list, rel: str) -> list:
    """Everything else worth logging (warnings, non-block codes, other paths)."""
    blocked = set(id(f) for f in blocking_findings(findings, rel))
    return [f for f in findings if id(f) not in blocked]


def block_message(blocked: list, rel: str) -> str:
    lines = [f"docpolicy: write to {rel} blocked by repo doc policy:"]
    lines += [f"  {f.get('severity', 'error').upper()} [{f.get('code')}] {f.get('message', '')}"
              for f in blocked]
    lines.append("  Run docpolicy check/fix on this repo, or see README (docpolicy).")
    return "\n".join(lines)


def decide(findings: list, rel: str, advisory: bool):
    """Return ('block', message) or ('allow', None). advisory=True is log-only."""
    blocked = blocking_findings(findings, rel)
    if blocked and not advisory:
        return "block", block_message(blocked, rel)
    return "allow", None


# ---------------------------------------------------------------------------
# Checker subprocess (the only external call; failure => allow)
# ---------------------------------------------------------------------------

def run_checker(repo: Path, extra_args: list, timeout: float = CHECKER_TIMEOUT_S,
                stdin_text: str | None = None):
    """Run python3 -m docpolicy ... --json; returns (rc, parsed_json_or_None)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = DOCPOLICY_SRC + os.pathsep + env.get("PYTHONPATH", "")
    cmd = ["python3", "-m", "docpolicy"] + list(extra_args) + ["--json"]
    proc = subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True,
                          text=True, timeout=timeout, check=False,
                          input=stdin_text)
    payload = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, payload


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def _repos_allowed(repo_root: Path, cfg: dict) -> bool:
    repos = cfg.get("repos") or []
    if not repos:
        return True
    return str(repo_root) in [str(r) for r in repos]


def extract_content(args) -> str | None:
    """New-file content for write tools (str), else None."""
    if isinstance(args, dict):
        content = args.get("content")
        return content if isinstance(content, str) else None
    return None


def _checker_args(repo_root: Path, tool_name: str, args, rel: str, path_str: str):
    """Pre-write evaluation: when the tool carries the new content (write_file),
    evaluate the VIRTUAL doc from stdin so FM/C2 see the new state, not disk.
    For patch (content not cheaply reconstructable) fall back to on-disk state —
    the git pre-commit hook is the authoritative gate for patch results."""
    content = extract_content(args) if tool_name == "write_file" else None
    if content is not None:
        return ["check", "--repo", str(repo_root), "--stdin-file", rel], content
    return ["check", "--repo", str(repo_root), "--files", path_str], None


def _on_pre_tool_call(tool_name: str = "", args=None, task_id: str = "", **kwargs):
    """Blocking hook: block *.md writes that produce error-severity FM/C2/C7/C9."""
    session_id = str(kwargs.get("session_id", "") or "")
    try:
        if not is_watched_tool(tool_name):
            return None
        path_str = extract_file_path(args)
        if not path_str or not is_markdown(path_str):
            return None
        repo_root = find_repo_root(path_str)
        if repo_root is None:
            return None  # no policy file up the tree — not our scope
        cfg = load_config()
        if not _repos_allowed(repo_root, cfg):
            return None
        try:
            rel = Path(path_str).expanduser().resolve().relative_to(repo_root).as_posix()
        except ValueError:
            return None
        extra_args, stdin_text = _checker_args(repo_root, tool_name, args, rel, path_str)
        rc, payload = run_checker(repo_root, extra_args, stdin_text=stdin_text)
        findings = []
        if isinstance(payload, dict) and isinstance(payload.get("findings"), list):
            findings = payload["findings"]
        if rc not in (0, 1) or payload is None:
            # exit 2 = config error, timeout/parse error = checker trouble: fail open
            log_line(f"pre_tool_call checker failed rc={rc} on {rel}", session_id)
            return None
        action, message = decide(findings, rel, bool(cfg.get("advisory")))
        if action == "block":
            log_findings("pre_tool_call BLOCK", findings, session_id)
            return {"action": "block", "message": message}
        advisory = non_blocking_findings(findings, rel)
        if advisory:
            log_findings("pre_tool_call advisory", advisory, session_id)
        return None
    except subprocess.TimeoutExpired:
        log_line(f"pre_tool_call checker timeout on {args}", session_id)
        return None
    except Exception as e:
        log_line(f"pre_tool_call internal error: {e!r}", session_id)
        return None  # fail open


def _run_plain(repo: Path, extra_args: list, timeout: float = CHECKER_TIMEOUT_S):
    """Run python3 -m docpolicy ... (no --json) and return (rc, stdout)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = DOCPOLICY_SRC + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(["python3", "-m", "docpolicy"] + list(extra_args),
                          cwd=str(repo), env=env, capture_output=True, text=True,
                          timeout=timeout, check=False)
    return proc.returncode, proc.stdout


# --- inbox pickup ----------------------------------------------------------
# on_session_start is an OBSERVER hook (its return is ignored — see
# website/docs/user-guide/features/hooks.md), so it can only log. pre_llm_call
# is a DIRECTIVE hook: strings / {"context": ...} returns are joined into the
# user message. That is the pickup path: the agent is told about untriaged
# captures at the start of the turn that can act on them.

_inbox_cache = {"repo": "", "at": 0.0, "text": None}


def has_inbox_class(repo: Path) -> bool:
    """Cheap text scan — do not pay for a subprocess on repos without an inbox."""
    try:
        return "mode: inbox" in (repo / POLICY_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return False


def inbox_notice(repo: Path) -> str | None:
    """One line when the repo has untriaged captures, else None. TTL-cached
    because pre_llm_call runs every turn."""
    now = time.time()
    if _inbox_cache["repo"] == str(repo) and now - _inbox_cache["at"] < INBOX_NOTICE_TTL_S:
        return _inbox_cache["text"]
    text = None
    try:
        rc, payload = run_checker(repo, ["triage", "--repo", str(repo), "--no-llm"],
                                  timeout=TRIAGE_TIMEOUT_S)
        if isinstance(payload, dict) and payload.get("count"):
            n = payload["count"]
            needs = payload.get("needs_judgement", 0)
            text = (f"docpolicy inbox: {n} untriaged capture(s) in {repo.name}"
                    + (f", {needs} needing judgement" if needs else "")
                    + " — call docpolicy_triage for proposals; apply=true promotes"
                      " the clean ones.")
    except Exception as e:
        log_line(f"inbox notice failed: {e!r}")
        text = None
    _inbox_cache.update({"repo": str(repo), "at": now, "text": text})
    return text


def _on_pre_llm_call(**kwargs):
    """Inject the inbox notice into the turn. Fail-open, never blocks."""
    try:
        session_id = str(kwargs.get("session_id", "") or "")
        repo = Path.cwd().resolve()
        if not (repo / POLICY_FILENAME).is_file():
            return None
        if not _repos_allowed(repo, load_config()):
            return None
        if not has_inbox_class(repo):
            return None
        text = inbox_notice(repo)
        if not text:
            return None
        log_line(f"inbox notice | {text}", session_id)
        return {"context": text}
    except Exception:
        return None  # never raise from a directive hook


def _on_session_start(**kwargs):
    """Log the repo policy brief so it lands in the session context."""
    try:
        session_id = str(kwargs.get("session_id", "") or "")
        repo = Path.cwd().resolve()
        if not (repo / POLICY_FILENAME).is_file():
            return
        rc, out = _run_plain(repo, ["brief", "--repo", str(repo), "--md"])
        if rc != 0 or not out.strip():
            log_line(f"session_start brief rc={rc} (no output)", session_id)
            return
        for line in out.rstrip().splitlines():
            log_line(f"brief | {line}", session_id)
    except Exception:
        pass  # never raise from an observer


def _on_session_end(**kwargs):
    """Log a one-line summary of the repo check at session end."""
    try:
        session_id = str(kwargs.get("session_id", "") or "")
        repo = Path.cwd().resolve()
        rc, payload = run_checker(repo, ["check", "--repo", str(repo)])
        if isinstance(payload, dict):
            s = payload.get("summary", {})
            log_line(f"session_end check rc={rc} total={s.get('total', 0)} "
                     f"errors={s.get('errors', 0)} warnings={s.get('warnings', 0)} "
                     f"by_code={s.get('by_code', {})}", session_id)
        else:
            log_line(f"session_end check rc={rc} (no JSON)", session_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Plugin tools
# ---------------------------------------------------------------------------

BRIEF_SCHEMA = {
    "name": "docpolicy_brief",
    "description": (
        "Show the doc policy brief (classes + core invariant) for the current repo, "
        "from .docpolicy.yaml. Use to learn which doc classes exist and how "
        "subject/status frontmatter must be used before writing docs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string",
                     "description": "Repo root (default: current working directory)"},
        },
        "required": [],
    },
}

CHECK_SCHEMA = {
    "name": "docpolicy_check",
    "description": (
        "Run the docpolicy checker on a repo and return findings as JSON "
        "(codes C1-C11/FM with severity, path, message). Use to see documentation-"
        "drift violations before/after writing markdown docs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string",
                     "description": "Repo root (default: current working directory)"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "Optional: restrict to these repo-relative paths"},
        },
        "required": [],
    },
}


TRIAGE_SCHEMA = {
    "name": "docpolicy_triage",
    "description": (
        "Triage the repo inbox: raw captures dropped in an 'inbox' class directory "
        "get a proposed subject/title/description/tags and a promotion destination. "
        "Propose-only by default; pass apply=true to promote the items with no open "
        "questions (flagged items need force=true), or queue=true to file a task per "
        "flagged item in the configured task store."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string",
                     "description": "Repo root (default: current working directory)"},
            "apply": {"type": "boolean",
                      "description": "Promote items that carry no open questions"},
            "force": {"type": "boolean",
                      "description": "Promote every item, including flagged ones"},
            "queue": {"type": "boolean",
                      "description": "File a task for each flagged item"},
            "project": {"type": "string",
                        "description": "Project code for queued tasks (default: policy 'project:')"},
            "llm": {"type": "boolean",
                    "description": "Use the optional model layer for labels (needs DOCPOLICY_LLM_KEY)"},
            "no_llm": {"type": "boolean",
                       "description": "Deterministic labels only"},
        },
        "required": [],
    },
}


def tool_docpolicy_triage(args: dict, **kwargs) -> str:
    """Handler: docpolicy triage --json for a repo."""
    try:
        args = args or {}
        repo = Path((args.get("repo") or os.getcwd())).expanduser().resolve()
        if not (repo / POLICY_FILENAME).is_file():
            return json.dumps({"error": f"no {POLICY_FILENAME} in {repo}"})
        extra = ["triage", "--repo", str(repo)]   # run_checker appends --json
        for flag in ("apply", "force", "queue", "llm", "no_llm"):
            if args.get(flag):
                extra.append("--" + flag.replace("_", "-"))
        project = args.get("project")
        if isinstance(project, str) and project.strip():
            extra += ["--project", project.strip()]
        timeout = TRIAGE_LLM_TIMEOUT_S if not args.get("no_llm") else TRIAGE_TIMEOUT_S
        rc, payload = run_checker(repo, extra, timeout=timeout)
        if payload is None:
            return json.dumps({"error": f"triage exited {rc} without JSON",
                               "stderr_hint": "run docpolicy triage manually"})
        return json.dumps({"repo": str(repo), "exit_code": rc, **payload})
    except Exception as e:
        return json.dumps({"error": f"docpolicy_triage failed: {e}"})


def tool_docpolicy_brief(args: dict, **kwargs) -> str:
    """Handler: docpolicy brief text for a repo."""
    try:
        args = args or {}
        repo = Path((args.get("repo") or os.getcwd())).expanduser().resolve()
        if not (repo / POLICY_FILENAME).is_file():
            return json.dumps({"error": f"no {POLICY_FILENAME} in {repo}"})
        rc, out = _run_plain(repo, ["brief", "--repo", str(repo), "--md"])
        if rc != 0:
            return json.dumps({"error": f"docpolicy brief exited {rc}"})
        return json.dumps({"repo": str(repo), "brief": out})
    except Exception as e:
        return json.dumps({"error": f"docpolicy_brief failed: {e}"})


def tool_docpolicy_check(args: dict, **kwargs) -> str:
    """Handler: docpolicy check --json for a repo (optionally scoped to files)."""
    try:
        args = args or {}
        repo = Path((args.get("repo") or os.getcwd())).expanduser().resolve()
        if not (repo / POLICY_FILENAME).is_file():
            return json.dumps({"error": f"no {POLICY_FILENAME} in {repo}"})
        extra = ["check", "--repo", str(repo)]
        files = args.get("files")
        if isinstance(files, list) and files:
            extra += ["--files"] + [str(f) for f in files]
        rc, payload = run_checker(repo, extra)
        if payload is None:
            return json.dumps({"error": f"checker exited {rc} without JSON",
                               "stderr_hint": "config error? run docpolicy check manually"})
        return json.dumps({"repo": str(repo), "exit_code": rc, **payload})
    except Exception as e:
        return json.dumps({"error": f"docpolicy_check failed: {e}"})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _cli_hooks():
    """Import the CLI module lazily.

    The loader execs this package under a name IT chooses (not necessarily
    'hermes_plugin'), so an absolute self-import breaks; a relative import is
    the reliable form, with a sys.path fallback for a non-package exec. Returns
    (None, None) if neither works — CLI registration is optional, the rest of
    the plugin still loads."""
    try:
        from .cli import docpolicy_command, register_cli
        return register_cli, docpolicy_command
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            import cli as _cli
            return _cli.register_cli, _cli.docpolicy_command
        except ImportError:
            return None, None


def register(ctx):
    """Wire tools and lifecycle hooks. Never raises past registration."""
    ctx.register_tool(name="docpolicy_brief", toolset=PLUGIN_NAME,
                      schema=BRIEF_SCHEMA, handler=tool_docpolicy_brief)
    ctx.register_tool(name="docpolicy_check", toolset=PLUGIN_NAME,
                      schema=CHECK_SCHEMA, handler=tool_docpolicy_check)
    ctx.register_tool(name="docpolicy_triage", toolset=PLUGIN_NAME,
                      schema=TRIAGE_SCHEMA, handler=tool_docpolicy_triage)
    register_cli, docpolicy_command = _cli_hooks()
    if register_cli is None:
        log_line("cli module unavailable — hermes docpolicy not registered")
    else:
        try:
            ctx.register_cli_command(
                name="docpolicy",
                help="Documentation policy: check, audit, brief, triage, fix, llm",
                setup_fn=register_cli,
                handler_fn=docpolicy_command,
                description=("Operate the docpolicy checker for any repo with a "
                             ".docpolicy.yaml: drift checks, policy brief, inbox triage "
                             "(promote or queue raw captures), and the optional "
                             "model-layer config."))
        except Exception as e:  # fail open: never break plugin registration
            log_line(f"register_cli_command failed: {e!r}")
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
