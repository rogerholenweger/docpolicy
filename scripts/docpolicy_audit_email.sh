#!/bin/bash
# docpolicy_audit_email.sh — weekly doc-drift audit, email-only alerting.
#
# Runs `docpolicy audit` over the repos below; if any findings, emails a
# summary via msmtp (expects a configured msmtp account; a local MTA also works).
# Silent when clean, except for the always-appended one-line status to a
# log file so silence is checkable.
#
# Crontab (Sundays 08:10 local):
#   10 8 * * 0  $HOME/src/docpolicy/scripts/docpolicy_audit_email.sh >> $HOME/.docpolicy_audit.log 2>&1
# Env: DOCPOLICY_REPOS, DOCPOLICY_AUDIT_TO, DOCPOLICY_AUDIT_FROM
set -uo pipefail

DP="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]:-$0}")")/.." && pwd)"
REPOS="${DOCPOLICY_REPOS:-$HOME/src/project-a $HOME/src/project-b $DP}"
TO="${DOCPOLICY_AUDIT_TO:-you@example.com}"
FROM="${DOCPOLICY_AUDIT_FROM:-docpolicy@localhost}"
export PYTHONPATH="$DP${PYTHONPATH:+:$PYTHONPATH}"

BODY=""
for repo in $REPOS; do
    [ -f "$repo/.docpolicy.yaml" ] || continue
    OUT="$(python3 -m docpolicy audit --repo "$repo" 2>&1)"
    RC=$?
    BODY+="== $repo (exit $RC)"$'\n'"$OUT"$'\n\n'
done

if [ -z "$BODY" ]; then
    echo "$(date -Iseconds) no auditable repos"
    exit 0
fi

# Findings = any repo summary line "N findings (...)" with N > 0.
# (Errors AND warnings count - the old grep fired on errors only, so
# warning-only drift was silent even when the mail path worked.)
if printf '%s' "$BODY" | grep -qE '[1-9][0-9]* findings \(' ; then
    if printf 'From: %s\nTo: %s\nSubject: [docpolicy] doc-drift findings\n\n%s\n' \
        "$FROM" "$TO" "$BODY" | msmtp -t; then
        echo "$(date -Iseconds) findings - emailed $TO"
    else
        echo "$(date -Iseconds) SEND FAILED - findings detected but email not delivered"
        exit 1
    fi
else
    echo "$(date -Iseconds) all clean"
fi
