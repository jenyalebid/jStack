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

INPUT=$(cat)
ASKED=$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("custom_instructions") or "")
except Exception: pass' 2>/dev/null || true)

# The user's own instructions lead, and are marked as theirs so the summarizer weights them
# above the standing checklist below.
if [ -n "$ASKED" ]; then
    cat <<'HDR'
The user asked for this compaction with these instructions, and they take priority over
everything below:
HDR
    printf '\n%s\n\n' "$ASKED"
fi

cat <<'CARRY'
Agent session. Also carry this operational state, concretely — absolute paths and names,
never descriptions ("edited watcher.json to add retry, uncommitted", not "edited the config"):
1. UNCOMMITTED WORK — every file touched, by absolute path, and whether it is committed and
   pushed. In a tree several sessions share, uncommitted is beyond git's reach, so a
   forgotten edit is lost.
2. THE TASK — the user's ask in their framing, what is done, what is owed. Name in-flight work
   as in-flight; a half-written file must be named half-written.
3. WHAT WAS RULED OUT, AND WHY — the reason, not just the verdict. Losing this is what makes a
   session re-propose what it discarded and re-litigate what the user already decided.
4. VERIFICATION DONE — commands run and what they returned; sources read and what they said.
   Flag anything unverified but assumed.
5. BOOKED WAKES AND SENT MESSAGES — scheduled wakes, messages still owed a reply, anything
   already sent. These live outside the session; forgetting one duplicates it.
6. OPEN DEBTS — anything muted, skipped or switched off owed a restoration this session, and
   what turns it back on.
CARRY
