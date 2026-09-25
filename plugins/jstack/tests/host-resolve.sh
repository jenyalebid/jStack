#!/usr/bin/env bash
# jStack live test — hooks/_host.py (where an INSTALLED copy of the plugin finds the host).
#
# Every host-importing hook used to compose `parents[3] / "host"`, which is the
# repo only in a checkout. Claude Code runs an installed plugin from
# `~/.claude/plugins/cache/<marketplace>/jstack/<version>/`, where parents[3]
# holds no host/ — so on every installed machine those hooks exited 0 having
# done nothing, while the suites, run from a checkout, passed. This test runs
# the hooks FROM A CACHE-SHAPED COPY, the way an install runs them. What it pins:
#   - a cache copy resolves the checkout through the directory marketplace it
#     was installed from — `known_marketplaces.json` (Claude) or Codex's
#     `config.toml` — and a directory with no host/jstack_host/__init__.py is
#     not a checkout, whatever its name in the ledger says.
#   - a failed load leaves one line in ~/.claude/jstack/host-load.txt saying
#     why, and the next successful load removes it.
#   - end to end: a real hook run from the cache copy does its work — the one
#     assertion the old code could never have passed.
#   - a re-exec cannot loop: a child marked JSTACK_HOST_REEXEC records the
#     reason and exits, and a real re-exec into a venv interpreter that is
#     itself below the floor happens exactly once.
#
# HOME is a tmpdir throughout: nothing under the real ~/.claude or ~/.codex is
# read or written. Exit 0 = all pass, exit 1 = any fail.

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
CHECKOUT="$(cd "$PLUGIN_ROOT/../.." && pwd -P)"

[[ -f "$PLUGIN_ROOT/hooks/_host.py" ]] || { echo "FAIL: $PLUGIN_ROOT/hooks/_host.py missing" >&2; exit 1; }
[[ -f "$CHECKOUT/host/jstack_host/__init__.py" ]] || { echo "FAIL: $CHECKOUT is not a host checkout" >&2; exit 1; }

TMP=$(mktemp -d /tmp/jstack-hostresolve-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# The plugin laid out exactly as Claude Code's cache lays it out.
CACHE="$TMP/.claude/plugins/cache/jStack/jstack/0.0.0"
mkdir -p "$CACHE"
cp -R "$PLUGIN_ROOT"/. "$CACHE"/
rm -rf "$CACHE/tests"

# The hooks run `python3` off PATH; so does this test.
export PYTHONDONTWRITEBYTECODE=1
unset CODEX_HOME JSTACK_HOST_REEXEC JREMOTE_STATE_DIR JREMOTE_EMBED_MARKER

python3 - "$CACHE" "$CHECKOUT" "$TMP" <<'PY'
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

CACHE, CHECKOUT, TMP = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
HOOKS = CACHE / "hooks"
HOST_PY = HOOKS / "_host.py"
CEILING = HOOKS / "context-ceiling.py"
REAL_PY = sys.executable

fails = []
def check(name, cond, detail=""):
    print(("ok" if cond else "FAIL") + f": {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

def same(a, b):
    return a is not None and b is not None and os.path.realpath(a) == os.path.realpath(b)

# --- fixtures ---------------------------------------------------------------
# A directory that LOOKS like a checkout in the ledger and is not one: it has a
# host/ and no jstack_host/__init__.py under it.
BARE = TMP / "bare-checkout"
(BARE / "host").mkdir(parents=True)
(BARE / "plugins" / "jstack" / "hooks").mkdir(parents=True)

def home(name):
    """A fresh HOME per case, so no ledger from one case leaks into the next."""
    h = TMP / "homes" / name
    (h / ".claude" / "plugins").mkdir(parents=True, exist_ok=True)
    return h

def claude_ledger(h, entries):
    (h / ".claude" / "plugins" / "known_marketplaces.json").write_text(json.dumps(entries))

def codex_config(h, text, codex_home=None):
    d = Path(codex_home) if codex_home else h / ".codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(textwrap.dedent(text))

def reason_file(h):
    return h / ".claude" / "jstack" / "host-load.txt"

def reason(h):
    try:
        return reason_file(h).read_text()
    except OSError:
        return None

def env_for(h, extra=None):
    env = os.environ.copy()
    env["HOME"] = str(h)
    env.pop("CODEX_HOME", None)
    env.pop("JSTACK_HOST_REEXEC", None)
    env.update(extra or {})
    return env

DRIVER = textwrap.dedent("""
    import importlib.util, json, os, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("_host", sys.argv[1])
    _host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_host)
    action = sys.argv[2]
    out = {"reexec_env_popped": "JSTACK_HOST_REEXEC" not in os.environ}
    if action == "find":
        found = _host.find_checkout()
        out["checkout"] = str(found) if found else None
    elif action.startswith("load"):
        if "floor99" in action:
            _host.requires_python = lambda root: (99, 0)
        kw = {"reexec": False} if "noreexec" in action else {}
        try:
            mod = _host.load("compaction", **kw)
            out["loaded"] = mod.__file__
        except ImportError as e:
            out["error"] = str(e)
    out["alive"] = True
    print(json.dumps(out))
""")

def drive(h, action, extra_env=None):
    """Import the CACHE copy's _host.py by path in a fresh process under HOME=h."""
    r = subprocess.run([REAL_PY, "-c", DRIVER, str(HOST_PY), action],
                       env=env_for(h, extra_env), capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"alive": False, "rc": r.returncode, "stderr": r.stderr[-800:]}

def find(h, extra_env=None):
    return drive(h, "find", extra_env).get("checkout")

# --- (0) the cache copy is not beside a host: the case this file exists for --
check("the cache copy has no host beside it (parents[1] of the plugin)",
      not (CACHE.parents[1] / "host" / "jstack_host" / "__init__.py").exists())
h = home("nothing")
check("no ledger anywhere resolves nothing", find(h) is None)

# --- (a) known_marketplaces.json, directory source ---------------------------
h = home("claude-good")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(CHECKOUT)},
                             "installLocation": str(CHECKOUT)}})
check("a directory marketplace named by the cache path resolves the checkout",
      same(find(h), CHECKOUT), f"got {find(h)}")

h = home("claude-bare")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(BARE)},
                             "installLocation": str(BARE)}})
check("a directory entry with no host/jstack_host/__init__.py does not resolve",
      find(h) is None, f"got {find(h)}")

h = home("claude-github")
claude_ledger(h, {"jStack": {"source": {"source": "github", "repo": "someone/jStack"},
                             "installLocation": str(CHECKOUT)}})
check("a github-source entry is not a checkout even when installLocation is one",
      find(h) is None, f"got {find(h)}")

h = home("claude-othername")
claude_ledger(h, {"claude-plugins-official": {"source": {"source": "github", "repo": "a/b"}},
                  "mine": {"source": {"source": "directory", "path": str(BARE)}},
                  "acme": {"source": {"source": "directory", "path": str(CHECKOUT)}}})
check("a directory entry under another name still resolves when it ships this plugin's _host.py",
      same(find(h), CHECKOUT), f"got {find(h)}")

h = home("claude-corrupt")
(h / ".claude" / "plugins" / "known_marketplaces.json").write_text("{not json")
check("a corrupt ledger resolves nothing and does not raise", find(h) is None)

h = home("claude-tilde")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": "~/checkout"}}})
os.symlink(CHECKOUT, h / "checkout")
check("a ~ in the ledger path expands against the redirected HOME",
      same(find(h), CHECKOUT), f"got {find(h)}")

# --- (b) Codex config.toml, local marketplace --------------------------------
h = home("codex-good")
codex_config(h, f'''
    model = "gpt-5"

    [marketplaces.jStack]
    source_type = "local"
    source = "{CHECKOUT}"

    [other]
    key = "value"
''')
check("a Codex local marketplace named by the cache path resolves the checkout",
      same(find(h), CHECKOUT), f"got {find(h)}")

h = home("codex-bare")
codex_config(h, f'''
    [marketplaces.jStack]
    source_type = "local"
    source = "{BARE}"
''')
check("a Codex entry at a directory with no host package does not resolve",
      find(h) is None, f"got {find(h)}")

h = home("codex-lowercase")
codex_config(h, f'''
    [marketplaces.jstack]
    source_type = "local"
    source = "{CHECKOUT}"
''')
check("a Codex entry under a different case still resolves by this plugin's _host.py",
      same(find(h), CHECKOUT), f"got {find(h)}")

h = home("codex-git")
codex_config(h, f'''
    [marketplaces.jStack]
    source_type = "git"
    source = "{CHECKOUT}"
''')
check("a Codex non-local source is not a checkout", find(h) is None, f"got {find(h)}")

h = home("codex-home-env")
alt = TMP / "codex-alt"
codex_config(h, f'''
    [marketplaces.jStack]
    source_type = "local"
    source = "{CHECKOUT}"
''', codex_home=alt)
check("CODEX_HOME names where config.toml is read from",
      same(find(h, {"CODEX_HOME": str(alt)}), CHECKOUT) and find(h) is None)

h = home("claude-over-codex")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(BARE)}}})
codex_config(h, f'''
    [marketplaces.jStack]
    source_type = "local"
    source = "{CHECKOUT}"
''')
check("a Claude entry that is not a checkout falls through to the Codex one",
      same(find(h), CHECKOUT), f"got {find(h)}")

# --- (c) the receipt: one line on failure, gone on success -------------------
h = home("receipt")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(BARE)}}})
out = drive(h, "load")
check("a load with no checkout raises ImportError", "error" in out and "no host checkout" in out["error"], str(out))
line = reason(h)
check("a failed load writes host-load.txt", line is not None)
check("the receipt is one line", bool(line) and line.count("\n") == 1 and line.endswith("\n"), repr(line))
check("the receipt is timestamp, hook, reason", bool(line) and len(line.rstrip("\n").split("\t")) == 3, repr(line))
check("the receipt says why", bool(line) and "no host checkout" in line and "known_marketplaces.json" in line, repr(line))
check("the receipt is one line after a second failure too",
      (drive(h, "load") or True) and reason(h).count("\n") == 1)

claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(CHECKOUT)}}})
out = drive(h, "load")
check("a load with a checkout imports the host from it",
      same(Path(out.get("loaded", "/nowhere")).parent.parent, CHECKOUT / "host"), str(out))
check("a successful load removes host-load.txt", reason(h) is None, repr(reason(h)))
check("the re-exec marker is popped, not read", out.get("reexec_env_popped") is True)

h = home("receipt-missing-module")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(CHECKOUT)}}})
r = subprocess.run([REAL_PY, "-c", DRIVER.replace('"compaction"', '"no_such_module_here"'),
                    str(HOST_PY), "load"], env=env_for(h), capture_output=True, text=True, timeout=30)
check("a module the host does not provide is recorded as the host's gap, no re-exec",
      r.returncode == 0 and "does not provide what was asked" in (reason(h) or ""), repr(reason(h)))

# --- (d) end to end from the cache copy: the hook does its work ---------------
def transcript(name, readings):
    rows = [{"type": "user", "message": {"content": "go"}}]
    for i, tokens in enumerate(readings):
        rows.append({"type": "assistant", "message": {
            "id": f"msg_{i}",
            "usage": {"input_tokens": 12, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": tokens - 12},
            "content": [{"type": "text", "text": "working"}]}})
    path = TMP / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path

def ceiling(h, path, extra_env=None):
    payload = {"session_id": "sid-1", "hook_event_name": "PreToolUse",
               "transcript_path": str(path), "tool_name": "Bash",
               "tool_input": {"command": "ls"}}
    r = subprocess.run([REAL_PY, str(CEILING)], input=json.dumps(payload),
                       env=env_for(h, extra_env), capture_output=True, text=True, timeout=30)
    out = r.stdout.strip()
    note = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
    return r.returncode, note, r.stderr

heavy = transcript("heavy.jsonl", [50_000, 150_000, 165_000])

# The silent skip, reproduced: the cache copy under a HOME that knows no checkout.
h = home("e2e-silent")
code, note, err = ceiling(h, heavy)
check("with no checkout reachable the cache hook exits 0 and says nothing",
      code == 0 and note == "" and err == "", f"rc={code} note={note!r} err={err!r}")
check("…and names itself in the receipt",
      "\tcontext-ceiling.py\t" in (reason(h) or ""), repr(reason(h)))

# The fix: the same copy, the same payload, with the marketplace it was installed from.
h = home("e2e-claude")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(CHECKOUT)}}})
code, note, err = ceiling(h, heavy)
check("context-ceiling.py run FROM THE CACHE COPY speaks on a 160k crossing",
      code == 0 and "165,000" in note and "heavy band" in note, f"rc={code} note={note!r} err={err!r}")
check("…with nothing on stderr", err == "", err)
check("…and leaves no receipt", reason(h) is None, repr(reason(h)))
code, note, err = ceiling(h, transcript("light.jsonl", [50_000, 90_000]))
check("the cache hook is still silent when there is nothing to say", code == 0 and note == "")

h = home("e2e-codex")
codex_config(h, f'''
    [marketplaces.jStack]
    source_type = "local"
    source = "{CHECKOUT}"
''')
code, note, err = ceiling(h, heavy)
check("context-ceiling.py from the cache copy speaks through a Codex marketplace too",
      code == 0 and "165,000" in note, f"rc={code} note={note!r} err={err!r}")

# --- (e) a re-exec cannot loop ------------------------------------------------
h = home("reexec-marked")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(CHECKOUT)}}})
out = drive(h, "load-floor99", {"JSTACK_HOST_REEXEC": "1"})
check("a marked child below the floor stays alive and raises instead of exec'ing",
      out.get("alive") is True and "below requires-python" in out.get("error", ""), str(out))
check("…and records that it already re-executed once",
      "already re-executed once" in (reason(h) or ""), repr(reason(h)))
check("…and the marker was popped before anything it spawns could inherit it",
      out.get("reexec_env_popped") is True)

h = home("reexec-noven")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(CHECKOUT)}}})
has_venv = (CHECKOUT / "host" / ".venv" / "bin" / "python3").is_file()
out = drive(h, "load-floor99")
if has_venv:
    # The checkout under test carries a venv; the driver would exec into it and
    # the process it becomes is not ours to read. Cover the no-venv arm on a
    # fake checkout below instead.
    check("(venv present) an unmarked load below the floor does not return normally",
          "loaded" not in out, str(out))
else:
    check("no venv to re-exec under: stays alive, raises, records the missing interpreter",
          out.get("alive") is True and "below requires-python" in out.get("error", "")
          and "no interpreter to re-exec under" in (reason(h) or ""), f"{out} {reason(h)!r}")

out = drive(h, "load-floor99-noreexec")
check("reexec=False never execs: stays alive and records that the caller does not re-exec",
      out.get("alive") is True and "this caller does not re-exec" in (reason(h) or ""),
      f"{out} {reason(h)!r}")

# A real exec, exactly once: a fake checkout whose floor nothing satisfies and
# whose venv python is a counter in front of the real interpreter. The first
# process execs into it; the second, marked, must stop.
FAKE = TMP / "fake-checkout"
(FAKE / "host" / "jstack_host").mkdir(parents=True)
(FAKE / "host" / "jstack_host" / "__init__.py").write_text("")
(FAKE / "host" / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=99.0"\n')
(FAKE / "host" / ".venv" / "bin").mkdir(parents=True)
COUNT = TMP / "exec-count"
venv_py = FAKE / "host" / ".venv" / "bin" / "python3"
venv_py.write_text(f'#!/bin/sh\necho "$JSTACK_HOST_REEXEC $*" >> "{COUNT}"\nexec "{REAL_PY}" "$@"\n')
venv_py.chmod(0o755)

h = home("reexec-real")
claude_ledger(h, {"jStack": {"source": {"source": "directory", "path": str(FAKE)}}})
code, note, err = ceiling(h, heavy)
lines = COUNT.read_text().splitlines() if COUNT.exists() else []
check("a hook below the floor re-execs under the checkout venv exactly once",
      len(lines) == 1, f"venv python ran {len(lines)} times: {lines}")
check("the re-exec runs the same hook, marked",
      bool(lines) and lines[0].startswith("1 ") and lines[0].endswith(str(CEILING)), str(lines))
check("the marked child exits 0 silently rather than exec'ing again",
      code == 0 and note == "" and err == "", f"rc={code} note={note!r} err={err!r}")
# The wrapper execs the real interpreter, so the child's sys.executable is that
# interpreter, not the wrapper: the receipt names what actually ran second.
named = (reason(h) or "").rsplit("already re-executed once under ", 1)[-1].strip()
check("…and the receipt names the interpreter it was already re-executed under",
      named and same(named, REAL_PY), repr(reason(h)))

print()
if fails:
    print(f"host-resolve: {len(fails)} FAILED", file=sys.stderr)
    sys.exit(1)
print("host-resolve: all pass")
PY
