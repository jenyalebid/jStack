#!/bin/bash
# PreCompact: tell the summarizer what an agent session cannot afford to lose.
#
# A compaction cannot be deferred. The client tests for it at the top of every model call,
# so it lands between two tool calls, mid-task; PreCompact runs after that decision, and a
# blocked PreCompact hook's output is discarded rather than honoured. The boundary is going
# to happen. The only question this hook gets to answer is what survives it.
#
# Left alone, the summary keeps the shape of the conversation and drops the operational
# residue -- which on a working machine is the expensive half. A session that forgets it
# edited four files and pushed none of them leaves them uncommitted in a tree several
# sessions share, where git cannot reach them. One that forgets it booked a wake books it
# twice. One that forgets what it already ruled out proposes it again, which is the exact
# failure a session's first reflex exists to prevent.
#
# stdout becomes the summarizer's custom instructions verbatim (client: newCustomInstructions
# = the hook outputs, joined). That also means it REPLACES anything the user typed after
# `/compact`, so their instructions are echoed back through first -- their framing outranks
# this checklist and must not be dropped by the hook that is trying to help.
#
# Fires on both triggers: `auto` at the client's threshold, `manual` on /compact.
#
# KEEP THIS OUTPUT SHORT. The client also echoes it to the user's terminal verbatim -- it
# builds `userDisplayMessage` from the same string it puts in newCustomInstructions, with no
# suppressOutput check on this path. Every line here is a line they read at every compaction.
# The summarizer is a model and does not need the prose; they need none of it.

set -eu

# The text is file-sourced (rules-stage/prompt-sourcing.md): one `## name` section of
# prompts/precompact-carry.md, printed as written.
PROMPT="$(dirname "$0")/../prompts/precompact-carry.md"
section() {
    awk -v want="## $1" '$0 == want { on = 1; next } /^## [^ ]+$/ { on = 0 } on' "$PROMPT" \
        | awk 'NF { seen = 1 } seen { buf = buf $0 "\n"; if (NF) { printf "%s", buf; buf = "" } }'
}

INPUT=$(cat)
ASKED=$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("custom_instructions") or "")
except Exception: pass' 2>/dev/null || true)

# The user's own instructions lead, and are marked as theirs so the summarizer weights them
# above the standing checklist below.
if [ -n "$ASKED" ]; then
    section asked
    printf '\n%s\n\n' "$ASKED"
fi

section carry
