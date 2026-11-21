"""Minimal YAML subset parser (stdlib only, no PyYAML dependency).

Supported subset — deliberately small, everything in .docpolicy.yaml and doc
frontmatter fits it:
  - comments (full-line and trailing ' #...' outside quotes)
  - mappings:  key: value   /   key:            (nested block)
  - lists:     - item       /   - key: value   (list of one-line or block maps)
  - scalars:   bare/quoted strings, ints, floats, true/false/null
Indentation: spaces only. Tabs are rejected.
"""


class YamlError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    out, in_s, in_d = [], False, False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif c == "#" and not in_s and not in_d and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


def _split_flow(s: str):
    """Split a flow sequence body on top-level commas, respecting quotes."""
    items, buf, in_s, in_d, depth = [], "", False, False, 0
    for c in s:
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif c in "[{" and not in_s and not in_d:
            depth += 1
        elif c in "]}" and not in_s and not in_d:
            depth -= 1
        if c == "," and not in_s and not in_d and depth == 0:
            items.append(buf)
            buf = ""
        else:
            buf += c
    if buf.strip():
        items.append(buf)
    return items


def _scalar(tok: str):
    tok = tok.strip()
    if tok == "":
        return None
    if tok[0] == "[" and tok[-1] == "]":
        return [_scalar(x) for x in _split_flow(tok[1:-1])]
    if (tok[0] == tok[-1] == "'" and len(tok) >= 2) or (
        tok[0] == tok[-1] == '"' and len(tok) >= 2
    ):
        return tok[1:-1]
    low = tok.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def _split_key(s: str):
    """Split 'key: value' respecting quotes. Returns (key, rest) or None."""
    in_s = in_d = False
    for i, c in enumerate(s):
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif c == ":" and not in_s and not in_d:
            if i + 1 >= len(s) or s[i + 1] in " \t":
                return s[:i].strip(), s[i + 1 :].strip()
    return None


def _parse_block(lines, idx, indent):
    """Parse a block starting at lines[idx] with the given indent. Returns (obj, next_idx)."""
    # List block?
    if lines[idx][1].lstrip().startswith("- "):
        items = []
        while idx < len(lines) and lines[idx][0] == indent and lines[idx][1].lstrip().startswith("- "):
            content = lines[idx][1].lstrip()[2:]
            first = _split_key(content)
            if first is None:
                items.append(_scalar(content))
                idx += 1
            else:
                # list-of-maps: first pair inline, rest of the map at deeper indent
                item_indent = indent + (lines[idx][1].index("-") ) + 2
                k, v = first
                item = {}
                # inline value or nested block under the same key
                if v == "":
                    sub, idx = _parse_block(lines, idx + 1, _next_indent(lines, idx + 1, item_indent))
                    item[k] = sub
                else:
                    item[k] = _scalar(v)
                    idx += 1
                # continue map at the same visual column as the inline key
                while idx < len(lines) and lines[idx][0] == item_indent and not lines[idx][1].lstrip().startswith("- "):
                    pair = _split_key(lines[idx][1].strip())
                    if pair is None:
                        raise YamlError(f"line {lines[idx][2]}: expected 'key: value' in list item")
                    k2, v2 = pair
                    if v2 == "":
                        sub, idx = _parse_block(lines, idx + 1, _next_indent(lines, idx + 1, item_indent + 1))
                        item[k2] = sub
                    else:
                        item[k2] = _scalar(v2)
                        idx += 1
                items.append(item)
        return items, idx
    # Mapping block
    mapping = {}
    while idx < len(lines) and lines[idx][0] == indent:
        text = lines[idx][1].strip()
        if text.startswith("- "):
            break
        pair = _split_key(text)
        if pair is None:
            raise YamlError(f"line {lines[idx][2]}: expected 'key: value'")
        k, v = pair
        if v in (">", "|", ">-", "|-", ">+", "|+"):
            # Block scalar: collect deeper-indented lines until dedent.
            body_lines = []
            idx += 1
            while idx < len(lines) and lines[idx][0] > indent:
                body_lines.append(lines[idx][1])
                idx += 1
            joiner = "\n" if v[0] == "|" else " "
            mapping[k] = joiner.join(body_lines)
            continue
        if v == "":
            nidx = idx + 1
            if nidx < len(lines) and lines[nidx][0] > indent:
                sub, idx = _parse_block(lines, nidx, lines[nidx][0])
                mapping[k] = sub
            elif nidx < len(lines) and lines[nidx][0] == indent and lines[nidx][1].lstrip().startswith("- "):
                sub, idx = _parse_block(lines, nidx, indent)
                mapping[k] = sub
            else:
                mapping[k] = None
                idx = nidx
        else:
            mapping[k] = _scalar(v)
            idx += 1
    return mapping, idx


def _next_indent(lines, idx, fallback):
    if idx < len(lines):
        return lines[idx][0]
    return fallback


def loads(text: str):
    """Parse the supported YAML subset. Returns dict (top level must be a mapping)."""
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" "))]:
            raise YamlError(f"line {n}: tabs are not allowed in indentation")
        clean = _strip_comment(raw)
        if clean.strip() == "":
            continue
        indent = len(clean) - len(clean.lstrip(" "))
        lines.append((indent, clean.strip(), n))
    if not lines:
        return {}
    if lines[0][1].startswith("- "):
        raise YamlError("top level must be a mapping")
    obj, idx = _parse_block(lines, 0, lines[0][0])
    if idx != len(lines):
        raise YamlError(f"line {lines[idx][2]}: unexpected indentation")
    if not isinstance(obj, dict):
        raise YamlError("top level must be a mapping")
    return obj
