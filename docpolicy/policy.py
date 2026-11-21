"""Policy loading: .docpolicy.yaml at the repo root."""

import sys
from pathlib import Path

from docpolicy.miniyaml import YamlError, loads as yaml_loads

POLICY_FILENAME = ".docpolicy.yaml"

MODES = ("canonical", "generated", "archive", "ephemeral", "append-only", "exempt", "inbox")

CODES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C12", "FM")

DEFAULT_SEVERITY = {
    "C1": "error",  # classification (unclassified)
    "C2": "error",  # subject uniqueness (at most one active per subject)
    "C3": "error",  # supersession integrity (superseded_by must exist and be active)
    "C4": "error",  # generated freshness
    "C5": "warn",   # relative link resolution
    "C6": "warn",   # issue refs exist in the task store
    "C7": "error",  # ephemeral age / pointer requirement
    "C8": "warn",   # duplicate H1 across canonical docs
    "C9": "error",  # archive hygiene (archive class must not be active)
    "C10": "warn",  # tag collision (two active docs claiming one topic)
    "C11": "warn",  # inbox age (capture waiting for triage)
    "C12": "error", # assertion drift (spec assert: contradicts machine facts)
    "FM": "error",  # frontmatter contract (subject + status) on canonical docs
}

# Per-mode extra keys beyond id/mode/paths
MODE_EXTRA_KEYS = {
    "canonical": set(),
    "generated": {"generator", "facts"},
    "archive": set(),
    "ephemeral": {"max_age_days", "require_pointer"},
    "append-only": set(),
    "exempt": set(),
    "inbox": {"dest", "max_age_days"},
}

SKIP_DIRS = {".git", "node_modules", "target", ".venv", "__pycache__", ".pytest_cache"}

GENERIC_TEMPLATE = """\
# docpolicy — one policy per repo. Docs: see the docpolicy README.
# Class order matters: FIRST matching class wins — declare narrow classes
# (archive, ephemeral) BEFORE broad canonical globs.
version: 1

classes:
  - id: archive
    mode: archive
    paths: ["**/archive/**"]
  - id: scratch
    mode: ephemeral
    paths: [".hermes/plans/**"]
    max_age_days: 2
    require_pointer: true
  - id: inbox
    mode: inbox
    paths: ["inbox/**"]
    dest: "docs"
    max_age_days: 14
  - id: generated
    mode: generated
    paths: []
    generator: ""
  - id: exempt
    mode: exempt
    paths: []
  - id: docs
    mode: canonical
    paths: ["docs/**/*.md"]
"""


class PolicyError(ValueError):
    pass


class Policy:
    def __init__(self, repo: Path, data: dict):
        self.repo = repo
        self.path = repo / POLICY_FILENAME
        self.classes = []
        self.severity = dict(DEFAULT_SEVERITY)
        self.sources = {}
        rules = data.get("rules") or {}
        if isinstance(rules, dict):
            overrides = rules.get("severity") or {}
            if isinstance(overrides, dict):
                for code, sev in overrides.items():
                    if code not in CODES:
                        raise PolicyError(f"unknown check code {code!r}")
                    if sev not in ("warn", "error", "off"):
                        raise PolicyError(f"bad severity {sev!r} for {code}")
                    self.severity[code] = sev
            elif "severity" in rules:
                print(
                    f"docpolicy: {self.path}: rules.severity is not a mapping "
                    f"(inline maps are not supported, use block style); "
                    f"ignoring it, using default severities",
                    file=sys.stderr,
                )
        sources = data.get("sources") or {}
        if isinstance(sources, dict):
            self.sources = {k: Path(v).expanduser() for k, v in sources.items()}
        self.project = str(data.get("project") or "").strip()
        classes = data.get("classes") or []
        if not isinstance(classes, list):
            raise PolicyError("'classes' must be a list")
        seen_ids = set()
        for i, c in enumerate(classes):
            if not isinstance(c, dict):
                raise PolicyError(f"classes[{i}] must be a mapping")
            cid = c.get("id")
            mode = c.get("mode")
            if not cid or cid in seen_ids:
                raise PolicyError(f"classes[{i}]: missing or duplicate id {cid!r}")
            seen_ids.add(cid)
            if mode not in MODES:
                raise PolicyError(f"class {cid!r}: unknown mode {mode!r}")
            raw_paths = c.get("paths") or []
            if isinstance(raw_paths, str):
                raw_paths = [raw_paths]
            if not isinstance(raw_paths, list):
                raise PolicyError(f"class {cid!r}: 'paths' must be a list")
            extra = MODE_EXTRA_KEYS[mode]
            unknown = set(c) - {"id", "mode", "paths"} - extra
            if unknown:
                raise PolicyError(
                    f"class {cid!r} (mode {mode}): unsupported keys {sorted(unknown)}"
                )
            self.classes.append(
                {
                    "id": cid,
                    "mode": mode,
                    "paths": [str(p) for p in raw_paths],
                    "generator": c.get("generator"),
                    "facts": c.get("facts"),
                    "max_age_days": c.get("max_age_days"),
                    "require_pointer": bool(c.get("require_pointer")),
                    "dest": c.get("dest"),
                }
            )
        self.classes_by_id = {c["id"]: c for c in self.classes}

    def class_for(self, rel_posix: str):
        """First-match-wins on class order (declare narrow classes — archive,
        ephemeral — before broad canonical globs). Returns 0 or 1 class ids."""
        from docpolicy.classify import match

        for c in self.classes:
            for pat in c["paths"]:
                if match(pat, rel_posix):
                    return [c["id"]]
        return []

    def class_mode(self, class_id: str) -> str:
        c = self.classes_by_id.get(class_id)
        return c["mode"] if c else "exempt"


def load(repo: Path) -> Policy:
    repo = Path(repo).expanduser().resolve()
    path = repo / POLICY_FILENAME
    if not path.is_file():
        raise PolicyError(f"no {POLICY_FILENAME} in {repo} — run: docpolicy init --repo {repo}")
    try:
        data = yaml_loads(path.read_text(encoding="utf-8"))
    except YamlError as e:
        raise PolicyError(f"{path}: {e}") from e
    return Policy(repo, data)
