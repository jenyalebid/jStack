#!/usr/bin/env bash
# The host's menu bar app — build it here, install it, keep it running.
#
#   ./install.sh                  build and install
#   ./install.sh --dry-run        print the plan, touch nothing
#   ./install.sh --uninstall      take the icon off and remove the app
#
# There is nothing to download and nothing to trust. The app is one Swift file
# next to this script, compiled on this Mac by this Mac's own toolchain — so
# there is no signature to check, no notarization to verify, and no publisher
# to believe. What ends up in your menu bar was built from the source you can
# read, by you.
#
# It installs as a **user** LaunchAgent, like the host itself: no sudo, nothing
# written outside your home directory. The agent is what makes the icon mean
# anything — a status item belongs to the process that made it, so an indicator
# that only runs while some other app is open is an indicator that lies the
# moment you quit that app.
#
# Requires a Swift compiler. That is the Xcode command line tools, which is the
# same `xcode-select --install` the host installer already asks for when git is
# missing. No Xcode, no project file, no package manifest.

set -uo pipefail

APP_NAME="JStack Host"
BUNDLE_ID="com.jremote.menubar"
LABEL="com.jremote.menubar"
SOURCE="JStackHostBar.swift"

# Not ~/Applications. The bundle is `LSUIElement: 1` — it never appears in the
# Dock, the app switcher or Spotlight, and there is nothing to launch: the
# LaunchAgent runs it and the only thing it does is put an icon on the menu
# bar. A background agent in the folder you browse your apps in is litter, and
# it reads as a whole app shipped for one menu. Application Support is where a
# support binary belongs. --apps-dir still overrides for anyone who wants it
# somewhere else.
APPS_DIR="${JSTACK_APPS_DIR:-$HOME/Library/Application Support/jStack}"
BIN_DIR="${JSTACK_BIN_DIR:-$HOME/.local/bin}"

DRY_RUN=0
DO_UNINSTALL=0

usage() {
    cat <<'EOF'
usage: menubar/install.sh [options]

  --dry-run          print what would happen and change nothing
  --uninstall        unload the agent and remove the app
  --apps-dir DIR     where the bundle goes (default ~/Library/Application Support/jStack)
  --state-dir DIR    the host's state dir, if it is not the default
  --token-path FILE  the bearer token to read, if it is not inside the state dir
  --help, -h         this

The app shows whether this Mac's host is up, what it is serving, and which
sessions are running on it right now. It can restart, stop and start the host,
because it is a locally built app and not a sandboxed one.

The two path options are for a host this script cannot interrogate. The app
normally reads them off the installed `com.jremote.host` agent's own plist, but
a host embedded in a larger application has no such agent — nothing on disk
says where its state went, so it must be told. Any `JREMOTE_*` variable
exported when you run this is carried into the agent too.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   DRY_RUN=1 ;;
        --uninstall) DO_UNINSTALL=1 ;;
        --apps-dir)  APPS_DIR="${2:-}"; shift ;;
        --state-dir)  JREMOTE_STATE_DIR="${2:-}"; export JREMOTE_STATE_DIR; shift ;;
        --token-path) JREMOTE_TOKEN_PATH="${2:-}"; export JREMOTE_TOKEN_PATH; shift ;;
        --agent-label) JREMOTE_AGENT_LABEL="${2:-}"; export JREMOTE_AGENT_LABEL; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ]; then B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; Z=$'\033[0m'
else B=""; DIM=""; RED=""; GRN=""; YEL=""; Z=""; fi

step()  { printf '\n%s==>%s %s\n' "$B" "$Z" "$1"; }
ok()    { printf '  %sok%s   %s\n' "$GRN" "$Z" "$1"; }
warn()  { printf '  %swarn%s %s\n' "$YEL" "$Z" "$1"; }
die()   { printf '  %sfail%s %s\n' "$RED" "$Z" "$1" >&2; exit 1; }
note()  { printf '  %s%s%s\n' "$DIM" "$1" "$Z"; }
would() { printf '  %swould%s %s\n' "$DIM" "$Z" "$1"; }
run()   { if [ "$DRY_RUN" = "1" ]; then would "$*"; return 0; fi; "$@"; }
# `ok` for a fact observed, `did` for an action taken — and in a dry run no
# action was taken, so `did` says nothing. An "ok installed" printed by
# --dry-run is a check reporting state it cannot observe.
did()   { [ "$DRY_RUN" = "1" ] || ok "$1"; }

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$APPS_DIR/$APP_NAME.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# ── uninstall ───────────────────────────────────────────────────────────────

if [ "$DO_UNINSTALL" = "1" ]; then
    step "Removing the menu bar app"
    run launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    run rm -f "$PLIST"
    run rm -rf "$APP"
    did "the icon is off your menu bar; the host is untouched"
    exit 0
fi

# ── 0. the toolchain ────────────────────────────────────────────────────────

step "Checking prerequisites"

[ "$(uname -s)" = "Darwin" ] || die "this builds a macOS app; $(uname -s) is not supported"

# Through `xcrun`, never the path `xcrun --find` prints. That path is the raw
# compiler inside the toolchain, and run directly it has no SDK to compile
# against — "unable to load standard library" on every Mac with Xcode, which is
# most of them. `xcrun` is what resolves the active developer directory and
# hands the SDK over, and it is also what `/usr/bin/swiftc` is a shim for.
SWIFTC=()
if xcrun --find swiftc >/dev/null 2>&1; then
    SWIFTC=(xcrun swiftc)
elif command -v swiftc >/dev/null 2>&1; then
    SWIFTC=(swiftc)
fi
[ ${#SWIFTC[@]} -gt 0 ] || die "no Swift compiler — run \`xcode-select --install\` and try again"
ok "swift compiler: ${SWIFTC[*]}"

[ -f "$SELF_DIR/$SOURCE" ] || die "no $SOURCE beside this script"
ok "source at $SELF_DIR/$SOURCE"

# Where `jstack-host` ended up, baked into the agent's arguments. The app can
# search for it, but this script is the one thing that knows for certain, and a
# search that guesses wrong on a machine with two installs picks the wrong host.
HOST_BIN=""
for cand in "$BIN_DIR/jstack-host" "$SELF_DIR/../.venv/bin/jstack-host" \
            /opt/homebrew/bin/jstack-host /usr/local/bin/jstack-host; do
    if [ -x "$cand" ]; then HOST_BIN="$(cd "$(dirname "$cand")" && pwd)/$(basename "$cand")"; break; fi
done
if [ -n "$HOST_BIN" ]; then ok "jstack-host at $HOST_BIN"
else warn "jstack-host not found — the app will search for it at launch"; fi

# ── 1. build ────────────────────────────────────────────────────────────────
#
# Into a temp bundle, then moved into place. Compiling straight over the
# installed app would leave a half-written binary in the menu bar if the build
# failed, which is the one state worse than the old version still running.

step "Building"

if [ "$DRY_RUN" = "1" ]; then
    would "${SWIFTC[*]} -O -o <bundle>/Contents/MacOS/JStackHostBar $SELF_DIR/$SOURCE"
    would "install $APP"
    would "launchctl bootstrap $DOMAIN $PLIST"
    printf '\n%sDry run — nothing was changed.%s\n' "$B" "$Z"
    exit 0
fi

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
STAGE="$BUILD/$APP_NAME.app"
mkdir -p "$STAGE/Contents/MacOS"

"${SWIFTC[@]}" -O -o "$STAGE/Contents/MacOS/JStackHostBar" "$SELF_DIR/$SOURCE" \
    || die "the build failed — the compiler output above says why"
ok "compiled"

# LSUIElement is what makes it an agent app: no Dock tile, no app menu, nothing
# but the status item. The app also sets it at runtime, so a binary run out of a
# build directory behaves the same as the installed bundle.
cat > "$STAGE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>JStackHostBar</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

# Ad-hoc, so the bundle has a stable identity for the system to hang
# permissions and login state off. Locally built code needs no notarization —
# nothing here was downloaded, so nothing here is quarantined.
codesign --force --sign - "$STAGE" >/dev/null 2>&1 \
    || warn "could not ad-hoc sign the bundle; it will still run"
ok "bundle assembled"

# ── 2. install ──────────────────────────────────────────────────────────────

step "Installing"

# Out of the menu bar before the bundle underneath it is replaced: an app whose
# executable is swapped while running is one that crashes at its next page-in.
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
pkill -f "$APP_NAME.app/Contents/MacOS/JStackHostBar" >/dev/null 2>&1

mkdir -p "$APPS_DIR"
rm -rf "$APP"
mv "$STAGE" "$APP" || die "could not install to $APP"
did "$APP"

ARGS_XML="        <string>$APP/Contents/MacOS/JStackHostBar</string>"
if [ -n "$HOST_BIN" ]; then
    ARGS_XML="$ARGS_XML
        <string>--host-bin</string>
        <string>$HOST_BIN</string>"
fi

# The host-configuration variables, baked into the agent.
#
# Under launchd a GUI agent inherits nothing, so a variable that was exported
# in the shell here would be gone by the time the app looks for it — and the
# app's fallback is the *installed host agent's* plist, which a host embedded
# in a larger application does not have. On such a machine the app resolves
# the stock defaults, finds no token beside them and reports "no token" while
# the host it is pointed at is serving perfectly well. Writing them down is
# what makes the indicator true on a host this script cannot interrogate.
#
# Named one by one rather than swept up by prefix. `JREMOTE_` is not only the
# config namespace: `managed.py` exports `JREMOTE_SID` and
# `JREMOTE_COMPOSE_DIR` into every managed session's shell, so a prefix sweep
# run from inside one — which is exactly where an agent installs this — writes
# that session's id into a permanent LaunchAgent, and the indicator outlives
# the session it was pinned to. This list mirrors the seam in `hostenv.py`;
# extend it there and here together.
#
# The two mesh variables carry no prefix and are here for the same reason
# `install_host.MESH_VARS` exists: they are what says whether this Mac owns a
# WireGuard mesh, and a host whose mesh predates the package is read as having
# none without them. The app normally takes them off the host agent's own
# plist; this is the path for a host that has no agent to read.
ENV_VARS="JREMOTE_STATE_DIR JREMOTE_TOKEN_PATH JREMOTE_CREDENTIALS_DIR
          JREMOTE_RELEASES_DIR JREMOTE_INSTANCE_ROOT JREMOTE_PROFILE_MODULE
          JREMOTE_HOST_ID JREMOTE_HOST_NAME JREMOTE_HOST_PROFILE
          JREMOTE_PEER_SCRIPT JREMOTE_AGENT_LABEL JREMOTE_MENUBAR_QUIT
          WG_PEER_DIR WG_ENDPOINT"
# An embedded host's paths and its agent come off its marker, not off the
# installing shell.
#
# Everything below pins only what the caller happened to export, which is the
# same trap `install_host.render_plist` refuses: launchd builds the job from
# the user record, not from the shell that installed it. A host embedded in
# another server has no agent plist to read either, so an install run without
# those exports wrote an EnvironmentVariables dict that was simply empty — and
# the bar then resolved the package defaults, presented a credential from a
# state dir the live hub has never read, and drew "No Access" beside a host
# that was up and serving. That is exactly what happened here on 2026-09-11.
#
# The agent label is in the loop for the second half of that same morning. A
# reinstall that exported the two paths and not the label left the bar hunting
# for `com.jremote.host`, which an embedded host never has — so it read the hub
# as not installed and dropped Restart and Shut Down off the menu entirely,
# on the Mac that runs the hub. Whether a control appears must not depend on
# what a shell had exported hours earlier.
#
# Read, never guessed, and only as a default: an explicit --state-dir or an
# exported value still wins, because it is below these in the loop's `:-`.
EMBED_MARKER="${JREMOTE_EMBED_MARKER:-$HOME/.local/state/jremote/embedded.json}"
if [ -r "$EMBED_MARKER" ]; then
    for pair in "JREMOTE_STATE_DIR:state_dir" "JREMOTE_TOKEN_PATH:token_path" \
                "JREMOTE_AGENT_LABEL:agent_label"; do
        var="${pair%%:*}"; key="${pair##*:}"
        eval "cur=\${$var:-}"
        [ -n "$cur" ] && continue
        # sed and not a JSON parser: this installer ships to machines that are
        # not guaranteed a python, and the file it reads is written by
        # `embed.declare()` with `json.dumps(indent=2)` — one key per line,
        # always quoted. A hand-mangled marker yields nothing here and the
        # install carries on unpinned, which is the behaviour before this.
        val=$(sed -n "s/^[[:space:]]*\"$key\"[[:space:]]*:[[:space:]]*\"\(.*\)\"[[:space:]]*,\{0,1\}[[:space:]]*$/\1/p" \
              "$EMBED_MARKER" 2>/dev/null | head -1)
        [ -n "$val" ] && eval "$var=\$val" && eval "export $var"
    done
fi

ENV_XML=""
for var in $ENV_VARS; do
    eval "val=\${$var:-}"
    [ -n "$val" ] || continue
    ENV_XML="$ENV_XML
        <key>$var</key><string>$val</string>"
done
if [ -n "$ENV_XML" ]; then
    ENV_XML="    <key>EnvironmentVariables</key>
    <dict>$ENV_XML
    </dict>"
fi

mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
$ARGS_XML
    </array>
$ENV_XML
    <key>RunAtLoad</key><true/>
    <!-- Restart it when it crashes, never when it was quit. A plain
         KeepAlive would make the menu's own Quit item a no-op: launchd would
         put the icon straight back, and the one control the app offers over
         itself would be the one thing that does not work. -->
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
EOF
did "$PLIST"

if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    # Already loaded is the ordinary case on a re-run, not a failure. What
    # would be a failure is nothing in the menu bar afterwards, which the
    # check below is for.
    launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1
fi
launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if pgrep -f "$APP/Contents/MacOS/JStackHostBar" >/dev/null 2>&1; then
        ok "running"
        break
    fi
    sleep 0.4
done

if ! pgrep -f "$APP/Contents/MacOS/JStackHostBar" >/dev/null 2>&1; then
    warn "the agent is installed but the app is not running yet"
    note "open it once by hand: open \"$APP\""
fi

cat <<EOF

${B}The icon is on your menu bar.${Z}

  It shows whether the host is up, and how many sessions are live right now.
  Open it for the list, and for start / stop / restart.

  $0 --uninstall       take the icon back off
EOF
