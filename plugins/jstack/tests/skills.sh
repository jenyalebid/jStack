#!/usr/bin/env bash
# jStack live test — skills manifest validation.
#
# Validates every skill bundled under plugins/jstack/skills/ as a live artifact:
#   - SKILL.md exists and is non-empty
#   - YAML frontmatter present, well-formed, with name + description
#   - frontmatter `name:` matches the directory name (skill ID must be stable)
#   - SKILL.md stays under the size ceiling (read whole on every invocation)
#   - any bin/* adapters the skill references actually exist + are executable
#
# Validation is per-skill so failures localize. Each systems.json skill entry
# points at this same script — running it produces a full report. Exit 0 = all
# pass, exit 1 = any fail.
#
# Live, in the sense that matters: parses the real shipped SKILL.md, checks
# the real bin/ adapters on disk. Catches the most common drift (rename, delete,
# broken path) without spinning up a Claude session.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS_DIR="$PLUGIN_ROOT/skills"
BIN_DIR="$PLUGIN_ROOT/bin"

if [[ ! -d "$SKILLS_DIR" ]]; then
  echo "FAIL: skills dir missing at $SKILLS_DIR" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL: python3 not on PATH" >&2
  exit 1
fi

fails=0
passes=0

validate_frontmatter() {
  local skill_md="$1"
  python3 - "$skill_md" <<'PY'
import re, sys
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text()
if not text.startswith("---"):
    print(f"no frontmatter: {p}")
    sys.exit(1)
end = text.find("\n---", 3)
if end == -1:
    print(f"unterminated frontmatter: {p}")
    sys.exit(1)
block = text[3:end]
fields = {}
for line in block.splitlines():
    m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.*)$', line)
    if m:
        fields[m.group(1)] = m.group(2).strip().strip('"').strip("'")
expected_name = p.parent.name
got_name = fields.get("name", "")
got_desc = fields.get("description", "")
if got_name != expected_name:
    print(f"name mismatch: dir={expected_name} frontmatter={got_name}")
    sys.exit(1)
if not got_desc:
    print(f"empty description: {p}")
    sys.exit(1)
# The description is the trigger the model matches on, not a pitch for the
# skill. It states when to invoke and nothing else — no rationale, no summary
# of what the skill does. That is what the body is for.
if not got_desc.startswith(("Use when", "Use only when", "Use only if", "Use at")):
    print(f"description must open with a trigger clause (Use when/Use only when/Use only if/Use at), not a pitch: {p}")
    sys.exit(1)
if len(got_desc) > 140:
    print(f"description is {len(got_desc)} chars, ceiling is 140 — cut the reasons, keep the trigger: {p}")
    sys.exit(1)
PY
}

# SKILL.md is read whole on every invocation. Ceiling is the one stated in
# ~/.claude/rules/claude-md-editing.md: under 1,000 tokens (~4,000 chars).
# Overflow belongs in a sibling reference file the skill names, or in a script
# it invokes by path.
SIZE_CEILING=4000

check_size() {
  local skill_md="$1"
  local chars
  # -m not -c: these files are full of em-dashes, and counting their bytes
  # would measure something the ceiling is not stated in.
  chars=$(wc -m < "$skill_md" | tr -d ' ')
  if [[ "$chars" -gt "$SIZE_CEILING" ]]; then
    echo "SKILL.md is $chars chars, ceiling is $SIZE_CEILING — move what a reader needs only sometimes into a sibling file"
    return 1
  fi
  return 0
}

check_bin_refs() {
  local skill_md="$1"
  shift
  local missing=()
  for adapter in "$@"; do
    if [[ ! -x "$BIN_DIR/$adapter" ]]; then
      missing+=("$adapter")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "missing bin adapter(s): ${missing[*]}"
    return 1
  fi
  return 0
}

run_skill() {
  local name="$1"
  shift
  local skill_md="$SKILLS_DIR/$name/SKILL.md"

  if [[ ! -f "$skill_md" ]]; then
    echo "FAIL [$name]: SKILL.md missing at $skill_md" >&2
    fails=$((fails+1))
    return
  fi
  if [[ ! -s "$skill_md" ]]; then
    echo "FAIL [$name]: SKILL.md empty" >&2
    fails=$((fails+1))
    return
  fi

  if ! err=$(validate_frontmatter "$skill_md" 2>&1); then
    echo "FAIL [$name]: $err" >&2
    fails=$((fails+1))
    return
  fi

  if ! err=$(check_size "$skill_md" 2>&1); then
    echo "FAIL [$name]: $err" >&2
    fails=$((fails+1))
    return
  fi

  if [[ $# -gt 0 ]]; then
    if ! err=$(check_bin_refs "$skill_md" "$@" 2>&1); then
      echo "FAIL [$name]: $err" >&2
      fails=$((fails+1))
      return
    fi
  fi

  echo "PASS [$name]"
  passes=$((passes+1))
}

# Skill → required bin adapters (referenced in its SKILL.md procedure)
run_skill work
run_skill install-rules
run_skill handoff open-terminal-here seat
run_skill audit open-terminal-here
run_skill push
run_skill report file-issue
run_skill elevator
run_skill issue
run_skill task task-create
run_skill post-session-review file-followup log_event
run_skill showme open-artifact
run_skill day-audit log_event
run_skill recall log_event
run_skill tag log_event
run_skill pict pict open-artifact
run_skill splitoff dub-session open-terminal-here
run_skill takeover open-terminal-here seat
run_skill print

# Catch skills added to skills/ but not registered above
for dir in "$SKILLS_DIR"/*/; do
  name=$(basename "$dir")
  if ! grep -qE "^run_skill $name( |$)" "$0"; then
    echo "FAIL [$name]: skill present in skills/ but not registered in tests/skills.sh" >&2
    fails=$((fails+1))
  fi
done

echo ""
if [[ $fails -gt 0 ]]; then
  echo "$fails skill(s) failed validation" >&2
  exit 1
fi
echo "ALL PASS — $passes skills verified"
