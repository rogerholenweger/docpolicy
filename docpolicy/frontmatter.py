"""Markdown frontmatter handling (--- delimited), parsed with the miniyaml subset."""

from pathlib import Path

from docpolicy.miniyaml import loads as _yaml_loads

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_ARCHIVED = "archived"
STATUS_DRAFT = "draft"
KNOWN_STATUSES = {STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_ARCHIVED, STATUS_DRAFT}

REQUIRED_CANONICAL_FIELDS = ("subject", "status")


def parse(text: str):
    """Return (frontmatter: dict|None, body: str, had_frontmatter: bool)."""
    if not text.startswith("---"):
        return None, text, False
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return None, text, False
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            try:
                fm = _yaml_loads(fm_text)
            except ValueError:
                return None, text, True  # malformed frontmatter: caller flags it
            return fm, body, True
    return None, text, True  # unterminated frontmatter


def parse_file(path) -> dict:
    """Read a file and return its frontmatter dict ({} when absent/malformed)."""
    fm, _, _ = parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    return fm if isinstance(fm, dict) else {}
