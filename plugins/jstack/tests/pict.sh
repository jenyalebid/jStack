#!/usr/bin/env bash
# Hermetic test for bin/pict — fixture home tree, fixture rules, fixture
# review config, stub log_event. No real ~/.claude or timeline touched.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PICT="$PLUGIN_ROOT/bin/pict"

# pwd -P: pict resolves symlinks (macOS /var → /private/var), the fixture
# key must be computed from the same physical path
TMP="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

FIX="$TMP/home"
CLAUDE="$FIX/.claude"
mkdir -p "$FIX/Agents/Alpha/chat" "$CLAUDE/rules" "$CLAUDE/jstack"

# --- fixture walk-up chain -------------------------------------------------
echo "# Org root law" > "$FIX/CLAUDE.md"
echo "# Alpha identity" > "$FIX/Agents/Alpha/CLAUDE.md"
echo "# Alpha chat seat" > "$FIX/Agents/Alpha/chat/CLAUDE.md"

# --- fixture rules: one unconditional, one anchored, one elsewhere ---------
printf '# Always-on gates\nGate body.\n' > "$CLAUDE/rules/gates.md"
printf -- '---\npaths:\n  - "Agents/*/chat/**"\n---\n\n# Chat law\nChat rule body.\n' \
  > "$CLAUDE/rules/chat-law.md"
printf -- '---\npaths:\n  - "Other/place/**"\n---\n\n# Elsewhere law\nBody.\n' \
  > "$CLAUDE/rules/elsewhere.md"
# filename-pattern rule with NO matching file under the spawn dir — must NOT
# list as anchored (the possibility-based trap: "a .xyz could exist here")
printf -- '---\npaths:\n  - "**/*.xyz"\n---\n\n# Pattern law\nPattern body.\n' \
  > "$CLAUDE/rules/pattern.md"

# --- fixture auto-memory keyed by the dir (no git repo in fixture) ---------
KEY="$(printf '%s' "$FIX/Agents/Alpha/chat" | sed 's/[^A-Za-z0-9]/-/g')"
mkdir -p "$CLAUDE/projects/$KEY/memory"
echo "- memory index line" > "$CLAUDE/projects/$KEY/memory/MEMORY.md"

# --- fixture timeline config + stub log_event -------------------------------
cat > "$CLAUDE/jstack/review.json" <<EOF
{"agent_root": "$FIX/Agents", "timeline_inject": {"*/chat": 2}}
EOF
# Origin-aware stub: the injection view is `tail --origin direct`. A caller
# that drops the flag gets the cron line too — which is how a re-derived
# injection window (any consumer that doesn't ask for the direct view) shows
# up as an auto row in the render.
cat > "$TMP/log_event" <<'EOF'
#!/usr/bin/env bash
[ "$1" = "tail" ] || exit 1
echo "2026-01-01 — did a thing"
echo "2026-01-02 — did another thing"
case " $* " in
  *" --origin direct "*) ;;
  *) echo "2026-01-03 — published by a cron" ;;
esac
EOF
chmod +x "$TMP/log_event"

# --- fixture settings hook ---------------------------------------------------
cat > "$CLAUDE/settings.json" <<'EOF'
{"hooks": {"PreToolUse": [{"matcher": "Bash",
  "hooks": [{"type": "command", "command": "/some/nudge.sh"}]}]}}
EOF

OUT="$(PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event" \
      JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
      "$PICT" "$FIX/Agents/Alpha/chat")"

fail() { echo "FAIL: $1"; echo "---- output ----"; echo "$OUT"; exit 1; }

# 1. all three walk-up mds present, parent-most first
echo "$OUT" | grep -q "Org root law"    || fail "org root md missing"
echo "$OUT" | grep -q "Alpha identity"  || fail "agent root md missing"
echo "$OUT" | grep -q "Alpha chat seat" || fail "seat md missing"
ROOT_LINE=$(echo "$OUT" | grep -n "Org root law" | head -1 | cut -d: -f1)
SEAT_LINE=$(echo "$OUT" | grep -n "Alpha chat seat" | head -1 | cut -d: -f1)
[ "$ROOT_LINE" -lt "$SEAT_LINE" ] || fail "walk-up order wrong (root after seat)"

# 2. unconditional rule injected as global, BEFORE the walk-up chain
echo "$OUT" | grep -q "Gate body." || fail "unconditional rule missing"
GATE_LINE=$(echo "$OUT" | grep -n "Gate body." | head -1 | cut -d: -f1)
[ "$GATE_LINE" -lt "$ROOT_LINE" ] || fail "globals must precede walk-up"

# 3. anchored path rule body included; elsewhere rule excluded but listed
echo "$OUT" | grep -q "Chat rule body." || fail "anchored rule body missing"
echo "$OUT" | grep -q "elsewhere" || fail "non-anchored rule not listed"
echo "$OUT" | grep -A8 "no matching files here" | grep -q "elsewhere" \
  || fail "elsewhere rule not in no-matching-files section"
BODY_HITS=$(echo "$OUT" | grep -c "Elsewhere law" || true)
[ "$BODY_HITS" -eq 0 ] || fail "non-anchored rule body leaked into output"

# 3b. filename-pattern rule with no matching files: listed, body NOT included
echo "$OUT" | grep -A9 "no matching files here" | grep -q "pattern" \
  || fail "pattern rule not in no-matching-files section"
PAT_HITS=$(echo "$OUT" | grep -c "Pattern body." || true)
[ "$PAT_HITS" -eq 0 ] || fail "pattern rule anchored despite no matching files"

# 3c. ...and once a matching file exists, it anchors
touch "$FIX/Agents/Alpha/chat/thing.xyz"
OUT3="$(PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event" \
       JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
       "$PICT" "$FIX/Agents/Alpha/chat")"
echo "$OUT3" | grep -q "Pattern body." || fail "pattern rule not anchored after file created"
rm "$FIX/Agents/Alpha/chat/thing.xyz"

# 4. auto-memory content present
echo "$OUT" | grep -q "memory index line" || fail "memory index missing"

# 5. timeline entries present via stub log_event
echo "$OUT" | grep -q "did another thing" || fail "timeline entries missing"
echo "$OUT" | grep -q 'seat `alpha/chat`' || fail "seat resolution wrong"

# 5b. the injection carries the human-driven narrative only — pict must ask
# for the direct view, exactly as the hook does. An auto row in the render
# means the emulation drifted off the injection law it claims to preview.
echo "$OUT" | grep -q "published by a cron" \
  && fail "emulated injection carried an auto row (tail asked without --origin direct)"

# 6. headless preview: interactive_only seat goes quiet
cat > "$CLAUDE/jstack/review.json" <<EOF
{"agent_root": "$FIX/Agents",
 "timeline_inject": {"*/chat": {"n": 2, "interactive_only": true}}}
EOF
OUT2="$(PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event" \
       JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
       PICT_INTERACTIVE=0 "$PICT" "$FIX/Agents/Alpha/chat")"
echo "$OUT2" | grep -q "did another thing" && fail "headless preview injected timeline"

# 7. settings hook enumerated
echo "$OUT" | grep -q "nudge.sh" || fail "settings hook not listed"

# 8. summary separates the request-share total from the on-demand pool
echo "$OUT" | grep -q "emulatable share" || fail "request-share total missing"
echo "$OUT" | grep -q "on-demand pool" || fail "on-demand pool total missing"

# --- spawn-descriptor flags fixtures ----------------------------------------
# a project command reachable by walk-up from the seat (agent-root .claude);
# carries $ARGUMENTS/$n to prove harness-style substitution
mkdir -p "$FIX/Agents/Alpha/.claude/commands"
printf '# Reply procedure\nCommand body law. Arg=$1 Tail=$2 All=$ARGUMENTS\n' \
  > "$FIX/Agents/Alpha/.claude/commands/do-thing.md"
# an HTML comment in a walk-up md — the harness strips these in flight
printf '# Alpha chat seat\n\n<!-- template: something -->\nSeat law.\n' \
  > "$FIX/Agents/Alpha/chat/CLAUDE.md"
# an --append payload (what a spawner passes via --append-system-prompt)
printf 'Append body law.\n' > "$TMP/append-law.md"
# a captured tool-served blob
printf 'Served blob data.\n' > "$TMP/blob.txt"
# a fixture plugin with a SessionStart hook that injects a marker
PLUG="$TMP/plugins/fake/1.0.0"
mkdir -p "$PLUG/hooks" "$CLAUDE/plugins"
cat > "$PLUG/hooks/hooks.json" <<'EOF'
{"hooks": {"SessionStart": [{"matcher": "startup|clear",
  "hooks": [{"type": "command",
             "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/inject.sh\""}]}]}}
EOF
cat > "$PLUG/hooks/inject.sh" <<'EOF'
#!/usr/bin/env bash
cat > /dev/null
echo "PLUGIN-CTX-MARKER content."
EOF
chmod +x "$PLUG/hooks/inject.sh"
cat > "$CLAUDE/plugins/installed_plugins.json" <<EOF
{"version": 2, "plugins": {"fake@test": [
  {"scope": "user", "installPath": "$PLUG", "version": "1.0.0"}]}}
EOF

OUT4="$(PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event" \
       JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
       "$PICT" "$FIX/Agents/Alpha/chat" --headless --no-memory --exec-hooks \
       --append "$TMP/append-law.md" --append "$CLAUDE/rules/chat-law.md" \
       --prompt "/do-thing post=123" --serve "$TMP/blob.txt")"
fail4() { echo "FAIL: $1"; echo "---- output ----"; echo "$OUT4"; exit 1; }

# 9. --no-memory: index body absent, DISABLED note present
echo "$OUT4" | grep -q "memory index line" && fail4 "--no-memory still loaded index"
echo "$OUT4" | grep -q "auto-memory DISABLED" || fail4 "DISABLED note missing"

# 10. wire order: append (system prompt) BEFORE walk-up (first-message
#     reminder) BEFORE command body BEFORE hook context BEFORE served blob
echo "$OUT4" | grep -q "Append body law." || fail4 "append body missing"
echo "$OUT4" | grep -q "Command body law." || fail4 "command body missing"
echo "$OUT4" | grep -q "command-args>post=123" || fail4 "command args missing"
echo "$OUT4" | grep -q "Served blob data." || fail4 "served blob missing"
echo "$OUT4" | grep -q "PLUGIN-CTX-MARKER" || fail4 "plugin hook output missing"
APPEND_LINE=$(echo "$OUT4" | grep -n "Append body law." | head -1 | cut -d: -f1)
WALK_LINE=$(echo "$OUT4" | grep -n "Org root law" | head -1 | cut -d: -f1)
CMD_LINE=$(echo "$OUT4" | grep -n "Command body law." | head -1 | cut -d: -f1)
HOOK_LINE=$(echo "$OUT4" | grep -n "PLUGIN-CTX-MARKER" | head -1 | cut -d: -f1)
BLOB_LINE=$(echo "$OUT4" | grep -n "Served blob data." | head -1 | cut -d: -f1)
[ "$APPEND_LINE" -lt "$WALK_LINE" ] || fail4 "append (system prompt) must precede walk-up"
[ "$WALK_LINE" -lt "$CMD_LINE" ] || fail4 "walk-up must precede command body"
[ "$CMD_LINE" -lt "$HOOK_LINE" ] || fail4 "hook context arrives AFTER the first message"
[ "$HOOK_LINE" -lt "$BLOB_LINE" ] || fail4 "hook context must precede served blob"

# 11. $ARGUMENTS/$n substituted like the harness does
echo "$OUT4" | grep -q "Arg=post=123 Tail= All=post=123" \
  || fail4 "\$ARGUMENTS/\$n substitution wrong"

# 12. HTML comment stripped from walk-up content, flagged in the banner
echo "$OUT4" | grep -q "template: something" && fail4 "HTML comment not stripped"
echo "$OUT4" | grep -q "Seat law." || fail4 "seat body lost by comment strip"
echo "$OUT4" | grep -q "strips HTML comments" || fail4 "comment-strip note missing"

# 12b. a rule both appended AND in the on-demand pool is cross-marked, and
#      its body renders once (the append), never twice
echo "$OUT4" | grep -q "SAME FILE as the --append" || fail4 "append/pool duplicate not marked"
DUP_HITS=$(echo "$OUT4" | grep -c "Chat rule body." || true)
[ "$DUP_HITS" -eq 1 ] || fail4 "appended pool rule body must render exactly once (got $DUP_HITS)"

# 13. plugin hook enumerated in the hooks table with plugin origin
echo "$OUT4" | grep -q "plugin fake@test" || fail4 "plugin hook not enumerated"

# 14. structure is real markdown and # is pict's lane: every separator is a
#     level-1 heading; bodies are plain text (no fences, no backslash
#     escapes) with content headings demoted one level (# -> ##) so they
#     nest under pict anatomy instead of colliding with it
echo "$OUT4" | grep -q "^# Stage " || fail4 "no stage headings"
echo "$OUT4" | grep -Eq "^# [0-9]+/[0-9]+ · " || fail4 "no per-source headings"
echo "$OUT4" | grep -q '^## Org root law' || fail4 "content heading not demoted"
echo "$OUT4" | grep -q '^# Org root law' && fail4 "content heading leaked at level 1"
echo "$OUT4" | grep -q '^\\#' && fail4 "backslash escape survived (scheme is demotion)"
echo "$OUT4" | grep -q '^`\{3,\}' && fail4 "code fence in output (paints as code)"
echo "$OUT4" | python3 -c '
import re, sys
ANATOMY = re.compile(r"^# (pict|Stage |[0-9]+/[0-9]+ · |on-demand · |"
                     r"On-demand rules|Path rules|All registered hooks)")
for ln in sys.stdin.read().splitlines():
    if re.match(r"^# ", ln):
        assert ANATOMY.match(ln), f"level-1 heading that is not pict anatomy: {ln}"
' || fail4 "content leaked into pict's level-1 lane"

# 15. totals present, honestly labeled
echo "$OUT4" | grep -q "emulatable share" || fail4 "request-share total missing"
echo "$OUT4" | grep -q "TOTAL of the above" || fail4 "grand total missing"

# --- 16. --request mode: byte truth — model-read order, envelope
#         completeness, per-file splits inside composite blocks, gaps -------
python3 - "$TMP" <<'PY'
import json, sys
from pathlib import Path
tmp = Path(sys.argv[1])
src = ("# Character law\n\n" + "Character body line that is long enough to "
       "attribute cleanly to its source file. " * 10).strip()
(tmp / "character.md").write_text(src + "\n")
# a prompt-store md: literals + an inline {token} + a token-only line the
# engine drops whole — the filled body must still attribute to the file
store = ("PROMPT-STORE-HEAD " +
         "literal head long enough to clear the pool floor. " * 6 +
         "\nUp to {k} picks allowed.\n{note}\nPROMPT-STORE-TAIL " +
         "literal tail long enough to clear the pool floor. " * 6).strip()
(tmp / "store.md").write_text(store + "\n")
filled = (store.replace("Up to {k} picks", "Up to 3 picks")
               .replace("\n{note}\n", "\n"))
big = "DRIVER-PRE-BYTES\n\n---\n" + src + "\n---\n\nDRIVER-TAIL-BYTES"
req = {"model": "test-model",
       "max_tokens": 111,
       "metadata": {"user_id": "UMARKER-1"},
       "system": [{"type": "text", "text": "BASE-PROMPT-BYTES"},
                  {"type": "text", "text": big,
                   "cache_control": {"type": "ephemeral"}}],
       "tools": [{"name": "Bash", "description": "runs bash",
                  "input_schema": {}}],
       "messages": [
         {"role": "user", "content": [
           {"type": "text", "text": "WALKUP-REMINDER-BYTES"},
           {"type": "text", "text": "COMMAND-BODY-BYTES"},
           {"type": "text", "text": filled}]},
         {"role": "system", "content": "HOOK-CONTEXT-BYTES"}]}
(tmp / "req.json").write_text(json.dumps(req))
PY
OUT5="$(cd "$FIX/Agents/Alpha/chat" && PICT_CLAUDE_DIR="$CLAUDE" \
       PICT_WALK_ROOT="$FIX" JSTACK_RULES_DIR="$CLAUDE/rules" \
       "$PICT" . --request "$TMP/req.json" --sources "$TMP/character.md" \
       --sources "$TMP/store.md")"
fail5() { echo "FAIL: $1"; echo "---- output ----"; echo "$OUT5"; exit 1; }
for marker in BASE-PROMPT-BYTES DRIVER-PRE-BYTES DRIVER-TAIL-BYTES \
              WALKUP-REMINDER-BYTES COMMAND-BODY-BYTES HOOK-CONTEXT-BYTES; do
  echo "$OUT5" | grep -q "$marker" || fail5 "--request missing $marker"
done
echo "$OUT5" | grep -q "tool schema — Bash" || fail5 "--request missing tool schema"
# model-read order: system -> tools -> messages -> envelope keys (the CLI
# serializes messages before system in the JSON; the doc follows the model)
P_BASE=$(echo "$OUT5" | grep -n "BASE-PROMPT-BYTES" | head -1 | cut -d: -f1)
P_TOOL=$(echo "$OUT5" | grep -n "^# .* tool schema — Bash" | head -1 | cut -d: -f1)
P_WALK=$(echo "$OUT5" | grep -n "WALKUP-REMINDER-BYTES" | head -1 | cut -d: -f1)
P_HOOK=$(echo "$OUT5" | grep -n "HOOK-CONTEXT-BYTES" | head -1 | cut -d: -f1)
P_ENV=$(echo "$OUT5" | grep -n "^# .*envelope · model" | head -1 | cut -d: -f1)
[ "$P_BASE" -lt "$P_TOOL" ] && [ "$P_TOOL" -lt "$P_WALK" ] && \
  [ "$P_WALK" -lt "$P_HOOK" ] && [ "$P_HOOK" -lt "$P_ENV" ] \
  || fail5 "--request model-read order wrong"
echo "$OUT5" | grep -q "raw wire key order" || fail5 "raw wire key order not stated"
# every top-level key the renderer does not model still gets a section
echo "$OUT5" | grep -q "envelope · model" || fail5 "model envelope section missing"
echo "$OUT5" | grep -q "envelope · max_tokens" || fail5 "max_tokens envelope missing"
echo "$OUT5" | grep -q "envelope · metadata" || fail5 "unknown key dropped (metadata)"
# envelope sections self-identify as CLI plumbing, never prompt content
echo "$OUT5" | grep -q "CLI plumbing, not prompt text" || fail5 "envelope not labeled as plumbing"
echo "$OUT5" | grep -q "UMARKER-1" || fail5 "unknown key content invisible"
# composite block splits at the source file, gaps stay visible + labeled
echo "$OUT5" | grep -q "character.md" || fail5 "file boundary not attributed"
echo "$OUT5" | grep -q "unattributed" || fail5 "gap segment not labeled"
# a body whose {tokens} an engine filled (inline value + a dropped
# token-only line) still attributes to its prompt-store file, labeled so
echo "$OUT5" | grep -q "store.md\` (tokens filled)" \
  || fail5 "token-filled body not attributed to its store file"
# content headings demote one level; a bare --- directly under text gets a
# preceding blank so it can never read as a setext underline
echo "$OUT5" | grep -q '^## Character law' || fail5 "content heading not demoted"
echo "$OUT5" | grep -q '^# Character law' && fail5 "content heading leaked at level 1"
echo "$OUT5" | grep -q '^\\#' && fail5 "backslash escape survived (scheme is demotion)"
echo "$OUT5" | python3 -c '
import sys
lines = sys.stdin.read().splitlines()
for a, b in zip(lines, lines[1:]):
    assert not (a.strip() and set(b.strip()) == {"-"} and len(b.strip()) >= 3), \
        f"setext hazard: {a!r} / {b!r}"
' || fail5 "bare rule line rides directly under text (phantom setext heading)"
# a non-text field on a block (cache_control) is noted, never dropped
echo "$OUT5" | grep -q "cache_control" || fail5 "non-text block field invisible"

# --- 6. --bare: the injection and nothing else -----------------------------
# An always-on rule that is genuinely empty — a live slot holding no content.
# It must render as empty, NOT as "not renderable": that message belongs only
# to the base prompt, and using it for an empty file would claim a hole where
# there is none.
: > "$CLAUDE/rules/empty-gate.md"
OUT6="$(PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event" \
       JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
       "$PICT" "$FIX/Agents/Alpha/chat" --bare)"
fail6() { echo "FAIL: $1"; echo "---- output ----"; echo "$OUT6"; exit 1; }

# everything that actually injects is present, in wire order
for marker in "Gate body." "Org root law" "Alpha identity" "Alpha chat seat" \
              "memory index line" "did a thing"; do
  echo "$OUT6" | grep -q "$marker" || fail6 "--bare dropped an injected body: $marker"
done
B_GATE=$(echo "$OUT6" | grep -n "Gate body." | head -1 | cut -d: -f1)
B_ROOT=$(echo "$OUT6" | grep -n "Org root law" | head -1 | cut -d: -f1)
B_SEAT=$(echo "$OUT6" | grep -n "Alpha chat seat" | head -1 | cut -d: -f1)
[ "$B_GATE" -lt "$B_ROOT" ] && [ "$B_ROOT" -lt "$B_SEAT" ] \
  || fail6 "--bare lost wire order"

# the on-demand layer is the whole point of the flag — it must be gone,
# body and mention alike
ON_HITS=$(echo "$OUT6" | grep -c "Chat rule body." || true)
[ "$ON_HITS" -eq 0 ] || fail6 "--bare included an on-demand rule body"
echo "$OUT6" | grep -qi "on-demand" && fail6 "--bare still talks about on-demand"
echo "$OUT6" | grep -q "no matching files here" && fail6 "--bare listed elsewhere rules"
echo "$OUT6" | grep -q "All registered hooks" && fail6 "--bare listed hooks"

# no commentary: no weight table, no mechanism column, no per-file meta line
echo "$OUT6" | grep -q "~tokens" && fail6 "--bare kept token weights"
echo "$OUT6" | grep -q "| # | stage |" && fail6 "--bare kept the summary table"
echo "$OUT6" | grep -q "rides the first-message" && fail6 "--bare kept mechanism notes"

# the one thing that survives the strip: emulation cannot render the base
# prompt, and a doc that hid that would read as a complete context
echo "$OUT6" | grep -q "base system prompt" || fail6 "--bare hid the base prompt"

# empty file vs unrenderable file stay distinct
echo "$OUT6" | grep -q "(empty)" || fail6 "--bare mislabeled an empty rule"
NR_HITS=$(echo "$OUT6" | grep -c "not renderable" || true)
[ "$NR_HITS" -eq 1 ] || fail6 "--bare 'not renderable' should be the base prompt alone"

# ── a repo seat previews the identity block the hook actually sends ─────────
# systems.json claims pict "previews exactly what an IDE session in a repo
# would receive". It showed the timeline and silently dropped the role files —
# the larger of the two blocks, and the one whose absence looks like a session
# with no identity rather than a preview with a hole in it.
mkdir -p "$FIX/repos/Widget"
git -C "$FIX/repos/Widget" init -q 2>/dev/null
echo "# Widget repo docs" > "$FIX/repos/Widget/CLAUDE.md"
cat > "$FIX/Agents/agents.json" <<EOF
{"alpha": {"workspace": "$FIX/Agents/Alpha/chat", "repos": ["Widget"]}}
EOF
cat > "$CLAUDE/jstack/review.json" <<EOF
{"agent_root": "$FIX/Agents", "repo_root": "$FIX/repos",
 "agent_registry": "$FIX/Agents/agents.json",
 "timeline_inject": {"*/chat": 2}}
EOF
OUT7="$(PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event" \
       JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
       "$PICT" "$FIX/repos/Widget")"
fail7() { echo "FAIL: $1"; echo "---- output ----"; echo "$OUT7"; exit 1; }

echo "$OUT7" | grep -q 'role files — seat `alpha/chat`' \
  || fail7 "repo seat previews no identity block"
echo "$OUT7" | grep -q "Alpha identity" || fail7 "agent role file missing from identity"
echo "$OUT7" | grep -q 'seat `alpha/chat`' || fail7 "repo seat did not resolve"
# order matters: identity is who you are, and it precedes what you last did
IL=$(echo "$OUT7" | grep -n 'role files — seat' | head -1 | cut -d: -f1)
TL=$(echo "$OUT7" | grep -n 'timeline tail — seat' | head -1 | cut -d: -f1)
[ -n "$IL" ] && [ -n "$TL" ] && [ "$IL" -lt "$TL" ] \
  || fail7 "identity must precede the timeline, as the hook orders them"

# a session standing in its own seat already has its role by walk-up — the
# block would be a duplicate, and the hook does not send one
OUT8="$(PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event" \
       JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
       "$PICT" "$FIX/Agents/Alpha/chat")"
echo "$OUT8" | grep -q 'role files — seat' \
  && { echo "FAIL: cockpit seat got a duplicate identity block"; exit 1; }

# ── the window's provenance: one row per sitting, with its SUBJECTS ─────────
# A tag is a relation on the session, so a sitting filed under two subjects
# lands in BOTH windows. The block's prose never says which one it was really
# about, and reading it as the pinned subject's work is the whole failure
# mode. The table names the other subject; nothing else in the render can.
cat > "$TMP/log_event2" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  tail)
    echo "2026-02-01 09:00 [alpha/chat] — pinned work one"
    echo "2026-02-02 10:00 [beta/chat] — pinned work two"
    echo "2026-02-02 10:30 [beta/chat] — pinned work two, later"
    ;;
  recall)
    cat <<'JSON'
[{"id": 1, "date": "2026-02-01", "time": "09:00", "source": "alpha/chat",
  "headline": "pinned work one", "details": [], "session_id": "s1",
  "origin": "direct", "tags": ["widgets"]},
 {"id": 2, "date": "2026-02-02", "time": "10:00", "source": "beta/chat",
  "headline": "pinned work two", "details": [], "session_id": "s2",
  "origin": "direct", "tags": ["other-thing", "widgets"]},
 {"id": 3, "date": "2026-02-02", "time": "10:30", "source": "beta/chat",
  "headline": "pinned work two, later", "details": [], "session_id": "s2",
  "origin": "direct", "tags": ["other-thing", "widgets"]}]
JSON
    ;;
  tag) echo '[{"name": "widgets", "carried": true},
               {"name": "other-thing", "carried": false}]' ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$TMP/log_event2"
cat > "$CLAUDE/jstack/review.json" <<EOF
{"agent_root": "$FIX/Agents", "timeline_inject": {"*/chat": 2}}
EOF
run_pinned() {
  PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event2" \
  JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
  JSTACK_TIMELINE_TAG=widgets "$PICT" "$@" "$FIX/Agents/Alpha/chat"
}
OUT9="$(run_pinned)"
fail9() { echo "FAIL: $1"; echo "---- output ----"; echo "$OUT9"; exit 1; }

echo "$OUT9" | grep -q '| sitting | seat | entries | subjects |' \
  || fail9 "no provenance table under the timeline block"
echo "$OUT9" | grep -q '| 2026-02-01 09:00 | `alpha/chat` | 1 |' \
  || fail9 "sitting row missing its seat or entry count"
# the window's unit is the SITTING: one session that logged twice is one row
echo "$OUT9" | grep -q '| 2026-02-02 10:00 | `beta/chat` | 2 |' \
  || fail9 "two entries of one session did not collapse into one sitting"
echo "$OUT9" | grep -q '| 2026-02-02 10:30 |' \
  && fail9 "second entry of a sitting got its own row"
# the pin renders plain, the subject riding beside it renders bold
echo "$OUT9" | grep -q '\*\*other-thing\*\*, `widgets`' \
  || fail9 "foreign subject not marked against the pin"
echo "$OUT9" | grep -q 'besides `widgets`: \*\*other-thing\*\*' \
  || fail9 "no summary of the subjects riding this window"

# --bare is the injection and nothing else — an annotation there would be a
# claim about bytes the session never received
OUT10="$(run_pinned --bare)"
echo "$OUT10" | grep -q 'pinned work one' || fail9 "--bare lost the timeline body"
echo "$OUT10" | grep -q '| sitting | seat |' \
  && { echo "FAIL: --bare rendered the annotation table"; exit 1; }

# ── --session: the pin is the session's, never this shell's ────────────────
# The pin lives in an env var, so emulation from any other shell previews a
# window the session never got. The harness records SessionStart hook output
# verbatim in the transcript — that is where the real block comes from.
SID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SDIR="$CLAUDE/projects/fixture-proj"
mkdir -p "$SDIR"
python3 - "$SDIR/$SID.jsonl" "$FIX/Agents/Alpha/chat" <<'PY'
import json, sys
path, cwd = sys.argv[1], sys.argv[2]
block = ("<jstack-timeline>\nInjected on entry by jStack.\n\n"
         "2026-02-01 09:00 [alpha/chat] — pinned work one\n"
         "2026-02-02 10:00 [beta/chat] — pinned work two\n"
         "2026-02-02 10:30 [beta/chat] — pinned work two, later\n"
         "</jstack-timeline>")
rows = [
    {"type": "attachment", "cwd": cwd, "attachment": {
        "type": "hook_success", "hookName": "SessionStart:startup",
        "hookEvent": "SessionStart", "command": "session-start-inject.py",
        "stdout": json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": block}})}},
    {"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}},
]
with open(path, "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
PY
# no dir argument and NO pin in the environment: both must come from the
# transcript, or the read is the guess it exists to replace
OUT11="$(cd "$TMP" && env -u JSTACK_TIMELINE_TAG \
       PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" PICT_LOG_EVENT="$TMP/log_event2" \
       JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" JSTACK_RULES_DIR="$CLAUDE/rules" \
       "$PICT" --session "$SID")"
fail11() { echo "FAIL: $1"; echo "---- output ----"; echo "$OUT11"; exit 1; }

echo "$OUT11" | grep -q "captured from session .$SID" \
  || fail11 "--session did not render the captured block"
echo "$OUT11" | grep -q 'pinned work one' || fail11 "captured body missing"
# the spawn dir came from the transcript, so the walk-up is the session's own
echo "$OUT11" | grep -q "Alpha chat seat" \
  || fail11 "--session did not compose against the session's recorded cwd"
# the pin is DERIVED from the block (seat named on every line + one subject
# common to every sitting) — never parsed out of the block's prose
echo "$OUT11" | grep -q 'opened on subject `widgets`' \
  || fail11 "--session did not derive the pin from the window"
echo "$OUT11" | grep -q 'filed under: `widgets`' \
  || fail11 "--session did not report what the db has the session filed under"
echo "$OUT11" | grep -q '\*\*other-thing\*\*' \
  || fail11 "captured view lost the foreign subject"
# emulation must not sneak in beside the capture — one start stage, one truth
echo "$OUT11" | grep -q 'emulated read-only' \
  && fail11 "--session rendered the emulated timeline too"

# an id with no transcript is a usage error, not an empty render
if (cd "$TMP" && PICT_CLAUDE_DIR="$CLAUDE" PICT_WALK_ROOT="$FIX" \
    JSTACK_REVIEW_CONFIG="$CLAUDE/jstack/review.json" \
    "$PICT" --session no-such-session >/dev/null 2>&1); then
  echo "FAIL: --session accepted an id with no transcript"; exit 1
fi

echo "PASS: pict"
