#!/bin/bash
# WHAT: the managed update journeys — thirteen receipts from managed_update_accept over a ref
# TIME: ~60m
# GUEST: managed by the lab tool (disposable)
# PYTHON: $JSTACK_PYTHON, else python3 — the shell journeys import the host
#         package (jstack_host.enrolment → fastapi), so the interpreter needs
#         host/pyproject.toml's dependencies; the runner refuses one that lacks
#         them before any journey runs
#
# This scenario delegates to the existing lab: host/tools/managed_update_accept.py
# drives fresh install, upgrade, fleet, interruption, offline catch-up,
# off-network, revocation and the four shell-access journeys — adoption, the
# leaf-to-leaf flip, the promotion of a Mac that predates shell access, and
# detach — against a branch, and writes its own receipts, one per journey. It
# exists in the catalog so an update question is one command, not a re-derived
# campaign.
#
# The subject is a commit. The guests clone the ref from the public repo and
# build the Hub themselves, so nothing is staged here but the Mac app, which
# is closed source and comes from wherever this Mac already has it installed.
#
#   JSTACK_PYTHON=/path/to/venv/bin/python3 \
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

PY="${JSTACK_PYTHON:-python3}"
PYTHONPATH="$REPO/host" "$PY" "$REPO/host/tools/managed_update_accept.py" "${args[@]}"
