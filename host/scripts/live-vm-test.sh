#!/usr/bin/env bash
# Stand a real jRemote host up in a throwaway VM and run the live suite at it.
#
# The in-process suite proves the code; this proves the *product*. It installs
# from a git checkout the way a stranger would, mints a device token the way the
# app does, and then calls every route the host serves over a real socket from a
# machine that is not the host. The coverage gate in tests-live/test_zz_coverage.py
# is what makes "every action" checkable rather than claimed.
#
#   live-vm-test.sh [--vm NAME] [--ref GIT_REF] [--keep] [--reset] [--install-only]
#
#   --vm NAME       guest to use (default: live-actions)
#   --ref REF       branch/tag/sha of THIS repo to install (default: the working tree)
#   --reset         throw the guest away and clone a fresh one first
#   --keep          leave the guest running afterwards (default: leave it running)
#   --install-only  stop once the host answers — print its URL and token, skip
#                   the suite. For rigs (the jRemote UI tests) that need a live
#                   host but bring their own tests; one install path for all.
#
# Why a VM and not this Mac: the suite opens sessions, writes files and revokes
# devices. tests-live/conftest.py refuses a base URL that resolves to the machine
# running it, so pointing this at production fails closed rather than wiping the
# board someone is looking at.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST_DIR="$REPO_ROOT/host"
# No hard-coded default. The VM driver lives wherever the machine running this
# keeps it, and baking one installation's tree into a published script both
# names that installation and sends every other one to a path that does not
# exist. PATH first, then say plainly what to set.
VM_SH="${VM_SH:-$(command -v vm.sh 2>/dev/null || true)}"
: "${VM_SH:?set VM_SH to the vm.sh that boots your test VM}"

VM_NAME="live-actions"
GIT_REF=""
DO_RESET=0
INSTALL_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --vm)    VM_NAME="$2"; shift 2 ;;
        --ref)   GIT_REF="$2"; shift 2 ;;
        --reset) DO_RESET=1; shift ;;
        --keep)  shift ;;
        --install-only) INSTALL_ONLY=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -x "$VM_SH" ] || die "no vm.sh at $VM_SH — see ~/Systems/vm/SYSTEM.md"

if [ "$DO_RESET" = "1" ]; then
    say "resetting $VM_NAME to a pristine clone"
    "$VM_SH" reset "$VM_NAME"
fi
# reset clones without booting — its job ends at "pristine". Booting is
# always this script's to do, fresh clone or not. Keep up's last words: a
# boot that dies (VM slot limit, wedged run) otherwise surfaces twenty
# lines later as a bare "no address".
UP_OUT="$("$VM_SH" up "$VM_NAME" 2>&1)" || true

IP="$("$VM_SH" ip "$VM_NAME" | tail -1 | tr -d '[:space:]')"
[ -n "$IP" ] || die "$VM_NAME has no address — up said: $(printf '%s\n' "$UP_OUT" | tail -1)"
say "guest $VM_NAME at $IP"

vssh() { "$VM_SH" ssh "$VM_NAME" "$@"; }

# ── dependencies the host installer requires ──
# The base guest ships Python 3.9.6 and no tmux; the installer needs 3.11+ and
# refuses to start chats without tmux. Both are idempotent, so a re-run on a
# guest that already has them costs a few seconds rather than ten minutes.
say "installing python3.12 + tmux in the guest (slow on a pristine guest)"
vssh 'command -v tmux >/dev/null 2>&1 || /opt/homebrew/bin/brew install -q tmux' || \
    die "could not install tmux in the guest"
vssh '/opt/homebrew/bin/brew list python@3.12 >/dev/null 2>&1 || /opt/homebrew/bin/brew install -q python@3.12' || \
    die "could not install python3.12 in the guest"

# ── the checkout the guest installs from ──
# Default is THIS working tree, copied in: the point of the run is usually to
# prove the code in front of you, and a --ref that silently tested `production`
# instead would be the worst kind of green.
if [ -n "$GIT_REF" ]; then
    say "guest clones $GIT_REF from the public repo"
    # The URL comes from this checkout's own origin, not a literal. A published
    # script that hard-codes one account's remote names that account, and sends
    # every fork's guest to clone somebody else's tree.
    CLONE_URL="${JSTACK_REPO:-$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)}"
    : "${CLONE_URL:?no origin remote here — set JSTACK_REPO to the repo the guest should clone}"
    vssh "rm -rf ~/jStack && git clone --depth 1 -b '$GIT_REF' '$CLONE_URL' ~/jStack" || \
        die "clone failed"
else
    # Staged through a clean copy rather than sent straight from the tree. A
    # `.venv` is symlinks into the interpreter that built it, so copying this
    # Mac's into the guest lands a python3 that passes `-x` and aborts on
    # exec — which is exactly how the first run of this harness failed, with
    # the installer reporting `ok` on a virtualenv that could not run. The
    # build artifacts go for the same reason: a stranger installing from a
    # fresh clone has none of them, and the run is meant to look like that.
    say "copying this working tree into the guest (without build artifacts)"
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    # Credentials never ride along, in either direction. Outbound, this
    # Mac's own keys have no business in a guest. And the guest's copy is
    # its mesh identity: a hub set up in there (install_hub.sh) keeps its
    # server keys and peer table under host/Credentials, and regenerating
    # those revokes every device that ever paired — so the refresh moves
    # them aside and puts them back rather than rebuilding them away.
    rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
          --exclude '.git' --exclude '*.egg-info' --exclude 'Credentials' \
          "$HOST_DIR/" "$STAGE/host/" || die "could not stage the tree"
    # `host/` is not the whole install. The menubar app stamps its own
    # version from the plugin manifest, which sits outside this directory —
    # so a guest that got only `host/` could install the host fine and then
    # fail to name the bar app it built, which is how the acceptance leg
    # went red with all 18 client tests passing. Carried narrowly: the one
    # manifest, not the plugins tree.
    MANIFEST='plugins/jstack/.claude-plugin/plugin.json'
    [ -f "$REPO_ROOT/$MANIFEST" ] || die "no plugin manifest at $REPO_ROOT/$MANIFEST — the guest would have no version to stamp"
    mkdir -p "$STAGE/$(dirname "$MANIFEST")"
    cp "$REPO_ROOT/$MANIFEST" "$STAGE/$MANIFEST" || die "could not stage the plugin manifest"
    # The copy leaves `.git` behind on purpose, so the guest cannot work out
    # which commit it is running — and a build that cannot name its source is
    # the thing a hash-and-date identity exists to stop. This end knows, so
    # this end records it, as of the moment the bytes were taken.
    printf '{"sha": "%s", "dirty": %s}\n' \
        "$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)" \
        "$([ -n "$(git -C "$REPO_ROOT" status --porcelain -uno 2>/dev/null)" ] && echo true || echo false)" \
        > "$STAGE/host/copied-from.json" || die "could not record the staged tree's commit"
    vssh 'if [ -d ~/jStack/host/Credentials ]; then
              rm -rf /tmp/jr-keep-credentials
              mv ~/jStack/host/Credentials /tmp/jr-keep-credentials
          fi
          rm -rf ~/jStack && mkdir -p ~/jStack'
    "$VM_SH" cp "$VM_NAME" "$STAGE/host" '~/jStack/host' || die "copy failed"
    "$VM_SH" cp "$VM_NAME" "$STAGE/plugins" '~/jStack/plugins' || die "copying the plugin manifest failed"
    vssh 'if [ -d /tmp/jr-keep-credentials ]; then
              rm -rf ~/jStack/host/Credentials
              mv /tmp/jr-keep-credentials ~/jStack/host/Credentials
          fi'
fi

# ── install the host ──
say "running host/install.sh in the guest"
vssh 'bash ~/jStack/host/install.sh --yes 2>&1 | tail -25' || \
    die "host install failed — see the output above"

# ── give the guest an agent to be about ──
#
# A fresh host resolves agents from `~/Agents`, and a guest that has never had
# one answers `{"agents": []}` — correctly, and deliberately: an absent agents
# tree must read as zero agents rather than as every folder in the home
# directory (hostenv.instance_root, the blank-thread bug). But zero agents
# means the roster, the tree, the Files pane and every session route have no
# subject, so the suite would skip the largest half of the surface and call it
# covered.
#
# So the guest gets what a real install gets: a seat directory with a CLAUDE.md
# in it. Made here rather than in a fixture because it is machine setup, not
# behaviour under test — and the suite asserts the host *finds* it, which is
# the part that can break.
say "seeding an agent tree in the guest"
vssh 'mkdir -p ~/Agents/testbench/chat/pad ~/Agents/testbench/pad
printf "# Testbench\n\nA seat that exists so the live suite has a subject.\n" > ~/Agents/testbench/CLAUDE.md
printf "# Testbench · chat\n\nThe seat the live suite opens sessions in.\n" > ~/Agents/testbench/chat/CLAUDE.md
printf "seeded by live-vm-test.sh\n" > ~/Agents/testbench/pad/seed.txt' \
    || die "could not seed an agent tree in the guest"

# ── give the guest a plan to be about ──
#
# The plan routes are the one family nothing on the wire can create:
# `plans.open_plan` has a single caller in the whole package and it is `cli.py`,
# so a suite calling from another machine can only ever prove their 404s. The
# guest has the CLI, so the guest authors one — like the agent tree above, this
# is machine setup rather than behaviour under test, and what the suite asserts
# is that the host *serves* it.
#
# Under `~/.claude/plans` because that is the tree `docfence.read_roots()`
# admits a plan document out of, and markdown because the fence takes nothing
# else — a plan seeded anywhere else would be a row whose document answers 403
# and a `GET /plans/{id}/document` proven on the wrong branch.
#
# Idempotent: a guest a developer did not `--reset` keeps the plan it has rather
# than collecting one per run.
say "seeding a plan in the guest"
vssh 'set -e
HOSTBIN=~/jStack/host/.venv/bin/jstack-host
PLAN=~/.claude/plans/live-suite-plan.md
mkdir -p ~/.claude/plans
cat > "$PLAN" <<"MD"
# Live suite plan

Seeded by host/scripts/live-vm-test.sh so the plan read routes answer about a
real plan instead of only refusing an id nobody minted. Nothing runs this plan.

## Stage 1 — the plan list answers

Verify: none — GET /plans returns this row.

## Stage 2 — the document reads through the fence

Verify: none — GET /plans/{plan_id}/document serves this file text.
MD
if "$HOSTBIN" plan list --json | grep -q live-suite-plan; then
    echo "a live-suite plan is already on this guest"
    exit 0
fi
PID="$("$HOSTBIN" plan open "Live suite plan" --file "$PLAN" | tail -1 | tr -d "[:space:]")"
[ -n "$PID" ] || { echo "plan open printed no id" >&2; exit 1; }
echo "plan $PID"
# Stages are a bonus, not the point: the two read routes are proven by the row
# and its document. A parser that refuses this markdown is worth SAYING —
# `plan stages` writes nothing when it complains — but it is not worth failing
# a suite run over, and the live tests assert an empty stage list is legal.
"$HOSTBIN" plan stages "$PID" --from-file "$PLAN" \
    || echo "note: the parser refused the seeded plan; its row and document are still seeded" >&2' \
    || die "could not seed a plan in the guest"

# ── prove it is listening, then mint a device token the way the app does ──
say "waiting for the host to answer"
for _ in $(seq 1 30); do
    if curl -fsS -m 3 "http://$IP:9090/api/health" >/dev/null 2>&1; then break; fi
    sleep 2
done
curl -fsS -m 5 "http://$IP:9090/api/health" >/dev/null || \
    die "host is not answering on http://$IP:9090 — check \`$VM_SH ssh $VM_NAME\`"

say "minting a device token for the suite"
TOKEN="$(vssh 'cd ~/jStack/host && ./.venv/bin/python3 -c "
import sys; sys.path.insert(0, \".\")
from jstack_host import devices
print(devices.internal_token())
"' | tail -1 | tr -d '[:space:]')"
[ -n "$TOKEN" ] || die "could not mint a device token in the guest"

# ── a rig caller stops here: the live host is the product, not the suite ──
# Parseable coordinates on stdout; everything above already went to the tty.
if [ "$INSTALL_ONLY" = "1" ]; then
    say "host is live — stopping before the suite (--install-only)"
    printf 'JREMOTE_LIVE_URL=http://%s:9090\nJREMOTE_LIVE_TOKEN=%s\n' "$IP" "$TOKEN"
    exit 0
fi

# ── run the suite from THIS machine, against the guest ──
# From here, not inside the guest: half of what is being proven is that a second
# machine can reach the advertised addresses, and a suite running on the host
# would pass that on loopback without ever leaving the box.
say "running the live suite against http://$IP:9090"
cd "$HOST_DIR"
JREMOTE_LIVE_URL="http://$IP:9090" \
JREMOTE_LIVE_TOKEN="$TOKEN" \
    ./.venv/bin/python3 -m pytest tests-live/ -v --tb=short "$@"
