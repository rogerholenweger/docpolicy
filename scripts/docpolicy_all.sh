#!/bin/bash
# docpolicy_all.sh — run docpolicy across ALL repos with a policy, one command.
#
# Usage:
#   docpolicy_all.sh               # fast check (C1-C3, frontmatter) on every repo
#   docpolicy_all.sh --audit       # full audit (adds C4-C8)
#   docpolicy_all.sh repo1 repo2   # only named repos (basename or path)
#
# Defaults: repos = every entry in REPOS below that has a .docpolicy.yaml.
# Exit code: 0 if all clean, 1 if any repo has findings.
set -uo pipefail

# Resolve $0 through symlinks so PYTHONPATH points at the real repo.
SELF="$(readlink -f "${BASH_SOURCE[0]:-$0}")"
DP="$(cd "$(dirname "$SELF")/.." && pwd)"
export PYTHONPATH="${DP}${PYTHONPATH:+:$PYTHONPATH}"

# Repo list: DOCPOLICY_REPOS (space-separated paths) wins; otherwise edit
# REPOS below to your own layout. Repos without a .docpolicy.yaml are skipped.
if [ -n "${DOCPOLICY_REPOS:-}" ]; then
  read -r -a REPOS <<< "$DOCPOLICY_REPOS"
else
  REPOS=(
    "$HOME/src/project-a"
    "$HOME/src/project-b"
    "$DP"
  )
fi

MODE="check"
SELECT=()
for arg in "$@"; do
  case "$arg" in
    --audit) MODE="audit" ;;
    *) SELECT+=("$arg") ;;
  esac
done

# Filter: named repos if given, else all; skip repos without a policy file.
targets=()
if [ ${#SELECT[@]} -gt 0 ]; then
  for want in "${SELECT[@]}"; do
    for r in "${REPOS[@]}"; do
      if [ "$(basename "$r")" = "$(basename "$want")" ] || [ "$r" = "$want" ]; then
        targets+=("$r")
      fi
    done
  done
else
  targets=("${REPOS[@]}")
fi

fail=0
ran=0
for repo in "${targets[@]}"; do
  if [ ! -f "$repo/.docpolicy.yaml" ]; then
    echo "-- $(basename "$repo"): no .docpolicy.yaml, skipping"
    continue
  fi
  ran=$((ran + 1))
  out="$(python3 -m docpolicy "$MODE" --repo "$repo" 2>&1)"
  rc=$?
  summary="$(printf '%s' "$out" | tail -1)"
  echo "-- $(basename "$repo") [$MODE]: $summary"
  [ $rc -ne 0 ] && fail=1
done

echo "— ${ran} repo(s) checked."
if [ $fail -ne 0 ]; then
  echo "FAIL: errors present (run 'docpolicy_all.sh ${SELECT[*]:-}' for detail)"
  exit 1
fi
echo "OK: all clean"
