#!/bin/bash
# verify/run.sh — the product acceptance suite, one scenario at a time.
#
# Every scenario is a file under verify/scenarios/<category>/<name>.sh and
# runs the product the way a user would: a GUI macOS guest, the guest's own
# Terminal, the real installer from the CDN, the real app. No ssh-invisible
# probes stand in for a surface a user can see.
#
#     verify/run.sh list
#     verify/run.sh run hub/full-reset          one scenario
#     verify/run.sh run hub                     a whole category
#     verify/run.sh run plugin hub/install      any mix
#
# Receipts (terminal log, screenshots, verdict) land under
# $JSTACK_VERIFY_RECEIPTS (default ~/.local/state/jstack-verify), one
# directory per scenario per run. The guest is left booted and on screen
# after the run — reading the verdict off the glass is the point.
set -u -o pipefail   # a scenario's exit code must survive the tee into its log

HERE="$(cd "$(dirname "$0")" && pwd)"
SCEN="$HERE/scenarios"
RECEIPTS_ROOT="${JSTACK_VERIFY_RECEIPTS:-$HOME/.local/state/jstack-verify}"

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; }

list() {
    for f in "$SCEN"/*/*.sh; do
        id="${f#"$SCEN/"}"; id="${id%.sh}"
        what="$(sed -n 's/^# WHAT: //p' "$f" | head -1)"
        time="$(sed -n 's/^# TIME: //p' "$f" | head -1)"
        printf '  %-24s %-8s %s\n' "$id" "${time:--}" "${what:-}"
    done
}

resolve() {  # category or id -> scenario files
    local want="$1"
    if [ -f "$SCEN/$want.sh" ]; then echo "$SCEN/$want.sh"; return; fi
    if [ -d "$SCEN/$want" ]; then ls "$SCEN/$want"/*.sh; return; fi
    echo "unknown scenario or category: $want" >&2; exit 2
}

case "${1:-}" in
    list) list; exit 0 ;;
    run)  shift ;;
    *)    usage; exit 2 ;;
esac
[ $# -ge 1 ] || { usage; exit 2; }

files=""
for want in "$@"; do files="$files $(resolve "$want")"; done

fail=0; summary=""
for f in $files; do
    id="${f#"$SCEN/"}"; id="${id%.sh}"
    run_dir="$RECEIPTS_ROOT/${id//\//-}/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$run_dir"
    echo "== $id  (receipts: $run_dir)"
    if RECEIPTS="$run_dir" SCENARIO_ID="$id" bash "$f" 2>&1 | tee "$run_dir/run.log"; then
        v=PASS
        # A green guest has said everything it has to say — its screen is in
        # the receipts. A red one stays booted for inspection.
        [ "${JSTACK_VERIFY_KEEP:-0}" = "1" ] \
            || "${VM_SH:-$HOME/Operations/Infrastructure/scripts/vm.sh}" stop "vfy-${id//\//-}" >/dev/null 2>&1 || true
    else
        v=FAIL; fail=1
    fi
    echo "$v" > "$run_dir/verdict.txt"
    summary="$summary$(printf '  %-24s %s' "$id" "$v")\n"
done

echo "== summary"
printf '%b' "$summary"
exit $fail
