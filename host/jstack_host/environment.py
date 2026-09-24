"""Session environment — how a session WORKS, not what it works on.

The same few preferences get re-stated at the top of every sitting: deliver by
distribute or by build, run the simulator journey or skip it, one agent or one
per plan stage. Said once here, per session or per agent, announced by the
hooks from then on.

A value equal to its default announces NOTHING: `instruction[default]` is `""`
and every renderer below drops it. That is the safety property the module is
built around — where nobody has set anything, not one word is injected into any
session, so this cannot degrade a session that never asked for it.

There is deliberately no applicability rule, no session-type gate. An infra
session grows a UI task halfway through, and a static answer to "does this
session have a simulator" is wrong from that moment on. A setting nothing
triggers simply never comes up.
"""

import time
from dataclasses import dataclass

from . import store


@dataclass(frozen=True)
class Trigger:
    """Where a setting's instruction is worth repeating mid-session.

    Declared here and read by the hook, so the moment a setting is about to be
    ignored is a property of the setting and not a regex living in a hook file
    that nobody edits when a setting changes.

    `pattern`'s meaning follows `tool`, one field rather than two so a
    descriptor cannot carry a command regex and a path glob at once: against
    Bash it is a regex matched on the command line, against an edit tool a
    path glob matched on the file. Empty matches anything the event fires on.
    """

    event: str
    tool: str = ""
    pattern: str = ""


@dataclass(frozen=True)
class Setting:
    """One fact about how this session runs.

    `values` are the only storable strings and everything is stored as TEXT —
    a bool is `"on"`/`"off"` like any other two-member enum, so the layer walk,
    the app's picker and the SQL all have exactly one shape to handle.

    `instruction` maps value → the single sentence announced while that value
    is in force, and MUST map `default` to `""`.
    """

    key: str
    kind: str
    values: tuple[str, ...]
    default: str
    label: str
    triggers: tuple[Trigger, ...]
    instruction: dict[str, str]


#: Every setting this host knows. Registry order is render order, so a line the
#: user reads twice reads the same way twice.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="delivery_method",
        kind="enum",
        values=("distribute", "testflight", "build", "sim_demo", "none"),
        default="none",
        label="Delivery",
        triggers=(
            Trigger("PreToolUse", "Bash",
                    r"git commit|git push|release\.sh|distribute"),
            Trigger("Stop"),
        ),
        instruction={
            "distribute": "When the work is done, ship it through this project's distribute path without being asked.",
            "testflight": "When the work is done, archive it and upload it to TestFlight without being asked.",
            "build": "When the work is done, build it and install it on the device without being asked.",
            "sim_demo": "When the work is done, run it in the simulator and show the result without being asked.",
            "none": "",
        },
    ),
    Setting(
        key="sim_verify",
        kind="bool",
        values=("on", "off"),
        default="on",
        label="Simulator verification",
        triggers=(
            Trigger("PreToolUse", "Bash", r"xcodebuild|simctl|xcrun"),
            Trigger("PostToolUse", "Edit|Write", "**/*.swift"),
        ),
        instruction={
            "on": "",
            "off": "Skip the simulator verification journey on this work — the user reviews this change themselves.",
        },
    ),
    Setting(
        key="use_subagents",
        kind="bool",
        values=("on", "off"),
        default="off",
        label="Subagents per stage",
        triggers=(
            Trigger("PreToolUse", "ExitPlanMode"),
            Trigger("PreToolUse", "Agent"),
        ),
        instruction={
            "on": "Dispatch one subagent per plan stage and have it verify its own stage before reporting.",
            "off": "",
        },
    ),
)

_SESSION = ("session_env", "session_id")
_AGENT = ("agent_env", "agent_id")


def setting(key: str) -> Setting | None:
    for s in SETTINGS:
        if s.key == key:
            return s
    return None


def _target(session_id: str | None, agent_id: str | None) -> tuple[str, str, str]:
    """Which table one call writes to, refusing anything ambiguous.

    A call naming both layers has no single answer and a call naming neither
    would write a row owned by `''` — inherited by every session whose id is
    also empty, which on a store this size is a setting that appears to have
    been set globally and cannot be found from any screen.
    """
    if bool(session_id) == bool(agent_id):
        raise ValueError("name exactly one of session_id / agent_id")
    table, column = _SESSION if session_id else _AGENT
    return table, column, session_id or agent_id or ""


def get(key: str, *, session_id: str | None = None,
        agent_id: str | None = None) -> str:
    """The value stored at ONE layer, `""` if that layer is silent.

    No walk and no default: this is what an editor screen needs to show a
    session's own value as set-here rather than as inherited. `resolve` is the
    one that answers what is actually in force.

    Unusable values read as silence, exactly as `_layer` treats them — a value
    outside the setting's current `values` is one no instruction exists for any
    more, and a layer holding one has nothing to say. Filtering in only one of
    the two readers is the real hazard: they would answer differently about the
    same row, and every caller would have to know which one it was holding.
    """
    table, column, owner = _target(session_id, agent_id)
    with store.get_store().conn() as db:
        row = db.execute(
            f"SELECT value FROM {table} WHERE {column}=? AND key=?",
            (owner, key)).fetchone()
    if not row:
        return ""
    s = setting(key)
    return row["value"] if s is not None and row["value"] in s.values else ""


def set_value(key: str, value: str | None, *, session_id: str | None = None,
              agent_id: str | None = None) -> None:
    """Store a value at one layer, or clear it with `None`/`""`.

    Clearing DELETES rather than writing an empty string, because the layer
    walk reads presence: a row holding `''` would be a session pinned to
    nothing, shadowing the agent default it was meant to hand back to.

    An unknown key or a value outside `values` raises before the connection is
    opened. Both are the same failure — a picker in the app that stores
    cleanly, answers 200, and changes nothing any session will ever read.
    """
    s = setting(key)
    if s is None:
        raise ValueError(f"unknown setting: {key!r}")
    if value and value not in s.values:
        raise ValueError(f"{key}: {value!r} not one of {s.values}")
    table, column, owner = _target(session_id, agent_id)
    with store.get_store().conn() as db:
        if not value:
            db.execute(f"DELETE FROM {table} WHERE {column}=? AND key=?",
                       (owner, key))
            return
        db.execute(
            f"INSERT INTO {table} ({column}, key, value, updated_at)"
            f" VALUES (?,?,?,?)"
            f" ON CONFLICT({column}, key) DO UPDATE SET"
            f" value=excluded.value, updated_at=excluded.updated_at",
            (owner, key, value, time.time()))


def _layer(db, table: str, column: str, owner: str) -> dict[str, str]:
    """One layer's rows, with anything unusable dropped rather than returned.

    A value outside the setting's current `values` is one an instruction no
    longer exists for — the residue of an enum that lost a member while rows
    holding it stayed. Left in, it would reach `state_line` and advertise a
    mode to the model that nothing in the registry can act on.
    """
    if not owner:
        return {}
    rows = db.execute(f"SELECT key, value FROM {table} WHERE {column}=?",
                      (owner,)).fetchall()
    out = {}
    for r in rows:
        s = setting(r["key"])
        if s is not None and r["value"] in s.values:
            out[r["key"]] = r["value"]
    return out


def resolve(session_id: str, agent_id: str = "") -> dict[str, tuple[str, str]]:
    """`{key: (value, source)}` for EVERY setting, session → agent → default.

    Every setting, always, so a caller cannot read a missing key as an absent
    feature; `source` is what lets a screen render an inherited value as
    inherited instead of as one somebody set on this sitting.

    An unknown `session_id` is not an error — hooks fire on sessions the index
    has not caught up with, and the honest answer for one is the agent layer
    skipped and the defaults returned, never a raise inside a hook.
    """
    with store.get_store().conn() as db:
        if not agent_id and session_id:
            row = db.execute("SELECT agent_id FROM sessions WHERE session_id=?",
                             (session_id,)).fetchone()
            agent_id = (row["agent_id"] if row else "") or ""
        session = _layer(db, *_SESSION, session_id)
        agent = _layer(db, *_AGENT, agent_id)
    out: dict[str, tuple[str, str]] = {}
    for s in SETTINGS:
        if s.key in session:
            out[s.key] = (session[s.key], "session")
        elif s.key in agent:
            out[s.key] = (agent[s.key], "agent")
        else:
            out[s.key] = (s.default, "default")
    return out


def announce(key: str, value: str) -> str:
    """The sentence for a value in force — `""` for a default or an unknown."""
    s = setting(key)
    return s.instruction.get(value, "") if s else ""


def state_line(resolved: dict[str, tuple[str, str]]) -> str:
    """The compact entry form, or `""` when nothing has been moved.

    Non-defaults only, and the empty string is the point: a session on an
    untouched machine gets no line, not a line saying everything is normal.
    """
    parts = [f"{s.key}={resolved[s.key][0]}" for s in SETTINGS
             if s.key in resolved and resolved[s.key][0] != s.default]
    return "SESSION ENVIRONMENT: " + " · ".join(parts) if parts else ""


def delta_lines(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """One line per setting that moved mid-session, in registry order.

    A side that omits a key reads as that key's default, because the natural
    `before` for a caller is a capture of the non-defaults it already knows
    about — requiring a complete dict would turn the first setting ever set in
    a session into a silent change.
    """
    lines = []
    for s in SETTINGS:
        was = before.get(s.key, s.default)
        now = after.get(s.key, s.default)
        if was == now or now not in s.values:
            continue
        name = s.key.upper().replace("_", " ")
        lines.append(f"{name} TURNED {now.upper()}" if s.kind == "bool"
                     else f"{name} SWITCHED: {now.upper()}")
    return lines


def prefs() -> list[dict]:
    """What the app's settings screen renders — the registry, as data."""
    return [{"key": s.key, "label": s.label, "kind": s.kind,
             "values": list(s.values), "default": s.default} for s in SETTINGS]
