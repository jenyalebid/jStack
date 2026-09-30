"""What each CLI offers today, and which roster rows have fallen behind.

`engines.py` pins model ids because an alias (`--model opus`) resolves to
whatever the vendor moved it to this morning, records nothing, and can change
between a session opening and its next turn. The cost of pinning is that a
pinned id goes stale in silence: nothing errors, the picker simply keeps
offering last season's models and every unattended spawn runs one. A CLI's own
default is no safer — it moves without telling anyone, and half our spawns
name a model anyway.

So the ids are checked by machine instead. This module asks each CLI what it
has RIGHT NOW and compares that to the roster:

* **Claude** has no catalogue command, but its family aliases always point at
  the current model, so resolving `opus`/`sonnet`/`fable`/`haiku` yields the
  four ids that should be listed. The resolved id is read out of the run's own
  `modelUsage`, which is the model that actually answered, not a string the
  CLI was willing to accept.
* **Codex** publishes `codex debug models`. The list-visible rows carry the
  vendor's own `priority`, so "the top N" is the vendor's ordering, not ours.

`drift()` returns one line per divergence and nothing when the roster is
current; a probe that cannot reach its CLI raises `ProbeError` rather than
returning "no drift", because an unanswerable question is not a clean bill.

Run it: `python3 -m jstack_host.modelprobe [--json]` — exit 1 on drift, 2 when
a probe could not run.
"""

import argparse
import json
import subprocess
import sys
import tempfile

from . import engines

#: One per Claude model family. These are what the roster's claude ids must be.
CLAUDE_ALIASES = ("opus", "sonnet", "fable", "haiku")


class ProbeError(RuntimeError):
    """A CLI could not be asked. Never the same thing as "nothing changed"."""


def _run(argv, timeout, cwd=None, runner=subprocess.run):
    try:
        return runner(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except Exception as exc:                       # missing binary, timeout, …
        raise ProbeError(f"{argv[0]}: {exc}") from exc


def claude_latest(runner=subprocess.run, timeout=180) -> dict:
    """`{alias: model id}` for each family, from a real turn on that alias.

    Run from an empty directory: a probe that inherits a project's CLAUDE.md
    walk-up pays for context it does not read and fires that tree's hooks.
    """
    out = {}
    with tempfile.TemporaryDirectory() as neutral:
        for alias in CLAUDE_ALIASES:
            proc = _run(["claude", "-p", "--model", alias,
                         "--output-format", "json", "ok"],
                        timeout=timeout, cwd=neutral, runner=runner)
            if proc.returncode != 0:
                detail = " ".join(((proc.stderr or proc.stdout) or "").split())[:180]
                raise ProbeError(f"claude --model {alias}: "
                                 f"{detail or f'exit {proc.returncode}'}")
            try:
                used = list((json.loads(proc.stdout or "{}").get("modelUsage")
                             or {}).keys())
            except json.JSONDecodeError as exc:
                raise ProbeError(f"claude --model {alias}: unparseable json ({exc})")
            if len(used) != 1:
                raise ProbeError(f"claude --model {alias}: {len(used)} models "
                                 f"answered one turn ({', '.join(used) or 'none'})")
            out[alias] = used[0]
    return out


def codex_catalog(runner=subprocess.run, timeout=120) -> list:
    """The list-visible Codex models, best `priority` first.

    Hidden rows (`gpt-reserve`, the auto-review model) are dropped: they are
    not models anyone picks, and offering one in a picker is a support call.
    """
    proc = _run(["codex", "debug", "models"], timeout=timeout, runner=runner)
    if proc.returncode != 0:
        detail = " ".join(((proc.stderr or proc.stdout) or "").split())[:180]
        raise ProbeError(f"codex debug models: {detail or f'exit {proc.returncode}'}")
    try:
        models = json.loads(proc.stdout or "{}").get("models")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"codex debug models: unparseable json ({exc})")
    if not models:
        raise ProbeError("codex debug models: empty catalog")
    listed = [m for m in models if m.get("visibility") == "list"]
    if not listed:
        raise ProbeError("codex debug models: catalog has no list-visible rows")
    return sorted(listed, key=lambda m: (m.get("priority", 1_000), m.get("slug", "")))


def drift(claude_ids: dict, codex_models: list, roster=None) -> list:
    """One line per divergence between the roster and what the CLIs offer."""
    roster = roster if roster is not None else engines.ENGINES
    by_id = {e["id"]: e for e in roster}
    lines = []

    claude = by_id.get("claude")
    if claude:
        want = [claude_ids[a] for a in CLAUDE_ALIASES if a in claude_ids]
        have = [m["id"] for m in claude["models"]]
        for alias in CLAUDE_ALIASES:
            current = claude_ids.get(alias)
            if current and current not in have:
                lines.append(f"claude: `{alias}` is now {current}, not in the roster")
        for stale in [i for i in have if i not in want]:
            lines.append(f"claude: {stale} is listed but no family alias "
                         f"resolves to it")
        opus = claude_ids.get("opus")
        if opus and claude["default_model"] != opus:
            lines.append(f"claude: default is {claude['default_model']}, "
                         f"latest Opus is {opus}")

    codex = by_id.get("codex")
    if codex:
        have = [m["id"] for m in codex["models"]]
        top = [m.get("slug") for m in codex_models[:len(have)]]
        for slug in [s for s in top if s not in have]:
            lines.append(f"codex: {slug} is in the catalog's top {len(have)} "
                         f"and not in the roster")
        catalog = {m.get("slug") for m in codex_models}
        for stale in [i for i in have if i not in catalog]:
            lines.append(f"codex: {stale} is listed and the catalog no longer "
                         f"offers it")
        if codex["default_model"] not in catalog:
            lines.append(f"codex: default {codex['default_model']} is not in "
                         f"the catalog")

    return lines


def report(runner=subprocess.run) -> dict:
    claude_ids = claude_latest(runner=runner)
    codex_models = codex_catalog(runner=runner)
    return {"claude": claude_ids,
            "codex": [m.get("slug") for m in codex_models],
            "drift": drift(claude_ids, codex_models)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        r = report()
    except ProbeError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        for alias, mid in r["claude"].items():
            print(f"claude {alias:<7} → {mid}")
        print("codex catalog: " + ", ".join(r["codex"]))
        print("\n".join(r["drift"]) if r["drift"] else "roster is current")
    return 1 if r["drift"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
