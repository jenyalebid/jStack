#!/usr/bin/env bash
# jStack live test — the org registry's merge, and this half's own rows.
#
# The merge is what a host reads every registry question through, so its rule
# is held here rather than in any one host: an overlay adds and never restates,
# a clash keeps the base's value and is named, components from both halves
# survive, and the union carries one row per id. The base half is read too —
# two rows under one id INSIDE jStack is the same defect one level down.
#
# Exit 0 = all pass, exit 1 = any fail.

set -u
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${JSTACK_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "FAIL: no python3 on PATH (set JSTACK_PYTHON)"; exit 1; }
export PYTHONDONTWRITEBYTECODE=1

"$PY" - "$PLUGIN_ROOT" <<'PY'
import importlib.util, json, os, sys
from collections import Counter

plugin_root = sys.argv[1]
spec = importlib.util.spec_from_file_location("jstack_systems", os.path.join(plugin_root, "systems.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fails = 0
def check(cond, what):
    global fails
    print(("ok: " if cond else "FAIL: ") + what)
    fails += 0 if cond else 1

base = [
    {"id": "pkg", "name": "Pkg", "slug": None, "code": ["pkg/"],
     "test": {"type": "script", "path": "tests/pkg.sh"},
     "subsystems": [{"id": "pkg-part", "test": {"type": "script", "path": "tests/part.sh"}}]},
    {"id": "other", "name": "Other"},
]
host = [
    {"id": "pkg", "slug": "pkg", "dashboard_route": "/pkg",
     "subsystems": [{"id": "pkg-host", "test": {"type": "endpoint"}}]},
    {"id": "own", "name": "Own"},
]
frozen = json.dumps([base, host], sort_keys=True)
own, merged, clashes = mod.union(host, base)
row = next(r for r in merged if r["id"] == "pkg")

check(json.dumps([base, host], sort_keys=True) == frozen, "inputs are not mutated")
check([r["id"] for r in own] == ["own"], "an overlay is consumed by its base row, a host system stands alone")
ids = [r["id"] for r in own + merged]
check(len(ids) == len(set(ids)) == 3, "one row per id")
check(row["slug"] == "pkg", "an empty base field is the host's to fill")
check(row["dashboard_route"] == "/pkg", "a field the base lacks is added")
check(row["test"]["type"] == "script", "the base's test is the row's test")
check([s["id"] for s in row["subsystems"]] == ["pkg-part", "pkg-host"],
      "components from both halves survive, base first")
check(clashes == {}, "an overlay that only adds reports no clash")

_, merged, clashes = mod.union([{"id": "pkg", "name": "Forked", "code": ["pkg/"]}], base)
row = next(r for r in merged if r["id"] == "pkg")
check(row["name"] == "Pkg", "a restated field keeps the base's value")
check(clashes == {"pkg": ["name"]}, "a differing restatement is named; an equal one is not")

with open(os.path.join(plugin_root, "systems.json")) as fh:
    rows = json.load(fh)["systems"]
def flat(rs):
    for r in rs or []:
        yield r
        yield from flat(r.get("subsystems"))
dup = sorted(i for i, n in Counter(r.get("id") for r in flat(rows)).items() if n > 1)
check(dup == [], f"jStack's own half holds one row per id{': ' + ', '.join(dup) if dup else ''}")

sys.exit(1 if fails else 0)
PY
