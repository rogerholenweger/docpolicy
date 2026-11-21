"""Path classification: gitignore-style ** globs -> regex, matched on posix relpaths."""

import re


def glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a glob with ** support into a compiled regex.

    'docs/specs/*.md'    matches one level under docs/specs/
    'docs/**/*.md'       matches any depth
    '**/archive/**'      matches any path containing an archive segment
    """
    out = []
    i = 0
    pat = pattern.strip().lstrip("/")
    while i < len(pat):
        c = pat[i]
        if pat[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pat[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def match(pattern: str, rel_posix: str) -> bool:
    return glob_to_regex(pattern).match(rel_posix) is not None
