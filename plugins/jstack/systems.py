"""systems — a host's registry and jStack's own, read as one org registry.

jStack's `systems.json` is the base. A host row under a base id is an overlay:
what only the host knows about its install of that system. The union is one
row per id; this is where every host gets that answer, so none computes its own.

An overlay adds and never restates. A field the base sets keeps the base's
value and a differing overlay value is reported as a clash, never applied; a
field the base leaves empty is the host's to fill. `subsystems` is the one
field both contribute to: the host's components hang after the base's.

Stdlib only, nothing from this package — hosts load this file by path, as
they load root.py.
"""

from __future__ import annotations

#: The field both halves contribute to. Every other field has one owner.
NESTED = "subsystems"


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def overlay(base: dict, host: dict) -> tuple[dict, list[str]]:
    """`base` with `host`'s additions, and the fields `host` tried to restate.

    Neither input is mutated. The host's components are appended after the
    base's, each as the host wrote it.
    """
    merged = dict(base)
    clashes: list[str] = []
    for key, value in host.items():
        if key == "id":
            continue
        if key == NESTED:
            merged[NESTED] = list(base.get(NESTED) or []) + list(value or [])
            continue
        if key not in base or _empty(base[key]):
            merged[key] = value
        elif base[key] != value:
            clashes.append(key)
    return merged, clashes


def union(host_rows: list[dict], base_rows: list[dict]) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """(host rows that stand alone, base rows with overlays applied, clashes).

    A host row whose id names a base row is that row's overlay and is consumed
    by it; every other host row is the host's own system. Clashes map an id to
    the fields its overlay tried to restate.
    """
    hosted = {row.get("id"): row for row in host_rows or []}
    base_ids = {row.get("id") for row in base_rows or []}
    merged: list[dict] = []
    clashes: dict[str, list[str]] = {}
    for row in base_rows or []:
        extra = hosted.get(row.get("id"))
        if extra is None:
            merged.append(dict(row))
            continue
        out, bad = overlay(row, extra)
        merged.append(out)
        if bad:
            clashes[row.get("id")] = bad
    own = [dict(row) for row in host_rows or [] if row.get("id") not in base_ids]
    return own, merged, clashes
