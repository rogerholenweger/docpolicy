# docpolicy Hermes plugin

Enforcement surface for the docpolicy checker. The plugin contains NO rules of
its own — every block decision comes from `python3 -m docpolicy check --json`
(`DOCPOLICY_SRC` defaults to the repo root this package lives in). All hook paths
fail OPEN: on any internal error the plugin logs and allows, so a broken plugin
cannot brick the agent.

## What it does

- `pre_tool_call` (blocking): intercepts `write_file` / `patch` / `create_file`
  on `*.md` paths. Walks up from the target to the nearest `.docpolicy.yaml`;
  no policy file → not our scope, allow.
  - `write_file` carries the new content → evaluated as a VIRTUAL doc
    (`check --stdin-file`), so missing frontmatter and subject collisions are
    caught before the write lands.
  - `patch` → falls back to on-disk state; the git pre-commit hook is the
    authoritative gate for patch results.
  - Blocks error-severity FM / C2 / C7 / C9 on the target. A pre-existing
    collision elsewhere in the repo never blocks an unrelated write.
  - Checker failure / timeout / exit 2 → allow + log (fail open).
- `on_session_start`: logs `docpolicy brief --md` for the cwd repo.
- `on_session_end`: logs a one-line `docpolicy check` summary.
- Tools: `docpolicy_brief`, `docpolicy_check` (JSON in, JSON out).
- Non-blocking findings are logged to `~/.hermes/docpolicy/advisory.log`
  (runtime state lives OUTSIDE the install path, which is a symlink).

## Config (optional)

`~/.hermes/docpolicy/config.json` — created with defaults on first run
(plugin state only; the checker's optional model/credential config lives beside
it in `llm.json`, owned by the checker, not by this plugin):

    {"advisory": false, "repos": []}

- `advisory: true` — log-only, never block.
- `repos: []` — empty = enforce everywhere a policy file exists; non-empty =
  only those repo roots.

## Deploy

    bash deploy_plugin.sh

Symlinks `~/.hermes/plugins/docpolicy` → `<repo>/hermes_plugin`, drift-guarded
(refuses to overwrite a non-symlink install). Activate on next Hermes restart.
Validate any time with:

    hermes plugins doctor <repo>/hermes_plugin --ci

## Tests

    python3 -m unittest discover hermes_plugin/tests -v   # pure logic, stdlib only
