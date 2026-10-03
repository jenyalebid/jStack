"""What a managed Mac may do, as its hub said it — the leaf contract in one place.

The hub holds every word on its `hosts` row: the mode, the two visibility
switches, the Usage policy, whether a Headless client shows Agents, and the
line the leaf follows. The leaf caches what its hub last said and never
overrules it. The full contract is jStack-Project `docs/leaf.md`.

Headless does not overwrite the Standard switches — `resolve` reads past them,
so switching back restores them as they were.

The way a change arrives is `usage_reporting`'s, generalised: the row is the
authority, a poke makes the leaf pull now, and the pull rides the roster call
the leaf's client already makes, so a missed poke is "at the next pull" and
never a leaf enforcing a word its hub retired.
"""

from __future__ import annotations

import json
import threading

from . import hostenv, usage_reporting
#: The lines a leaf can follow — the parent's offers. Empty on the row means
#: home has not picked one and the leaf's own choice stands.
from .release_manifest import LINES

STANDARD = "standard"
HEADLESS = "headless"
MODES = (STANDARD, HEADLESS)


#: The leaf route a poke hits. Carries no payload — the machine pulls.
REFRESH_PATH = "/api/jremote/v1/leaf/refresh"

_STATE = hostenv.state_dir() / "jremote_leaf_policy.json"
_lock = threading.Lock()


class PolicyError(ValueError):
    """A value outside what the row may hold. Refused, never stored."""


class LocalLineRefused(PermissionError):
    """A Headless leaf asked to switch its own line."""


# ── the hub's half: the row ──────────────────────────────────────────────────

def normalise(fields: dict) -> dict:
    """Validate a partial policy write; returns only the fields given."""
    out: dict = {}
    if "mode" in fields:
        if fields["mode"] not in MODES:
            raise PolicyError(f"unknown mode {fields['mode']!r} — expected "
                              + " or ".join(MODES))
        out["mode"] = fields["mode"]
    for flag in ("agents_tab", "sees_home", "sees_leaves"):
        if flag in fields:
            if not isinstance(fields[flag], bool):
                raise PolicyError(f"{flag} is true or false")
            out[flag] = int(fields[flag])
    if "usage_reporting" in fields:
        try:
            out["usage_reporting"] = usage_reporting.normalise(fields["usage_reporting"])
        except usage_reporting.UnknownState as exc:
            raise PolicyError(str(exc)) from exc
    if "line" in fields:
        if fields["line"] not in LINES:
            raise PolicyError(f"unknown line {fields['line']!r} — expected "
                              + " or ".join(LINES))
        out["line"] = fields["line"]
    return out


def resolve(row: dict) -> dict:
    """What a leaf is actually subject to, given its row.

    The one place Headless is applied: everything that reads a leaf's reach
    or its client's word reads it through here, so no reader can forget it.
    """
    mode = row.get("mode") if row.get("mode") in MODES else STANDARD
    usage = row.get("usage_reporting") or usage_reporting.CLIENT
    if mode == HEADLESS:
        return {"mode": HEADLESS, "sees_home": False, "sees_leaves": False,
                "usage_reporting": usage_reporting.HIDDEN,
                "agents_tab": bool(row.get("agents_tab", 1)),
                "line": row.get("line") or ""}
    return {"mode": STANDARD, "sees_home": bool(row.get("sees_home", 1)),
            "sees_leaves": bool(row.get("sees_leaves", 1)),
            "usage_reporting": usage, "agents_tab": True,
            "line": row.get("line") or ""}


def console_view(row: dict) -> dict:
    """The row as home's Leaf Settings window reads it: the stored switches,
    not the resolved ones, so Standard's survive a trip through Headless."""
    return {"mode": row.get("mode") if row.get("mode") in MODES else STANDARD,
            "agents_tab": bool(row.get("agents_tab", 1)),
            "sees_home": bool(row.get("sees_home", 1)),
            "sees_leaves": bool(row.get("sees_leaves", 1)),
            "usage_reporting": row.get("usage_reporting") or usage_reporting.CLIENT,
            "line": row.get("line") or ""}


def poke(host_key: str, *, poster=None) -> dict:
    """Tell one leaf its policy changed. Graded, never raising — the row is
    already the authority, so an unreachable leaf catches up at its next pull.
    """
    from . import grants
    from .store import get_store
    step = {"step": f"leaf-refresh:{host_key}", "ok": False, "note": ""}
    row = get_store().host_row(host_key)
    if row is None or row["deleted"]:
        step["note"] = "unknown machine"
        return step
    try:
        access = grants.mint_on(dict(row), "Leaf policy refresh",
                               poster=poster, owner_id="host-internal")
        status, body = (poster or grants._httpx_post)(
            f"http://{access['address']}:{access['port']}{REFRESH_PATH}",
            {}, access["token"])
    except grants.GrantError as exc:
        step["note"] = f"{exc} — it applies the change at its next roster pull"
        return step
    if status == 404:
        # A leaf predating this route still pulls Usage through its own one.
        return usage_reporting.poke(host_key, poster=poster)
    if status != 200:
        step["note"] = (f"the machine answered {status}: "
                        f"{body.get('detail', 'no detail')} — it applies the "
                        "change at its next roster pull")
        return step
    step["ok"] = True
    step["note"] = f"applied {body.get('mode', '?')}"
    return step


# ── the leaf's half: the cache ───────────────────────────────────────────────

def _load() -> dict:
    try:
        d = json.loads(_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def cached() -> dict:
    """What this leaf's hub last said: mode and Agents tab."""
    d = _load()
    return {"mode": d.get("mode") if d.get("mode") in MODES else STANDARD,
            "agents_tab": d.get("agents_tab") is not False}


def is_headless() -> bool:
    from . import managed_access
    return managed_access.is_leaf() and cached()["mode"] == HEADLESS


def note_parent(answer: dict) -> dict:
    """Record the hub's word, straight off a `/managed/hosts` answer.

    Tolerant like `usage_reporting.note_parent`: a hub that predates a field
    says nothing, and nothing is Standard with the Agents tab on.
    """
    self_block = answer.get("self") or {}
    usage_reporting.note_parent(self_block.get("usage_reporting"))
    mode = self_block.get("mode") if self_block.get("mode") in MODES else STANDARD
    agents_tab = self_block.get("agents_tab") is not False
    want = {"mode": mode, "agents_tab": agents_tab}
    with _lock:
        if {k: _load().get(k) for k in want} != want:
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            _STATE.write_text(json.dumps(want, indent=2, sort_keys=True))
    line = self_block.get("line")
    if line in LINES:
        _follow(line)
    return want


def _follow(line: str) -> None:
    """Point this machine's updater at home's line. Only on a change, and only
    where updates are set up — a leaf without an updater has nothing to point.
    """
    from . import build_source
    from .update_supervisor import atomic_json
    path = hostenv.state_dir() / "updates" / "config.json"
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if build_source.channel_ref(config) != line:
        atomic_json(path, {**config, "channel": line})


def refuse_local_line() -> None:
    """The one gate every local line switch passes: a Headless leaf's line is
    home's alone."""
    if is_headless():
        raise LocalLineRefused("this Mac's line is set by its hub")


def host_block() -> dict | None:
    """`/host`'s `leaf` block for the client running on this Mac, or None on a
    machine nobody manages."""
    from . import managed_access
    return cached() if managed_access.is_leaf() else None
