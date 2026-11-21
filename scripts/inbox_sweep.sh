#!/bin/bash
# inbox_sweep.sh — daily inbox triage sweep, email-only alerting.
#
# For each repo with an 'inbox' class: run `docpolicy triage` (propose-only, no
# --apply, deterministic labels). If any captures are pending, email the
# proposals via msmtp (expects a configured msmtp account) so the triage
# decision can be made away from the keyboard. Silent when every inbox is
# empty, EXCEPT an always-appended one-line status, so silence stays checkable.
#
# Safety: this script never promotes anything. Promotion is `docpolicy triage
# --apply`, run deliberately (or by the agent through docpolicy_triage).
#
# Crontab (daily 08:20 local):
#   20 8 * * *  $HOME/src/docpolicy/scripts/inbox_sweep.sh >> $HOME/.docpolicy_inbox.log 2>&1
# Env: DOCPOLICY_INBOX_REPOS, DOCPOLICY_INBOX_TO
set -uo pipefail

REPOS="${DOCPOLICY_INBOX_REPOS:-$HOME/src/project-a $HOME/src/project-b}"
TO="${DOCPOLICY_INBOX_TO:-you@example.com}"
DP="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")/.." && pwd)"
export PYTHONPATH="$DP${PYTHONPATH:+:$PYTHONPATH}"

BODY=""
PENDING=0
for repo in $REPOS; do
    [ -f "$repo/.docpolicy.yaml" ] || continue
    grep -q 'mode: inbox' "$repo/.docpolicy.yaml" || continue
    OUT="$(python3 -m docpolicy triage --repo "$repo" --no-llm 2>&1)"
    # 'inbox — 0 capture(s)' / 'inbox: empty' mean nothing to do
    if printf '%s' "$OUT" | grep -qE '^inbox: empty|— 0 capture\(s\)'; then
        continue
    fi
    PENDING=$((PENDING + 1))
    BODY+="== $repo"$'\n'"$OUT"$'\n\n'
done

if [ "$PENDING" -eq 0 ]; then
    echo "$(date -Iseconds) all inboxes empty"
    exit 0
fi

{
    echo "Subject: [docpolicy] inbox triage pending in $PENDING repo(s)"
    echo "To: $TO"
    echo
    printf 'Untriaged captures. Promote deliberately:\n'
    printf '  PYTHONPATH=%s python3 -m docpolicy triage --repo <repo> --apply\n\n' "$DP"
    printf '%s' "$BODY"
} | msmtp "$TO"
echo "$(date -Iseconds) $PENDING repo(s) with pending captures — emailed $TO"
