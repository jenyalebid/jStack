#!/usr/bin/env bash
# jStack live test — is the tree still the shape structure.json declares?
#
# structure.json is the CLOSED list of what this repo ships at its two roots
# (repo top, plugin top). This test derives both listings FROM GIT — tracked
# entries are what a clone gets — and compares each against the manifest in
# both directions: a tracked entry the manifest does not name is an unmanaged
# addition, and a manifest entry with nothing tracked behind it is a stale
# claim. Untracked files are a session's work-in-progress, invisible to every
# consumer of the repo, so they are out of scope — the same split manifest.sh
# draws.
#
# Exit 0 = both roots match the manifest. Exit 1 = at least one drift, or the
# tree could not be read (a check that cannot look must fail, never pass).

set -u

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_ROOT/../.." && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL: python3 not on PATH" >&2
  exit 1
fi

python3 - "$PLUGIN_ROOT" "$REPO_ROOT" <<'PY'
import json, os, subprocess, sys

plugin_root, repo_root = sys.argv[1], sys.argv[2]
fails = []

def ok(label, detail=""):
    print(f"  ok   {label}" + (f" — {detail}" if detail else ""))

def bad(label, detail):
    fails.append(label)
    print(f"  FAIL {label} — {detail}")

manifest_path = os.path.join(plugin_root, "structure.json")
try:
    with open(manifest_path) as fh:
        manifest = json.load(fh)
except Exception as exc:
    print(f"  FAIL structure.json — unreadable: {exc}")
    print("\nFAIL — the manifest could not be read; nothing was checked.")
    sys.exit(1)

def tracked_top(subdir=None):
    """First path segment of every git-tracked file, optionally under subdir."""
    args = ["git", "-C", repo_root, "ls-files", "-z"]
    if subdir:
        args += ["--", subdir]
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "git ls-files failed")
    strip = f"{subdir}/" if subdir else ""
    tops = set()
    for path in res.stdout.split("\0"):
        if not path:
            continue
        if strip:
            path = path[len(strip):]
        tops.add(path.split("/", 1)[0])
    return tops

try:
    live = {
        "repo_root": tracked_top(),
        "plugin_root": tracked_top("plugins/jstack"),
    }
except Exception as exc:
    print(f"  FAIL git tracking — cannot list tracked files: {exc}")
    print("\nFAIL — the tree could not be read; nothing was checked.")
    sys.exit(1)

for section in ("repo_root", "plugin_root"):
    print(section)
    declared = manifest.get(section)
    if not isinstance(declared, dict) or not declared:
        bad(f"{section} manifest section", "missing or empty in structure.json")
        continue
    on_disk = live[section]
    unmanaged = sorted(on_disk - set(declared))
    stale = sorted(set(declared) - on_disk)
    if unmanaged:
        bad(f"{section} unmanaged",
            f"tracked, absent from structure.json: {', '.join(unmanaged)}"
            " — declare each with a reason in the same commit, or remove it")
    if stale:
        bad(f"{section} stale",
            f"named in structure.json, nothing tracked: {', '.join(stale)}")
    if not unmanaged and not stale:
        ok(f"{section} closed", f"{len(on_disk)} entries, all declared")

print()
if fails:
    print(f"FAIL — structure drift: {'; '.join(fails)}")
    sys.exit(1)
print("PASS — the tree matches structure.json at both roots.")
PY
