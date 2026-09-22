#!/usr/bin/env bash
# jStack installer — a bare machine to a working stack, in one command.
#
#   curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash
#   ./install.sh --yes --agent Ada                   # unattended, everything
#   ./install.sh --dry-run                           # print the plan, touch nothing
#
# Setup used to be seven steps across two surfaces, each documented, each
# failing invisibly when skipped. This does all seven and then runs
# `jstack-doctor`, so the install ends with a verdict rather than an assumption.
#
# What it will not do: run as root, overwrite a file it did not write, or touch
# anything outside $CHECKOUT, ~/.claude, ~/Agents, ~/Applications and the shell
# profile line it appends. Every step is idempotent — running it twice is a
# no-op with a different report, which is what makes it safe as an updater.

set -uo pipefail

REPO_URL="${JSTACK_REPO_URL:-https://github.com/jenyalebid/jStack.git}"
CHECKOUT="${JSTACK_CHECKOUT:-$HOME/jStack}"
AGENT_ROOT="${JSTACK_AGENT_ROOT:-$HOME/Agents}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=9

ASSUME_YES=0
DRY_RUN=0
DO_UNINSTALL=0
DO_PURGE=0
AGENT_NAME=""
WANT_SCHEDULER=1
WANT_CLAUDE=1
WANT_HOST=1
WANT_MENUBAR=1
WANT_APP=1
DECLARE_ROOT=0
ROOT_FROM_FLAG=0
LAST_LOG=""
LAST_ELAPSED=""
# Set by the two steps that can each half-fail without stopping the install.
# Pairing needs BOTH — a host with no app has nothing to introduce itself to,
# and an app with no host has nothing to be introduced to.
HOST_INSTALLED=0
SIGNED_HUB=0
APP_INSTALLED=0

usage() {
    cat <<'EOF'
usage: install.sh [options]

  --yes, -y           don't ask; accept every default
  --dry-run           print what would happen and change nothing
  --uninstall         take jStack back off; leave your data and dependencies
  --purge             uninstall AND delete the host's state, token and root decl
  --root DIR          root for Agents, Logs, Config, State, Credentials
  --agent NAME        create this agent workspace (default: ask, or "Jarvis" with --yes)
  --agent-root DIR    where agent workspaces live (default: <root>/Agents)
  --checkout DIR      where to clone jStack (default: ~/jStack)
  --no-scheduler      don't install the scheduler daemon (no recurring wakes)
  --no-claude         don't install Claude Code even if it is missing
  --no-host           use the client with another Hub; no local Hub or menu
  --no-menubar        install the host but not its menu bar icon
  --no-app            don't install the Mac app
  --help, -h          this

The install asks two things — where the root goes and what to call the first
agent workspace — and then runs to the end. Every other part of the stack has
one sensible answer, so it is installed, and the way to decline it is a flag
above rather than a prompt.

Environment: JSTACK_REPO_URL, JSTACK_CHECKOUT, JSTACK_AGENT_ROOT override the
defaults above. JSTACK_ROOT, if you export it, is honoured by everything the
stack does afterwards — see docs/systems/root-derivation.md.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)      ASSUME_YES=1 ;;
        --dry-run)     DRY_RUN=1 ;;
        --uninstall)   DO_UNINSTALL=1 ;;
        --purge)       DO_UNINSTALL=1; DO_PURGE=1 ;;
        --agent)       AGENT_NAME="${2:-}"; shift ;;
        --agent-root)  AGENT_ROOT="${2:-}"; shift ;;
        --checkout)    CHECKOUT="${2:-}"; shift ;;
        # ROOT_FROM_FLAG separates "someone asked for this root, now" from "this
        # shell happens to export one". Both arrive as $JSTACK_ROOT and they
        # need opposite handling: an exported root is already declared
        # somewhere, an asked-for one is a request to change what is declared.
        --root)        JSTACK_ROOT="${2:-}"; ROOT_FROM_FLAG=1; shift ;;
        --scheduler)   WANT_SCHEDULER=1 ;;   # back-compat: it is the default now
        --no-scheduler) WANT_SCHEDULER=0 ;;
        --no-claude)   WANT_CLAUDE=0 ;;
        --no-host)     WANT_HOST=0 ;;
        --no-menubar)  WANT_MENUBAR=0 ;;
        --no-app)      WANT_APP=0 ;;
        -h|--help)     usage; exit 0 ;;
        *)             echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# The one installer. Sub-installers check this and refuse direct invocation.
export JSTACK_INSTALLER=1

# ── output ──────────────────────────────────────────────────────────────────

if [ -t 1 ]; then B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; Z=$'\033[0m'
else B=""; DIM=""; RED=""; GRN=""; YEL=""; Z=""; fi

step()  { printf '\n%s==>%s %s\n' "$B" "$Z" "$1"; }
ok()    { printf '  %sok%s   %s\n' "$GRN" "$Z" "$1"; }
warn()  { printf '  %swarn%s %s\n' "$YEL" "$Z" "$1"; }
die()   { printf '  %sfail%s %s\n' "$RED" "$Z" "$1" >&2; exit 1; }
note()  { printf '  %s%s%s\n' "$DIM" "$1" "$Z"; }
would() { printf '  %swould%s %s\n' "$DIM" "$Z" "$1"; }

# The file this machine's shell reads, and where a root declaration lands.
profile_path() {
    case "${SHELL:-}" in
        */zsh)  printf '%s\n' "$HOME/.zshrc" ;;
        */bash) printf '%s\n' "$HOME/.bash_profile" ;;
        *)      printf '%s\n' "$HOME/.profile" ;;
    esac
}

# The root this machine has already been told about, if any.
#
# WHY THIS EXISTS. The declaration is an `export JSTACK_ROOT=…` line in the
# shell profile, and the documented way to re-run this installer is
# `curl … | bash` — a non-login, non-interactive shell, which sources no
# profile at all. So a machine with a perfectly good root elsewhere arrived at
# the question with $JSTACK_ROOT unset and was offered $HOME as the default.
# Pressing return on an update then declared a SECOND root and orphaned the
# tree the first one owns: agents, logs and credentials still on disk under a
# root nothing points at any more.
#
# A re-install must not be able to lose the root by agreeing with the prompt.
# Reading it back from where the last install wrote it is the whole fix; every
# profile is checked, not just this shell's, because the shell that installed
# is not always the shell that updates.
declared_root() {
    local line=""
    for f in "$(profile_path)" "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.profile"; do
        [ -f "$f" ] || continue
        line="$(grep -hE '^[[:space:]]*export[[:space:]]+JSTACK_ROOT=' "$f" 2>/dev/null | tail -1)"
        [ -n "$line" ] || continue
        line="${line#*JSTACK_ROOT=}"
        line="${line%\"*}"; line="${line#\"}"
        line="${line%\'*}"; line="${line#\'}"
        line="${line/#\~/$HOME}"
        case "$line" in /*) printf '%s\n' "$line"; return 0 ;; esac
    done
    return 1
}

# Everything that changes the machine goes through here, so --dry-run is a
# property of the script rather than a flag each step remembers to check.
run() {
    if [ "$DRY_RUN" = "1" ]; then would "$*"; return 0; fi
    "$@"
}

# ── uninstall ────────────────────────────────────────────────────────────────
# Take jStack back off without touching anything it merely used. THE RULE that
# makes this safe: a dependency the installer SKIPS when it is already present —
# Claude Code, git, Python, python-dateutil, the WireGuard tools — is never
# removed here. Only what jStack itself wrote comes off. The destructive halves
# (the signed app bundle, the host's state and token) are delegated to the
# sub-installers that placed them, so this orchestrates rather than reimplements.
# --uninstall leaves your data (agent workspaces, host state, token); --purge
# also takes the host state/token/credentials and the root declaration.
uninstall() {
    local purge="$1"
    PLUGIN="$CHECKOUT/plugins/jstack"
    BIN="$PLUGIN/bin"
    PROFILE="$(profile_path)"
    local CLAUDE; CLAUDE="$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")"

    step "Uninstalling jStack${purge:+ (purge)}"
    note "dependencies are left untouched: Claude Code, git, Python, python-dateutil, WireGuard"

    # 1. the Mac app — its own installer knows the signed bundle it placed.
    if [ -f "$CHECKOUT/app/install.sh" ]; then
        run bash "$CHECKOUT/app/install.sh" --uninstall || warn "app uninstall reported a problem"
    else
        note "no app installer in the checkout — skipping the Mac app"
    fi

    # 2. the host LaunchAgent; the menu bar icon comes off with it. --purge also
    #    deletes the state, token and credentials it keeps.
    if [ -f "$CHECKOUT/host/install.sh" ]; then
        if [ -n "$purge" ]; then
            run bash "$CHECKOUT/host/install.sh" --purge || warn "host purge reported a problem"
        else
            run bash "$CHECKOUT/host/install.sh" --uninstall || warn "host uninstall reported a problem"
        fi
    else
        note "no host installer in the checkout — skipping the host"
    fi

    # 3. the scheduler daemon.
    if command -v jstack-scheduler >/dev/null 2>&1; then
        run jstack-scheduler uninstall || warn "scheduler uninstall reported a problem"
    elif [ -x "$BIN/jstack-scheduler" ]; then
        run "$BIN/jstack-scheduler" uninstall || warn "scheduler uninstall reported a problem"
    else
        note "no scheduler on PATH or in the checkout — skipping the daemon"
    fi

    # 4. the Claude Code plugin and its marketplace entry.
    if [ -x "$CLAUDE" ] || command -v claude >/dev/null 2>&1; then
        run "$CLAUDE" plugin uninstall jstack >/dev/null 2>&1 && ok "plugin removed" || note "plugin was not installed"
        run "$CLAUDE" plugin marketplace remove jStack >/dev/null 2>&1 && ok "marketplace jStack removed" || note "marketplace jStack was not registered"
    else
        note "Claude Code not found — skipping the plugin"
    fi

    # 5. the rule and command symlinks — only the ones pointing back into this
    #    checkout, so a symlink you made yourself is never touched.
    local removed=0 d f tgt
    for d in "$HOME/.claude/rules" "$HOME/.claude/commands"; do
        [ -d "$d" ] || continue
        for f in "$d"/*; do
            [ -L "$f" ] || continue
            tgt="$(readlink "$f" 2>/dev/null)"
            case "$tgt" in
                "$CHECKOUT"/*|*/rules-stage/*|*/commands-stage/*)
                    run rm -f "$f" && removed=$((removed+1)) ;;
            esac
        done
    done
    ok "removed $removed rule/command symlink(s)"

    # 6. the profile lines the installer appended: the `# jstack` PATH line
    #    always, the root declaration only on --purge (it names a tree that
    #    survives an --uninstall).
    if [ -f "$PROFILE" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            would "strip the '# jstack' PATH line from $PROFILE"
            [ -n "$purge" ] && would "strip the JSTACK_ROOT declaration from $PROFILE"
        else
            # grep -v exits 1 when it prints nothing — the case where the last
            # line is the one being stripped — so never gate the rewrite on its
            # exit, or removing the final line becomes a silent no-op.
            local tmp; tmp="$(mktemp)"
            grep -vE '^[[:space:]]*export[[:space:]]+PATH=.*# jstack[[:space:]]*$' "$PROFILE" > "$tmp" || true
            if [ -n "$purge" ]; then
                grep -vE '^[[:space:]]*export[[:space:]]+JSTACK_ROOT=' "$tmp" > "$tmp.2" || true
                mv "$tmp.2" "$tmp"
            fi
            cat "$tmp" > "$PROFILE"; rm -f "$tmp"
            ok "cleaned $PROFILE"
        fi
    fi

    # 7. your data is yours, not ours to delete — name what was left behind.
    if declared_root >/dev/null 2>&1; then
        note "your JSTACK_ROOT tree at $(declared_root) was left in place"
    fi
    [ -d "$AGENT_ROOT" ] && note "agent workspaces under $AGENT_ROOT were left in place — remove by hand if you want them gone"

    # 8. the checkout itself, last, from outside it. Guarded so a blank or
    #    home-valued CHECKOUT can never expand into rm -rf $HOME or rm -rf /.
    case "$CHECKOUT" in
        ""|"/"|"$HOME") warn "refusing to remove CHECKOUT=$CHECKOUT — remove it by hand if that is right" ;;
        *)
            if [ -d "$CHECKOUT" ]; then
                cd "$HOME" 2>/dev/null || cd /
                run rm -rf "$CHECKOUT" && ok "removed $CHECKOUT"
            fi ;;
    esac

    step "jStack is off this Mac"
    exit 0
}

if [ "$DO_UNINSTALL" = "1" ]; then
    if [ "$DO_PURGE" = "1" ]; then uninstall purge; else uninstall ""; fi
fi

# Is there a human at a terminal to answer a question?
#
# NOT `[ -t 0 ]`. The documented way to run this is `curl … | bash`, which
# makes stdin the pipe carrying the script itself — never a tty, no matter who
# is sitting there. Testing stdin therefore silently turned every question in
# this installer into its default for the one invocation the README teaches:
# the workspace path was stamped as ~/Agents/Main without asking, the scheduler
# was skipped without offering, and the install ended by telling the operator
# to go run the thing it had just decided not to run.
#
# /dev/tty is the controlling terminal regardless of what stdin is piped from,
# which is exactly the question being asked. Absent — cron, a Docker build, a
# CI step — it cannot be opened, and defaults are right.
interactive() {
    [ "$ASSUME_YES" = "1" ] && return 1
    [ -r /dev/tty ] && [ -w /dev/tty ] || return 1
    # Readable and writable is not the same as attached: a detached process
    # keeps the device node and fails at open. Prove it by opening it.
    { : >/dev/tty; } 2>/dev/null || return 1
    return 0
}

# Free-text answer with a default. $1 prompt, $2 default.
#
# The only kind of question this installer asks. There is deliberately no
# yes/no helper: a y/n prompt is an offer, and every offer this script used to
# make had one answer that worked and one that produced a green install missing
# a part. Those are flags now.
ask_value() {
    local reply
    if ! interactive; then printf '%s' "$2"; return; fi
    printf '  %s [%s]: ' "$1" "$2" >/dev/tty
    read -r reply </dev/tty || reply=""
    printf '%s' "${reply:-$2}"
}

# A long step that would otherwise look hung. Runs the command with its output
# in a log, prints elapsed seconds in place, and leaves one line behind.
#
# The Claude Code download, the clone and the host's wheel build are minutes
# each with nothing on screen; silence for that long reads as a hang, and the
# operator's next move is Ctrl-C on a working install.
run_long() {
    local label="$1"; shift
    local log; log="$(mktemp -t jstack-step)"
    if [ "$DRY_RUN" = "1" ]; then would "$*"; return 0; fi
    "$@" >"$log" 2>&1 &
    local pid=$! start=$SECONDS elapsed
    while kill -0 "$pid" 2>/dev/null; do
        elapsed=$((SECONDS - start))
        printf '\r  %s…%s %s  %ss' "$DIM" "$Z" "$label" "$elapsed"
        sleep 1
    done
    wait "$pid"; local rc=$?
    elapsed=$((SECONDS - start))
    printf '\r\033[2K'
    LAST_LOG="$log"
    LAST_ELAPSED="$elapsed"
    return $rc
}

# ── 0. preflight ────────────────────────────────────────────────────────────

step "Checking prerequisites"

[ "$(id -u)" != "0" ] || die "don't run this as root — jStack installs per-user, and a root-owned checkout is a machine only root can fix"

case "$(uname -s)" in
    Darwin|Linux) ok "$(uname -s) $(uname -m)" ;;
    *) die "unsupported platform $(uname -s) — macOS and Linux only" ;;
esac

command -v git >/dev/null 2>&1 || die "no git on PATH — install it first (macOS: xcode-select --install)"
ok "git — $(git --version)"

PY=""
for cand in python3 python3.13 python3.12 python3.11; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PY_MAJOR,$MIN_PY_MINOR) else 1)" 2>/dev/null; then
        PY="$(command -v "$cand")"; break
    fi
done
[ -n "$PY" ] || die "no python3 >= $MIN_PY_MAJOR.$MIN_PY_MINOR on PATH — the scheduler needs zoneinfo, which arrived in 3.9"
ok "python — $("$PY" --version 2>&1) at $PY"

# ── 0.5 everything this install needs to be told ────────────────────────────
#
# Every question lives here, before any of them is acted on, and there are
# three. Nothing below this block stops to ask.
#
# The rule that decides what belongs here: a question earns its place when the
# answer genuinely differs between machines AND cannot be worked out from the
# machine itself. The root does — a personal Mac, a shared box and an external
# volume are three different right answers. The workspace name does — it is a
# name, and only a person has one. The app does — it downloads a signed
# release from the internet, which is a different kind of decision from
# building a local file.
#
# Everything else got asked once and shouldn't have been. PATH, the host, the
# icon, dateutil, the scheduler: each had exactly one sensible answer, each
# produced a broken-but-green install when answered the other way, and each
# turned a three-minute install into a quiz. They are flags in --help now,
# which is where a rarely-wanted answer belongs.

step "What this install needs to know"

if [ -n "${JSTACK_ROOT:-}" ]; then
    JSTACK_ROOT="${JSTACK_ROOT/#\~/$HOME}"
    case "$JSTACK_ROOT" in
        /*) : ;;
        *)  die "JSTACK_ROOT=$JSTACK_ROOT is not an absolute path — launchd refuses a relative one and the daemons will not start. Try $HOME/${JSTACK_ROOT#./}" ;;
    esac
    if [ "$ROOT_FROM_FLAG" = "1" ]; then
        # --root is a request to MOVE the root, so it has to outlive this run.
        # It did not: the flag arrives as $JSTACK_ROOT, this branch read that as
        # "already declared in the environment", and DECLARE_ROOT stayed 0 — so
        # the profile was never written and the root reverted the moment the
        # shell closed. The branch below even told people to "re-run with --root
        # DIR to move it", which was the one thing that could not work.
        DECLARE_ROOT=1
        ok "root requested — $JSTACK_ROOT"
    else
        ok "root declared in the environment — $JSTACK_ROOT"
    fi
elif JSTACK_ROOT="$(declared_root)"; then
    # Already answered on this machine, on a previous install. Asking again
    # would put the tree one keystroke from being orphaned — see
    # `declared_root`. `--root` still overrides, because it arrives as
    # $JSTACK_ROOT above and never reaches here.
    ok "root already declared on this machine — $JSTACK_ROOT"
    note "re-run with --root DIR to move it"
else
    # Re-asked until it is absolute, rather than accepted and repaired.
    #
    # A typed "work" used to be taken literally: every derived dir became a
    # relative string, and the plist the scheduler writes put that string in
    # WorkingDirectory and StandardErrorPath — two fields launchd requires to
    # be absolute. The job exited 78 before running a line, KeepAlive retried
    # it forever, and the install ended on a red FAIL naming the daemon rather
    # than the answer that broke it.
    #
    # Silently anchoring it to $HOME would hide the same typo behind a tree
    # nobody meant to create, so the loop says why and shows the fix. Under
    # --yes or with no terminal, ask_value returns the default, which is
    # absolute — so this cannot spin.
    # The default is shown with a trailing slash so the answer reads as a
    # directory to put things under, not as a thing to replace; the slash is
    # stripped again before use, because every path below joins with one.
    #
    # Then it is read back and confirmed. This is the one answer the installer
    # cannot check for you — every derived directory hangs off it and it goes
    # into the shell profile — and a typo is silent until an agent workspace
    # turns up somewhere nobody meant. Declining goes back to the question
    # rather than forward with the answer.
    while :; do
        JSTACK_ROOT="$(ask_value "Root for Agents, Logs, Config, State and Credentials" "$HOME/")"
        JSTACK_ROOT="${JSTACK_ROOT/#\~/$HOME}"
        while [ "$JSTACK_ROOT" != "/" ] && [ "${JSTACK_ROOT%/}" != "$JSTACK_ROOT" ]; do
            JSTACK_ROOT="${JSTACK_ROOT%/}"
        done
        case "$JSTACK_ROOT" in
            /*) ;;
            "") warn "the root cannot be empty"; interactive || die "JSTACK_ROOT must be an absolute path"; continue ;;
            *)  warn "a root must be an absolute path — try $HOME/${JSTACK_ROOT#./}"
                interactive || die "JSTACK_ROOT must be an absolute path"; continue ;;
        esac
        interactive || break
        note "everything hangs off it: $JSTACK_ROOT/Agents, /Logs, /Config, /State, /Credentials"
        case "$(ask_value "Use $JSTACK_ROOT? (y/n)" "y")" in
            [Yy]*) break ;;
            *) note "let's try again" ;;
        esac
    done
    if [ "$JSTACK_ROOT" != "$HOME" ]; then
        DECLARE_ROOT=1
        ok "root — $JSTACK_ROOT (declared in your shell profile below)"
    else
        ok "root — $HOME"
    fi
fi
export JSTACK_ROOT

# --agent-root wins where it was given; otherwise the root answer governs.
case "$AGENT_ROOT" in
    "$HOME/Agents") AGENT_ROOT="$JSTACK_ROOT/Agents" ;;
esac

# The second and last question — and only when there is something to answer.
#
# Asked here rather than at step 4 so both answers are given before anything is
# installed: an install that stops to ask something ten minutes in cannot be
# walked away from. But hoisting it that far up also hoisted it past the check
# that made it worth asking, and re-running the installer on a machine that
# already has agents then asked for a "first" workspace it would never create.
#
# The probe below is a shell approximation of root.agents(); the real answer at
# step 4 is still root.py's, so the two cannot disagree in a way that matters.
# This one is only ever allowed to SKIP the question — if it is wrong and finds
# nothing, step 4 finds the agents anyway and the answer goes unused. It can
# never cause a workspace to be created.
have_agents=0
if [ -d "$AGENT_ROOT" ]; then
    for candidate in "$AGENT_ROOT"/*/CLAUDE.md "$AGENT_ROOT"/*/*/CLAUDE.md; do
        [ -f "$candidate" ] && { have_agents=1; break; }
    done
fi

if [ "$have_agents" = "1" ]; then
    ok "agents — $AGENT_ROOT already holds some, nothing to name"
else
    # Same read-back as the root: the name becomes a directory, a CLAUDE.md
    # and the seat every later session opens into, and none of that is easy to
    # rename afterwards.
    while [ -z "$AGENT_NAME" ]; do
        AGENT_NAME="$(ask_value "Name for your first agent workspace" "Jarvis")"
        interactive || break
        case "$(ask_value "Create $AGENT_ROOT/$AGENT_NAME? (y/n)" "y")" in
            [Yy]*) ;;
            *) AGENT_NAME=""; note "let's try again" ;;
        esac
    done
    AGENT_NAME="${AGENT_NAME:-Jarvis}"
    ok "first agent — $AGENT_ROOT/$AGENT_NAME"
fi

# ── 1. Claude Code ──────────────────────────────────────────────────────────

step "Claude Code"

CLAUDE="$(command -v claude 2>/dev/null || true)"
[ -n "$CLAUDE" ] || [ ! -x "$HOME/.local/bin/claude" ] || CLAUDE="$HOME/.local/bin/claude"

if [ -n "$CLAUDE" ]; then
    ok "already installed — $("$CLAUDE" --version 2>&1 | head -1)"
elif [ "$WANT_CLAUDE" = "0" ]; then
    warn "not installed, and --no-claude was passed — the plugin cannot be registered without it"
else
    # Not a question: every step after this one registers a plugin, links a
    # rule or installs a command into Claude Code. Answering no here produces
    # an install that finishes green and delivers nothing.
    if [ "$DRY_RUN" = "1" ]; then
        would "curl -fsSL https://claude.ai/install.sh | bash"
    elif run_long "downloading Claude Code" bash -c 'curl -fsSL https://claude.ai/install.sh | bash'; then
        CLAUDE="$HOME/.local/bin/claude"
        ok "installed in ${LAST_ELAPSED}s — $("$CLAUDE" --version 2>&1 | head -1)"
    else
        warn "the Claude Code installer failed; see $LAST_LOG"
    fi
fi

# The installer drops it in ~/.local/bin, which is not on a default PATH.
export PATH="$HOME/.local/bin:$PATH"

# ── 2. the checkout ─────────────────────────────────────────────────────────

step "jStack source at $CHECKOUT"

# Which release is current, resolved once over the git protocol (no API rate
# limit). Everything below installs THIS — the published, tested snapshot with
# its release identity — never raw main.
RELEASE_TAG="$(git ls-remote --tags "$REPO_URL" 'refs/tags/stack-release-2*' 2>/dev/null \
    | sed 's|.*refs/tags/||' | sort | tail -1)"

if [ -d "$CHECKOUT/.git" ] && [ -n "$RELEASE_TAG" ] \
        && [ "${JSTACK_SOURCE:-0}" != "1" ] && [ -z "${JSTACK_RELEASE_CONFIG:-}" ] \
        && [ -z "$(git -C "$CHECKOUT" status --porcelain 2>/dev/null)" ] \
        && [ -z "$(git -C "$CHECKOUT" log --oneline '@{u}..HEAD' 2>/dev/null)" ]; then
    # A clean source clone is what earlier installers left behind, and it is
    # exactly why machines reported stale versions: main is source, not the
    # release. Convert it — old bytes move aside rather than vanish. A dirty
    # tree or local commits mean somebody's work: leave it on the git path.
    # JSTACK_SOURCE=1 keeps a deliberate source install; the publisher is
    # recognized by JSTACK_RELEASE_CONFIG and never converted.
    ASIDE="$CHECKOUT-source-$(date +%Y%m%d%H%M%S)"
    if [ "$DRY_RUN" = "1" ]; then
        would "move source clone to $ASIDE and install $RELEASE_TAG"
    else
        mv "$CHECKOUT" "$ASIDE"
        ok "source clone moved aside to $ASIDE — installing the published release"
    fi
fi

if [ -d "$CHECKOUT/.git" ]; then
    # An existing checkout is somebody's working tree. Fetch so the install is
    # current, but never reset — an installer that discards local commits is a
    # worse outcome than an installer that is one commit behind.
    run git -C "$CHECKOUT" fetch --quiet origin
    if [ "$DRY_RUN" = "1" ]; then
        would "git -C $CHECKOUT merge --ff-only (if clean)"
    elif [ -z "$(git -C "$CHECKOUT" status --porcelain)" ] && git -C "$CHECKOUT" merge --ff-only --quiet '@{u}' 2>/dev/null; then
        ok "updated to $(git -C "$CHECKOUT" log --oneline -1)"
    elif [ -n "$(git -C "$CHECKOUT" log --oneline '@{u}..HEAD' 2>/dev/null)" ]; then
        # Local commits are somebody's work: refuse loudly, never silently.
        # An install that keeps running old code under a green summary is the
        # failure this script exists to stop, so this is a die, not a warn.
        die "$CHECKOUT has local commits and is $(git -C "$CHECKOUT" rev-list --count 'HEAD..@{u}') behind — rebase or move it aside, then re-run"
    else
        # Uncommitted noise (build junk, an aborted edit) must not pin the
        # whole machine to an old stack. Save the exact bytes aside, then take
        # upstream — the machine ends current, and nothing is lost.
        STASH="$CHECKOUT/../jstack-local-changes-$(date +%Y%m%d%H%M%S).diff"
        git -C "$CHECKOUT" diff HEAD > "$STASH" 2>/dev/null
        run git -C "$CHECKOUT" checkout -- . && run git -C "$CHECKOUT" clean -fdq -e .venv -e .venv312
        if git -C "$CHECKOUT" merge --ff-only --quiet '@{u}' 2>/dev/null; then
            ok "updated to $(git -C "$CHECKOUT" log --oneline -1) (local edits saved to $STASH)"
        else
            die "could not fast-forward $CHECKOUT — it has diverged from origin; move it aside and re-run"
        fi
    fi
elif [ -f "$CHECKOUT/host/release-identity.json" ] && [ -f "$CHECKOUT/plugins/jstack/.claude-plugin/plugin.json" ]; then
    # A publisher snapshot intentionally has no mutable git checkout. Keep
    # those exact local bytes; never fetch main over a selected release.
    ok "using the local release snapshot (no source checkout update)"
elif [ -e "$CHECKOUT" ]; then
    die "$CHECKOUT exists and is not a git checkout — move it aside or pass --checkout DIR"
else
    # A fresh machine gets the published release, resolved above. Only if no
    # release exists at all does the clone remain, so a brand-new repo can
    # still bootstrap.
    if [ -n "$RELEASE_TAG" ]; then
        SNAP="$(mktemp -t jstack-release).tar.gz"
        run_long "downloading release $RELEASE_TAG" \
            curl -fsSL -o "$SNAP" "${REPO_URL%.git}/releases/download/$RELEASE_TAG/stack.tar.gz" \
            || die "release download failed — see $LAST_LOG"
        if [ "$DRY_RUN" = "1" ]; then
            would "unpack $RELEASE_TAG into $CHECKOUT"
        else
            mkdir -p "$CHECKOUT"
            tar xzf "$SNAP" -C "$CHECKOUT" || die "could not unpack $RELEASE_TAG into $CHECKOUT"
            rm -f "$SNAP"
            ok "installed release $RELEASE_TAG ($(sed -n 's/.*"release": *"\([^"]*\)".*/\1/p' "$CHECKOUT/host/release-identity.json"))"
        fi
    else
        run_long "cloning $REPO_URL" git clone --quiet "$REPO_URL" "$CHECKOUT" || die "clone failed — see $LAST_LOG"
        [ "$DRY_RUN" = "1" ] || ok "cloned in ${LAST_ELAPSED}s at $(git -C "$CHECKOUT" log --oneline -1)"
    fi
fi

PLUGIN="$CHECKOUT/plugins/jstack"
BIN="$PLUGIN/bin"

# ── 3. the plugin ───────────────────────────────────────────────────────────

step "Registering the plugin"

if [ -z "$CLAUDE" ]; then
    warn "no claude — skipping marketplace registration"
else
    # A directory-source marketplace means the plugin runs FROM the checkout:
    # `git pull` is the update, and there is no versioned cache to go stale.
    if "$CLAUDE" plugin marketplace list 2>/dev/null | grep -q "jStack"; then
        ok "marketplace jStack already registered"
    else
        run "$CLAUDE" plugin marketplace add "$CHECKOUT" >/dev/null 2>&1 \
            && ok "marketplace jStack → $CHECKOUT" \
            || warn "could not register the marketplace"
    fi
    if "$CLAUDE" plugin list 2>/dev/null | grep -q "jstack@jStack"; then
        ok "plugin jstack@jStack already installed"
    else
        run "$CLAUDE" plugin install "jstack@jStack" --config "agent_root=$AGENT_ROOT" >/dev/null 2>&1 \
            && ok "plugin jstack@jStack installed, agent_root=$AGENT_ROOT" \
            || warn "could not install the plugin — run: claude plugin install jstack@jStack"
    fi
fi

# ── 4. an agent workspace ───────────────────────────────────────────────────
#
# The step whose absence looks like the tools being broken: with no agent, every
# seat-aware tool succeeds against nobody. log_event writes for an agent that
# does not exist and the session-end engine reviews nothing — no errors anywhere.

step "Agent workspace"

# Asked of root.py rather than re-derived here: an agent is a directory with a
# CLAUDE.md, or with one immediate subdirectory that has one, and a second
# opinion of that rule is how an installer ends up creating a workspace beside
# three the tools can already see.
existing=""
if [ -d "$AGENT_ROOT" ]; then
    existing="$(JSTACK_AGENTS_DIR="$AGENT_ROOT" PYTHONPATH="$PLUGIN" "$PY" -c \
        'import root; print(" ".join(root.agents()))' 2>/dev/null || true)"
fi

if [ -n "$existing" ]; then
    ok "$AGENT_ROOT already holds agents"
else
    # root.py is the authority here and it found nothing, so the question above
    # was skipped by a probe that disagreed with it. Never build a path out of
    # an empty name: "$AGENT_ROOT/" would take mkdir and the heredoc with it.
    AGENT_NAME="${AGENT_NAME:-Jarvis}"
    seat="$AGENT_ROOT/$AGENT_NAME"
    if [ -f "$seat/CLAUDE.md" ]; then
        ok "$seat/CLAUDE.md already exists"
    else
        run mkdir -p "$seat"
        if [ "$DRY_RUN" = "1" ]; then
            would "write $seat/CLAUDE.md"
        else
            cat > "$seat/CLAUDE.md" <<EOF
# $AGENT_NAME

Who this agent is, and what it owns. jStack reads this file's EXISTENCE to
decide that $seat is an agent workspace — the contents are yours.

Replace everything below.

## What I own

- (the systems, repos or areas this agent is responsible for)

## How I work

- (conventions a session here should follow)
EOF
            ok "created $seat/CLAUDE.md"
        fi
    fi
fi

# ── 5. rules and bare commands ──────────────────────────────────────────────
#
# Claude Code loads both from user scope only, so a plugin cannot deliver them.
# Symlinked, not copied, and pointed at the CHECKOUT — a link into a versioned
# plugin cache works until that version is reaped, then silently points at
# nothing. `jstack-doctor` grades exactly that case as a failure.

step "Rules and bare commands"

link_stage() {
    local src="$1" dst="$2" label="$3" made=0 kept=0
    [ -d "$src" ] || { warn "no $src"; return; }
    run mkdir -p "$dst"
    for f in "$src"/*.md; do
        [ -e "$f" ] || continue
        local target="$dst/$(basename "$f")"
        if [ -e "$target" ] || [ -L "$target" ]; then kept=$((kept+1)); continue; fi
        run ln -s "$f" "$target" && made=$((made+1))
    done
    if [ "$kept" -gt 0 ]; then
        ok "$made $label linked, $kept left alone (already present)"
    else
        ok "$made $label linked into $dst"
    fi
}

link_stage "$PLUGIN/rules-stage"    "$HOME/.claude/rules"    "rules"
link_stage "$PLUGIN/commands-stage" "$HOME/.claude/commands" "bare commands"

if command -v codex >/dev/null 2>&1; then
    step "Codex plugin and shared skills"
    run python3 "$CHECKOUT/host/tools/codex_setup.py" --workspace "$AGENT_ROOT" \
        || warn "Codex setup failed — see the error above"
fi

# ── 6. bin on PATH ──────────────────────────────────────────────────────────

step "Adapters on PATH"

PROFILE="$(profile_path)"
LINE="export PATH=\"$BIN:\$PATH\"  # jstack"

if command -v log_event >/dev/null 2>&1; then
    ok "already reachable — $(command -v log_event)"
elif [ -f "$PROFILE" ] && grep -qF "$BIN" "$PROFILE" 2>/dev/null; then
    ok "$PROFILE already has it — open a new shell to pick it up"
elif [ "$DRY_RUN" = "1" ]; then
    would "append to $PROFILE: $LINE"
else
    # Appended, not asked. Every skill, hook and agent in the stack calls these
    # 18 tools by bare name, so declining left an install that was complete and
    # unusable — and said so in one yellow line above a green verdict. The line
    # is printed instead, which is what a prompt was really for.
    printf '\n%s\n' "$LINE" >> "$PROFILE"
    ok "appended to $PROFILE — open a new shell, or: source $PROFILE"
    note "$LINE"
fi
export PATH="$BIN:$PATH"

# A non-default root is only real if it outlives this shell. Everything the
# stack derives — Agents, Logs, Config, State, Credentials — reads this, so a
# root chosen at install time and never exported is a root that applies to the
# installer and to nothing afterwards.
if [ "$DECLARE_ROOT" = "1" ]; then
    ROOT_LINE="export JSTACK_ROOT=\"$JSTACK_ROOT\""
    # What the profile says today, if anything. Read before deciding, because
    # "a declaration exists" and "the declaration is the one we were asked for"
    # are different questions and only the second one matters.
    OLD_ROOT="$(declared_root 2>/dev/null || true)"

    if [ -f "$PROFILE" ] && grep -qE '^[[:space:]]*export[[:space:]]+JSTACK_ROOT=' "$PROFILE" 2>/dev/null; then
        if [ "$OLD_ROOT" = "$JSTACK_ROOT" ]; then
            ok "$PROFILE already declares this root"
        elif [ "$DRY_RUN" = "1" ]; then
            would "rewrite the root in $PROFILE: $OLD_ROOT -> $JSTACK_ROOT"
        else
            # REWRITE, not append. This branch used to say "already declares a
            # root" and change nothing, which made a wrong root permanent: the
            # only documented way to move it was the very flag that was being
            # ignored, so `--root /new/path` reported success and left the old
            # one in place. A second `export` appended below the first would be
            # no better — the last one wins, so the file would disagree with
            # itself and which root you got would depend on line order.
            #
            # The old line is kept, commented, with the date. A root is where
            # somebody's agents, logs and credentials live; leaving a trail
            # back to the previous answer costs one line.
            cp "$PROFILE" "$PROFILE.jstack-bak" 2>/dev/null || true
            STAMP="$(date +%Y-%m-%d)"
            awk -v new="$ROOT_LINE" -v stamp="$STAMP" '
                /^[[:space:]]*export[[:space:]]+JSTACK_ROOT=/ && !done {
                    print "# replaced by the jStack installer on " stamp ": " $0
                    print new
                    done = 1
                    next
                }
                /^[[:space:]]*export[[:space:]]+JSTACK_ROOT=/ {
                    print "# removed by the jStack installer on " stamp ": " $0
                    next
                }
                { print }
            ' "$PROFILE" > "$PROFILE.jstack-new" && mv "$PROFILE.jstack-new" "$PROFILE"
            ok "root moved: ${OLD_ROOT:-?} -> $JSTACK_ROOT in $PROFILE"
            note "the old line is kept commented; a copy is at $PROFILE.jstack-bak"
            note "open a new shell, or: source $PROFILE"
        fi
    elif [ "$DRY_RUN" = "1" ]; then
        would "append to $PROFILE: $ROOT_LINE"
    else
        printf '%s\n' "$ROOT_LINE" >> "$PROFILE"
        ok "declared JSTACK_ROOT=$JSTACK_ROOT in $PROFILE"
    fi
fi

# ── 7. the scheduler and what it needs ──────────────────────────────────────
#
# Booking and firing are different halves. Without a daemon the registry accepts
# a job, `list` shows it, and the hour passes in silence.
#
# dateutil is installed rather than reported. It is the difference between
# recurring jobs working and not, it is one pip install, and an installer that
# ends by handing the operator a command it could have run itself has not
# finished — that warning was the first thing a fresh install put on screen.

step "Scheduler daemon"

if "$PY" -c 'import dateutil' 2>/dev/null; then
    ok "python-dateutil present"
elif [ "$DRY_RUN" = "1" ]; then
    would "$PY -m pip install --user python-dateutil"
else
    # Two attempts, because the interpreter this resolves to on a Mac with
    # Homebrew is one pip refuses to install into: PEP 668 marks it
    # externally managed and the plain --user install exits 1 with a wall of
    # text about virtualenvs. --break-system-packages is the documented
    # override and Homebrew's own message recommends pairing it with --user,
    # which keeps the package in the user site and out of the managed tree.
    #
    # The verdict is the import, not pip's exit status. A wheel can land and
    # still not be importable by the interpreter the scheduler will run under,
    # and that is the only question worth reporting.
    run_long "installing python-dateutil" "$PY" -m pip install --quiet --user python-dateutil \
        || run_long "installing python-dateutil (PEP 668 override)" \
               "$PY" -m pip install --quiet --user --break-system-packages python-dateutil
    if "$PY" -c 'import dateutil' 2>/dev/null; then
        ok "python-dateutil installed — recurring jobs can book"
    else
        warn "could not install python-dateutil; recurring jobs will not book — see $LAST_LOG"
    fi
fi

# Not a question. `jstack-doctor` runs at the end of this script and grades an
# absent scheduler as a warning — so asking here hands the reader a warning they
# chose and cannot act on. Installed by default; --no-scheduler declines it.
if [ "$WANT_SCHEDULER" = "0" ]; then
    note "skipped by --no-scheduler — run \`jstack-scheduler install\` any time"
elif [ "$DRY_RUN" = "1" ]; then
    would "$BIN/jstack-scheduler install"
else
    "$PY" "$BIN/jstack-scheduler" install && ok "daemon installed" || warn "daemon install reported a problem"
fi

# ── 8. the host and its menu bar icon ───────────────────────────────────────
#
# Steps 1-7 leave a stack you drive from a terminal on this machine. The host
# is what makes the machine reachable at all — from a phone, from another Mac,
# across the tunnel — and the menu bar icon is the only surface that ever says
# whether it is running.
#
# Chained here because the first line of this file promises a working stack in
# one command, and until now it stopped one installer short of one: a stranger
# who ran it got no host and no icon, and nothing on screen said a second
# command existed. That gap was found by installing onto a clean VM and asking
# where the icon was — the answer was that this script never put one there.
#
# The host binds 0.0.0.0 by design (a host only 127.0.0.1 can see is not a
# host) and every route but /api/health requires its bearer token. --no-host
# skips it; `host/install.sh --uninstall` removes it later without touching
# the state or the token.

step "Host and menu bar"

HOST_INSTALLER="$CHECKOUT/host/install.sh"
if [ -n "$RELEASE_TAG" ] && [ -f "$CHECKOUT/host/release-identity.json" ] && [ "$WANT_HOST" != "0" ] \
        && [ "$(uname -s)" = "Darwin" ] && [ ! -d "/Applications/jStack Hub.app" ]; then
    # A release install gets the SIGNED, notarized Hub the release published —
    # never a source-built unsigned menubar with python launch agents. The
    # sealed app carries its own installer; codesign/spctl verification happens
    # inside install_signed before anything is adopted.
    HUB_ZIP="$(mktemp -t jstack-hub).zip"
    run_long "downloading the signed jStack Hub" \
        curl -fsSL -o "$HUB_ZIP" "${REPO_URL%.git}/releases/download/$RELEASE_TAG/menubar-notarized.zip" \
        || die "hub download failed — see $LAST_LOG"
    if [ "$DRY_RUN" = "1" ]; then
        would "install the signed Hub into /Applications and run its sealed installer"
    else
        run_long "installing the signed Hub" ditto -x -k "$HUB_ZIP" /Applications \
            || die "could not unpack the Hub into /Applications — see $LAST_LOG"
        rm -f "$HUB_ZIP"
        [ -d "/Applications/jStack Hub.app" ] || die "the Hub zip did not contain 'jStack Hub.app'"
        if run_long "running the Hub's sealed installer" \
                "/Applications/jStack Hub.app/Contents/MacOS/JStackRuntime" install \
                --app "/Applications/jStack Hub.app" --state-dir "$HOME/.local/state/jremote"; then
            ok "signed Hub installed"
            HOST_INSTALLED=1
            SIGNED_HUB=1
            # Pairing and every later step reach the host through
            # ~/.local/bin/jstack-host. The sealed Hub's CLI picks its role
            # from its own executable name, so this is a wrapper, not a link.
            mkdir -p "$HOME/.local/bin"
            printf '#!/bin/sh\nexec "/Applications/jStack Hub.app/Contents/MacOS/JStackCLI" "$@"\n' \
                > "$HOME/.local/bin/jstack-host"
            chmod +x "$HOME/.local/bin/jstack-host"
        else
            # A machine with a Hub app but no provisioned host service is the
            # green-looking broken install this script exists to prevent.
            die "sealed Hub install failed — $(tail -3 "$LAST_LOG" 2>/dev/null | tr '\n' ' ')"
        fi
    fi
elif [ ! -f "$HOST_INSTALLER" ]; then
    note "no host installer in this checkout — skipped"
elif [ "$WANT_HOST" = "0" ]; then
    note "skipped by --no-host — run $HOST_INSTALLER any time"
else
    # Not a question. The host is the machine's reachability and the icon is
    # the only surface that ever says whether it is running — asking makes
    # both read as extras, and a "no" here produces an install that looks
    # complete and answers nothing. --no-host is the way out, stated in
    # --help, rather than a prompt that has one sensible answer.
    # --no-pair: the app is installed in the NEXT step, so pairing here would
    # be introducing the host to something that is not on the disk yet. Step 10
    # does it, once both halves exist.
    host_args=(--yes --no-pair)
    [ "$WANT_MENUBAR" = "0" ] && host_args+=(--no-menubar)
    if [ "$DRY_RUN" = "1" ]; then
        would "$HOST_INSTALLER ${host_args[*]}"
    elif bash "$HOST_INSTALLER" "${host_args[@]}"; then
        ok "host installed"
        HOST_INSTALLED=1
    else
        warn "host install reported a problem — re-run $HOST_INSTALLER to see it"
    fi
fi

# ── 9. the Mac app ──────────────────────────────────────────────────────────
#
# The host makes the machine reachable; the app is what reaches it. Offered
# here rather than left to a second document, for the same reason the host is:
# a machine set up to be reached, with nothing on it that can reach, is half
# an install that reads as a finished one.
#
# It downloads a signed release rather than building from this checkout, which
# is why it is the one step that can be declined without leaving a hole:
# --no-app. app/install.sh verifies the hash, the signature, notarization and
# the signing team before anything lands in /Applications.

step "The Mac app"

APP_INSTALLER="$CHECKOUT/app/install.sh"
if [ "$(uname -s)" != "Darwin" ]; then
    note "macOS only — skipped"
elif [ ! -f "$APP_INSTALLER" ]; then
    note "no app installer in this checkout — skipped"
elif [ "$WANT_APP" = "0" ]; then
    note "skipped by --no-app — run $APP_INSTALLER any time"
elif [ "$DRY_RUN" = "1" ]; then
    would "$APP_INSTALLER"
else
    app_args=()
    [ "$ASSUME_YES" = "1" ] && app_args+=(--yes)
    # A release snapshot has no git remote for the app installer to derive its
    # repo from — hand it the one this install already came from.
    app_args+=(--repo "$(printf '%s' "$REPO_URL" | sed -E 's#^git@[^:]+:##; s#^https?://[^/]+/##; s#\.git$##')")
    # macOS ships bash 3.2, where "${app_args[@]}" on an empty array trips
    # `set -u`; the ${arr[@]+...} form expands to nothing instead of dying.
    run_long "downloading and verifying the app" bash "$APP_INSTALLER" ${app_args[@]+"${app_args[@]}"}
    case $? in
        0) ok "app installed in ${LAST_ELAPSED}s"; APP_INSTALLED=1 ;;
        # 3 is "no release published yet". This used to be a note, on the
        # reasoning that a reader cannot act on a fact about the repository and
        # a warn would end a clean install on a line that looks like something
        # to fix. Both halves of that were wrong. The app IS something to fix —
        # it is the only thing on the machine that can reach the host this
        # install just built — and the line the reader could not act on was the
        # one that never mentioned the consequence: pairing is gated on the app
        # being here, so a note here silently took step 10 out too. An install
        # that ends with a host, no client and no code, under a green summary,
        # is the failure this whole script exists to stop.
        3) warn "no signed release published yet — no app, and nothing paired.
     Install it later with: $APP_INSTALLER" ;;
        *) warn "app install reported a problem — re-run $APP_INSTALLER to see it: $LAST_LOG" ;;
    esac
fi

# ── 10. introducing the two halves ──────────────────────────────────────────
#
# Both halves are on the disk and they have never heard of each other. Left
# there, the next thing this install asks of a person is the hardest thing in
# it: open the app, find Add a Mac, type an address and a 43-character token
# that has to be copied off a terminal — for a machine sitting under the app,
# on an install that just did everything else by itself.
#
# So the hub introduces itself. It mints a one-time enrolment code and hands it
# to the app over the app's own URL scheme; the app spends it for its own device
# token and writes the machine down. That is also what opens the app, which is
# the other thing this step is for. `jstack-host pair --open` is the whole of
# it, and `host/jstack_host/cli.py` is where the mechanism is written down.
#
# The hub is the only party that CAN do this — it is the only one that knows
# the address before the machine has a resolvable name, and the only one
# authorized to mint a credential. Which is the point: adding a machine is
# something the hub does, never something an app talks its way into.

# A host with no app still mints a code. The old gate required BOTH halves, so
# a machine that got the host and missed the app skipped this step in silence —
# and the code is exactly what that machine needs, because the app is going to
# arrive later by hand and will have nothing to enrol with when it does.
if [ "$HOST_INSTALLED" = "1" ] && [ "$APP_INSTALLED" = "0" ] && [ "$DRY_RUN" = "0" ] \
   && [ -x "$HOME/.local/bin/jstack-host" ]; then
    step "Pairing"
    note "no app on this Mac yet — here is a code for whichever device gets one first"
    "$HOME/.local/bin/jstack-host" pair "$(scutil --get ComputerName 2>/dev/null || hostname -s)" \
        || warn "could not mint a pairing code — run \`jstack-host pair\` once the app is installed"
fi

if [ "$HOST_INSTALLED" = "1" ] && [ "$APP_INSTALLED" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    step "Pairing the app to this Mac"
    HOSTBIN="$HOME/.local/bin/jstack-host"
    device_name="$(scutil --get ComputerName 2>/dev/null || hostname -s)"
    # The output is kept, not discarded: `pair --open` exits non-zero when the
    # app did not spend the code, and what it prints in that case IS the
    # recovery — the code itself, still good. Swallowing it left a warn telling
    # someone to re-run a command whose whole output we had just thrown away.
    pair_log="$(mktemp -t jstack-pair)"
    if [ ! -x "$HOSTBIN" ]; then
        warn "jstack-host is not on this Mac — pair by hand with \`jstack-host pair\`"
    elif "$HOSTBIN" pair "$device_name" --open >"$pair_log" 2>&1; then
        ok "the app is open and connected to this Mac — nothing to type"
        # And the app opens on something rather than on nothing. The session
        # comes up already checking the machine it was just installed on, so
        # the first thing in the window is a report someone can ask questions
        # about — not an empty prompt on a stack they have not learned yet.
        #
        # Never fatal, and deliberately after pairing: a session opened before
        # the app has a credential is a board row nobody can see.
        if [ -n "$AGENT_NAME" ] && "$HOSTBIN" welcome >/dev/null 2>&1; then
            ok "$AGENT_NAME is in the app going over the install with you"
        fi
    else
        warn "the app did not pair itself — the code below still works, or run \`jstack-host pair --open\` again"
        sed 's/^/  /' "$pair_log"
    fi
    rm -f "$pair_log"
fi

if [ "$WANT_HOST" = "0" ] && [ "$APP_INSTALLED" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    step "Connect jRemote to a Hub"
    note "client installed; pairing required"
    note "On the target Hub, choose Pair a Device."
    note "In jRemote, choose Add a Mac and enter the Hub address and pairing code."
    open -a jRemote || warn "open jRemote from Applications to finish pairing"
fi

# ── 11. the verdict ─────────────────────────────────────────────────────────

if [ "${SIGNED_HUB:-0}" = "1" ]; then
    step "Managed updater"
    ok "the sealed Hub runs its own updater service — nothing to bootstrap"
elif [ "$HOST_INSTALLED" = "1" ] && [ "$WANT_MENUBAR" = "1" ]; then
    step "Managed updater"
    if [ "$DRY_RUN" = "1" ]; then
        would "bootstrap the restart-independent updater with the shipped release trust key"
    elif "$CHECKOUT/host/.venv/bin/python3" -m jstack_host.install_updater; then
        ok "managed updater installed; the menu bar owns stack and app updates"
    else
        die "updater bootstrap failed — the existing host and app remain installed"
    fi
fi

step "Verifying"

if [ "$DRY_RUN" = "1" ]; then
    would "$BIN/jstack-doctor"
    printf '\n%sdry run — nothing was changed%s\n' "$B" "$Z"
    exit 0
fi

echo
doctor_out="$(mktemp -t jstack-doctor)"
"$PY" "$BIN/jstack-doctor" | tee "$doctor_out"
rc=${PIPESTATUS[0]}

# The failing checks, by name. The last line used to read "something above is
# broken", which hands the reader a scroll-and-hunt in the one place they most
# need a name — and the doctor already printed every name, so the installer was
# being vaguer than the tool it had just run.
broken="$(awk '$1 == "FAIL" { print $2 }' "$doctor_out" | tr '\n' ' ')"
broken="${broken% }"
rm -f "$doctor_out"

echo
case "$rc" in
    0) printf '%sjStack is installed and every check passed.%s\n' "$GRN$B" "$Z" ;;
    1) printf '%sjStack is installed and working.%s The warnings above are capabilities\nthat stay absent until you add them — normal on a fresh machine.\n' "$GRN$B" "$Z" ;;
    *) printf '%sInstalled, but %s is broken.%s Its FAIL line above names the fix;\nre-run `jstack-doctor` after it.\n' "$YEL$B" "${broken:-a check above}" "$Z" ;;
esac

cat <<EOF

Next: open a new shell so PATH takes effect, then start a session inside an
agent workspace —

    cd $AGENT_ROOT/${AGENT_NAME:-<agent>}
    claude

and run /jstack:work on any topic. Re-run this script any time to update;
it changes only what has drifted.
EOF

exit 0
