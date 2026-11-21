"""`hermes docpolicy ...` — thin passthrough to the docpolicy checker.

Deliberately rule-free, like the rest of this plugin: every argument after the
subcommand is forwarded verbatim to `python3 -m docpolicy <subcommand> ...`, so
new checker flags work here without touching this file.

    hermes docpolicy brief
    hermes docpolicy check   --repo ~/notes
    hermes docpolicy triage  --repo ~/notes --apply
    hermes docpolicy llm     --set-model qwen/qwen3.8-flash
    hermes docpolicy llm     --set-key        (key read from stdin, hidden)
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Same resolution rule as the plugin package; duplicated on purpose so this
# module stays importable without the package (and without a cycle).
DOCPOLICY_SRC = os.environ.get("DOCPOLICY_SRC") or str(Path(__file__).resolve().parent.parent)


def checker_argv():
    """Installed `docpolicy` executable if present, else the package source."""
    exe = shutil.which("docpolicy")
    if exe:
        return [exe], dict(os.environ)
    env = dict(os.environ)
    env["PYTHONPATH"] = DOCPOLICY_SRC + os.pathsep + env.get("PYTHONPATH", "")
    return ["python3", "-m", "docpolicy"], env

PASSTHROUGH = (
    ("check", "fast check (C1-C3, frontmatter, archive)"),
    ("audit", "full audit (adds C4-C8, links, tags)"),
    ("brief", "policy table for a repo"),
    ("triage", "inbox captures -> proposed promotions"),
    ("fix", "mechanical frontmatter fixes"),
    ("llm", "show/set the optional model-layer config"),
)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """One positional command + REMAINDER.

    NOT add_subparsers: inside a subparser a leading REMAINDER sees argv
    starting with '--repo', argparse tries to match it as a flag of the
    subparser and dies with "unrecognized arguments". A single positional
    command followed by REMAINDER forwards everything verbatim, which is what
    we want (checker flags change without touching this file).
    """
    subparser.add_argument(
        "docpolicy_cmd", nargs="?", choices=[name for name, _ in PASSTHROUGH],
        help="one of: " + ", ".join(f"{n} ({h})" for n, h in PASSTHROUGH))
    subparser.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="everything after the subcommand is passed to the checker unchanged")
    subparser.set_defaults(func=docpolicy_command)


def docpolicy_command(args: argparse.Namespace) -> int:
    cmd = getattr(args, "docpolicy_cmd", None)
    if not cmd:
        print("usage: hermes docpolicy {%s} [args...]"
              % "|".join(name for name, _ in PASSTHROUGH))
        print("       e.g. hermes docpolicy triage --repo ~/notes --apply")
        return 2
    argv = [cmd] + list(getattr(args, "args", []) or [])
    if "--repo" not in argv:
        argv += ["--repo", os.getcwd()]
    base, env = checker_argv()
    if base[0] == "python3" and not Path(DOCPOLICY_SRC).is_dir():
        print(f"docpolicy: no checker — uv tool install docpolicy "
              f"(or set DOCPOLICY_SRC; source not found at {DOCPOLICY_SRC})",
              file=sys.stderr)
        return 2
    try:
        proc = subprocess.run(base + argv,
                              env=env, cwd=os.getcwd(), timeout=120, check=False)
    except subprocess.TimeoutExpired:
        print("docpolicy: timed out after 120s", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"docpolicy: cannot run checker: {e}", file=sys.stderr)
        return 2
    return proc.returncode
