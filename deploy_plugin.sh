#!/bin/bash
# deploy_plugin.sh — install/refresh the docpolicy Hermes plugin via symlink.
# Idempotent, drift-guarded: refuses to touch a non-symlink install.
#
# Usage:
#   deploy_plugin.sh              # deploy to default profile + ALL profiles
#   deploy_plugin.sh work oss  # deploy only to named profiles
#   deploy_plugin.sh --default-only
#
# Default profile plugins live in ~/.hermes/plugins; named profiles in
# ~/.hermes/profiles/<name>/plugins. All targets are symlinks to this repo's
# hermes_plugin/, so a git pull refreshes every profile at once.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)/hermes_plugin"
PLUGIN_ID="docpolicy"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"

[ -f "${SRC_DIR}/plugin.yaml" ] || { echo "ERROR: ${SRC_DIR}/plugin.yaml missing" >&2; exit 1; }

deploy_one() {
  local dest="$1"
  if [ -e "${dest}" ] && [ ! -L "${dest}" ]; then
    echo "ERROR: ${dest} exists and is NOT a symlink — refusing to overwrite." >&2
    echo "  Remove it manually if you really want the symlink deploy:" >&2
    echo "    rm -rf ${dest}" >&2
    return 1
  fi

  mkdir -p "$(dirname "${dest}")"
  ln -sfn "${SRC_DIR}" "${dest}"

  # Verify: symlink resolves, manifest present, name matches, module imports clean.
  local resolved
  resolved="$(readlink -f "${dest}")"
  [ "${resolved}" = "$(readlink -f "${SRC_DIR}")" ] || { echo "ERROR: symlink mismatch at ${dest}" >&2; return 1; }
  grep -q "^name: ${PLUGIN_ID}$" "${dest}/plugin.yaml" || { echo "ERROR: plugin.yaml name mismatch at ${dest}" >&2; return 1; }
  python3 -c "import importlib.util,sys; spec=importlib.util.spec_from_file_location('docpolicy_plugin_check','${dest}/__init__.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)" \
    || { echo "ERROR: plugin module failed to import (dest: ${dest})" >&2; return 1; }

  echo "OK: ${dest} -> ${SRC_DIR}"
}

targets=()
if [ "${1:-}" = "--default-only" ]; then
  targets+=("${HERMES_HOME}/plugins/${PLUGIN_ID}")
elif [ "$#" -gt 0 ]; then
  for p in "$@"; do
    targets+=("${HERMES_HOME}/profiles/${p}/plugins/${PLUGIN_ID}")
  done
else
  # Default profile + every existing profile.
  targets+=("${HERMES_HOME}/plugins/${PLUGIN_ID}")
  if [ -d "${HERMES_HOME}/profiles" ]; then
    for dir in "${HERMES_HOME}/profiles"/*/; do
      [ -d "${dir}" ] || continue
      p="$(basename "${dir}")"
      targets+=("${HERMES_HOME}/profiles/${p}/plugins/${PLUGIN_ID}")
    done
  fi
fi

fail=0
for t in "${targets[@]}"; do
  deploy_one "${t}" || fail=1
done

echo "Activate on next Hermes restart per profile. Validate:"
echo "  default: hermes plugins doctor ${HERMES_HOME}/plugins/${PLUGIN_ID} --ci"
echo "  profile: hermes -p <name> plugins doctor <dest> --ci"
exit "${fail}"
