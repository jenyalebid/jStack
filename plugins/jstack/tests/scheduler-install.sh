#!/bin/bash
# The other half of a fresh install. Booking works everywhere; FIRING only
# works where a daemon runs, and the daemon's boot recipe used to be a private
# plist plus a venv .pth that never shipped — so "I scheduled it and nothing
# happened" was the default experience of a second machine. If this file is
# absent, nothing proves that `jstack-scheduler install` emits a definition
# the platform would actually accept, lands every data path outside the public
# checkout, honours JSTACK_ROOT, defaults to a label that cannot collide with
# a pre-existing hand-rolled service, and touches NOTHING under --dry-run —
# which means the first attempt to fix a silent second machine could clobber
# the one daemon that was already working.
#
# This test never loads, unloads, or writes a real service: every invocation
# is --dry-run or status, and HOME is redirected into the tmpdir so even a bug
# in the tool under test cannot reach a real ~/Library/LaunchAgents or
# ~/.config/systemd.

set -u
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
REPO_ROOT="$(cd "$PLUGIN_ROOT/../.." && pwd -P)"
PY="${JSTACK_PYTHON:-python3}"
TOOL="$PLUGIN_ROOT/bin/jstack-scheduler"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: no python3 on PATH (set JSTACK_PYTHON)"; exit 1; }

TMP=$(mktemp -d /tmp/jstack-scheduler-install.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/root" "$TMP/home"

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

# HOME redirected: ~/Library/LaunchAgents and ~/.config/systemd resolve into
# the tmpdir, so nothing here can touch a real service dir even by accident.
# SCHEDULER_INSTALL_FILE pins the machine's own scheduler.json out of a
# portability test, and SCHEDULER_API_PORT points at a port nothing serves so
# the API probe can only report what this test controls.
run_tool() {
    HOME="$TMP/home" JSTACK_ROOT="$TMP/root" \
    SCHEDULER_INSTALL_FILE="$TMP/root/absent-scheduler.json" \
    SCHEDULER_API_PORT=59991 \
    "$PY" "$TOOL" "$@"
}

# ── install --dry-run: the generated definition ──────────────────────────────

out=$(run_tool install --dry-run 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
    pass "install --dry-run exits 0"
else
    fail "install --dry-run exited $rc: $(printf '%s' "$out" | tail -3)"
fi

target=$(printf '%s\n' "$out" | sed -n 's/^would write: //p' | head -1)
case "$target" in
    "$TMP/home/"*) pass "definition targets the (redirected) home service dir" ;;
    *) fail "definition target '$target' is not under the test HOME" ;;
esac

DEF_LABEL=$(basename "$target")
DEF_LABEL=${DEF_LABEL%.plist}
DEF_LABEL=${DEF_LABEL%.service}

# The shipped default must not name a service this machine already runs, or
# `install` would adopt — and on the next run reload — a live daemon nobody
# asked it to manage. Read off the real service dir rather than a hardcoded
# label: the collision this guards against is whatever is actually installed
# here, which differs per machine and changes without this file hearing of it.
COLLIDES=""
for svc in "$HOME/Library/LaunchAgents"/*.plist \
           "$HOME/.config/systemd/user"/*.service; do
    [ -e "$svc" ] || continue
    name=$(basename "$svc"); name=${name%.plist}; name=${name%.service}
    [ "$name" = "$DEF_LABEL" ] && COLLIDES="$svc"
done
if [ -n "$DEF_LABEL" ] && [ -z "$COLLIDES" ]; then
    pass "default label '$DEF_LABEL' does not collide with a service already installed here"
else
    fail "default label '$DEF_LABEL' collides with an installed service: $COLLIDES"
fi

printf '%s\n' "$out" | sed -n "/^--- begin /,/^--- end /p" | sed '1d;$d' > "$TMP/gen.def"
if [ -s "$TMP/gen.def" ]; then
    pass "dry-run prints the full definition"
else
    fail "no definition body between the begin/end markers"
fi

if grep -q "written by jstack-scheduler" "$TMP/gen.def"; then
    pass "definition carries the ownership stamp"
else
    fail "no ownership stamp — install could never tell its own file from a hand-rolled one"
fi

case "$(uname)" in
Darwin)
    # plistlib parsing IS the validity assertion — grep can bless a plist
    # launchd would reject.
    msg=$("$PY" - "$TMP/gen.def" "$TMP/root" "$REPO_ROOT" "$PLUGIN_ROOT" 2>&1 <<'EOF'
import plistlib, sys
gen, jroot, repo, plugin = sys.argv[1:5]
with open(gen, "rb") as fh:
    d = plistlib.load(fh)
assert d["RunAtLoad"] is True, "RunAtLoad missing/false"
assert d["KeepAlive"] is True, "KeepAlive missing/false"
pa = d["ProgramArguments"]
assert pa[-2:] == ["-m", "scheduler"], f"ProgramArguments {pa!r}"
env = d["EnvironmentVariables"]
assert env.get("JSTACK_ROOT") == jroot, f"JSTACK_ROOT={env.get('JSTACK_ROOT')!r} not {jroot!r}"
assert env.get("PYTHONPATH") == plugin, f"PYTHONPATH={env.get('PYTHONPATH')!r}"
assert env.get("PATH"), "no PATH — the service manager hands the daemon a stripped one"
for key in ("StandardOutPath", "StandardErrorPath"):
    assert d[key].startswith(jroot + "/State/"), f"{key}={d[key]} not under the root's State"
for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
    p = d[key]
    assert not (p == repo or p.startswith(repo + "/")), f"{key}={p} resolves inside the checkout"
print("plist ok")
EOF
)
    if [ $? -eq 0 ]; then
        pass "plist parses with plistlib: RunAtLoad/KeepAlive, -m scheduler, root-derived env and State logs, no path in the checkout"
    else
        fail "generated plist assertions: $msg"
    fi
    ;;
Linux)
    # No plistlib equivalent for units, and systemd-analyze is not guaranteed
    # present — assert the load-bearing lines directly.
    grep -q "ExecStart=.* -m scheduler$" "$TMP/gen.def" \
        && pass "unit ExecStart runs -m scheduler" \
        || fail "unit ExecStart does not run -m scheduler"
    grep -q "Environment=\"JSTACK_ROOT=$TMP/root\"" "$TMP/gen.def" \
        && pass "unit env carries the resolved JSTACK_ROOT" \
        || fail "unit env lacks JSTACK_ROOT=$TMP/root"
    grep -q "StandardOutput=append:$TMP/root/State/" "$TMP/gen.def" \
        && pass "unit logs land under the root's State" \
        || fail "unit stdout does not land under $TMP/root/State"
    grep -q "Restart=always" "$TMP/gen.def" \
        && pass "unit restarts on death" \
        || fail "unit lacks Restart=always"
    if grep -E "^(WorkingDirectory|StandardOutput|StandardError)=" "$TMP/gen.def" | grep -q "$REPO_ROOT"; then
        fail "a unit data path resolves inside the checkout"
    else
        pass "no unit data path resolves inside the checkout"
    fi
    ;;
*)
    fail "unrecognized platform $(uname) — the platform branch above needs a case"
    ;;
esac

# ── dry-run touches nothing ──────────────────────────────────────────────────

if [ ! -e "$target" ]; then
    pass "dry-run wrote no definition file"
else
    fail "dry-run WROTE $target"
fi
# The service dirs specifically — not all of $HOME: Apple's system python
# writes its own bytecode cache under ~/Library/Caches the moment it runs,
# which is the interpreter's doing, not the tool's.
if [ ! -e "$TMP/home/Library/LaunchAgents" ] && [ ! -e "$TMP/home/.config/systemd" ]; then
    pass "dry-run created no service directory"
else
    fail "dry-run created a service directory under the test HOME"
fi
if [ "$(uname)" = "Darwin" ]; then
    if launchctl list "$DEF_LABEL" >/dev/null 2>&1; then
        fail "a service named '$DEF_LABEL' is loaded — dry-run must load nothing (or the label is not free)"
    else
        pass "dry-run loaded no service"
    fi
fi

# ── dateutil: warned about when missing, named as a package, never a refusal ─

if "$PY" -c "import dateutil" >/dev/null 2>&1; then
    if printf '%s' "$out" | grep -q "python-dateutil"; then
        fail "warned about python-dateutil though this interpreter has it"
    else
        pass "no spurious dateutil warning when the interpreter has it"
    fi
else
    if printf '%s' "$out" | grep -q "python-dateutil"; then
        pass "install names python-dateutil when the interpreter lacks it"
    else
        fail "no dateutil in this interpreter, and install did not name python-dateutil"
    fi
fi

# ── --label override lands in the definition ─────────────────────────────────

out2=$(run_tool install --dry-run --label test.jstack.override 2>&1)
if [ $? -eq 0 ] && printf '%s' "$out2" | grep -q "test.jstack.override"; then
    pass "--label override lands in the generated definition"
else
    fail "--label override missing from the dry-run output"
fi

# ── a hand-rolled definition is refused, not clobbered ───────────────────────

if [ "$(uname)" = "Darwin" ]; then
    SVCDIR="$TMP/home/Library/LaunchAgents"; EXT="plist"
else
    SVCDIR="$TMP/home/.config/systemd/user"; EXT="service"
fi
mkdir -p "$SVCDIR"
FOREIGN="$SVCDIR/handrolled.test.$EXT"
printf '<?xml version="1.0"?>\n<plist version="1.0"><dict><key>Label</key><string>handrolled.test</string></dict></plist>\n' > "$FOREIGN"
before=$(cat "$FOREIGN")
out3=$(run_tool install --dry-run --label handrolled.test 2>&1)
rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out3" | grep -qi "refus"; then
    pass "install refuses a definition it did not write"
else
    fail "install did not refuse a hand-rolled definition (rc=$rc): $(printf '%s' "$out3" | tail -2)"
fi
if [ "$before" = "$(cat "$FOREIGN")" ]; then
    pass "the hand-rolled file is untouched"
else
    fail "the hand-rolled file was modified"
fi

# ── uninstall --dry-run: reports, removes nothing ────────────────────────────

OURS="$SVCDIR/$DEF_LABEL.$EXT"
cp "$TMP/gen.def" "$OURS"
out4=$(run_tool uninstall --dry-run 2>&1)
rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out4" | grep -q "would remove: $OURS"; then
    pass "uninstall --dry-run names what it would remove"
else
    fail "uninstall --dry-run (rc=$rc): $(printf '%s' "$out4" | tail -2)"
fi
if [ -e "$OURS" ]; then
    pass "uninstall --dry-run removed nothing"
else
    fail "uninstall --dry-run REMOVED the definition"
fi
rm -f "$OURS"

# ── unsupported platform: non-zero, and a runnable foreground command ────────

out5=$(JSTACK_SCHEDULER_PLATFORM=sunos run_tool install 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
    pass "unsupported platform exits non-zero"
else
    fail "unsupported platform exited 0"
fi
if printf '%s' "$out5" | grep -q -- "-m scheduler"; then
    pass "unsupported platform prints the foreground command"
else
    fail "no runnable foreground command in: $(printf '%s' "$out5" | tail -3)"
fi

# ── status on an absent label: clean, documented, no traceback ───────────────

out6=$(run_tool status --label definitely.absent.test 2>&1)
rc=$?
if [ $rc -eq 3 ]; then
    pass "status exits 3 for not-installed (the documented code)"
else
    fail "status on an absent label exited $rc, expected 3"
fi
if printf '%s' "$out6" | grep -q "not installed"; then
    pass "status says 'not installed' plainly"
else
    fail "status output does not say 'not installed': $(printf '%s' "$out6" | head -2)"
fi
if printf '%s' "$out6" | grep -q "Traceback"; then
    fail "status raised: $(printf '%s' "$out6" | tail -3)"
else
    pass "status raised nothing"
fi

echo
if [ "$fails" -eq 0 ]; then
    echo "PASS — the service installer generates a valid, root-derived, checkout-free definition and touches nothing under --dry-run"
    exit 0
fi
echo "$fails check(s) failed"
exit 1
