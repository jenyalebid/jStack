# Sourced by the work-harness payloads INSIDE the guest (plugin/plan-gate,
# hub/work-env). The caller sets REF and DONE before sourcing and ships
# work_probe.py beside this file. Everything here either sets up the one
# machine shape both journeys need — a Hub installed from the ref under test,
# its token, the plugin its engine loads — or refuses by name.

set -u
say() { printf '%s\n' "$*"; }
# A bail still prints the sentinel: finish_verdict reads a missing marker as
# "never reached" and stops before it prints the FAIL lines, so a run that died
# for a nameable reason would arrive nameless.
bail() { echo "FAIL $*"; echo "$DONE"; exit 0; }

PROBE=/Users/admin/work_probe.py
probe() { python3 "$PROBE" "$@"; }

export JSTACK_ROOT=/Users/admin/Desktop/Alpine
SEAT="$JSTACK_ROOT/Agents/Alpha"
mkdir -p "$JSTACK_ROOT"
export PATH="$HOME/.local/bin:$PATH"
# Every switch that would make a journey prove the opposite of its claim, and
# every override that would steer the hooks somewhere other than where they
# look on their own. `claude` inherits this shell, so unsetting here is the
# whole guarantee: the hooks must find the Hub's store on their own — the embed
# marker when a server mounts the host, the host's default when the Hub stands
# alone — and write markers where the host reads them.
unset JSTACK_PLAN_GATE_DISABLED JSTACK_ENV_INJECT_DISABLED JREMOTE_STATE_DIR \
      JSTACK_CACHE_ROOT JSTACK_RULE_REINJECT_BYTES JSTACK_TASKS_DIR
MARKS=/tmp/jstack-rule-cache   # markers.DEFAULT_CACHE_ROOT, with the override unset

hub_at_ref() {
    echo "== install the Hub from $REF, the README way with --ref =="
    # refs/heads/ keeps a ref with a slash in it (release/…) unambiguous.
    curl -fsSL "https://raw.githubusercontent.com/jenyalebid/jStack/refs/heads/$REF/install.sh" \
        | bash -s -- --yes --ref "$REF" --agent Alpha 2>&1 | tail -25
    [ -f ~/jStack/host/jstack_host/plans.py ] && [ -f ~/jStack/plugins/jstack/hooks/plan-exit.py ] \
        || bail "ref $REF carries no work harness — nothing here would be under test"
    (cd ~/jStack && git log -1 --format='  head: %h %s')
    [ -f "$SEAT/CLAUDE.md" ] || bail "the installer made no Alpha seat at $SEAT"

    # The store a hook will read, resolved the way a hook resolves it
    # (attention.state_dir, then _env.host_environment): the embed marker when
    # another server mounts the host, else the host's own default. A sealed Hub
    # installed the README way is not embedded in anything and writes no
    # marker — `embed.declare()` refuses to on a standalone profile — so the
    # marker alone is not the test. What the Hub serves is in its service
    # settings; the gate is that both name one directory.
    HOOK_STATE="$(python3 - <<'PY'
import json, pathlib
home = pathlib.Path.home()
try:
    declared = json.load(open(home / ".local/state/jremote/embedded.json")).get("state_dir") or ""
except (OSError, ValueError):
    declared = ""
print(declared or home / ".local/state/jremote")
PY
)"
    STATE="$(python3 -c 'import json,pathlib;print(json.load(open(pathlib.Path.home()/".local/state/jremote/service-settings.json"))["environment"]["JREMOTE_STATE_DIR"])' 2>/dev/null)"
    [ -n "$STATE" ] || bail "no service settings — the Hub does not say which store it serves"
    [ "$HOOK_STATE" = "$STATE" ] || bail "the hooks would read $HOOK_STATE but the Hub serves $STATE"
    say "OK the hooks read the store the Hub serves: $STATE"
    export JSTACK_TOKEN="$(jstack-host token 2>/dev/null)"
    [ -n "$JSTACK_TOKEN" ] || bail "jstack-host token printed nothing"
    probe api GET /host >/dev/null || bail "the Hub's API refused its own token"
    say "OK the Hub answers on 9090 with its own token"
}

# The hooks are exec'd by their shebang, `/usr/bin/env python3`, on the PATH
# `claude` inherits from this shell, and reach the host through the tree above
# the installed plugin (_env._host_importable). They fail OPEN on any
# exception — an ImportError included — so an interpreter that cannot import
# the host turns every env and plan hook into a silent no-op. Checked here, with
# that interpreter and that tree, so the failure has a name instead of reading
# as a dozen unrelated FAILs further down.
hook_host_check() {
    PLUGIN="$(python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".claude/plugins/installed_plugins.json"
try:
    d = json.loads(p.read_text())
except (OSError, ValueError):
    d = {}
rows = d.get("plugins", d) if isinstance(d, dict) else {}
for name, entries in (rows.items() if isinstance(rows, dict) else []):
    if name.startswith("jstack@"):
        entries = entries if isinstance(entries, list) else [entries]
        for e in entries:
            if isinstance(e, dict) and e.get("installPath"):
                print(e["installPath"]); raise SystemExit
PY
)"
    [ -n "$PLUGIN" ] || bail "the engine records no installed jstack plugin"
    say "  the engine loads jstack from $PLUGIN"
    # Where the host tree actually is. NOT simply above installPath: jstack is
    # installed from a *directory* marketplace (install.sh:756), so installPath
    # names a cache copy under ~/.claude/plugins/cache with nothing but the
    # marketplace above it, while the files the engine executes and the host
    # beside them are the checkout's. Requiring it above installPath bailed on
    # every correct install, and this gate is the first thing a guest hits: it
    # failed both work journeys before their first assertion.
    CHECKOUT="$(python3 - <<'MARKETPLACE'
import json, pathlib
try:
    settings = json.loads((pathlib.Path.home() / ".claude/settings.json").read_text())
except (OSError, ValueError):
    settings = {}
for name, row in (settings.get("extraKnownMarketplaces") or {}).items():
    source = (row or {}).get("source") or {}
    if source.get("source") == "directory" and source.get("path"):
        print(source["path"]); raise SystemExit
MARKETPLACE
)"
    local host_tree=""
    for tree in "$PLUGIN/../../host" "$CHECKOUT/host" "$HOME/jStack/host"; do
        [ "$tree" = "/host" ] && continue
        if [ -f "$tree/jstack_host/plans.py" ]; then host_tree="$tree"; break; fi
    done
    [ -n "$host_tree" ] \
        || bail "no host tree beside the plugin the engine loads (tried $PLUGIN/../../host, $CHECKOUT/host, $HOME/jStack/host) — every env and plan hook fails open"
    say "  the hooks reach the host at $host_tree"
    if out="$(python3 - "$host_tree" 2>&1 <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import jstack_host.environment, jstack_host.plans  # noqa: F401
PY
)"; then
        say "OK the hooks' interpreter ($(python3 -V 2>&1)) imports the host they write through"
    else
        bail "the hooks run under $(command -v python3) ($(python3 -V 2>&1)), which cannot import the host — every env and plan hook fails open: $(printf '%s' "$out" | tail -1)"
    fi
}

# One engine run in the seat. Sets R_TEXT (the reply) and R_SID (the session
# id the engine actually ran under — read from its own result, because every
# marker and row below is keyed on it and a resume that forked would put them
# under an id this script never looks at).
run_claude() {  # run_claude <tag> <dir> <claude args...>
    local tag="$1" dir="$2"; shift 2
    # The stream, not the final `result`: the plugin's own Stop hook blocks a
    # print session's first stop once (stop-timeline-remind), and the turn the
    # model spends answering the hook becomes `result`. The reply to the prompt
    # is the first assistant text the session produced.
    (cd "$dir" && claude "$@" --output-format stream-json --verbose) > "$HOME/$tag.json" 2> "$HOME/$tag.err"
    eval "$(python3 - "$HOME/$tag.json" <<'STREAM'
import json, shlex, sys
text, sid = "", ""
for line in open(sys.argv[1]):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    sid = ev.get("session_id") or sid
    if ev.get("type") == "assistant" and not text:
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                text = block["text"]; break
print("R_TEXT=" + shlex.quote(text)); print("R_SID=" + shlex.quote(sid))
STREAM
)"
    printf '%s\n' "$R_TEXT" | tail -6 | sed "s/^/  $tag: /"
    [ -n "$R_SID" ] || { tail -5 "$HOME/$tag.err" | sed "s/^/  $tag stderr: /"; }
}

# A plan-mode session, for real: the print CLI (`-p`) offers neither
# EnterPlanMode nor ExitPlanMode (proven on 2.1.281, 2026-09-25), so the one
# journey that needs a plan approved runs an interactive `claude` in a tmux
# pane, shown in the guest's own Terminal, and this drives it the way a person
# would: the prompt pasted in, the approval dialog answered by keys. "Yes,
# manually approve edits" is the answer, so an approved session can edit nothing
# without a further key — the project the gates grade below stays untouched by
# construction, not by promise.
#
# The CLI's own first-run screens (theme, security notes, the workspace trust
# question) are settled the way the CLI documents for a driven session: its
# own config keys (`hasCompletedOnboarding`, `projects[dir].hasTrustDialogAccepted`
# — the CLI names the latter in its own guidance). The driver still answers
# them by key if one appears anyway.
# plan_session <tag> <dir> <sid> <plan-file> [resume]
plan_session() {
    local tag="$1" dir="$2" sid="$3" plan="$4" resume="${5:-}" T="plan-$1"
    local tmux="/Applications/jStack Hub.app/Contents/MacOS/tmux"
    [ -x "$tmux" ] || tmux="$(command -v tmux)"
    [ -n "$tmux" ] || { R_SID=""; R_TEXT=""; echo "FAIL no tmux in the guest to hold an interactive session"; return; }
    python3 - "$dir" <<'SEED'
import json, os, sys
f = os.path.expanduser("~/.claude.json")
try: d = json.load(open(f))
except Exception: d = {}
d.setdefault("theme", "dark"); d["hasCompletedOnboarding"] = True
d.setdefault("projects", {}).setdefault(sys.argv[1], {})["hasTrustDialogAccepted"] = True
json.dump(d, open(f, "w"), indent=1)
SEED
    prompt_for "$plan" > "$HOME/$tag.prompt"
    "$tmux" kill-session -t "$T" 2>/dev/null
    local flag="--session-id"; [ -n "$resume" ] && flag="--resume"
    "$tmux" new-session -d -s "$T" -x 160 -y 48 -c "$dir" \
        "claude $flag $sid --permission-mode plan; echo '-- session ended --'; sleep 5"
    printf '#!/bin/bash\nexec "%s" attach -t %s\n' "$tmux" "$T" > "$HOME/$T.command"
    chmod +x "$HOME/$T.command"; open -a Terminal "$HOME/$T.command" 2>/dev/null || true
    python3 - "$tmux" "$T" "$HOME/$tag.prompt" "$HOME/$tag.json" "$sid" <<'DRIVE'
import json, re, subprocess, sys, time
tmux, target, prompt_file, out, sid = sys.argv[1:6]
def pane():  # the visible screen only: a dismissed dialog must not linger in scrollback
    return subprocess.run([tmux, "capture-pane", "-p", "-t", target],
                          capture_output=True, text=True).stdout
def keys(*k):
    subprocess.run([tmux, "send-keys", "-t", target, *k], check=False)
def choose(p, want_text):
    rows = [l for l in p.splitlines() if re.search(r"\b(Yes|No),", l)]
    cur = next((i for i, l in enumerate(rows) if re.match(r"[\s│]*[❯>]", l)), 0)
    want = next((i for i, l in enumerate(rows) if want_text in l), None)
    if want is None:
        return None
    for _ in range(abs(want - cur)):
        keys("Down" if want > cur else "Up"); time.sleep(0.3)
    keys("Enter"); return rows[want].strip()
log, sent, approved, done, first_run = [], False, False, False, 0
busy = re.compile(r"esc to interrupt")
deadline = time.monotonic() + 420
while time.monotonic() < deadline:
    p = pane()
    if "session ended" in p:
        log.append("ended"); break
    for marker in ("Choose the text style", "Security notes", "terminal setup?"):
        if marker in p and first_run < 6:
            keys("Enter"); first_run += 1; log.append("first-run:" + marker); time.sleep(2); break
    else:
        if "Quick safety check" in p or "Do you trust the files" in p:
            keys("Enter"); log.append("trusted"); time.sleep(2); continue
        if "Yes, I trust this folder" in p:
            log.append("trusted:" + str(choose(p, "Yes, I trust"))); time.sleep(2); continue
        if "Would you like to proceed" in p and not approved:
            pick = choose(p, "manually approve")
            if pick is None:
                time.sleep(1); continue
            approved = True; log.append("approved:" + pick); time.sleep(3); continue
        if not sent and "plan mode on" in p and not busy.search(p):
            subprocess.run([tmux, "load-buffer", prompt_file], check=True)
            subprocess.run([tmux, "paste-buffer", "-p", "-t", target], check=True)
            time.sleep(1); keys("Enter"); sent = True; log.append("prompt sent"); time.sleep(3); continue
        if approved and not busy.search(p) and "Would you like" not in p \
                and re.search(r"(?m)^[\s│]*[❯>]\s*(\S.*)?$", p) and "plan mode on" in p:
            done = True; log.append("turn over"); break
        time.sleep(2)
    continue
tail = pane()
if not done and approved: keys("Escape"); time.sleep(1)
keys("/exit", "Enter"); time.sleep(3)
json.dump({"session_id": sid, "sent": sent, "approved": approved, "done": done,
           "steps": log, "pane": tail}, open(out, "w"), indent=1)
print("  plan session: " + " → ".join(log))
if not approved: print("  pane: " + " | ".join(l for l in tail.splitlines()[-12:] if l.strip()))
DRIVE
    "$tmux" kill-session -t "$T" 2>/dev/null
    R_SID=""; ls "$HOME"/.claude/projects/*/"$sid".jsonl >/dev/null 2>&1 && R_SID="$sid"
    R_TEXT="$(probe py 'd.get("pane", "")' < "$HOME/$tag.json" 2>/dev/null)"
}

# A reply with the fence and quote ceremony a model adds taken off, so an exact
# comparison compares the line and not its wrapping.
norm() { printf '%s' "$1" | tr -d '`"' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | sed '/^$/d'; }

# bash 3.2 (the guest's /bin/bash) has no ${v^^}.
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }
