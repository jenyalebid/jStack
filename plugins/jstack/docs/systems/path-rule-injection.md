# Path-Rule Injection — Architecture & Extension Guide

PreToolUse hook that injects path-matched rule bodies into Claude file-edit tools and native Codex `apply_patch` calls — so cross-tree agents pick up the right conventions regardless of where the session was launched.

This doc is the canonical reference for the hook system. Read it before extending, debugging, or changing the dedup behavior.

## Why the hook exists

`~/.claude/rules/*.md` files declare path-scoped conventions via a `paths:` frontmatter glob. Claude Code's native rule resolution matches those globs against files *under the session's launch CWD*. That breaks the moment an agent runs from one tree and edits files in a sibling tree — e.g. a cron-launched session in `~/Agents/AgentA/` touching `~/Some-Project/Views/Foo.swift` got zero rules fired, even though `ios-sheets.md` clearly claims `**/Sheets/**.swift`.

The hook closes that gap. PreToolUse fires *before every tool call*, with the absolute file path Claude is about to touch. The hook reads each rule's `paths:` glob, matches against the absolute path, and injects matched rule bodies as `additionalContext` — independent of session CWD.

## What it intercepts

`tool_name ∈ {Edit, Write, MultiEdit, NotebookEdit, apply_patch}`. Native patch targets (add, update, delete and move) resolve against the payload cwd. Shell-based writes are outside this hook’s coverage. Everything else exits 0 immediately with no output.

## stdin contract

Claude Code invokes the hook with a JSON payload on stdin. Load-bearing fields:

```json
{
  "session_id": "abc-123",
  "tool_name": "Edit",
  "tool_input": { "file_path": "/Users/.../Foo.swift" },
  "transcript_path": "/Users/.../session.jsonl"
}
```

- `tool_input.file_path` — absolute path the tool will mutate. For `NotebookEdit` the hook also accepts `tool_input.notebook_path` as a fallback key.
- `transcript_path` — current session JSONL. Used to read the byte size and decide whether enough has happened since last injection to re-fire.
- `session_id` — used to scope the per-session marker directory. Sanitized to `[A-Za-z0-9._-]+`, truncated to 128 chars.

Any malformed or missing payload → silent exit 0. The hook NEVER blocks the tool.

## stdout contract

When a rule matches and the dedup state permits, the hook emits one line of JSON:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "additionalContext": "Path-matched rules for `<file>`. Apply these conventions...\n\n## Rule: <name>\n\n<body>"
  }
}
```

When nothing matches OR the dedup gate blocks re-injection → no output, exit 0. (Empty stdout is the "no-op" signal to Claude Code.)

The example is the Claude output contract. Native Codex receives the same `hookEventName` and `additionalContext` without `permissionDecision`. In real code-mode probes, an explicit `allow` discarded the context; context-only output reached the next model request without blocking the edit. The hook never gates a tool. See the [native compatibility evidence](codex-compatibility.md).

## Rule frontmatter contract

The hook scans `~/.claude/rules/**/*.md` for `paths:` arrays in YAML-ish frontmatter:

```markdown
---
name: ios-sheets
description: SwiftUI .sheet content conventions
paths:
  - "**/Sheets/**.swift"
  - "**/Forms/**.swift"
---

# Body of the rule...
```

The parser is intentionally minimal (no PyYAML dependency — pure stdlib). It accepts:

- A block-list under `paths:`:
  ```yaml
  paths:
    - "glob/one/**"
    - "glob/two/**"
  ```
- An inline list:
  ```yaml
  paths: ["glob/one/**", "glob/two/**"]
  ```

Quoting is optional but recommended (`'` or `"` both stripped). Frontmatter without a `paths:` array is ignored — that rule is treated as unscoped and never injected by this hook.

## Glob matching

The parser converts each glob to a regex:

| Pattern | Means |
|---------|-------|
| `*`     | `[^/]*` (single path segment) |
| `**`    | `.*` (cross-segment when bare; `(?:.*/)?` when followed by `/`) |
| `?`     | `[^/]` |
| `.+()[]{}|\^$` | escaped literally |

The match runs twice:
1. **Anchored full-path match** — `^<glob>$` against the absolute file path.
2. **Tail match fallback** — iteratively strips leading path segments and retries. This is what makes `**/Sheets/**.swift` match `/Users/me/Some-Project/Views/Sheets/SearchSheet.swift` even though the glob has no `/Users/...` prefix.

If neither matches, the rule is skipped for this call.

## Dedup state machine

Re-injecting the same rule body on every edit would flood the context window. The hook stores a tiny per-session marker file per rule:

```
/tmp/jstack-rule-cache/
└── <safe_session_id>/
    ├── ios-sheets.marker     # contains: "284503"
    ├── code-review.marker    # contains: "284503"
    └── ...
```

Root-level rules retain `<stem>.marker`. Nested rules add a hash of the relative path so equal stems in different directories do not share a marker. The marker content is the **transcript byte size at the moment of last injection**. On the next call:

```
current_size = os.path.getsize(transcript_path)
last_size    = int(marker.read_text())

if (current_size - last_size) >= JSTACK_RULE_REINJECT_BYTES:
    inject again, update marker
else:
    skip
```

This means re-fire happens roughly every `JSTACK_RULE_REINJECT_BYTES` of transcript growth (default `400_000` bytes ≈ 100K tokens at ~4 bytes/token). The window is generous on purpose — refresh once per long working stretch, not every tool call.

**On a fresh session_id** the marker is absent → cold-fire. The first call always injects matching rules.

**The marker root** (`/tmp/jstack-rule-cache/`) is intentionally on `tmpfs` — reboot wipes all session state cleanly, no manual cleanup needed.

## Env-var surface

| Var | Default | Purpose |
|-----|---------|---------|
| `JSTACK_PATH_RULES_DISABLED` | unset | If set to any non-empty value, exit 0 immediately. Per-session kill switch. |
| `JSTACK_RULE_REINJECT_BYTES` | `400000` | Bytes of transcript growth between re-fires. Lower = more aggressive. |
| `JSTACK_RULES_DIR` | `~/.claude/rules` | Override the rules source dir. **Test-only** — use to point the hook at a fixture without touching real rules. |
| `JSTACK_CACHE_ROOT` | `/tmp/jstack-rule-cache` | Override the marker root. **Test-only** — use to isolate test markers. |

The two `*_DIR`/`*_ROOT` overrides exist for `tests/path-rule-injection.sh` and any future automation. Production code paths use the defaults.

## Defensive guarantees

The hook is wrapped in broad `try/except` boundaries because **failing the hook must never fail the tool call**. Specifically:

- Unparseable stdin JSON → exit 0
- Missing `tool_input.file_path` → exit 0
- `~/.claude/rules/` doesn't exist → exit 0
- Frontmatter parse error on any rule → that rule skipped, others continue
- `mkdir -p` on the cache dir fails → exit 0
- Marker read returns garbage → treat as "first call", re-inject
- stdout write fails (Claude Code closed the pipe) → exit 0

Exit code is always `0` from `main()`. The only way the hook reports failure is by *not* emitting injection — which Claude Code interprets as "no extra context, proceed normally."

## hooks.json registration

The hook registers itself via `plugins/jstack/hooks/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PLUGIN_ROOT}/hooks/inject-path-rules.py"
          }
        ]
      }
    ]
  }
}
```

`${CLAUDE_PLUGIN_ROOT}` is substituted to the plugin's installed cache path at runtime. No `/jstack:install-hook` skill needed — plugins auto-load `hooks/hooks.json` when enabled.

## Test surface

`plugins/jstack/tests/path-rule-injection.sh` runs three live cases against a hermetic fixture (`JSTACK_RULES_DIR` + `JSTACK_CACHE_ROOT` pointed at a tempdir):

1. **cold-fire** — fresh session, rule matches → JSON output emitted, marker written.
2. **consecutive-dedup** — same session, no transcript growth → empty stdout.
3. **threshold re-fire** — transcript grown past `JSTACK_RULE_REINJECT_BYTES` (set to `100` for the test) → JSON output emitted again.

Run any time, anywhere. Real hook script, real frontmatter parser, real glob matcher, real marker state — just isolated from production state.

## Debug recipes

**See what's matching, live.** The hook is silent on success. To watch matches in real time, temporarily replace the `try/except` at the bottom of `main()` with a stderr log:

```python
sys.stderr.write(f"[jstack-rules] matched: {[r.stem for r in to_inject]}\n")
```

Claude Code surfaces hook stderr in `~/.claude/projects/.../logs/`. Restore once you're done.

**Inspect a session's markers.**

```bash
ls -la /tmp/jstack-rule-cache/$SESSION_ID/
cat   /tmp/jstack-rule-cache/$SESSION_ID/ios-sheets.marker   # byte offset at last inject
```

**Force a re-fire.** Delete the marker:

```bash
rm /tmp/jstack-rule-cache/$SESSION_ID/<rule-name>.marker
```

Next matching tool call cold-fires that rule.

**Force a re-fire across all rules this session.**

```bash
rm -rf /tmp/jstack-rule-cache/$SESSION_ID/
```

**Lower the threshold for testing.** Spawn Claude Code with `JSTACK_RULE_REINJECT_BYTES=10000` to see re-fires roughly every 10K bytes of transcript.

**Disable for one session.** Spawn with `JSTACK_PATH_RULES_DISABLED=1`.

## Extension points

**Adding a new tool type.** Append to the `TOOLS` set at the top of `inject-path-rules.py`. If the new tool exposes its target path under a different key, extend the lookup:

```python
file_path = (
    tool_input.get("file_path")
    or tool_input.get("notebook_path")
    or tool_input.get("path")    # hypothetical new key
)
```

**Adding a new matcher.** Glob is the only matcher today. Adding e.g. regex would mean a new frontmatter field (`path_regex:`) and a parallel scan; keep `paths:` working unchanged so existing rules don't break.

**Capturing more than file_path.** Add fields to `additionalContext`. e.g. inject the tool name, the surrounding directory contents, or recently-edited siblings — the contract is just markdown text; Claude Code injects it verbatim.

**Per-rule TTL or per-rule reinject_bytes.** Today the threshold is global. To make it per-rule, parse an optional `reinject_bytes:` field from frontmatter, fall through to the env default.

## Reading the source

`plugins/jstack/hooks/inject-path-rules.py` is ~260 lines, pure stdlib, no dependencies. Reading order:

1. `main()` — top-level flow
2. `_parse_paths_frontmatter()` — frontmatter handling
3. `_glob_to_regex()` + `_glob_match()` — path matching
4. `_read_marker()` / `_write_marker()` — dedup state

No tests live inside the script; all live tests live in `plugins/jstack/tests/`.

## Where this fits

This system is one of several inside jStack tracked in `plugins/jstack/systems.json`. A host machine may federate that file into its own systems registry at read time via an `imports:` field — each jStack entry then surfaces in the host's dashboard with an `origin: jstack` badge and a "Test" button that invokes this hook's `tests/path-rule-injection.sh` script.

Edit the hook where the code lives — in jStack. The federation propagates automatically on next dashboard read; `claude plugin update jstack` makes work-Claude's machine see the same change.
