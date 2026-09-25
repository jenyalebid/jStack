#!/bin/bash
# verify/run.sh — the product acceptance suite, one scenario at a time.
#
# Every scenario is a file under verify/scenarios/<category>/<name>.sh and
# runs the product the way a user would: a GUI macOS guest, the guest's own
# Terminal, the real installer from the CDN, the real app. No ssh-invisible
# probes stand in for a surface a user can see.
#
#     verify/run.sh list                        every journey: id, USER line, time, WHAT
#     verify/run.sh status                      last run, verdict and ref per journey
#     verify/run.sh check                       exit 1 if any scenario lacks its USER line
#     verify/run.sh run hub/full-reset          one scenario
#     verify/run.sh run hub                     a whole category
#     verify/run.sh run plugin hub/install      any mix
#
# Every scenario carries `# USER: <what the person does>` — the heading it
# implements in the product's TESTING.md, which is the map this runner is
# read against. A scenario without one is not a journey and `check` says so.
#
# Receipts (terminal log, screenshots, verdict) land under
# $JSTACK_VERIFY_RECEIPTS (default ~/.local/state/jstack-verify), one
# directory per scenario per run. The guest is left booted and on screen
# after the run — reading the verdict off the glass is the point.
set -u -o pipefail   # a scenario's exit code must survive the tee into its log

HERE="$(cd "$(dirname "$0")" && pwd)"
SCEN="$HERE/scenarios"
RECEIPTS_ROOT="${JSTACK_VERIFY_RECEIPTS:-$HOME/.local/state/jstack-verify}"
# The same driver every scenario uses, resolved the same way — this file
# only ever stops a guest with it. Missing is not fatal here: a run that
# needs it fails in common.sh with the remedy named.
VM="${VM_SH:-$(command -v vm.sh 2>/dev/null || true)}"

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

hdr() { sed -n "s/^# $1: //p" "$2" | head -1; }   # <FIELD> <file> -> the header line's value

list() {
    for f in "$SCEN"/*/*.sh; do
        id="${f#"$SCEN/"}"; id="${id%.sh}"
        user="$(hdr USER "$f")"; what="$(hdr WHAT "$f")"; time="$(hdr TIME "$f")"
        printf '  %-24s %-8s USER: %s\n  %-24s %-8s %s\n' "$id" "${time:--}" "${user:-MISSING}" "" "" "${what:-}"
    done
}

check() {  # every scenario names the journey it implements
    local bad=0
    for f in "$SCEN"/*/*.sh; do
        [ -n "$(hdr USER "$f")" ] || { echo "MISSING USER: ${f#"$SCEN/"}" >&2; bad=1; }
    done
    [ "$bad" = 0 ] && echo "verify/check: every scenario carries its USER line"
    return $bad
}

status() {  # the record: what each journey last said, read off its receipts
    printf '  %-24s %-16s %-6s %-10s %s\n' JOURNEY "LAST RUN" VERDICT REF USER
    for f in "$SCEN"/*/*.sh; do
        id="${f#"$SCEN/"}"; id="${id%.sh}"
        last="$(ls -d "$RECEIPTS_ROOT/${id//\//-}"/*/ 2>/dev/null | sort | tail -1)"
        if [ -n "$last" ]; then
            when="$(basename "$last")"
            v="$(cat "$last/verdict.txt" 2>/dev/null || echo '?')"
            ref="$(sed -n "s/^REF='\{0,1\}\([^']*\)'\{0,1\}$/\1/p" "$last/payload.sh" 2>/dev/null | head -1)"
        else
            when=never; v=-; ref=-
        fi
        printf '  %-24s %-16s %-6s %-10s %s\n' "$id" "$when" "$v" "${ref:--}" "$(hdr USER "$f")"
    done
}

resolve() {  # category or id -> scenario files
    local want="$1"
    if [ -f "$SCEN/$want.sh" ]; then echo "$SCEN/$want.sh"; return; fi
    if [ -d "$SCEN/$want" ]; then ls "$SCEN/$want"/*.sh; return; fi
    echo "unknown scenario or category: $want" >&2; exit 2
}

case "${1:-}" in
    list)   list; exit 0 ;;
    status) status; exit 0 ;;
    check)  check; exit $? ;;
    run)  shift ;;
    *)    usage; exit 2 ;;
esac
[ $# -ge 1 ] || { usage; exit 2; }

files=""
for want in "$@"; do files="$files $(resolve "$want")"; done

# Preconditions the operator names, checked before the first guest boots. A
# scenario that signs in needs a token file, and `guest_signin` runs after
# `guest_fresh` — so without this, the missing variable is discovered two
# minutes into a clone, and the boot slot and the clone are both spent.
for f in $files; do
    grep -q '^[[:space:]]*guest_signin' "$f" || continue
    tok="${JSTACK_VERIFY_OAUTH_TOKEN_FILE:-}"
    [ -n "$tok" ] || { echo "FAIL ${f#"$SCEN/"} signs in: set JSTACK_VERIFY_OAUTH_TOKEN_FILE to a file holding a long-lived Claude Code OAuth token (#140)" >&2; exit 1; }
    [ -s "$tok" ] || { echo "FAIL no lab sign-in token at $tok (#140)" >&2; exit 1; }
    break
done

fail=0; summary=""; total=$(echo $files | wc -w); n=0
for f in $files; do
    n=$((n + 1))
    id="${f#"$SCEN/"}"; id="${id%.sh}"
    run_dir="$RECEIPTS_ROOT/${id//\//-}/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$run_dir"
    echo "== $id  (receipts: $run_dir)"
    if RECEIPTS="$run_dir" SCENARIO_ID="$id" bash "$f" 2>&1 | tee "$run_dir/run.log"; then
        v=PASS
        # A green guest has said everything it has to say — its screen is in
        # the receipts. A red one stays booted for inspection.
        [ "${JSTACK_VERIFY_KEEP:-0}" = "1" ] \
            || "$VM" stop "vfy-${id//\//-}" >/dev/null 2>&1 || true
    else
        v=FAIL; fail=1
        # A red guest stays booted for inspection — but only the LAST one.
        # tart caps concurrent VMs; a kept guest mid-batch starved the next
        # scenario of its boot slot (proven 2026-09-22: full/pair never got
        # an address behind full/instances' kept guest). The receipts hold
        # the evidence either way.
        [ "$n" -lt "$total" ] && {
            echo "   (guest vfy-${id//\//-} stopped to free its VM slot — receipts keep the evidence)"
            "$VM" stop "vfy-${id//\//-}" >/dev/null 2>&1 || true
        }
    fi
    echo "$v" > "$run_dir/verdict.txt"
    summary="$summary$(printf '  %-24s %s' "$id" "$v")\n"
done

echo "== summary"
printf '%b' "$summary"
exit $fail
