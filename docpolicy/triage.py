"""Inbox triage: turn raw captures into promoted, conformant docs.

Design rules
------------
1. Deterministic first. Every proposal (subject, tags, title, description,
   destination, flags) is computed from the capture text and the existing
   corpus. Triage works with no model configured and no network.
2. The optional model layer only *refines labels* — title, description, tags,
   subject suggestion. It never decides where a file goes, and it is never
   required. Fail-open: any error degrades to the deterministic proposal.
3. Nothing is destructive without --apply. Triage proposes; the caller applies.
4. Subject is document identity, not topic. docpolicy's invariant is at most
   ONE active doc per subject, so promotion never reuses an existing active
   subject — related topics are linked through tags (see check C10), and a
   capture whose subject is already active is flagged as a supersession
   candidate instead.
"""

import json
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from docpolicy import frontmatter as fm
from docpolicy.policy import Policy

WORD = re.compile(r"[a-z][a-z0-9+#_.-]{2,}")
H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
TEXT_EXTS = {".md", ".markdown", ".txt"}
CONVERT_EXTS = {".pdf", ".html", ".htm", ".docx", ".epub", ".rtf", ".odt"}

STOPWORDS = set("""
the and for with that this from are was were has have had not but you your our
its it's their they them there here what when where which while who whom why how
all any both each few more most other some such only own same than too very can
will just should now about into over after before between under above again
also then once because during through out off down up no nor if or as at by in
on to of is are be been being do does did doing so we us he she his her him i
me my mine ours yours theirs one two three new use used using make makes made
get gets got give gives given take takes taken see sees seen note notes about
may might must shall per via etc eg ie figure table section page
against article building idea read today separately related based includes
including within without across between toward towards general specific
complete various several multiple first second third last next previous
""".split())


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def slugify(text: str, maxlen: int = 60) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:maxlen].rstrip("-")) or "untitled"


def word_tokens(text: str) -> list:
    out = []
    for w in WORD.findall(text.lower()):
        w = w.strip("._-")          # 'filter.' / 'e4.' must not become tags
        if len(w) < 3 or w in STOPWORDS:
            continue
        out.append(w)
        if "-" in w:                # 'rook-endings' must also match 'rook endings'
            out.extend(p for p in w.split("-") if len(p) >= 3 and p not in STOPWORDS)
    return out


def token_set(text: str) -> set:
    return set(word_tokens(text))


def first_paragraph(body: str) -> str:
    for block in re.split(r"\n\s*\n", body):
        b = block.strip()
        if not b or b.startswith(("#", ">", "|", "```", "-", "*", "1.")):
            continue
        b = re.sub(r"[`*_\[\]]", "", b)
        b = re.sub(r"\s+", " ", b).strip()
        if len(b) > 40:
            return b
    return ""


def wrap(text: str, width: int = 76, indent: str = "  ") -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(indent + cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(indent + cur)
    return "\n".join(lines)


def _yaml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dedupe_plural(tags: list) -> list:
    """'endgame' and 'endgames' are one tag wearing two hats — keep the shorter."""
    out = []
    for t in tags:
        stem = t[:-1] if t.endswith("s") and len(t) > 3 else t
        if any((o == stem) or (o == stem + "s") for o in out):
            continue
        out.append(stem if t.endswith("s") and t[:-1] in out else t)
    return out


def _as_list(value) -> list:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("[") and v.endswith("]"):
            return [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        return [v] if v else []
    return []


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@dataclass
class InboxItem:
    rel: str
    path: Path
    dest: str
    text: str
    body: str
    own_fm: dict
    title: str
    tokens: set
    paragraph: str
    size: int
    age_days: float
    needs: list = field(default_factory=list)

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()


def inbox_classes(policy: Policy) -> list:
    return [c for c in policy.classes if c["mode"] == "inbox" and c["paths"]]


def scan(repo: Path, policy: Policy) -> list:
    """All captures currently sitting in an inbox class."""
    items = []
    now = time.time()
    for c in inbox_classes(policy):
        dest = c.get("dest") or "inbox/promoted"
        for pat in c["paths"]:
            # pathlib's '**' matches directories, not files, so walk the static
            # prefix instead — and re-check first-match-wins, since a narrower
            # class declared earlier (inbox/archive/**, say) must still win.
            base = pat.split("*")[0].rstrip("/")
            base_dir = (repo / base) if base else repo
            if not base_dir.is_dir():
                continue
            for p in sorted(base_dir.rglob("*")):
                if not p.is_file() or p.name.startswith("."):
                    continue
                rel = p.relative_to(repo).as_posix()
                if c["id"] not in policy.class_for(rel):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                fmd, body, _had = fm.parse(text)
                if fmd is not None:
                    body = body
                else:
                    fmd = {}
                m = H1.search(body)
                title = (fmd.get("title") or "").strip() or (
                    m.group(1).strip() if m else p.stem.replace("_", " ").replace("-", " ").strip())
                item = InboxItem(
                    rel=rel, path=p, dest=dest, text=text, body=body, own_fm=fmd,
                    title=title, tokens=token_set(title + "\n" + body),
                    paragraph=(fmd.get("description") or "").strip() or first_paragraph(body),
                    size=p.stat().st_size,
                    age_days=(now - p.stat().st_mtime) / 86400,
                )
                if item.ext in CONVERT_EXTS:
                    item.needs.append(f"convert first: {item.ext} capture needs a text extraction")
                elif item.ext not in TEXT_EXTS:
                    item.needs.append(f"unknown extension {item.ext}: promote manually")
                if item.size > 60_000:
                    item.needs.append("large capture (>60KB): consider splitting before promotion")
                items.append(item)
    return items


# ---------------------------------------------------------------------------
# Corpus profile + deterministic proposal
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    rel: str
    subject: str
    tags: list
    tokens: set


def corpus(repo: Path, policy: Policy) -> list:
    """Profiles of already-promoted (canonical) docs, for topic linking."""
    from docpolicy import checks

    out = []
    for d in checks.collect(repo, policy):
        if "canonical" not in {policy.class_mode(c) for c in d.classes}:
            continue
        subject = (d.fm.get("subject") or "").strip()
        tags = _as_list(d.fm.get("tags"))
        title = str(d.fm.get("title") or "").strip()
        desc = str(d.fm.get("description") or "").strip()
        m = H1.search(d.body)
        blob = " ".join([subject, title, desc, m.group(1) if m else "", " ".join(tags)])
        out.append(Profile(d.rel, subject, tags, token_set(blob)))
    return out


@dataclass
class Proposal:
    item: InboxItem
    subject: str
    title: str
    description: str
    tags: list
    type: str
    related: list = field(default_factory=list)   # (rel, score)
    subject_owner: str = ""                       # existing active doc with this subject
    needs: list = field(default_factory=list)
    llm: str = "off"                              # off | applied | failed | no-key
    confidence: float = 0.0
    promoted_to: str = ""                         # set by the CLI after --apply

    def as_dict(self) -> dict:
        return {
            "rel": self.item.rel,
            "dest": self.item.dest,
            "subject": self.subject,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "type": self.type,
            "related": [{"path": r, "score": round(s, 3)} for r, s in self.related],
            "subject_owner": self.subject_owner,
            "needs": self.needs,
            "llm": self.llm,
            "confidence": round(self.confidence, 2),
            "promoted_to": self.promoted_to,
            "age_days": round(self.item.age_days, 1),
        }


def propose(item: InboxItem, profiles: list, policy: Policy) -> Proposal:
    scores = []
    for pr in profiles:
        if not pr.tokens or not item.tokens:
            continue
        inter = len(item.tokens & pr.tokens)
        if inter < 2:
            continue
        union = len(item.tokens | pr.tokens)
        scores.append((pr.rel, inter / union, pr.subject, pr.tags))
    scores.sort(key=lambda t: -t[1])
    related = [(rel, sc) for rel, sc, _s, _t in scores[:3]]

    stem_subject = slugify(item.path.stem)
    title = item.title
    subject = slugify(item.own_fm.get("subject") or stem_subject)

    # tags: capture tokens first, then the tags of the closest existing docs
    tag_counts = {}
    for w in word_tokens(title):
        tag_counts[w] = tag_counts.get(w, 0) + 3
    for w in word_tokens(item.paragraph):
        tag_counts[w] = tag_counts.get(w, 0) + 1
    for w in word_tokens(item.body[:4000]):
        tag_counts[w] = tag_counts.get(w, 0) + 1
    own_tags = _as_list(item.own_fm.get("tags"))
    ranked = [t for t, _ in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    tags = list(dict.fromkeys(own_tags + _dedupe_plural(ranked)[:6]))
    for _rel, sc, _subj, ptags in scores[:1]:
        if sc >= 0.08:
            tags = tags + [t for t in ptags[:3] if t not in tags]
    tags = list(dict.fromkeys(_dedupe_plural(tags)))[:8]

    prop = Proposal(
        item=item, subject=subject, title=title,
        description=(item.own_fm.get("description") or item.paragraph or title).strip(),
        tags=tags,
        type=str(item.own_fm.get("type") or "Research Note"),
        related=related,
        confidence=0.5 + (0.25 if item.tokens else 0.0),
    )
    prop.needs = list(item.needs)

    # subject identity: never promote a second ACTIVE doc onto a live subject
    for pr in profiles:
        if pr.subject and pr.subject == subject:
            prop.subject_owner = pr.rel
            prop.needs.append(
                f"subject {subject!r} is already active at {pr.rel} — this capture "
                "supersedes it (set the old doc status: superseded + superseded_by) "
                "or rename the subject")
            break

    if len(scores) >= 2 and scores[1][1] and scores[0][1] / max(scores[1][1], 1e-9) < 1.25 \
            and scores[0][1] > 0.04:
        prop.needs.append(
            f"ambiguous topic: {scores[0][0]} and {scores[1][0]} score within 25% — "
            "confirm the tags before promoting")
    if not scores:
        prop.confidence = max(0.3, prop.confidence - 0.2)  # new topic: tags are guesses
    return prop


# ---------------------------------------------------------------------------
# Optional model layer (labels only; never required, never authoritative)
# ---------------------------------------------------------------------------

LLM_PROMPT = """You label a captured document for a documentation knowledge base.

Return STRICT JSON only, no prose, with these keys:
  title        short human title (<= 80 chars)
  description  one or two sentences, factual, no marketing (<= 240 chars)
  tags         3-8 lowercase tags, hyphenated, specific (no generic words)
  subject      lowercase hyphenated slug identifying THIS document
               (not a topic you would reuse for another document)
  type         one of: Research Note, Spec, Runbook, Evidence, Thesis, Reference
  confidence   0.0-1.0, your confidence in the labels

Rules: never invent facts not present in the text. If the text is too thin to
label, return confidence below 0.3. Existing subjects you may link to via tags:
{existing}
Text:
---
{text}
---"""


DEFAULT_LLM_MODEL = "qwen/qwen3.8-flash"
DEFAULT_LLM_BASE = "https://openrouter.ai/api/v1"


def llm_config_path() -> Path:
    """Where the optional model layer is configured. Env override for tests.

    Deliberately NOT ~/.hermes/config.yaml (the agent's own provider config):
    the checker is standalone and should never read another tool's credentials."""
    override = os.environ.get("DOCPOLICY_LLM_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "docpolicy" / "llm.json"


def llm_config_load() -> dict:
    """{key, model, base, timeout} from the config file; {} when absent/broken."""
    try:
        data = json.loads(llm_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("key", "model", "base", "timeout") if k in data}


def llm_config_save(values: dict) -> Path:
    """Merge into the config file (0600). Does not require a key — model/base
    alone are fine and useful without one."""
    path = llm_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = llm_config_load()
    data.update({k: v for k, v in values.items() if v not in (None, "")})
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def llm_config() -> dict:
    """Resolution order: environment > config file > defaults.

    env: DOCPOLICY_LLM_KEY (or OPENROUTER_API_KEY), DOCPOLICY_LLM_MODEL,
    DOCPOLICY_LLM_BASE, DOCPOLICY_LLM_TIMEOUT.
    """
    file_cfg = llm_config_load()
    key = (os.environ.get("DOCPOLICY_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY")
           or file_cfg.get("key") or "")
    return {
        "key": str(key).strip(),
        "base": str(os.environ.get("DOCPOLICY_LLM_BASE") or file_cfg.get("base")
                    or DEFAULT_LLM_BASE).rstrip("/"),
        "model": str(os.environ.get("DOCPOLICY_LLM_MODEL") or file_cfg.get("model")
                     or DEFAULT_LLM_MODEL),
        "timeout": float(os.environ.get("DOCPOLICY_LLM_TIMEOUT")
                         or file_cfg.get("timeout") or 45),
    }


def llm_config_source() -> str:
    """Which layer supplied the key — for `docpolicy llm` output."""
    if os.environ.get("DOCPOLICY_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY"):
        return "environment"
    if llm_config_load().get("key"):
        return "config file"
    return "unset (deterministic-only)"


def llm_refine(prop: Proposal, profiles: list, cfg: dict) -> None:
    """Refine labels with a cheap model. Fail-open: on any error the
    deterministic proposal stands and prop.llm records why."""
    if not cfg.get("key"):
        prop.llm = "no-key"
        return
    existing = ", ".join(sorted({p.subject for p in profiles if p.subject})[:40]) or "(none)"
    text = (prop.item.title + "\n\n" + prop.item.body)[:4000]
    body = json.dumps({
        "model": cfg["model"],
        "temperature": 0,
        "messages": [{"role": "user",
                      "content": LLM_PROMPT.format(existing=existing, text=text)}],
    }).encode()
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        content = payload["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0)) if m else {}
        if not isinstance(data, dict):
            raise ValueError("model returned non-object")
        for key, target in (("title", "title"), ("description", "description"),
                            ("subject", "subject"), ("type", "type")):
            v = str(data.get(key) or "").strip()
            if v:
                setattr(prop, target, slugify(v) if target == "subject" else v)
        tags = _as_list(data.get("tags"))
        if tags:
            prop.tags = [slugify(t) for t in tags][:8]
        try:
            prop.confidence = max(prop.confidence, float(data.get("confidence") or 0))
        except (TypeError, ValueError):
            pass
        prop.llm = "applied"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        prop.llm = f"failed: {type(e).__name__}"
        return


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------

def render_frontmatter(prop: Proposal, extra: dict | None = None) -> str:
    """ONE frontmatter block carrying both docpolicy (subject/status) and OKF
    (type/title/description/tags/timestamp) fields — a second block would be
    invisible to the brain's FTS index."""
    fields = {
        "subject": prop.subject,
        "status": fm.STATUS_ACTIVE,
        "type": prop.type,
        "title": prop.title,
        "description": prop.description,
        "tags": prop.tags,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": prop.item.rel,
    }
    if extra:
        fields.update({k: v for k, v in extra.items() if v})
    lines = ["---"]
    for k, v in fields.items():
        if k == "tags":
            lines.append("tags: [" + ", ".join(_yaml_str(t) for t in v) + "]")
        elif k == "description":
            lines.append("description: >")
            lines.append(wrap(str(v)))
        elif k in ("title", "type", "source", "timestamp", "subject", "status"):
            lines.append(f"{k}: {_yaml_str(v)}" if k == "title" else f"{k}: {v}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def promote(repo: Path, prop: Proposal, apply: bool = False) -> dict:
    """Write the frontmatter block and move the capture into its dest dir."""
    item = prop.item
    dest_dir = repo / prop.item.dest
    target = dest_dir / (item.path.stem + ".md")
    n = 2
    while target.exists() and target != item.path:
        target = dest_dir / f"{item.path.stem}-{n}.md"
        n += 1
    body = item.body.lstrip("\n")
    own = {k: v for k, v in item.own_fm.items()
           if k in ("type", "title", "description", "tags") and v}
    new_text = render_frontmatter(prop, own) + "\n" + body
    result = {"from": item.rel, "to": target.relative_to(repo).as_posix(),
              "subject": prop.subject, "applied": bool(apply)}
    if not apply:
        return result
    dest_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")
    if target.resolve() != item.path.resolve():
        item.path.unlink()
    result["bytes"] = len(new_text.encode())
    return result


# ---------------------------------------------------------------------------
# Queue handoff (items that need a human decision)
# ---------------------------------------------------------------------------

def queue_task(db, project: str, prop: Proposal) -> int | None:
    if not db or not Path(db).is_file():
        return None
    title = f"triage: {prop.item.rel} needs a decision before promotion"
    notes = ("needs: " + "; ".join(prop.needs) + "\n"
             f"proposed subject: {prop.subject}\ntags: {', '.join(prop.tags)}\n"
             f"dest: {prop.item.dest}\n")
    try:
        con = sqlite3.connect(str(db))
        cur = con.execute(
            "INSERT INTO tasks (project, title, description, status, priority, notes) "
            "VALUES (?,?,?,?,?,?)",
            (project, title, "inbox triage: judgement required", "pending", 3, notes))
        con.commit()
        tid = cur.lastrowid
        con.close()
        return tid
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(proposals: list, as_json: bool = False) -> str:
    if as_json:
        return json.dumps({
            "items": [p.as_dict() for p in proposals],
            "count": len(proposals),
            "needs_judgement": sum(1 for p in proposals if p.needs),
        }, indent=2)
    if not proposals:
        return "inbox: empty — nothing to triage"
    lines = [f"inbox — {len(proposals)} capture(s)"]
    for p in proposals:
        mark = "!" if p.needs else " "
        state = f"PROMOTED -> {p.promoted_to}" if p.promoted_to else p.item.dest
        lines.append(f"{mark} {p.item.rel}  ({p.item.age_days:.0f}d, {p.item.size}B) -> {state}")
        lines.append(f"    subject: {p.subject}   tags: {', '.join(p.tags) or '-'}   llm: {p.llm}")
        if p.related:
            lines.append("    related: " + ", ".join(f"{r} ({s:.2f})" for r, s in p.related))
        for n in p.needs:
            lines.append(f"    needs: {n}")
    n_needs = sum(1 for p in proposals if p.needs)
    n_done = sum(1 for p in proposals if p.promoted_to)
    lines.append(f"— {len(proposals)} item(s), {n_done} promoted, {n_needs} needing judgement"
                 + ("  (run with --apply to promote the clean ones)"
                    if n_needs < len(proposals) and not n_done else ""))
    return "\n".join(lines)
