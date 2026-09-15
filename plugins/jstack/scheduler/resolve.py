"""Effective-settings resolver — the run-category override chain.

A job's effective value for an inherited setting (model, timeout_seconds, …)
resolves through three layers, most-specific first:

    per-job value  →  its category's default  →  global default

This is the single source of truth for "what settings does this run actually
use." The runner, the dashboard, and the ics feed all go through here so the
answer is identical everywhere. Categories let an operator set model / time-limit
once for a whole class of runs (reviews, replies, pipeline, …) while any single
job can still override inline.

Config shape (config/schedule.json):

    "defaults":   { "model": "opus", "timeout_seconds": 1800, ... },
    "categories": { "reviews": { "model": "sonnet", "timeout_seconds": 2700 }, ... },
    "jobs": [ { ..., "category": "reviews", "model": "opus[1m]" (optional override) } ]

A job with no `category`, or a category absent from the map, simply skips the
middle layer and resolves job → defaults (fully backward-compatible).
"""

# Settings that follow the inheritance chain. Mirrors registry._INHERITED_KEYS;
# kept here to avoid a circular import (registry imports nothing from resolve).
INHERITED_KEYS = (
    "engine",
    "codex_model",
    "codex_bin",
    "model",
    "timeout_seconds",
    "stall_timeout_seconds",
    "ttft_timeout_seconds",
    "claude_bin",
    "catch_up_grace_seconds",
    "permission_mode",
    "notify_on_failure",
)


def _present(value) -> bool:
    """A layer 'provides' a value only if it's truthy. Mirrors the runner's
    historical `job.get(k) or defaults[k]` semantics exactly — 0 / "" / None all
    fall through to the next layer (a 0-second timeout was never a real value)."""
    return bool(value)


def resolve_setting(job: dict, key: str, defaults: dict,
                    categories: "dict | None" = None):
    """Effective value of `key` for `job`: job → category → default.

    Returns None only if no layer provides the key."""
    if _present(job.get(key)):
        return job.get(key)
    cat = (categories or {}).get(job.get("category") or "")
    if cat and _present(cat.get(key)):
        return cat.get(key)
    return defaults.get(key)


def effective_settings(job: dict, defaults: dict,
                       categories: "dict | None" = None) -> dict:
    """The resolved value of every inherited key for `job`."""
    return {k: resolve_setting(job, k, defaults, categories) for k in INHERITED_KEYS}


def resolution_trace(job: dict, key: str, defaults: dict,
                     categories: "dict | None" = None) -> dict:
    """Which layer supplied the effective value — for the dashboard to show
    'inherited from category' vs 'job override' vs 'global default'."""
    if _present(job.get(key)):
        return {"value": job.get(key), "source": "job"}
    cat_name = job.get("category") or ""
    cat = (categories or {}).get(cat_name)
    if cat and _present(cat.get(key)):
        return {"value": cat.get(key), "source": "category", "category": cat_name}
    return {"value": defaults.get(key), "source": "default"}
