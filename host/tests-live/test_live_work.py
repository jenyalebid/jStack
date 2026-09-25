"""The work harness against a real host: the env layers, the Work view, plans.

Every route here is guarded by a *store migration* rather than by an import —
`_probe("session_env")` and `_probe("work")` are `SELECT 1` against five tables
each — so the thing only a live host can tell you is which side of that
migration the machine actually opened. An in-process test builds the schema it
then asserts on; a guest that installed from a checkout either has the columns
or does not, and both answers have to be legible.

**The plan routes are read-only, all four of them.** `plans.open_plan` has one
caller in the whole package and it is `cli.py` — nothing on the wire creates a
plan, so a suite calling from another machine cannot make one exist. So the
plan tests assert the refusal (an id no plan has → 404) unconditionally, and
deepen onto a real plan whenever the host has one: a guest seeded with a plan
gets the detail and the document checked, a bare one still proves both routes
refuse cleanly. Seeding a plan is `scripts/live-vm-test.sh`'s job, the way it
already seeds the agent tree.

**Nothing here is left behind.** The env writes go to a session id no session
has, and every one of them is cleared again — a cleared row is DELETEd, so the
table is left as it was found. The agent-layer write is restored to whatever
the agent had, which is not the same as cleared: an agent that had a default
must keep it.
"""

from __future__ import annotations

import uuid

import pytest

from conftest import BASE_URL

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]

#: Every mode `/sessions/{sid}/work` may answer. Duplicated from
#: `router.WORK_MODES` on purpose: the suite calls over HTTP and must not
#: import the host it is testing, or it would assert the tuple it just loaded
#: rather than the one the guest is serving.
WORK_MODES = ("none", "planning", "stages", "unavailable")


@pytest.fixture
def scratch_sid() -> str:
    """A session id shaped like a real one, belonging to no session.

    `_check_sid` only admits `[0-9a-f-]{32,40}`, so a readable name is a 400
    and tests nothing. An unindexed id is deliberately NOT an error on these
    routes — `environment.resolve` answers defaults for one, because a session
    can be live before the index catches up — so this is the honest subject for
    the layer tests and costs no real session.
    """
    return str(uuid.uuid4())


def _rows(payload: dict) -> list[dict]:
    assert isinstance(payload, dict), payload
    return payload.get("env", [])


def _row(payload: dict, key: str) -> dict:
    row = next((r for r in _rows(payload) if r.get("key") == key), None)
    assert row is not None, f"no {key!r} row in {payload}"
    return row


def _a_settable_row(payload: dict) -> dict:
    """A setting with a value other than the one in force to move it to.

    Read off the host's own registry rather than hardcoded: the enum is the
    guest's, and a test naming a key this build no longer ships would fail as
    a defect in the host instead of as a stale test.
    """
    for row in _rows(payload):
        if len([v for v in row.get("values", []) if v != row.get("value")]):
            return row
    pytest.skip(f"this host's registry has no setting with a second value: "
                f"{[r.get('key') for r in _rows(payload)]}")


# ── the two env layers ──

def test_the_session_env_names_every_setting_and_whose_value_it_is(api, scratch_sid):
    """`source` is the contract, not the value.

    A flat `{key: value}` cannot tell an inherited setting from one set on this
    sitting, so the screen cannot draw "clear this" honestly. And every setting
    must appear: a key missing from the list reads to the app as a host that
    does not have the feature, which is the sentence `available: false` exists
    to say and must not be said by accident.
    """
    payload = api.ok("GET", "/sessions/{sid}/env", fmt={"sid": scratch_sid})
    assert isinstance(payload, dict), payload
    if not payload.get("available", True):
        assert payload.get("reason"), payload
        assert payload.get("env") == [], (
            f"a host with no session environment served rows anyway: {payload}")
        pytest.skip("this host's store has no session_env/agent_env tables")

    for row in _rows(payload):
        assert row.get("key"), row
        assert "value" in row and "source" in row, row
        assert row["source"] in ("session", "agent", "default"), row
        assert isinstance(row.get("announced"), bool), (
            f"announced is {row.get('announced')!r}, not a bool — the app "
            f"branches on it: {row}")


def test_a_session_setting_is_set_then_cleared(api, scratch_sid):
    """The picker's whole round trip, and the reason the POST answers the list.

    The response is the stored truth, not an echo: a cleared row's new value is
    the agent's or the registry's, and the client neither knows it nor should
    have to compute it to redraw itself. So the assertions are on what comes
    back from the write, and then on what a fresh GET says — a write that only
    convinces its own response is a setting that vanishes on the next open.
    """
    before = api.ok("GET", "/sessions/{sid}/env", fmt={"sid": scratch_sid})
    if not before.get("available", True):
        # Called on the unavailable path too, never skipped around: the write
        # route's only behaviour on such a host is its refusal, and skipping
        # it would leave POST /sessions/{sid}/env exercised by nothing.
        refused = api.post("/sessions/{sid}/env", fmt={"sid": scratch_sid},
                           json={"key": "x", "value": None})
        assert refused.status_code == 503, (
            f"host says it has no session environment but accepts a write: "
            f"{refused.status_code} {refused.text[:200]}")
        pytest.skip("no session environment on this host — both routes were "
                    "still exercised")

    row = _a_settable_row(before)
    key = row["key"]
    target = next(v for v in row["values"] if v != row["value"])

    written = api.ok("POST", "/sessions/{sid}/env", fmt={"sid": scratch_sid},
                     json={"key": key, "value": target})
    after = _row(written, key)
    assert after["value"] == target, f"write did not take: {after}"
    assert after["source"] == "session", (
        f"a value set on this session reports source {after['source']!r} — the "
        f"screen would draw it as inherited and offer no way to clear it")

    reread = _row(api.ok("GET", "/sessions/{sid}/env", fmt={"sid": scratch_sid}), key)
    assert reread["value"] == target, f"value did not survive a reread: {reread}"

    cleared = _row(api.ok("POST", "/sessions/{sid}/env", fmt={"sid": scratch_sid},
                          json={"key": key, "value": None}), key)
    assert cleared["source"] != "session", (
        f"clearing left the session layer in place: {cleared} — a row holding "
        f"nothing shadows the default it was meant to hand back to")
    assert cleared["value"] == row["value"], (
        f"clearing landed on {cleared['value']!r}, not the {row['value']!r} "
        f"that was in force before: {cleared}")


def test_a_setting_the_registry_does_not_have_is_refused(api, scratch_sid):
    """400 and never 200. An unknown key stored cleanly is a picker that has
    drifted from this host's registry and changes nothing any session reads —
    the failure that answers success."""
    r = api.post("/sessions/{sid}/env", fmt={"sid": scratch_sid},
                 json={"key": f"not-a-setting-{uuid.uuid4().hex[:6]}",
                       "value": "whatever"})
    assert r.status_code in (400, 503), (
        f"host stored a setting its registry does not have ({r.status_code}) "
        f"{r.text[:200]}")


def test_the_agent_layer_is_read_written_and_restored(api, agent_id):
    """The layer every session of an agent inherits.

    Restored rather than cleared: this is a real agent on a real host, and an
    agent that already had a default must still have it after the suite runs.
    Clearing would be the tidy-looking write that silently changes how every
    future session of that agent starts.
    """
    before = api.ok("GET", "/agents/{agent_id}/env", fmt={"agent_id": agent_id})
    if not before.get("available", True):
        refused = api.post("/agents/{agent_id}/env", fmt={"agent_id": agent_id},
                           json={"key": "x", "value": None})
        assert refused.status_code == 503, (
            f"host says it has no agent environment but accepts a write: "
            f"{refused.status_code} {refused.text[:200]}")
        pytest.skip("no agent environment on this host — both routes were "
                    "still exercised")

    for row in _rows(before):
        assert row["source"] in ("agent", "default"), (
            f"nothing sits above the agent layer but the registry, so "
            f"{row['key']!r} cannot have source {row['source']!r}: {row}")
        assert row.get("announced") is False, (
            f"an agent value is what the next session inherits, not something "
            f"any session has been told: {row}")

    row = _a_settable_row(before)
    key, was = row["key"], row["value"]
    target = next(v for v in row["values"] if v != was)
    try:
        written = _row(api.ok("POST", "/agents/{agent_id}/env",
                              fmt={"agent_id": agent_id},
                              json={"key": key, "value": target}), key)
        assert written["value"] == target, f"write did not take: {written}"
        assert written["source"] == "agent", written
    finally:
        restore = was if row["source"] == "agent" else None
        api.ok("POST", "/agents/{agent_id}/env", fmt={"agent_id": agent_id},
               json={"key": key, "value": restore})
    assert _row(api.ok("GET", "/agents/{agent_id}/env",
                       fmt={"agent_id": agent_id}), key)["value"] == was, (
        "the agent's default was not put back")


# ── the Work view ──

def test_the_work_view_answers_in_one_call_and_agrees_with_the_env_route(api,
                                                                        scratch_sid):
    """One call because three can disagree — and the env it carries must be
    the env the env route serves.

    That agreement is the whole reason the route composes the two rather than
    letting the client fetch them separately: a Work screen drawing stages
    dispatched with settings that differ from the ones its own settings screen
    shows is the drift this shape exists to prevent, and it is invisible to a
    test of either route alone.
    """
    work = api.ok("GET", "/sessions/{sid}/work", fmt={"sid": scratch_sid})
    assert isinstance(work, dict), work
    assert work.get("mode") in WORK_MODES, f"unknown work mode: {work}"

    if not work.get("available", True):
        assert work["mode"] == "unavailable", (
            f"an unavailable work harness must say so in `mode` too, or a "
            f"client that ignores the flag draws an empty plan: {work}")
        assert work.get("plan") is None and work.get("stages") == []
        pytest.skip("this host has no work harness")

    assert work["mode"] == "none", (
        f"a session id no session has cannot be on a plan: {work}")
    assert work["plan"] is None and work["stages"] == []
    assert isinstance(work.get("tasks"), dict), work

    env = api.ok("GET", "/sessions/{sid}/env", fmt={"sid": scratch_sid})
    seen = {(r["key"], r["value"], r["source"]) for r in _rows(work)}
    served = {(r["key"], r["value"], r["source"]) for r in _rows(env)}
    assert seen == served, (
        f"the Work view's environment disagrees with the environment route "
        f"for the same session: only in /work {sorted(seen - served)}, only in "
        f"/env {sorted(served - seen)}")


# ── plans ──

def test_the_plan_list_answers(api):
    plans = api.ok("GET", "/plans", **{"params": {"limit": 5}})
    assert isinstance(plans, dict), plans
    if not plans.get("available", True):
        assert plans.get("reason") and plans.get("plans") == [], plans
    else:
        assert isinstance(plans["plans"], list), plans


def test_a_plan_id_nobody_minted_is_refused_by_both_read_routes(api):
    """404, not an empty plan.

    This is the only plan-detail case a single live host is guaranteed to be
    able to prove: `plans.open_plan` has exactly one caller in the package and
    it is `cli.py`, so nothing on the wire can make a plan exist and a suite
    running on another machine has no way to author one. The document route
    answers 503 rather than an empty body on a host without the harness,
    because there is no empty document that does not read as a plan whose text
    is gone.
    """
    missing = f"live-no-such-plan-{uuid.uuid4().hex[:8]}"

    detail = api.call("GET", "/plans/{plan_id}", fmt={"plan_id": missing})
    assert detail.status_code in (200, 404), detail.text[:200]
    if detail.status_code == 200:
        assert detail.json().get("available") is False, (
            f"host served a plan it does not have: {detail.text[:300]}")

    doc = api.call("GET", "/plans/{plan_id}/document", fmt={"plan_id": missing})
    assert doc.status_code in (404, 503), (
        f"a document route answered {doc.status_code} for a plan that does not "
        f"exist: {doc.text[:300]}")


def test_a_plan_this_host_holds_serves_its_stages_and_its_markdown(api):
    """The positive half, on whatever the guest was seeded with.

    Skipped rather than faked on a host with no plans: the routes above prove
    the refusals, and inventing a row through the store behind the host's back
    would be a live test of a fixture. `scripts/live-vm-test.sh` is where a
    seeded plan belongs, beside the agent tree it already plants.
    """
    listing = api.ok("GET", "/plans", **{"params": {"limit": 5}})
    rows = listing.get("plans", []) if listing.get("available", True) else []
    if not rows:
        pytest.skip("this host holds no plans — nothing on the wire creates "
                    "one (`plans.open_plan` is CLI-only), so the read routes "
                    "are proven on their refusals above")

    plan_id = rows[0].get("id") or rows[0].get("plan_id")
    assert plan_id, f"a plan row with no id: {rows[0]}"

    detail = api.ok("GET", "/plans/{plan_id}", fmt={"plan_id": plan_id})
    assert detail.get("available") is True, detail
    assert isinstance(detail.get("stages"), list), detail
    for shape in ("proofs", "tasks"):
        assert isinstance(detail.get(shape), dict), (
            f"{shape} is keyed by stage so the screen draws each under the "
            f"stage it belongs to: {detail}")

    authored = (detail["plan"].get("plan_file") or "").strip()
    doc = api.call("GET", "/plans/{plan_id}/document", fmt={"plan_id": plan_id})
    if doc.status_code == 404:
        # A plan minted at plan-mode ENTRY has no `plan_file` until approval,
        # and this route reads that column. The 404 is then the honest answer;
        # on a plan that names a file it is the route failing to read it.
        assert not authored, (
            f"plan {plan_id} names {authored!r} and the document route cannot "
            f"read it: {doc.text[:300]}")
        return
    if doc.status_code == 403:
        # The fence, doing its job: whatever authored the plan wrote that
        # column, and a path out of a row is no more trustworthy than one off
        # the wire. A plan authored outside `docfence.read_roots()` is refused.
        assert authored, f"a 403 on a plan with no document at all: {doc.text[:300]}"
        assert doc.json().get("detail"), (
            f"the fence refused and will not say why: {doc.text[:300]}")
        return
    assert doc.status_code == 200, f"{doc.status_code}: {doc.text[:300]}"
    body = doc.json()
    assert isinstance(body, dict) and body.get("text") is not None, (
        f"the document route served no text: {str(body)[:300]}")
