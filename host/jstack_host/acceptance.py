"""Acceptance receipts written from observations, never from a declaration.

The journeys in `release_manifest.RECEIPTS` are what a commit must survive
before a fleet is moved onto it. Each journey here names the facts it has to
observe. A run records those facts while the journey executes against a Mac
built from the commit under test, and the receipt is written from that record
— there is no call that marks a journey passed.

A journey that raises, that ends without every required observation, or that
never ran is written down as exactly that. `gate` then refuses promotion and
names every journey that is not a genuine pass, so a missing case can never
read as a covered one.

Receipts stay small and public: observation *names* go into the signed
manifest; their values go into the evidence log the receipt is hashed against.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import release_manifest as releases
from .update_supervisor import atomic_json

# What each journey must observe before its receipt can say passed. These are
# the contract's own words, not a convenient
# subset: adding a journey to the runner never adds a way to skip one of these.
REQUIRED: dict[str, tuple[str, ...]] = {
    "fresh_install": ("installed_build", "host_identity", "plugin_versions",
                      "app_versions", "pairing", "new_session"),
    "upgrade": ("previous_build", "installed_build", "host_identity",
                "plugin_versions", "app_versions"),
    "fleet": ("hub_self_update", "leaf_local_update", "leaf_remote_update",
              "update_all", "duplicate_request", "denied_authority"),
    "offline_catchup": ("queued_offline", "offline_state", "same_job_current",
                        "fresh_observation"),
    "session_survival": ("session_pid", "surviving_pid", "new_output", "app_relaunch",
                         "built_new_session"),
    "interruption": ("interrupted_job", "recovered_state", "retry_current", "reboot_resume"),
    "revocation": ("revoked_device", "cancelled_job", "rejected_request", "unchanged_build"),
    "off_network": ("device", "transport", "build_notice", "session_journey"),
    "shell_adopt": ("leaf_on_candidate", "granted_at_adoption", "capability_reported",
                    "hub_shell_answers", "no_standing_root", "joiner_rerun_idempotent"),
    "shell_flip": ("granted_pair", "peer_shell_answers", "revoked_pair",
                   "refused_immediately", "no_readoption"),
    "delegate_leaf": ("projected", "projection_answers", "stable_across_retries",
                      "cannot_administer", "parent_recheck", "non_leaf_refuses"),
    "upgrade_shell": ("pre_shell_machine", "promoted_build", "presented_on_refresh",
                      "hub_row_gained_identity", "hub_config_named", "hub_shell_answers",
                      "no_standing_root", "no_readoption"),
    "shell_detach": ("leaf_on_candidate", "shell_before_detach", "detach_steps",
                     "material_gone", "remote_login_restored", "hub_forgot_machine",
                     "hub_shell_refused"),
}
PASSED = "passed"
HARNESS = "harness"


class HarnessFault(RuntimeError):
    """The lab could not stage a journey — a fixture a reset guest lost, a guest
    the runner could not reach. Written as `harness`, never `failed`: a failed
    journey has to mean the commit under test, and this says nothing about it."""


SOURCE = re.compile(r"[0-9a-f]{40}")


def source_of(build: dict) -> str:
    """A receipt belongs to one commit, not to one machine's bytes.

    The artifact digest used to be the binding, and it cannot be one any more:
    every Mac builds the Hub itself, so two honest installs of the same commit
    differ byte for byte — a gzip stamp, a machine's own signing key. Binding
    on bytes would make a fleet's own receipts refuse the fleet. What all of
    them share, and what a journey actually put under test, is the commit.
    """
    sha = build.get("sha")
    if not isinstance(sha, str) or not SOURCE.fullmatch(sha):
        raise releases.ReleaseError("a build must name the commit it was built from")
    return sha


def when(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


class Journey:
    """One journey's live record. `observe` is the only way a fact gets in."""

    def __init__(self, name: str):
        self.name = name
        self.required = REQUIRED[name]
        self.observed: dict[str, object] = {}
        self.started = time.time()
        self.lines: list[str] = []

    def note(self, message: str) -> None:
        self.lines.append(f"{when(time.time())} note {message}")

    def observe(self, check: str, value) -> None:
        if check not in self.required:
            raise releases.ReleaseError(f"{self.name} does not require {check}")
        # An empty answer is what a broken probe returns. It is not an
        # observation, and recording it would be the lie the gate exists to stop.
        if value is None or value is False or (hasattr(value, "__len__") and not len(value)):
            raise releases.ReleaseError(f"{self.name}/{check} observed nothing")
        self.observed[check] = value
        self.lines.append(f"{when(time.time())} observed {check} "
                          + json.dumps(value, sort_keys=True, default=str))

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(check for check in self.required if check not in self.observed)


class Run:
    """An acceptance run over one commit, writing one receipt per journey."""

    def __init__(self, receipts: Path, build: dict):
        self.dir = Path(receipts)
        self.build = releases.identifier(build["build"])
        self.source = source_of(build)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, str] = {}

    @contextmanager
    def journey(self, name: str):
        """Run one journey. Its failure is a receipt, not an aborted run: the
        remaining journeys still tell us what else this commit cannot do."""
        if name not in REQUIRED:
            raise releases.ReleaseError(f"unknown acceptance journey: {name}")
        record = Journey(name)
        try:
            yield record
        except (KeyboardInterrupt, SystemExit):
            record.lines.append(when(time.time()) + " aborted by operator")
            self.write(record, "failed", "run aborted")
            raise
        except HarnessFault as exc:
            record.lines.append(traceback.format_exc().rstrip())
            self.write(record, HARNESS, str(exc))
        except Exception as exc:
            record.lines.append(traceback.format_exc().rstrip())
            self.write(record, "failed", f"{type(exc).__name__}: {exc}")
        else:
            self.write(record, PASSED if not record.missing else "incomplete", "")

    def skip(self, name: str, reason: str) -> None:
        """Record why a journey did not run. Silence would read as coverage."""
        record = Journey(name)
        record.note("not attempted: " + reason)
        self.write(record, "skipped", reason)

    def write(self, record: Journey, result: str, detail: str) -> dict:
        finished = time.time()
        log = self.dir / (record.name + ".log")
        body = [f"journey: {record.name}", f"build: {self.build}",
                f"source: {self.source}", f"started: {when(record.started)}", "--",
                *record.lines, "--", f"result: {result}",
                "observed: " + (", ".join(record.observed) or "none"),
                "missing: " + (", ".join(record.missing) or "none"),
                "detail: " + (detail or "none"), f"finished: {when(finished)}"]
        log.write_text("\n".join(body) + "\n")
        receipt = {"journey": record.name, "build": self.build, "result": result,
                   "skipped": 1 if result == "skipped" else 0, "detail": detail,
                   "source": self.source, "evidence_sha256": releases.digest(log),
                   "observed": sorted(record.observed), "missing": sorted(record.missing),
                   "started": when(record.started), "finished": when(finished)}
        atomic_json(self.dir / (record.name + ".json"), receipt)
        self.results[record.name] = result
        print(f"acceptance {record.name}: {result}"
              + (f" ({detail})" if detail else ""), flush=True)
        return receipt

    def summary(self) -> dict:
        return {"build": self.build, "source": self.source,
                "results": dict(sorted(self.results.items())),
                "passed": sorted(n for n, r in self.results.items() if r == PASSED)}


def _fabricated(receipt: dict, evidence: Path) -> str:
    """Why this receipt cannot have come from a run, or "" if it could.

    `observe()` guards the shape of a value and nothing about where it came
    from, so a typed dict and a measured one are the same object by the time
    the gate sees them. On 2026-09-22 that let a release promote with
    `fresh_install` PASSED and `pairing` among its observations, on a build
    whose pairing code was not in the bundle — the receipt said so itself
    (`"new_full_journey_performed": false`) and the gate had no way to care.

    Provenance cannot be proven from inside the file, so this does not try to.
    It refuses the one mark a real run cannot leave: every check answering
    with the same bag of facts. A run probes each check separately and writes
    back what that probe returned, so the payloads differ — they are answers
    to different questions. When they are all one value, one thing was
    measured (or typed) and then labelled N times, and that is true whether or
    not it says so.

    Compared with each payload's self-naming field removed. The receipt that
    shipped carried `"check": "<this check>"` inside an otherwise identical
    dict, so a byte comparison called six copies six observations — the paste
    was stamped with the name of the thing it was pretending to be.

    Deliberately not a timing rule. Elapsed seconds look like the obvious tell
    and are not one — the evidence log stamps whole seconds, so an honest run
    that is quick, or any synthetic one, starts and finishes in the same
    second. That rule would have failed every existing acceptance test and
    taught the next reader to widen the gate to get their run through.
    """
    payloads: dict[str, str] = {}
    try:
        for line in evidence.read_text().splitlines():
            parts = line.split(" observed ", 1)
            if len(parts) != 2:
                continue
            check, _, value = parts[1].partition(" ")
            try:
                body = json.loads(value)
            except ValueError:
                body = value
            if isinstance(body, dict):
                # Only when something survives it. A payload whose single
                # field is the check's own name is a probe answer that says
                # "this check ran" and nothing more — stripping it leaves an
                # empty dict under every check and makes honest runs look
                # pasted, which is how a guard like this gets deleted.
                rest = {k: v for k, v in body.items() if v != check}
                body = rest or body
            payloads[check] = json.dumps(body, sort_keys=True, default=str)
    except OSError:
        return ""
    if len(payloads) > 2 and len(set(payloads.values())) == 1:
        return (f"all {len(payloads)} checks recorded one identical answer — "
                "that is one thing measured and labelled many times, not many observations")
    return ""


def inspect(receipts: Path, build: dict) -> dict[str, dict]:
    """What every journey's receipt says right now, checked against its bytes."""
    receipts = Path(receipts)
    source = source_of(build)
    identifier = build["build"]
    state = {}
    for name in sorted(releases.RECEIPTS):
        path, evidence = receipts / (name + ".json"), receipts / (name + ".log")
        try:
            receipt = json.loads(path.read_text())
            if not isinstance(receipt, dict):
                raise ValueError("receipt is not an object")
        except FileNotFoundError:
            state[name] = {"state": "missing", "detail": "no receipt was produced", "receipt": None}
            continue
        except (ValueError, OSError) as exc:
            state[name] = {"state": "unreadable", "detail": str(exc), "receipt": None}
            continue
        result = str(receipt.get("result", "unknown"))
        if not evidence.is_file():
            answer = ("evidence_missing", "receipt has no evidence log")
        elif releases.digest(evidence) != receipt.get("evidence_sha256"):
            answer = ("evidence_changed", "evidence log changed after the run")
        elif receipt.get("source") != source:
            answer = ("other_source", "receipt was written against a different commit")
        elif receipt.get("build") not in (None, identifier):
            answer = ("other_build", f"receipt names build {receipt.get('build')}")
        elif result != PASSED or receipt.get("skipped") != 0:
            answer = (result, receipt.get("detail") or
                      ("missing " + ", ".join(receipt.get("missing") or []) if receipt.get("missing")
                       else f"journey result is {result}"))
        elif set(receipt.get("observed") or []) != set(REQUIRED[name]):
            answer = ("incomplete", "receipt does not name every required observation")
        elif reason := _fabricated(receipt, evidence):
            answer = ("not_observed", reason)
        else:
            answer = (PASSED, "")
        state[name] = {"state": answer[0], "detail": answer[1], "receipt": receipt}
    return state


def gate(receipts: Path, build: dict) -> dict[str, dict]:
    """The only door a commit reaches the fleet through. All pass, or none count."""
    state = inspect(receipts, build)
    problems = [f"{name}: {entry['state']}" + (f" — {entry['detail']}" if entry["detail"] else "")
                for name, entry in state.items() if entry["state"] != PASSED]
    if problems:
        raise releases.ReleaseError(
            "acceptance is incomplete; this commit is refused:\n  " + "\n  ".join(problems))
    return {name: entry["receipt"] for name, entry in state.items()}
