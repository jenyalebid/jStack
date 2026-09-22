#!/bin/bash
# WHAT: the managed update journeys — nine receipts from managed_update_accept over a signed candidate
# TIME: ~60m
# GUEST: managed by the lab tool (disposable, per docs/managed-update-lab.md)
#
# This scenario delegates to the existing lab: host/tools/managed_update_accept.py
# drives upgrade, interruption, rollback, offline catch-up and revocation
# against a signed candidate and writes its own receipts. It exists in the
# catalog so an update question is one command, not a re-derived campaign.
#
#   JSTACK_VERIFY_CANDIDATE=/path/to/signed/candidate verify/run.sh run hub/update
. "$(dirname "$0")/../../lib/common.sh"

CAND="${JSTACK_VERIFY_CANDIDATE:-}"
[ -n "$CAND" ] || { echo "FAIL set JSTACK_VERIFY_CANDIDATE to a signed candidate path"; exit 1; }
PLAN="${JSTACK_VERIFY_UPDATE_PLAN:-}"
[ -n "$PLAN" ] || { echo "FAIL set JSTACK_VERIFY_UPDATE_PLAN to a disposable lab plan (docs/managed-update-lab.md)"; exit 1; }
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"

PYTHONPATH="$REPO/host" python3 "$REPO/host/tools/managed_update_accept.py" \
    --candidate "$CAND" --receipts "$RECEIPTS" --plan "$PLAN"
