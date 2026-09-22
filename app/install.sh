#!/usr/bin/env bash
# jRemote for Mac — install the app from a signed release.
#
#   ./install.sh                 # newest release
#   ./install.sh --dry-run       # print the plan, touch nothing
#   ./install.sh --force         # reinstall even when that build is present
#   ./install.sh --uninstall     # remove the app (your data stays)
#
# The app itself is closed source. This script is not, and that is the point:
# what you are trusting when you install a binary is *where it came from* and
# *what was checked before it ran*, and both of those are here in plain sight.
#
# What it checks before anything lands in /Applications, in this order:
#
#   1. the SHA-256 of the download matches the manifest published beside it
#   2. the bundle's code signature verifies (`codesign --verify --strict`)
#   3. Gatekeeper accepts it as a *notarized* Developer ID app — the same
#      question macOS itself asks on first launch
#   4. it is signed by the expected Apple team, pinned below
#
# Any one of those failing stops the install with the app untouched. There is
# no `--skip-verify`, because a flag that turns the checks off is a flag that
# ends up in someone's copy-pasted command.
#
# No `sudo`. /Applications is group-writable by admins on a normal Mac, and an
# installer that asks for a password is one you have to trust rather than read.

set -uo pipefail

# One installer. This script is a step of the top-level install.sh, not a door.
if [ -z "${JSTACK_INSTALLER:-}" ]; then
    echo "this is an internal step of the jStack installer — run instead:" >&2
    echo "  curl -fsSL https://raw.githubusercontent.com/jenyalebid/jStack/main/install.sh | bash" >&2
    exit 2
fi

# The Apple team the app is signed by. Pinned here as a constant and NOT read
# from the downloaded manifest, which is the whole difference between a check
# and a formality: a manifest that could nominate its own signer would let a
# tampered release sign itself with anything and still pass. You can confirm
# this value against a copy you already trust with:
#
#     codesign -dv /Applications/jRemote.app 2>&1 | grep TeamIdentifier
#
TEAM_ID="MZ95H77RQQ"

# Which release series. Tags are per-product, because this repo also versions
# the stack itself — GitHub's own `/releases/latest` means "newest release in
# the repo", which would hand a stack release to someone asking for the app.
TAG_PREFIX="mac-app-"

APP_NAME="jRemote"
PREFIX="${JREMOTE_PREFIX:-/Applications}"
REPO="${JSTACK_REPO:-}"
TAG=""
DRY_RUN=0
DO_UNINSTALL=0
ASSUME_YES=0
FORCE=0

usage() {
    cat <<'EOF'
usage: install.sh [options]

  --dry-run        print what would happen and change nothing
  --yes, -y        don't ask; accept every default
  --force          reinstall even when the selected build is already installed
  --uninstall      remove the app (its settings and paired hosts stay)
  --tag TAG        install a specific release instead of the newest
  --repo OWNER/NAME  where to download from (default: this checkout's origin)
  --prefix DIR     where to install (default /Applications)
  --help, -h       this

The app talks to a host, which is the other half and installs separately:

    ./host/install.sh
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1 ;;
        --yes|-y)     ASSUME_YES=1 ;;
        --force)      FORCE=1 ;;
        --uninstall)  DO_UNINSTALL=1 ;;
        --tag)        TAG="${2:-}"; shift ;;
        --repo)       REPO="${2:-}"; shift ;;
        --prefix)     PREFIX="${2:-}"; shift ;;
        --help|-h)    usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

BOLD=""; DIM=""; RED=""; GREEN=""; RESET=""
if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"
    RED="$(printf '\033[31m')"; GREEN="$(printf '\033[32m')"
    RESET="$(printf '\033[0m')"
fi
note() { printf '%s\n' "${DIM}$1${RESET}"; }
ok()   { printf '%s\n' "${GREEN}ok${RESET} $1"; }
warn() { printf '%s\n' "${RED}warning${RESET} $1" >&2; }
die()  { printf '%s\n' "${RED}error${RESET} $1" >&2; exit 1; }
# Silent under --dry-run: a check that reports state it did not observe is
# worse than no check. Nothing happened, so nothing says it did.
did()  { [ "$DRY_RUN" = "1" ] || ok "$1"; }
run()  { if [ "$DRY_RUN" = "1" ]; then note "would: $*"; else "$@"; fi }

APP_PATH="$PREFIX/$APP_NAME.app"

# ── 0. preflight ────────────────────────────────────────────────────────────

[ "$(id -u)" != "0" ] || die "don't run this as root — the app installs as you"
[ "$(uname -s)" = "Darwin" ] || die "macOS only (this is a Mac app)"

for tool in curl ditto codesign spctl shasum; do
    command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done

# ── uninstall ───────────────────────────────────────────────────────────────

if [ "$DO_UNINSTALL" = "1" ]; then
    [ -d "$APP_PATH" ] || die "nothing installed at $APP_PATH"
    if pgrep -f "$APP_PATH/Contents/MacOS/$APP_NAME" >/dev/null 2>&1; then
        run pkill -TERM -f "$APP_PATH/Contents/MacOS/$APP_NAME" || true
        [ "$DRY_RUN" = "1" ] || sleep 2
    fi
    run rm -rf "$APP_PATH"
    did "removed $APP_PATH"
    note "settings, paired hosts and tokens are untouched — reinstalling picks"
    note "them back up. To clear those too, remove the app in Settings first."
    exit 0
fi

# ── 1. where to download from ───────────────────────────────────────────────
#
# Derived from the checkout this script is sitting in, not spelled: a hardcoded
# account name in a public installer is a name that outlives whoever owns the
# repo, and the script is normally run *from* a clone that already knows.

if [ -z "$REPO" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || true)"
    ORIGIN="$(git -C "$SELF_DIR" remote get-url origin 2>/dev/null || true)"
    # Both URL shapes git hands back, reduced to owner/name.
    REPO="$(printf '%s' "$ORIGIN" \
        | sed -E 's#^git@[^:]+:##; s#^https?://[^/]+/##; s#\.git$##')"
fi
[ -n "$REPO" ] || die "could not tell which repo to download from — run this from a clone, or pass --repo OWNER/NAME"
case "$REPO" in
    */*) ;;
    *) die "--repo wants OWNER/NAME, got: $REPO" ;;
esac
note "release source: $REPO"

# ── 2. which release ────────────────────────────────────────────────────────

API="https://api.github.com/repos/$REPO/releases"
DL="https://github.com/$REPO/releases/download"

if [ -z "$TAG" ]; then
    # One field out of the listing, which comes back newest first. Grepping a
    # single well-known key rather than parsing JSON on purpose: a stock Mac
    # has no jq, and `python3` is a stub that prompts to install developer
    # tools — an installer that opens a dialog before it downloads anything is
    # not an installer anyone finishes.
    TAG="$(curl -fsSL --max-time 30 "$API" 2>/dev/null \
        | grep -o "\"tag_name\": *\"${TAG_PREFIX}[^\"]*\"" \
        | head -1 | sed -E 's/.*"([^"]*)"$/\1/')"
fi
if [ -z "$TAG" ]; then
    # Exit 3, not 1: "no release has been published yet" is a fact about the
    # repository, not a fault in this machine. The top-level installer treats
    # 1 as a problem worth a warning line and 3 as a note, because a warning
    # the reader cannot act on is how the actionable ones get skimmed past.
    note "no ${TAG_PREFIX}* release published in $REPO yet — nothing to install"
    note "pass --tag to install a specific one, or re-run this script once a release exists"
    exit 3
fi
note "release: $TAG"

# ── 3. the manifest ─────────────────────────────────────────────────────────

WORK="$(mktemp -d "${TMPDIR:-/tmp}/jremote-install.XXXXXX")" || die "could not make a work directory"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

curl -fsSL --max-time 60 "$DL/$TAG/latest.json" -o "$WORK/latest.json" \
    || die "could not fetch the manifest for $TAG"

field() { grep -o "\"$1\": *\"[^\"]*\"" "$WORK/latest.json" | head -1 | sed -E 's/.*"([^"]*)"$/\1/'; }
num()   { grep -o "\"$1\": *[0-9]*"     "$WORK/latest.json" | head -1 | sed -E 's/.*: *//'; }

WANT_SHA="$(field sha256)"
ZIP_NAME="$(field file)"
BUILD="$(num build)"
VERSION="$(field version)"

[ -n "$WANT_SHA" ] || die "manifest has no sha256 — refusing to install unverified"
[ -n "$ZIP_NAME" ] || die "manifest does not name its archive"

note "build $BUILD (version ${VERSION:-?})"

if [ -d "$APP_PATH" ]; then
    HAVE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP_PATH/Contents/Info.plist" 2>/dev/null || echo '?')"
    note "installed now: build $HAVE"
    if [ "$HAVE" = "$BUILD" ] && [ "$FORCE" != "1" ] && [ "$DRY_RUN" != "1" ]; then
        note "build $BUILD is already installed — nothing to do"
        exit 0
    fi
fi

# ── 4. download and verify ──────────────────────────────────────────────────

if [ "$DRY_RUN" = "1" ]; then
    note "would: download $DL/$TAG/$ZIP_NAME"
    note "would: verify sha256 == $WANT_SHA"
    note "would: verify signature, notarization, and team $TEAM_ID"
    note "would: install to $APP_PATH"
    exit 0
fi

# Braced on purpose. In a UTF-8 locale bash reads the ellipsis as part of the
# name, so `$ZIP_NAME…` expands nothing and `set -u` kills the script one line
# before the download — "ZIP_NAME…: unbound variable", on the only line where
# the variable is obviously set.
echo "Downloading ${ZIP_NAME}…"
curl -fSL --max-time 900 --progress-bar "$DL/$TAG/$ZIP_NAME" -o "$WORK/$ZIP_NAME" \
    || die "download failed"

GOT_SHA="$(shasum -a 256 "$WORK/$ZIP_NAME" | cut -d' ' -f1)"
[ "$GOT_SHA" = "$WANT_SHA" ] || die "checksum mismatch — refusing to install
  expected $WANT_SHA
  got      $GOT_SHA"
ok "checksum matches the manifest"

run ditto -x -k "$WORK/$ZIP_NAME" "$WORK/unpacked" || die "could not unpack the archive"
STAGED="$WORK/unpacked/$APP_NAME.app"
[ -d "$STAGED" ] || die "the archive does not contain $APP_NAME.app"

codesign --verify --strict "$STAGED" 2>/dev/null \
    || die "the downloaded app's signature does not verify — do not install this"
ok "code signature verifies"

# The exact question macOS asks on first launch. A build that fails here is one
# that would open to "jRemote cannot be opened because the developer cannot be
# verified" — better to find out now than after it is in /Applications.
ASSESS="$(spctl -a -vvv -t exec "$STAGED" 2>&1)"
printf '%s' "$ASSESS" | grep -q "accepted" \
    || die "Gatekeeper rejects this build:
$ASSESS"
printf '%s' "$ASSESS" | grep -q "source=Notarized Developer ID" \
    || die "accepted, but not as a notarized Developer ID app:
$ASSESS"
ok "Gatekeeper: accepted (notarized Developer ID)"

GOT_TEAM="$(codesign -dv "$STAGED" 2>&1 | sed -n 's/^TeamIdentifier=//p')"
[ "$GOT_TEAM" = "$TEAM_ID" ] || die "signed by team '$GOT_TEAM', expected '$TEAM_ID' — refusing to install"
ok "signed by the expected team"

# ── 5. install ──────────────────────────────────────────────────────────────

WAS_RUNNING=0
if pgrep -f "$APP_PATH/Contents/MacOS/$APP_NAME" >/dev/null 2>&1; then
    WAS_RUNNING=1
    echo "Quitting the running copy…"
    # Signals, never `osascript -e 'quit app'`. That is an Apple Event, and an
    # app wedged during launch never answers one — the quit blocks forever and
    # takes the install with it.
    pkill -TERM -f "$APP_PATH/Contents/MacOS/$APP_NAME" >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        pgrep -f "$APP_PATH/Contents/MacOS/$APP_NAME" >/dev/null 2>&1 || break
        sleep 0.5
    done
    pkill -9 -f "$APP_PATH/Contents/MacOS/$APP_NAME" >/dev/null 2>&1 || true
fi

mkdir -p "$PREFIX" 2>/dev/null || true
[ -w "$PREFIX" ] || die "$PREFIX is not writable by you — pass --prefix ~/Applications"

# Delete, never trash. Trash renames a collision to "jRemote 10-14-33.app" and
# keeps it registered, so every install earns another tile in Launchpad.
rm -rf "$APP_PATH" || die "could not replace $APP_PATH"
ditto "$STAGED" "$APP_PATH" || die "could not install to $APP_PATH"
ok "installed build $BUILD to $APP_PATH"

if [ "$WAS_RUNNING" = 1 ]; then
    open "$APP_PATH" && ok "relaunched"
else
    echo
    echo "${BOLD}Open it:${RESET} open -a $APP_NAME"
    echo
    echo "Connect to a local or remote jStack Hub:"
    echo "    On the Hub, choose Pair a Device."
    echo "    In jRemote, choose Add a Mac and enter its address and pairing code."
    echo "No local Hub installation is required to use this client."
fi
