#!/bin/bash
# PreToolUse — the ceiling on the auto-memory index.
# MEMORY.md auto-loads into EVERY agent session, so its length is a tax on all of
# them. Memory holds personal things about the user; a durable rule belongs in the
# walk-up layer that owns it, where one write reaches every agent. This ceiling
# keeps the index at pointer scale so the pile can't rebuild itself.
CEILING=20

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')

case "$path" in
  */memory/MEMORY.md) ;;
  *) exit 0 ;;
esac

tool=$(printf '%s' "$input" | jq -r '.tool_name // empty')

deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
    "$(printf '%s' "$1" | jq -Rs .)"
  exit 0
}

if [ "$tool" = "Write" ]; then
  lines=$(printf '%s' "$input" | jq -r '.tool_input.content' | wc -l | tr -d ' ')
  if [ "${lines:-0}" -gt "$CEILING" ]; then
    deny "MEMORY.md ceiling is ${CEILING} lines; this write is ${lines}. It auto-loads into every agent session. Memory holds personal things about the user — a durable rule belongs in the layer of the walk-up that owns it (org CLAUDE.md, agent root, seat, or a path-scoped rule in ~/.claude/rules/), and a platform truth belongs in an on-demand file under ~/Research/. Move it there and drop it from here."
  fi
fi

if [ "$tool" = "Edit" ] || [ "$tool" = "MultiEdit" ]; then
  cur=$(wc -l < "$path" 2>/dev/null | tr -d ' ')
  if [ "${cur:-0}" -ge "$CEILING" ]; then
    deny "MEMORY.md is already at ${cur} lines (ceiling ${CEILING}). Adding via Edit is blocked. Rewrite it with Write under the ceiling first — move anything that is really a rule into the walk-up layer that owns it, and delete what has aged out."
  fi
fi

exit 0
