#!/usr/bin/env bash
# jRemote host installer — a Mac you can reach from your phone, in one command.
#
#   ./install.sh --port 9090 --yes         # from a checkout, unattended
#   ./install.sh --dry-run                 # print the plan, touch nothing
#   ./install.sh --update                  # pull, reinstall, restart
#   ./install.sh --uninstall               # take it back off
#   ./install.sh --purge                   # and delete everything it wrote
#
# What it does, and nothing else: clone (or update) the jStack checkout, build a
# private virtualenv beside the package, install the host into it, and register
# a **user** LaunchAgent so the host survives a logout and a reboot.
#
# It never asks for a password. No `sudo`, no root, nothing written outside
# your own home directory — an install that needs an admin password is one you
# have to trust rather than read, and this is a program that watches your
# terminal sessions. Everything it runs is in this repository.
#
# What it will not do: run as root, install into a Python it does not own, or
# overwrite a host that is already answering unless you say --force. Every step
# is idempotent — running it twice is an upgrade, which is what makes it safe
# to use as the updater.

set -uo pipefail

# One installer. This script is a step of the top-level install.sh, not a door.
if [ -z "${JSTACK_INSTALLER:-}" ]; then
    echo "this is an internal step of the jStack installer — run instead:" >&2
    echo "  curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash" >&2
    exit 2
fi

# The repo to clone when this is run outside a checkout. Not spelled here:
# the script normally runs *from* the checkout, and a hardcoded account name
# in a public installer is a name that outlives whoever owns the repo.
REPO_URL="${JSTACK_REPO_URL:-}"
CHECKOUT="${JSTACK_CHECKOUT:-$HOME/jStack}"
BIN_DIR="${JSTACK_BIN_DIR:-$HOME/.local/bin}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

ASSUME_YES=0
DRY_RUN=0
DO_UPDATE=0
DO_UNINSTALL=0
DO_PURGE=0
FORCE=0
WANT_MENUBAR=1
WANT_PAIR=1
PORT=9090
BIND="0.0.0.0"
STATE_DIR=""

usage() {
    cat <<'EOF'
usage: install.sh [options]

  --yes, -y          don't ask; accept every default
  --dry-run          print what would happen and change nothing
  --update           git pull the checkout, reinstall, restart the host
  --uninstall        remove the LaunchAgent (your state and token stay)
  --purge            uninstall AND delete state, token and credentials —
                     permanent, and every paired device must pair again
  --port N           port to serve on (default 9090)
  --bind ADDR        bind address (default 0.0.0.0 — see below)
  --state-dir DIR    where this host keeps its state
                     (default ~/.local/state/jremote)
  --force            install even if something already answers on the port
  --checkout DIR     where to clone jStack (default ~/jStack)
  --no-menubar       skip the menu bar app (the host is a terminal program
                     either way; the app is only its indicator)
  --no-pair          don't introduce the app to this host at the end
                     (the top-level installer passes this, because it puts
                     the app on the disk AFTER this script runs and does the
                     introduction itself once both halves exist)
  --help, -h         this

The default bind is 0.0.0.0 on purpose: a host is reached over a tunnel or
across your LAN, and one bound to 127.0.0.1 is a host only this Mac can see.
Every route requires the bearer token; /api/health is the one exception and
says nothing about what is on the machine.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)     ASSUME_YES=1 ;;
        --dry-run)    DRY_RUN=1 ;;
        --update)     DO_UPDATE=1 ;;
        --uninstall)  DO_UNINSTALL=1 ;;
        --purge)      DO_UNINSTALL=1; DO_PURGE=1 ;;
        --force)      FORCE=1 ;;
        --no-menubar) WANT_MENUBAR=0 ;;
        --no-pair)    WANT_PAIR=0 ;;
        --port)       PORT="${2:-}"; shift ;;
        --bind)       BIND="${2:-}"; shift ;;
        --state-dir)  STATE_DIR="${2:-}"; shift ;;
        --checkout)   CHECKOUT="${2:-}"; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# ── output ──────────────────────────────────────────────────────────────────

if [ -t 1 ]; then B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; Z=$'\033[0m'
else B=""; DIM=""; RED=""; GRN=""; YEL=""; Z=""; fi

step()  { printf '\n%s==>%s %s\n' "$B" "$Z" "$1"; }
ok()    { printf '  %sok%s   %s\n' "$GRN" "$Z" "$1"; }
warn()  { printf '  %swarn%s %s\n' "$YEL" "$Z" "$1"; }
die()   { printf '  %sfail%s %s\n' "$RED" "$Z" "$1" >&2; exit 1; }
note()  { printf '  %s%s%s\n' "$DIM" "$1" "$Z"; }

# The last meaningful line of the menu bar build log, fit to sit inside a
# `note`. Blank lines dropped so a trailing newline does not become the
# "reason", and the colour escapes stripped: that text was written for a
# terminal of its own, and pasted into a line that adds its own dim/reset the
# raw escapes garble the rest of the output.
_menubar_reason() {
    sed $'s/\033\\[[0-9;]*m//g' "$MENUBAR_LOG" 2>/dev/null \
        | grep -v '^[[:space:]]*$' | tail -n 1
}
would() { printf '  %swould%s %s\n' "$DIM" "$Z" "$1"; }

run() {
    if [ "$DRY_RUN" = "1" ]; then would "$*"; return 0; fi
    "$@"
}

# `ok` for a fact this script observed; `did` for the result of an action it
# took. In a dry run no action was taken, so `did` says nothing — an "ok
# installed" printed by --dry-run is a check reporting state it cannot observe,
# which is worse than printing nothing at all.
did() { [ "$DRY_RUN" = "1" ] || ok "$1"; }

# ── 0. preflight ────────────────────────────────────────────────────────────

step "Checking prerequisites"

[ "$(id -u)" != "0" ] || die "don't run this as root — the host installs per-user, and a root-owned host is one only root can remove"
[ "$(uname -s)" = "Darwin" ] || die "this installs a macOS LaunchAgent; $(uname -s) is not supported"
ok "macOS, running as $(id -un)"

# The interpreter that builds the venv is the one the LaunchAgent will run
# forever, so it is picked deliberately rather than taken from whatever `python3`
# resolves to in this shell. Homebrew first: the system python3 at
# /usr/bin/python3 is Apple's, gets replaced by OS updates, and has taken a
# venv's packages with it before.
PY=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null; then
        PY="$cand"; break
    fi
done
[ -n "$PY" ] || die "need Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ — install it with \`brew install python3\` and re-run"
ok "python $("$PY" -c 'import platform;print(platform.python_version())') at $PY"

command -v git >/dev/null 2>&1 || die "git is required — install the Xcode command line tools with \`xcode-select --install\`"
ok "git $(git --version | awk '{print $3}')"

# tmux, installed rather than reported.
#
# Every chat this host serves runs inside tmux, so its own setup check grades
# an absent one FAIL and prints "the host cannot serve chats until the failures
# above are fixed". macOS does not ship it. The installer knew the dependency,
# knew the fix, ran neither, and ended a clean install on a red line telling
# the reader to type the one command it could have typed itself.
#
# brew is looked for at its two install prefixes, not just on PATH. This
# script is chained from the top-level installer under `curl | bash`, whose
# PATH does not carry /opt/homebrew/bin — so `command -v brew` said no on a
# machine that had Homebrew, python and everything else, and the run ended on
# "there is no Homebrew to install it with" beside a python found at
# /opt/homebrew/bin/python3 three lines above.
BREW=""
for cand in /opt/homebrew/bin/brew /usr/local/bin/brew "$(command -v brew 2>/dev/null)"; do
    [ -n "$cand" ] && [ -x "$cand" ] && { BREW="$cand"; break; }
done

if command -v tmux >/dev/null 2>&1; then
    ok "tmux $(tmux -V | awk '{print $2}')"
elif [ -n "$BREW" ]; then
    printf "  installing tmux…\n"
    if "$BREW" install tmux >/dev/null 2>&1; then
        # brew's bin dir is not necessarily on this shell's PATH either, so the
        # confirmation asks brew where it put it rather than asking PATH.
        eval "$("$BREW" shellenv 2>/dev/null)" || true
    fi
    if command -v tmux >/dev/null 2>&1; then
        ok "tmux $(tmux -V | awk '{print $2}') installed"
    else
        warn "could not install tmux — run \`brew install tmux\`; chats cannot start without it"
    fi
else
    warn "tmux is missing and there is no Homebrew to install it with — chats cannot start until \`tmux\` is on PATH"
fi

# ── 1. the checkout ─────────────────────────────────────────────────────────
#
# Where the host runs from, permanently. The LaunchAgent points at this
# directory rather than at a copy: nothing is unpacked into a cache, so
# `git pull` here IS the update, and there is exactly one tree to read if you
# want to know what is running on your machine.

step "Getting the source"

# Running from inside a checkout already? Then that is the checkout — cloning a
# second one and installing *that* is how someone ends up editing a tree the
# host never reads.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/pyproject.toml" ] && [ -d "$SELF_DIR/jstack_host" ]; then
    HOST_DIR="$SELF_DIR"
    CHECKOUT="$(cd "$SELF_DIR/.." && pwd)"
    ok "using this checkout — $CHECKOUT"
    if [ "$DO_UPDATE" = "1" ] && [ -d "$CHECKOUT/.git" ]; then
        run git -C "$CHECKOUT" pull --ff-only || warn "git pull did not fast-forward; installing what is here"
    fi
elif [ -d "$CHECKOUT/.git" ]; then
    HOST_DIR="$CHECKOUT/host"
    ok "checkout already at $CHECKOUT"
    run git -C "$CHECKOUT" pull --ff-only || warn "git pull did not fast-forward; installing what is here"
else
    [ -n "$REPO_URL" ] || die "no checkout at $CHECKOUT — clone the repo and run host/install.sh from it, or set JSTACK_REPO_URL"
    note "cloning $REPO_URL"
    HOST_DIR="$CHECKOUT/host"
    run git clone --depth 1 "$REPO_URL" "$CHECKOUT" || die "clone failed"
    did "cloned to $CHECKOUT"
fi

if [ "$DRY_RUN" != "1" ]; then
    [ -f "$HOST_DIR/pyproject.toml" ] || die "no host package at $HOST_DIR"
fi

# ── 2. the virtualenv ───────────────────────────────────────────────────────
#
# Its own venv, beside the package. Never a `pip install --user` and never
# `--break-system-packages`: this host has real dependencies (FastAPI, uvicorn)
# and installing them into a Python shared with the rest of your machine is how
# an unrelated `pip install` takes your host down months later.

step "Building the environment"

VENV="$HOST_DIR/.venv"

# `-x` proves a file is there and executable. It does not prove it RUNS, and a
# venv is a set of symlinks into the interpreter that built it: upgrade
# Homebrew's python and every venv on the machine still passes `-x` while its
# python3 aborts with a dyld error naming a Cellar path that no longer exists.
# So the interpreter is asked to execute, which is the thing actually being
# claimed — a reused venv that cannot run is rebuilt rather than reported `ok`
# and handed to the next pip, which is where it used to fail.
# The subshell is load-bearing: a dyld failure kills the interpreter with
# SIGABRT, and bash reports a signalled *direct* child itself ("Abort trap: 6")
# on its own stderr, which no redirection on the command can reach. Run inside
# `( )` the notice belongs to the subshell and goes with its stderr — otherwise
# every rebuild prints a crash trace above the line explaining the rebuild.
venv_runs() { [ -x "$1/bin/python3" ] && ( "$1/bin/python3" -c pass ) >/dev/null 2>&1; }

if venv_runs "$VENV"; then
    ok "virtualenv already at $VENV"
elif [ -e "$VENV" ]; then
    # Nothing is recoverable from a venv whose base interpreter is gone — it
    # holds symlinks and compiled artifacts of an interpreter that no longer
    # exists — and it is a directory this script created. Scoped to that one
    # path, never to $HOST_DIR.
    warn "the virtualenv at $VENV cannot run (its base python moved) — rebuilding"
    run rm -rf "$VENV" || die "could not remove the broken virtualenv at $VENV"
    run "$PY" -m venv "$VENV" || die "could not create a virtualenv at $VENV"
    did "virtualenv rebuilt at $VENV"
else
    run "$PY" -m venv "$VENV" || die "could not create a virtualenv at $VENV"
    did "virtualenv at $VENV"
fi

run "$VENV/bin/python3" -m pip install --quiet --upgrade pip >/dev/null 2>&1
if ! run "$VENV/bin/python3" -m pip install --quiet -e "$HOST_DIR"; then
    die "pip install failed — the output above says why (no network is the usual one)"
fi
did "jstack-host installed into the virtualenv"

HOSTBIN="$VENV/bin/jstack-host"
if [ "$DRY_RUN" != "1" ]; then
    [ -x "$HOSTBIN" ] || die "pip finished but $HOSTBIN is missing"
fi

# ── 3. the command on PATH ──────────────────────────────────────────────────
#
# A symlink rather than a shell alias or a PATH edit to the venv: the venv is
# an implementation detail that may be rebuilt, and `jstack-host` is the name
# the documentation, the errors and the app's own instructions all use.

step "Putting jstack-host on your PATH"

run mkdir -p "$BIN_DIR"
run ln -sf "$HOSTBIN" "$BIN_DIR/jstack-host"
did "$BIN_DIR/jstack-host"

case ":$PATH:" in
    *":$BIN_DIR:"*) ok "$BIN_DIR is already on your PATH" ;;
    *) warn "$BIN_DIR is not on your PATH — add this to your shell profile:"
       note "export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# ── 4. uninstall, if that is what was asked ─────────────────────────────────

if [ "$DO_UNINSTALL" = "1" ]; then
    step "Removing the host"
    # The indicator first: an icon left in the menu bar after the thing it
    # indicates is gone is the one state worse than no icon at all.
    [ -x "$HOST_DIR/menubar/install.sh" ] && \
        run "$HOST_DIR/menubar/install.sh" --uninstall >/dev/null 2>&1
    # The paths come from the host, never from literals here. It is the only
    # thing that knows which state dir it was actually installed with, and a
    # purge that deletes a guessed path is a purge that leaves the real one and
    # takes something else. Read BEFORE the uninstall, while it can still answer.
    if [ "$DO_PURGE" = "1" ] && [ -x "$HOSTBIN" ]; then
        PURGE_PATHS="$("$HOSTBIN" where 2>/dev/null \
            | awk '$1 == "state" || $1 == "credentials" { print $2 }')"
    fi

    run "$HOSTBIN" uninstall

    if [ "$DO_PURGE" != "1" ]; then
        printf '\n%sThe host is off this Mac.%s Your state and token were left in place,\n' "$B" "$Z"
        printf 'so reinstalling brings the same instance back rather than a new one.\n'
        printf 'To remove those too: %s--purge%s\n' "$B" "$Z"
        exit 0
    fi

    # ── purge ───────────────────────────────────────────────────────────────
    #
    # This is the irreversible one, and it is the only thing in this installer
    # that destroys anything. State holds the token every paired device is
    # carrying, the device list, and the board — deleting it does not just
    # remove the host, it makes every phone and laptop that trusted this Mac a
    # stranger. So it is named out loud and confirmed, never a silent extra.
    step "Removing state"
    if [ -z "${PURGE_PATHS:-}" ]; then
        warn "could not ask the host where its state is — nothing removed"
        note "find it with \`jstack-host where\` and remove those paths by hand"
        exit 1
    fi

    printf '\nThis deletes, permanently:\n\n'
    printf '%s\n' "$PURGE_PATHS" | sed 's/^/    /'
    printf '\nEvery paired device has to be paired again afterwards.\n'

    if [ "$ASSUME_YES" != "1" ] && [ "$DRY_RUN" != "1" ]; then
        printf '\nType the word purge to confirm: '
        read -r reply </dev/tty || reply=""
        [ "$reply" = "purge" ] || { note "not confirmed — nothing was removed"; exit 1; }
    fi

    printf '%s\n' "$PURGE_PATHS" | while IFS= read -r p; do
        [ -n "$p" ] || continue
        # Never let an empty or root-ish value through to rm -rf. A `where`
        # that answered blank would otherwise expand to `rm -rf ` or `rm -rf /`.
        case "$p" in
            ""|"/"|"$HOME"|"$HOME/") warn "refusing to remove $p"; continue ;;
        esac
        run rm -rf "$p" && ok "removed $p"
    done

    # The convenience symlink last: while it exists, `jstack-host` still runs
    # and reports paths that are now gone, which reads as a broken install
    # rather than a removed one.
    [ -L "$BIN_DIR/jstack-host" ] && run rm -f "$BIN_DIR/jstack-host" \
        && ok "removed $BIN_DIR/jstack-host"

    printf '\n%sThe host and everything it wrote are gone.%s\n' "$B" "$Z"
    printf 'The checkout at %s is untouched — remove it yourself if you want it gone.\n' "$CHECKOUT"
    exit 0
fi

# ── 5. the LaunchAgent ──────────────────────────────────────────────────────

step "Installing the host"

ARGS=(install --port "$PORT" --bind "$BIND")
[ -n "$STATE_DIR" ] && ARGS+=(--state-dir "$STATE_DIR")
[ "$FORCE" = "1" ] && ARGS+=(--force)

if [ "$DRY_RUN" = "1" ]; then
    would "$HOSTBIN ${ARGS[*]}"
    [ "$WANT_MENUBAR" = "1" ] && would "$HOST_DIR/menubar/install.sh"
    printf '\n%sDry run — nothing was changed.%s\n' "$B" "$Z"
    exit 0
fi

if ! "$HOSTBIN" "${ARGS[@]}"; then
    printf '\n%sThe host did not come up.%s The message above says why. Once it is\n' "$RED$B" "$Z"
    printf 'fixed, re-run this script — it is an upgrade, not a second install.\n'
    exit 1
fi

# ── 6. the menu bar ─────────────────────────────────────────────────────────
#
# The host's own indicator. Never fatal: the host is a terminal program and is
# already up and serving by this point — failing the whole install because a
# Mac has no Swift compiler would be refusing the thing that works over the
# thing that is decoration.

if [ "$WANT_MENUBAR" = "1" ] && [ -x "$HOST_DIR/menubar/install.sh" ]; then
    step "Menu bar"

    # Read BEFORE the attempt: whether an icon was already there is what decides
    # what a failed build means, and after the attempt it is too late to ask.
    MENUBAR_APP="${JSTACK_APPS_DIR:-$HOME/Library/Application Support/jStack}/JStack Host.app"
    HAD_MENUBAR=0
    [ -d "$MENUBAR_APP" ] && HAD_MENUBAR=1

    # Kept, not discarded. The menu bar installer's own failure message is
    # "the build failed — the compiler output above says why", and sending that
    # output to /dev/null made the sentence a lie: there was nothing above, so
    # the one machine-specific reason was destroyed at the moment it was needed.
    MENUBAR_LOG="$(mktemp -t jstack-menubar)"

    if "$HOST_DIR/menubar/install.sh" >"$MENUBAR_LOG" 2>&1; then
        ok "icon installed — it shows whether this host is up and what is running"
        rm -f "$MENUBAR_LOG"
    elif [ "$HAD_MENUBAR" = "1" ]; then
        # The dangerous case, and the one that used to print as the harmless one.
        #
        # An icon is already in the menu bar, running the binary the last
        # SUCCESSFUL build left there. This run upgraded the host underneath it
        # and could not rebuild it, so what is on screen is now older than the
        # host it reports on — and it will keep showing whatever it showed
        # before, "No Access" included, through any number of reinstalls. The
        # old text ("add the icon later") was false twice: there is an icon, and
        # later is not optional. A stale indicator is worse than an absent one,
        # because an absent one cannot be believed.
        warn "the menu bar app could NOT be rebuilt — the icon already on your menu"
        warn "bar is now STALE: older than the host it reports on"
        note "what it shows may be wrong, \"No Access\" included. It will not fix"
        note "itself, and re-running this installer will not fix it either — the"
        note "icon can only change when it rebuilds."
        note "reason: $(_menubar_reason)"
        note "full output: $MENUBAR_LOG"
        note "fix the toolchain, then rebuild the icon with:"
        note "  $HOST_DIR/menubar/install.sh"
    else
        warn "the menu bar app did not build (a Swift compiler is needed)"
        note "reason: $(_menubar_reason)"
        note "full output: $MENUBAR_LOG"
        note "the host is up regardless; add the icon later with:"
        note "  $HOST_DIR/menubar/install.sh"
    fi
fi

# ── 7. pairing ──────────────────────────────────────────────────────────────
#
# The last mile, and the one people used to get stuck on: the host is up and
# the app still has to be told about it. A code rather than the raw token — it
# expires, it names the device before the device connects, and revoking it
# later does not re-key everything else.
#
# `--open` because on THIS machine there is nobody to read a code to. The app
# is right here; the code goes to it over `jremote://pair` and it enrols
# itself. A Mac with no app installed falls back to printing the code, which
# is what this step always did — and so does a Mac whose app took the link and
# then did nothing with it, because `--open` now waits to watch the code be
# spent rather than trusting that `open` returning 0 meant anything.

if [ "$WANT_PAIR" = "1" ]; then
    step "Pairing"
    DEVICE_NAME="${JSTACK_DEVICE_NAME:-$(scutil --get ComputerName 2>/dev/null || hostname -s)}"
    if ! "$HOSTBIN" pair "$DEVICE_NAME" --open; then
        warn "the app did not pair itself — the code above still works"
    fi
fi

cat <<EOF

${B}Your Mac is a jRemote host.${Z}

  jstack-host status      is it up
  jstack-host doctor      what is missing, and how to fix each thing
  jstack-host pair NAME   a code for another device
  jstack-host pair --open pair the app on this Mac again
  jstack-host where       every path this host resolves

  $0 --update             take new code
  $0 --uninstall          take it back off

The menu bar icon is the host's own UI — status, live sessions, and start /
stop / restart. It runs under its own agent, so it stays whatever else you
quit. ${HOST_DIR}/menubar/install.sh --uninstall takes just the icon off.
EOF
