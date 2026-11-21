# docpolicy

Portable documentation-drift enforcement for repos maintained by a human + AI coding agent. One policy file per repo, one checker, several enforcement surfaces (Hermes plugin hooks, git pre-commit, pre-deploy gate, cron audits) that all run the same binary — no surface carries rules of its own.

Works on any machine: the checker is Python 3 stdlib only, no dependencies. The optional LLM label layer is the only network consumer, and it never enforces anything.

**Repo layout:**

| Path | Purpose |
|---|---|
| `docpolicy/` | checker package (`python3 -m docpolicy`) |
| `hermes_plugin/` | Hermes plugin: hooks, tools, `hermes docpolicy` CLI |
| `scripts/` | cron helpers + multi-repo runner: `docpolicy_all.sh` (check/audit across all repos, one command), `docpolicy_audit_email.sh` (weekly audit), `inbox_sweep.sh` (daily inbox digest, never promotes) |
| `deploy_plugin.sh` | install/refresh the plugin symlink, drift-guarded |
| `docs/` | design note for the inbox-triage subsystem (`docs/design.md`) |
| `tests/` | unittest suite (`python3 -m unittest discover tests`) |

## The invariant

The enforceable form of "only one doc". Every doc carries frontmatter:

```yaml
---
subject: sprint-plan            # the identity: ONE active doc per subject
status: active                  # active | superseded | archived | draft
superseded_by: plans/new.md     # required when status: superseded
---
```

- For every `subject` value, **exactly one** doc may have `status: active`.
- Any other doc with that subject must be `status: superseded` with `superseded_by` pointing at the active doc.
- Collisions, dangling pointers, missing frontmatter, stale generated docs, broken links, and dead issue references are all mechanical findings — nothing relies on memory or discipline.

Replacing a doc (the "versioning" half of the deal):

1. Write the new doc: `subject: ladder`, `status: active`.
2. Flip the old one: `subject: ladder`, `status: superseded`, `superseded_by: <new path>`.
3. Commit. The checker enforces 1+2 as a pair (`C2` + `C3`).

## CLI

```bash
docpolicy init   [--repo DIR] [--force]    # policy template + .githooks/pre-commit + core.hooksPath
docpolicy check  [--repo DIR ...] [DIR ...] [--staged] [--files P ...] [--stdin-file R] [--json] [--advisory]
docpolicy audit  [--repo DIR ...] [DIR ...] [--json] [--advisory]    # full pass (adds C4-C6, C8, C10, C12)
docpolicy brief  [--repo DIR] [--md]                   # policy table (injected into agent sessions)
docpolicy fix    [--repo DIR] [--dry-run] [--apply]    # mechanical frontmatter conformance
docpolicy triage [--repo DIR] [--json] [--apply] [--force] [--queue --project CODE] [--llm] [--no-llm]
docpolicy llm    [--json] [--set-key] [--set-model M] [--set-base URL] [--clear]
```

`check` and `audit` take several repos: `--repo` is repeatable and bare paths work
too (`docpolicy check ~/src/a ~/src/b`). With more than one, output is a block per
repo and `--json` wraps as `{"repos": [...]}`; the single-repo JSON shape is
unchanged. `--files` and `--stdin-file` stay single-repo.

Via the Hermes plugin (same checker, any cwd):

```bash
hermes docpolicy <check|audit|brief|fix|triage|llm> [args...]
```

**Exit codes:** `0` clean/advisory · `1` error-severity findings · `2` config error (bad policy — or, when several repos are given, one of them has no policy file).

## Quickstart (new repo)

```bash
uv tool install docpolicy               # once per machine — see below
docpolicy init --repo ~/src/newrepo
```

Running from a clone instead of an installed package works identically:
`PYTHONPATH=/path/to/docpolicy python3 -m docpolicy init --repo ~/src/newrepo`.

This writes `.docpolicy.yaml` + `.githooks/pre-commit` and sets `git config core.hooksPath=.githooks`. Then **edit the classes** to match the repo (see [Policy reference](#policy-reference)).

For repos maintained through this Hermes instance, also deploy the plugin (see [Enforcement surfaces](#enforcement-surfaces)) — the pre-commit hook alone already covers human + agent commits; the plugin adds real-time blocking and inbox pickup.

## Install once, use everywhere

    uv tool install docpolicy      # global CLI in its own isolated env
    # or: pipx install docpolicy
    # or: pip install --user docpolicy

The checker is stdlib-only, so one global install serves every repo — including
repos with no Python environment of their own. The tool ships its own interpreter,
so it does not care whether a project has a venv, or whether `python3` is the
version you'd expect.

Do **not** `pip install docpolicy` inside a project venv to get it working there.
That copy shadows the global one whenever the venv is active, and a stale pin then
behaves like a stale CLI (the classic symptom: `unrecognized arguments` for a flag
that exists upstream). If you already have one, `pip uninstall docpolicy` in that
venv.

The git hook works with any of these layouts. Resolution order: `DOCPOLICY_SRC`
if set → `docpolicy` on `PATH` → a self-hosted `docpolicy/` in the repo → a
`~/src/docpolicy-public` clone. The plugin uses the same rule. It fails loudly if
none is found rather than silently passing every commit.

## Daily workflows

### Capture: drop a raw document

Drop any file into the repo's `inbox/` directory. No frontmatter needed — inbox docs are deliberately exempt from the frontmatter contract and from `FM`/`C2`/`C9` (zero friction at capture time is the point). The cost surfaces as two advisory checks instead:

- `C11` — capture older than the inbox class's `max_age_days` → triage or delete.
- `C10` — two active docs sharing 2+ tags → same topic? merge or differentiate.

The plugin's `pre_llm_call` hook injects an inbox notice into agent sessions ("N untriaged captures...") so captures cannot be silently forgotten.

### Triage: turn captures into canonical docs

```bash
hermes docpolicy triage --repo .               # propose only (LLM labels when a key is configured)
hermes docpolicy triage --repo . --no-llm      # propose with deterministic labels
hermes docpolicy triage --repo . --apply       # promote items with NO open questions
hermes docpolicy triage --repo . --apply --llm # same, forcing the LLM layer (~60s per doc)
hermes docpolicy triage --repo . --force       # promote even flagged items
hermes docpolicy triage --repo . --queue       # file a queue.db task per flagged item
```

- Proposals carry subject/title/description/tags/related-docs/confidence and a destination (the inbox class's `dest`, e.g. `docs/research`).
- Label source: when an LLM key is configured (env or `~/.hermes/docpolicy/llm.json`) triage applies the model layer **by default**; `--no-llm` forces deterministic labels, `--llm` forces the model layer (it degrades to deterministic labels when no key is set).
- `--apply` never promotes a flagged item; `--force` does; `--queue` files a task instead (project code from `--project` or the policy's `project:` key).
- Promotion writes **one** frontmatter block carrying both docpolicy (`subject`/`status`) and OKF (`type`/`title`/`description`/`tags`/`timestamp`/`source`) fields — a second block would be invisible to the brain's FTS index.
- Triage is propose-only by default. Nothing is ever auto-promoted.

### Re-triage an old doc (pre-docpolicy or badly labeled)

```bash
git mv docs/Junk_stem.md inbox/
hermes docpolicy triage --repo . --apply --llm
# review proposals, then: audit, repair any C5 broken links, commit
```

Batch in small groups; each LLM-labeled doc costs ~60s. `git mv` keeps history.

### Write or edit a canonical doc

Frontmatter (`subject` + `status`) is required on canonical docs. The plugin evaluates content **before** it lands (`write_file` content goes through a virtual doc): a write that would produce missing frontmatter, a second active doc for an existing subject, or a rule-violating write into an archive/ephemeral class gets **blocked** with the exact fix in the message. Fix and rewrite — do not `--no-verify`. Existing OKF frontmatter (`type`/`title`/`tags`/...) is preserved; docpolicy only requires `subject` + `status`.

### Retire a doc

Move it into the archive class (`docs/archive/**` here) with `status: superseded` (+ `superseded_by`) or `archived`. Archive docs must not be `status: active` (`C9`).

### Check / audit / fix / brief

```bash
hermes docpolicy check --repo .          # fast: C1-C3, FM, C7, C9, C11 (per-commit safe)
hermes docpolicy audit --repo .          # full: everything
hermes docpolicy audit --repo . --json   # machine-readable
hermes docpolicy audit --repo . --advisory   # all warn-level, exit 0 (scoring runs)
hermes docpolicy fix --repo . --dry-run  # propose frontmatter for unlabelled canonical docs
hermes docpolicy fix --repo . --apply    # write them (subject = filename stem)
```

## Policy reference

`.docpolicy.yaml` at the repo root:

```yaml
version: 1
sources:
  issues: ~/notes/queue.db          # task store for C6 (PROJECT-NN refs)
project: NAT                        # default project code for triage --queue
classes:
  - id: archive
    mode: archive
    paths: ["docs/archive/**"]
```

**Class order matters: first matching class wins.** Declare narrow classes (`archive`, `ephemeral`, `inbox`, `exempt`) **before** broad canonical globs.

| Mode | Extra keys | Meaning |
|---|---|---|
| `canonical` | — | the real docs; frontmatter contract enforced |
| `generated` | `generator`, `facts` | build output; `C4` re-runs the generator and diffs — the generated doc **and** the `facts:` JSON are snapshotted and restored, so an audit never mutates the repo. With `facts:`, specs' `assert:` frontmatter is checked against it (`C12`) |
| `archive` | — | history; must not be `status: active` (`C9`) |
| `ephemeral` | `max_age_days`, `require_pointer` | session scratch; ages out (`C7`) and must link to a canonical doc (the `.hermes/plans` rule) |
| `append-only` | — | logs; never checked for frontmatter |
| `exempt` | — | path-classified only, content **never** read (secrets: `.env`, key files, legal, licenses) |
| `inbox` | `dest`, `max_age_days` | raw captures; exempt from `FM`/`C2`/`C9`; ages into `C11`; triage promotes to `dest` |

Severity defaults — errors: `C1 C2 C3 C4 C7 C9 C12 FM`; warnings: `C5 C6 C8 C10 C11`. Override per repo (known codes only):

```yaml
# Block style only: miniyaml does not parse inline maps,
# so "severity: { C5: error }" would parse as a string and be ignored.
rules:
  severity:
    C5: error
    C10: off
```

## Checks reference

| Code | Sev | Meaning / fix |
|---|---|---|
| `C1` | error | unclassified file — add a class glob or move the file |
| `C2` | error | subject has >1 active doc — supersede all but one |
| `C3` | error | superseded doc missing/invalid `superseded_by` — point at the active doc |
| `C4` | error | generated file stale — re-run the generator (the doc and its `facts:` file are both restored afterwards, so the audit stays side-effect free) |
| `C5` | warn | broken relative md link — repair or unlink |
| `C6` | warn | `PROJECT-NN` reference not in the task store (codes are read *from* the store, so `SHA-256`/`AVX-512` cannot false-positive) |
| `C7` | error | ephemeral doc too old or missing pointer to a canonical doc |
| `C8` | warn | duplicate H1 across canonical docs — one subject, one doc? |
| `C9` | error | archive doc with active status — set superseded/archived |
| `C10` | warn | two active docs sharing ≥2 tags — same topic? merge or differentiate |
| `C11` | warn | inbox capture older than `max_age_days` — triage or delete |
| `C12` | error | spec `assert:` frontmatter contradicts the machine facts file — fix the doc or the code, then regenerate |
| `FM` | error | canonical doc without/malformed frontmatter — add `subject` + `status` |

Per-file semantics: in `--files` / `--stdin-file` mode, `C2`/`C3` still scan the whole repo but only report findings involving the target — a pre-existing collision elsewhere never blocks an unrelated write.

## Enforcement surfaces

All surfaces run the same checker.

1. **Hermes plugin** (`hermes_plugin/`, deploy via `deploy_plugin.sh`):
   - `pre_tool_call` — **blocks** `write_file`/`patch`/`create_file` on `*.md` that would violate `FM`/`C2`/`C7`/`C9`; `write_file` content is evaluated pre-write (virtual doc via `--stdin-file`); `patch` is evaluated against the on-disk state **before** the patch applies, so a patch that introduces a new violation is caught by the pre-commit hook, not here; fail-open on internal errors so a broken plugin cannot brick the agent.
   - `pre_llm_call` — injects the inbox notice ("N untriaged captures...").
   - `on_session_start` — logs `docpolicy brief --md` to the session log (observer hook: return values are ignored, only `pre_llm_call` injects into the turn).
   - `on_session_end` — logs a one-line check summary.
   - Tools: `docpolicy_brief`, `docpolicy_check`, `docpolicy_triage`.
   - Runtime state: `~/.hermes/docpolicy/` (`config.json` = plugin settings `{advisory, repos}`, `advisory.log`) — deliberately **not** inside the plugin install path (a symlink to this repo). Knobs: `advisory: true` = log-only, never block; `repos: [...]` = empty means all repos with a policy file.
2. **git pre-commit** — `.githooks/pre-commit` via `core.hooksPath`; checks **staged** `.md` files; blocks the commit on error-severity findings (`--no-verify` works but is discouraged).
3. **pre-deploy gate** — a deploy script runs `check` as a phase before shipping; the deploy aborts on error findings.
4. **Cron** (email-only alerting via msmtp):
   - `10 8 * * 0` — `scripts/docpolicy_audit_email.sh`: weekly full audit across `DOCPOLICY_REPOS`; emails `DOCPOLICY_AUDIT_TO` only on findings.
   - `20 8 * * *` — `scripts/inbox_sweep.sh`: daily inbox digest; **never** promotes anything.

## Multi-repo runner

One command checks every repo that has a policy:

```bash
docpolicy-all               # fast check (C1-C3, frontmatter, archive) on all repos
docpolicy-all --audit       # full audit (adds C4-C8, C10, C12)
docpolicy-all notes         # subset: named repos only (basename or path)
```

- Installed as `~/.local/bin/docpolicy-all` → symlink to `scripts/docpolicy_all.sh`; runs from any cwd.
- Default repo list: `DOCPOLICY_REPOS` (space-separated paths), else `~/.config/docpolicy/repos.txt` (one path per line), else the `REPOS` array in the script. Repos without a `.docpolicy.yaml` are skipped with a note — add each repo once you init it.
- Prints a one-line summary per repo; exit `1` if any repo has error findings.
- For full per-finding detail across several repos, `check` and `audit` take more than one: `--repo` is repeatable and bare paths work, so `docpolicy check ~/src/a ~/src/b` or `docpolicy check --repo . --repo ~/src/b`. Output is a block per repo; with more than one repo `--json` wraps as `{"repos": [...]}` (the single-repo JSON shape is unchanged). `--files` and `--stdin-file` stay single-repo.

## LLM label layer (optional)

Labels only — it never decides placement and is never required. Resolution: env (`DOCPOLICY_LLM_KEY` | `OPENROUTER_API_KEY`, `DOCPOLICY_LLM_MODEL`, `DOCPOLICY_LLM_BASE`, `DOCPOLICY_LLM_TIMEOUT`) → config file → defaults (model `qwen/qwen3.8-flash`, base `https://openrouter.ai/api/v1`).

```bash
hermes docpolicy llm                      # show resolved config + key source
hermes docpolicy llm --set-key            # key from hidden stdin, stored 0600
hermes docpolicy llm --set-model qwen/qwen3.8-flash
hermes docpolicy llm --clear              # remove the config file
```

- Config file: `~/.hermes/docpolicy/llm.json` (`DOCPOLICY_LLM_CONFIG` overrides the path). `llm.json` is the checker's credential file (0600); `config.json` in the same directory is the plugin's state — different owners.
- Env note: commands run under `hermes` inherit Hermes' secrets, so `hermes docpolicy triage` may be model-enabled while a bare shell or cron run is not — set `llm.json` when cron should match. The checker never reads Hermes' own provider config.
- Fail-open: any LLM error degrades to deterministic labels (`prop.llm` records why).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Write blocked with `ERROR [FM]...` | the doc needs frontmatter — add `subject` + `status`; do not `--no-verify` |
| OpenRouter 401 `Missing Authentication header` or `User not found.` | the stored key is **malformed**, not a model/network problem. Canonical shape: `sk-or-v1-` + exactly 64 hex (73 chars). Re-set: `hermes docpolicy llm --set-key` |
| triage tool call times out (~8s) | the Hermes tool wrapper caps runtime; run the CLI directly with `--llm` and a generous timeout, or `--no-llm` |
| audit "0 findings" right after an inbox drop | correct — inbox is exempt until triaged; `C11` ages stale captures out |
| `--repodir: unrecognized argument` | the flag is `--repo` |
| Will an audit mutate my repo? | No: `C4` restores both the generated doc and the `facts:` side-car file it re-generates |

## Status

Self-hosting: this repo carries its own `.docpolicy.yaml` and a pre-commit hook
(`core.hooksPath=.githooks`), so a commit that violates the policy is rejected by
the same checker the repo ships. Both test suites are stdlib-only and run offline.

- Version 0.1.4 — published on PyPI: `pip install docpolicy` or
  `uv tool install docpolicy`. Check codes, class modes and CLI flags are stable;
  internal modules (`miniyaml`, the triage heuristics) may still move.
- 0.1.4 fixes a real side effect: `C4` restored the generated doc but not the
  `facts:` JSON the same generator writes, so an audit silently rewrote that
  file. Both are now snapshotted and restored.
- PyPI filenames are immutable. Any change, however small, needs a version bump —
  rebuilding and re-uploading an already-published version always fails with
  `400 File already exists`.
- Known limits are documented in `docs/design.md`: related-doc scoring is
  bag-of-words overlap (no embeddings), and the LLM layer only ever labels —
  it never decides placement or blocks anything.
- Secrets hygiene: `exempt`-classed docs are never read beyond path
  classification, and the optional LLM credential (`llm.json`, 0600) is the only
  credential file the checker touches.
