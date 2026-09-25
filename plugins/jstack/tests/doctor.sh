#!/bin/bash
# Does the doctor tell the truth, and does it change nothing while doing it?
#
# A validator is the one tool nobody double-checks: its whole job is to be the
# thing you believe about a machine you cannot see. So two properties matter
# more than any individual check.
#
# READ-ONLY. The first run of this tool created three empty directories at the
# root — its writability probe did `mkdir -p` on its own subject — and then
# reported them healthy. That is a doctor describing a machine it just changed,
# and it is the failure mode that makes a green result worthless. Proven here
# against a sandbox root: run it, then assert not one path came into existence.
#
# WORST-GRADE EXIT. A script gating on this tool branches on its exit status,
# so 0/1/2 must track ok/warn/fail exactly — including the case where a check
# raises, which must land as FAIL rather than quietly dropping out of the list.
#
# Everything runs against a redirected HOME and a JSTACK_ROOT inside the
# tmpdir, so nothing here can read or write the real install.

set -u
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PY="${JSTACK_PYTHON:-python3}"
TOOL="$PLUGIN_ROOT/bin/jstack-doctor"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: no python3 on PATH (set JSTACK_PYTHON)"; exit 1; }

TMP=$(mktemp -d /tmp/jstack-doctor.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/root" "$TMP/home"

fails=0
fail() { echo "FAIL: $1" >&2; fails=$((fails+1)); }
pass() { echo "ok: $1"; }

# SCHEDULER_API_PORT points at a port nothing serves, so the scheduler probe
# resolves fast and can only report what this test controls.
run_doctor() {
    HOME="$TMP/home" JSTACK_ROOT="$TMP/root" \
    SCHEDULER_INSTALL_FILE="${INSTALL_FILE:-$TMP/root/absent-scheduler.json}" \
    SCHEDULER_API_PORT=59992 \
    "$PY" "$TOOL" "$@"
}

# Grade for one check name, out of the JSON. Absent name is an error, not an
# empty string: a check that vanished must not read as a check that passed.
grade_of() {
    "$PY" - "$1" "$2" <<'PY'
import json, sys
path, name = sys.argv[1], sys.argv[2]
data = json.load(open(path))
for c in data["checks"]:
    if c["name"] == name:
        print(c["grade"]); sys.exit(0)
print(f"MISSING:{name}")
PY
}

# ── read-only: the tool creates nothing ─────────────────────────────────────
# Snapshot the sandbox, run, compare. The root is deliberately EMPTY — every
# derived dir (Agents, Systems, Config, State, Logs, Credentials) is absent, so
# a probe that creates its subject has six chances to be caught.

before=$(find "$TMP" | sort)
run_doctor >/dev/null 2>&1
after=$(find "$TMP" | sort)
if [ "$before" = "$after" ]; then
    pass "read-only: an empty root is unchanged by a full run"
else
    fail "the doctor created or removed paths:"
    diff <(echo "$before") <(echo "$after") >&2
fi

# ── an empty root fails, and says which part ────────────────────────────────

run_doctor --json > "$TMP/empty.json" 2>/dev/null
rc=$?
[ "$rc" = "2" ] || fail "empty root should exit 2 (fail), got $rc"
[ "$rc" = "2" ] && pass "empty root exits 2"

g=$(grade_of "$TMP/empty.json" agents)
[ "$g" = "fail" ] || fail "no agents should grade fail, got '$g'"
[ "$g" = "fail" ] && pass "an agents dir with nothing in it is a failure"

# The root check itself must still pass: absent-but-creatable is a normal fresh
# install, and grading it fail would make every first run unfixable.
g=$(grade_of "$TMP/empty.json" root)
[ "$g" = "ok" ] || fail "absent-but-creatable dirs should grade ok, got '$g'"
[ "$g" = "ok" ] && pass "absent derived dirs are ok, not a failure"

# Same rule, and it was broken here longest: a timeline.db that does not exist
# yet is what every fresh install looks like, and the check said so in its own
# hint — "expected on a fresh install" — while grading itself WARN. A warning
# with no action behind it teaches the reader to skim warnings, and skimmed
# warnings are how the real ones get missed. The directory being writable is
# the thing worth checking, and that is checked separately.
g=$(grade_of "$TMP/empty.json" timeline)
[ "$g" = "ok" ] || fail "an uncreated timeline.db should grade ok, got '$g'"
[ "$g" = "ok" ] && pass "no timeline.db yet is ok — log_event creates it"

# ── one agent flips it, and gets named ──────────────────────────────────────

mkdir -p "$TMP/root/Agents/Testbed"
printf '# Testbed\n' > "$TMP/root/Agents/Testbed/CLAUDE.md"
run_doctor --json > "$TMP/one.json" 2>/dev/null
g=$(grade_of "$TMP/one.json" agents)
[ "$g" = "ok" ] || fail "one agent should grade ok, got '$g'"
[ "$g" = "ok" ] && pass "a directory with a CLAUDE.md is an agent"

if grep -q "Testbed" "$TMP/one.json"; then
    pass "the agent is named in the detail, not just counted"
else
    fail "found agents but did not name them — '0 agents' and 'agents I cannot name' read identically"
fi

# ── a dead rules symlink is a failure, not a passing file ───────────────────
# The version-cache rot: links into a versioned plugin path survive until that
# version is reaped. The name still appears in `ls`; only following it fails.

mkdir -p "$TMP/home/.claude/rules"
ln -s "$TMP/nonexistent-plugin-version/canvas.md" "$TMP/home/.claude/rules/canvas.md"
run_doctor --json > "$TMP/broken.json" 2>/dev/null
g=$(grade_of "$TMP/broken.json" rules)
[ "$g" = "fail" ] || fail "a dead rules symlink should grade fail, got '$g'"
[ "$g" = "fail" ] && pass "a dead symlink is a failure, not an installed rule"
rm -f "$TMP/home/.claude/rules/canvas.md"

# ── a hook that takes a subcommand is not a missing file ───────────────────
# The check tested the whole command line as a path, so every hook carrying an
# argument graded missing on a machine where the file was present and
# executable — seven of the shipped twenty-seven. A validator that reports a
# healthy machine as broken costs exactly what one reporting a broken machine
# as healthy costs: nobody reads it after the second time.

g=$(run_doctor --json 2>/dev/null | grade_of /dev/stdin hooks 2>/dev/null || true)
run_doctor --json > "$TMP/hooks.json" 2>/dev/null
g=$(grade_of "$TMP/hooks.json" hooks)
if [ "$g" = "ok" ]; then
    pass "hooks that take a subcommand are found, not reported missing"
else
    detail=$(python3 -c "import json,sys
d=json.load(open(sys.argv[1]))
for c in (d.get('checks') or d):
    if c.get('name')=='hooks': print(c.get('detail',''))" "$TMP/hooks.json" 2>/dev/null)
    fail "the shipped hooks should all resolve, got '$g': $detail"
fi

# ── unparseable config is a failure, because both readers swallow it ────────
# install() and load_defaults() catch JSONDecodeError and fall back to
# built-ins, silently. Nothing else on the machine will ever mention it.

printf '{ this is not json\n' > "$TMP/bad-scheduler.json"
INSTALL_FILE="$TMP/bad-scheduler.json" run_doctor --json > "$TMP/badcfg.json" 2>/dev/null
g=$(grade_of "$TMP/badcfg.json" config)
[ "$g" = "fail" ] || fail "unparseable scheduler.json should grade fail, got '$g'"
[ "$g" = "fail" ] && pass "a config file that silently falls back to defaults is a failure"

# ── versions: cache-vs-checkout drift is named, in step is ok ───────────────
# A throwaway checkout holding a copy of the plugin, and a ledger in the
# sandbox HOME pinning first the same commit (in step), then a stale one —
# which must warn AND call out that both trees answer to one version label,
# because that label being decoration is the trap this check exists for.

if command -v git >/dev/null 2>&1; then
    VREPO="$TMP/vrepo"
    mkdir -p "$VREPO/plugins"
    cp -R "$PLUGIN_ROOT" "$VREPO/plugins/jstack"
    rm -rf "$VREPO/plugins/jstack/__pycache__" "$VREPO/plugins/jstack"/*/__pycache__
    # The real checkout ignores bytecode; without this the copied tool's own
    # import caches read as uncommitted files and the in-step case warns.
    printf '__pycache__/\n' > "$VREPO/.gitignore"
    git -C "$VREPO" init -q
    git -C "$VREPO" -c user.email=t@t -c user.name=t add -A
    git -C "$VREPO" -c user.email=t@t -c user.name=t commit -qm one
    SHA1=$(git -C "$VREPO" rev-parse HEAD)
    LABEL=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
            "$VREPO/plugins/jstack/.claude-plugin/plugin.json")
    mkdir -p "$TMP/home/.claude/plugins"
    ledger() {
        printf '{"version":2,"plugins":{"jstack@jStack":[{"installPath":"%s","version":"%s","gitCommitSha":"%s"}]}}' \
            "$TMP/cache" "$LABEL" "$1" > "$TMP/home/.claude/plugins/installed_plugins.json"
    }
    run_vdoctor() {
        HOME="$TMP/home" JSTACK_ROOT="$TMP/root" \
        SCHEDULER_INSTALL_FILE="$TMP/root/absent-scheduler.json" \
        SCHEDULER_API_PORT=59992 \
        "$PY" "$VREPO/plugins/jstack/bin/jstack-doctor" --json
    }

    ledger "$SHA1"
    run_vdoctor > "$TMP/vsync.json" 2>/dev/null
    g=$(grade_of "$TMP/vsync.json" versions)
    [ "$g" = "ok" ] || fail "cache pinned at HEAD should grade ok, got '$g'"
    [ "$g" = "ok" ] && pass "a cache in step with its checkout is ok"

    echo "drift" >> "$VREPO/plugins/jstack/README-drift.md" 2>/dev/null || \
        echo "drift" > "$VREPO/plugins/jstack/README-drift.md"
    git -C "$VREPO" -c user.email=t@t -c user.name=t add -A
    git -C "$VREPO" -c user.email=t@t -c user.name=t commit -qm two
    run_vdoctor > "$TMP/vdrift.json" 2>/dev/null
    g=$(grade_of "$TMP/vdrift.json" versions)
    [ "$g" = "warn" ] || fail "a stale cache pin should grade warn, got '$g'"
    [ "$g" = "warn" ] && pass "a cache behind the checkout is a warning"
    if grep -q "behind the checkout" "$TMP/vdrift.json"; then
        pass "the drift is counted, not just detected"
    else
        fail "a stale pin did not say how far behind it is"
    fi
    if grep -q "label distinguishes nothing" "$TMP/vdrift.json"; then
        pass "two trees under one version label get called out"
    else
        fail "same-label drift not named — the version string would still read as identity"
    fi
    # ── the registered source is graded on its own terms ───────────────────
    # The blindness this closes: `versions` resolves
    # "the checkout" FROM the registration and then compares it against a cache
    # taken from that same copy. Point the registration at a frozen release
    # stage and the two agree forever — the one check built to catch drift
    # reads its ground truth from the thing that is wrong. So the registered
    # path is graded without asking it anything.

    marketplace() {
        mkdir -p "$TMP/home/.claude/plugins"
        printf '{"jStack":{"source":{"source":"directory","path":"%s"},"lastUpdated":"2026-09-20T11:15:37.694Z"}}' \
            "$1" > "$TMP/home/.claude/plugins/known_marketplaces.json"
    }

    ledger "$SHA1"
    STAGE="$TMP/state/updates/releases/76-68c646f7/stage-ev0umz_y/stack"
    mkdir -p "$STAGE"
    marketplace "$STAGE"
    run_vdoctor > "$TMP/mstage.json" 2>/dev/null
    g=$(grade_of "$TMP/mstage.json" marketplace)
    [ "$g" = "fail" ] || fail "a marketplace on a release stage should grade fail, got '$g'"
    [ "$g" = "fail" ] && pass "a source under releases/*/stage-* is a finding, not agreement"
    if grep -q "$STAGE" "$TMP/mstage.json"; then
        pass "the stage path is named, so the reader can repoint it"
    else
        fail "graded the stage without saying which path it is"
    fi

    # The point of the whole issue: this must be caught while every version
    # label on the machine still agrees. The cache is pinned at HEAD here.
    g=$(grade_of "$TMP/mstage.json" versions)
    [ "$g" != "fail" ] || fail "versions should not be the check that fails here"
    if grep -q '"grade": "fail"' "$TMP/mstage.json"; then
        pass "a machine whose versions all agree still fails on the registration"
    else
        fail "agreement on versions let a frozen registration pass the whole run"
    fi

    marketplace "$VREPO"
    run_vdoctor > "$TMP/mrepo.json" 2>/dev/null
    g=$(grade_of "$TMP/mrepo.json" marketplace)
    [ "$g" = "ok" ] || fail "a marketplace on a real checkout should grade ok, got '$g'"
    [ "$g" = "ok" ] && pass "serving from a checkout is the healthy case"

    SHIPPED="$TMP/opt/jstack-copy"
    mkdir -p "$SHIPPED"
    marketplace "$SHIPPED"
    run_vdoctor > "$TMP/mship.json" 2>/dev/null
    g=$(grade_of "$TMP/mship.json" marketplace)
    [ "$g" = "ok" ] || fail "a leaf's shipped copy should grade ok, got '$g'"
    [ "$g" = "ok" ] && pass "a shipped copy is not a failure — a leaf has no checkout"
    if grep -q "shipped copy" "$TMP/mship.json"; then
        pass "the shipped copy is named as one, not passed off as a checkout"
    else
        fail "a non-checkout source read as a checkout"
    fi

    marketplace "$TMP/not-on-disk"
    run_vdoctor > "$TMP/mgone.json" 2>/dev/null
    g=$(grade_of "$TMP/mgone.json" marketplace)
    [ "$g" = "fail" ] || fail "a source that is not there should grade fail, got '$g'"
    [ "$g" = "fail" ] && pass "a registration pointing at nothing is a failure"

    # ── and `versions` itself refuses a stage, where it can actually reach one ─
    # Every case above leaves the guard in `versions` unreachable: PLUGIN_ROOT
    # sits inside $VREPO, so `rev-parse --show-toplevel` answers first and the
    # registration is never consulted. The case that DOES reach it is a leaf —
    # the plugin unpacked outside any repo, where the registration is the only
    # candidate checkout there is. That is precisely where trusting it costs:
    # the cache was copied from the stage, so comparing the two agrees by
    # construction and `versions` would report a healthy machine forever.
    NOREPO="$TMP/norepo"
    mkdir -p "$NOREPO"
    cp -R "$VREPO/plugins/jstack" "$NOREPO/jstack"
    rm -rf "$NOREPO/jstack/__pycache__" "$NOREPO/jstack"/*/__pycache__

    # The stage has to be a real git tree standing at the very sha the cache
    # records. That agreement IS the trap, and it is the only fixture that
    # tests the guard: point this at a stage with no git in it and `versions`
    # warns "git cannot read it" for an unrelated reason, which passes whether
    # the guard is there or not.
    GSTAGE="$TMP/state/updates/releases/77-0ff57a9e/stage-qq31mb7k/stack"
    mkdir -p "$GSTAGE"
    printf 'stage\n' > "$GSTAGE/marker"
    git -C "$GSTAGE" init -q
    git -C "$GSTAGE" -c user.email=t@t -c user.name=t add -A
    git -C "$GSTAGE" -c user.email=t@t -c user.name=t commit -qm stage
    GSHA=$(git -C "$GSTAGE" rev-parse HEAD)

    vdoctor_norepo() {
        HOME="$TMP/home" JSTACK_ROOT="$TMP/root" \
        SCHEDULER_INSTALL_FILE="$TMP/root/absent-scheduler.json" \
        SCHEDULER_API_PORT=59992 \
        "$PY" "$NOREPO/jstack/bin/jstack-doctor" --json
    }

    if [ -n "$(git -C "$NOREPO" rev-parse --show-toplevel 2>/dev/null)" ]; then
        fail "the no-repo fixture is inside a git repo — the guard stays unreachable"
    else
        ledger "$GSHA"
        marketplace "$GSTAGE"
        vdoctor_norepo > "$TMP/vstage.json" 2>/dev/null
        g=$(grade_of "$TMP/vstage.json" versions)
        [ "$g" = "warn" ] || fail "versions took a release stage as its checkout, got '$g'"
        [ "$g" = "warn" ] && pass "versions refuses a stage even when every sha agrees"
        if grep -q "would mean nothing" "$TMP/vstage.json"; then
            pass "the refusal says why agreeing there would prove nothing"
        else
            fail "versions refused without naming the comparison as empty"
        fi

        # The guard must cost nothing on a leaf whose source is honest: same
        # no-repo plugin, shipped copy instead of a stage, and `versions` goes
        # back to grading the cache on its own terms.
        marketplace "$SHIPPED"
        vdoctor_norepo > "$TMP/vship.json" 2>/dev/null
        if grep -q "would mean nothing" "$TMP/vship.json"; then
            fail "the stage refusal fired on a shipped copy"
        else
            pass "a leaf on a shipped copy is still graded, not refused"
        fi
        ledger "$SHA1"
    fi

    rm -f "$TMP/home/.claude/plugins/known_marketplaces.json"
else
    echo "skip: no git — versions drift cases not run"
fi

# ── the exit status is the worst grade, and JSON agrees with it ─────────────

run_doctor --json > "$TMP/agree.json" 2>/dev/null
rc=$?
declared=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit"])' "$TMP/agree.json")
if [ "$rc" = "$declared" ]; then
    pass "process exit ($rc) matches the exit the JSON declares"
else
    fail "exit status $rc but JSON declares $declared — a gate would branch wrong"
fi

worst=$("$PY" - "$TMP/agree.json" <<'PY'
import json, sys
rank = {"ok": 0, "warn": 1, "fail": 2}
d = json.load(open(sys.argv[1]))
print(max(rank[c["grade"]] for c in d["checks"]))
PY
)
if [ "$rc" = "$worst" ]; then
    pass "exit status is the worst individual grade"
else
    fail "exit $rc is not the worst grade among the checks ($worst)"
fi

# ── injection: the READ half of the loop is graded, not assumed ────────────
# `review` proves sessions resolve to an agent and log. Nothing proved anything
# reads them back: `timeline_inject` is opt-in, no repo file ever wrote it, and
# a machine ran from bring-up to discovery writing a timeline no session opened.
# The states that must be distinguishable are: no config file, a config with no
# key, a key that matches no seat here, and a working catch-all.

CFG="$TMP/home/.claude/jstack/review.json"
mkdir -p "$(dirname "$CFG")"

run_doctor --json > "$TMP/inj-none.json" 2>/dev/null
g=$(grade_of "$TMP/inj-none.json" injection)
[ "$g" = "warn" ] || fail "no review.json should warn on injection, got '$g'"
[ "$g" = "warn" ] && pass "no config at all: the seat reads nothing, and it is said"

printf '{}\n' > "$CFG"
run_doctor --json > "$TMP/inj-empty.json" 2>/dev/null
g=$(grade_of "$TMP/inj-empty.json" injection)
[ "$g" = "warn" ] || fail "a config with no timeline_inject should warn, got '$g'"
[ "$g" = "warn" ] && pass "a config that declares no timeline_inject is named"

printf '{"timeline_inject": {"nobody/chat": 5}}\n' > "$CFG"
run_doctor --json > "$TMP/inj-miss.json" 2>/dev/null
g=$(grade_of "$TMP/inj-miss.json" injection)
[ "$g" = "warn" ] || fail "a key matching no seat should warn, got '$g'"
[ "$g" = "warn" ] && pass "a timeline_inject that matches no seat here is a warning"

printf '{"timeline_inject": {"*/*": 10}}\n' > "$CFG"
run_doctor --json > "$TMP/inj-ok.json" 2>/dev/null
g=$(grade_of "$TMP/inj-ok.json" injection)
[ "$g" = "ok" ] || fail "a catch-all over a real agent should grade ok, got '$g'"
[ "$g" = "ok" ] && pass "a catch-all injects every seat, and the count is named"

if grep -q "testbed/chat" "$TMP/inj-ok.json"; then
    pass "the injecting seat is named, not just counted"
else
    fail "graded ok without naming which seats inject"
fi

JSTACK_TIMELINE_INJECT_DISABLED=1 run_doctor --json > "$TMP/inj-off.json" 2>/dev/null
g=$(grade_of "$TMP/inj-off.json" injection)
[ "$g" = "warn" ] || fail "the kill switch should warn, got '$g'"
[ "$g" = "warn" ] && pass "the kill switch is reported, not silently obeyed"
rm -f "$CFG"

# ── a check that raises FAILS; it does not disappear ───────────────────────
# Proven by breaking a seam the doctor reads rather than by patching the tool:
# a root that resolves inside the shipping checkout makes root.py raise, and
# the only acceptable outcome is a fail-graded check that names the crash.

crash=$(cd "$PLUGIN_ROOT" && HOME="$TMP/home" JSTACK_ROOT="$PLUGIN_ROOT/state-in-checkout" \
        SCHEDULER_INSTALL_FILE="$TMP/root/absent-scheduler.json" SCHEDULER_API_PORT=59992 \
        "$PY" "$TOOL" --json 2>&1)
crash_rc=$?
if [ "$crash_rc" = "2" ] && printf '%s' "$crash" | grep -q '"grade": "fail"'; then
    pass "a root inside the shipping checkout fails loudly instead of vanishing"
else
    fail "a raising check did not surface as a failure (exit $crash_rc)"
fi

# ── report ─────────────────────────────────────────────────────────────────

echo
if [ "$fails" -eq 0 ]; then
    echo "doctor: all checks passed"
    exit 0
fi
echo "doctor: $fails failure(s)"
exit 1
