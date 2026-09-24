#!/bin/bash
# WHAT: the managed update journeys — eight receipts from managed_update_accept over a ref
# TIME: ~60m
# GUEST: managed by the lab tool (disposable)
#
# This scenario delegates to the existing lab: host/tools/managed_update_accept.py
# drives fresh install, upgrade, fleet, interruption, offline catch-up and
# revocation against a branch and writes its own receipts. It exists in the
# catalog so an update question is one command, not a re-derived campaign.
#
# The subject is a commit. The guests clone the ref from the public repo and
# build the Hub themselves, so nothing is staged here but the Mac app, which
# is closed source and comes from wherever this Mac already has it installed.
#
#   JSTACK_VERIFY_REF=dev JSTACK_VERIFY_PRIOR_REF=main \
#   JSTACK_VERIFY_UPDATE_PLAN=/path/to/plan.json verify/run.sh run hub/update
. "$(dirname "$0")/../../lib/common.sh"

REF="${JSTACK_VERIFY_REF:-}"
[ -n "$REF" ] || { echo "FAIL set JSTACK_VERIFY_REF to the branch under test"; exit 1; }
PLAN="${JSTACK_VERIFY_UPDATE_PLAN:-}"
[ -n "$PLAN" ] || { echo "FAIL set JSTACK_VERIFY_UPDATE_PLAN to a disposable lab plan"; exit 1; }
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"

args=(--ref "$REF" --receipts "$RECEIPTS" --plan "$PLAN")
if [ -n "${JSTACK_VERIFY_PRIOR_REF:-}" ]; then args+=(--prior-ref "$JSTACK_VERIFY_PRIOR_REF"); fi
if [ -n "${JSTACK_VERIFY_REPO:-}" ]; then args+=(--repo "$JSTACK_VERIFY_REPO"); fi
CLIENT="${JSTACK_VERIFY_CLIENT:-/Applications/jRemote.app}"
if [ -d "$CLIENT" ]; then args+=(--client "$CLIENT"); fi

PYTHONPATH="$REPO/host" python3 "$REPO/host/tools/managed_update_accept.py" "${args[@]}"
