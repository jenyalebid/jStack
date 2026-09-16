"""The gate between "we ran some tests" and "this release may be installed".

Every trap here is the same trap: a receipt that says passed when the journey
did not happen. It can be written by hand, left over from a previous build,
edited after the fact, or produced by a runner that crashed half way and never
reached the journey at all. Promotion has to refuse all four, and it has to
*name* what is missing — an acceptance run that silently covers eight of nine
journeys is the failure that ships a release nobody qualified.
"""
import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jstack_host import acceptance, publish_release, release_manifest as releases


@pytest.fixture
def candidate(tmp_path):
    """A signed, unpromoted candidate and its artifacts on disk."""
    directory = tmp_path / "candidate"
    directory.mkdir()
    components = {}
    for name in sorted(releases.COMPONENTS):
        body = (name + " artifact").encode()
        (directory / (name + ".zip")).write_bytes(body)
        components[name] = {"file": name + ".zip", "version": "1.2.3",
                            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    manifest = {"schema": 1, "release": "20260916T192622Z-1737a381", "notes": "test",
                "sources": {"stack": "a" * 40, "client": "b" * 40},
                "components": components,
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "26.0"},
                "receipts": {}}
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes_raw()
    (directory / "candidate.json").write_text(
        json.dumps(releases.sign(manifest, private, promoted=False)))
    return {"dir": directory, "manifest": manifest, "private": private,
            "public": base64.b64encode(key.public_key().public_bytes_raw()).decode()}


def pass_everything(run, skip=()):
    """What a complete, honest acceptance run leaves behind."""
    for name, checks in acceptance.REQUIRED.items():
        if name in skip:
            continue
        with run.journey(name) as journey:
            for check in checks:
                journey.observe(check, {"observed": check})


# ── What the runner may record


def test_required_observations_cover_every_receipt_the_contract_names():
    assert set(acceptance.REQUIRED) == releases.RECEIPTS


def test_a_journey_cannot_record_a_fact_its_contract_never_asked_for(tmp_path, candidate):
    run = acceptance.Run(tmp_path / "receipts", candidate["manifest"])
    with run.journey("rollback") as journey:
        with pytest.raises(releases.ReleaseError):
            journey.observe("cellular_transport", "LTE")
    assert run.results["rollback"] == "incomplete"


@pytest.mark.parametrize("empty", [None, False, "", [], {}])
def test_an_empty_probe_answer_is_not_an_observation(tmp_path, candidate, empty):
    run = acceptance.Run(tmp_path / "receipts", candidate["manifest"])
    with run.journey("revocation") as journey:
        with pytest.raises(releases.ReleaseError):
            journey.observe("revoked_device", empty)


def test_a_passing_journey_binds_its_receipt_to_the_exact_artifacts(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    run = acceptance.Run(receipts, candidate["manifest"])
    with run.journey("upgrade") as journey:
        journey.note("installed the previous release first")
        for check in acceptance.REQUIRED["upgrade"]:
            journey.observe(check, check + "-value")
    receipt = json.loads((receipts / "upgrade.json").read_text())
    assert receipt["result"] == "passed" and receipt["skipped"] == 0
    assert receipt["artifacts"] == acceptance.artifact_set(candidate["manifest"])
    assert receipt["evidence_sha256"] == releases.digest(receipts / "upgrade.log")
    # The values live in the evidence, the names in the signed receipt.
    log = (receipts / "upgrade.log").read_text()
    assert "installed the previous release first" in log and "host_identity-value" in log
    assert "-value" not in json.dumps(receipt)


def test_one_journey_failing_still_records_the_rest(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    run = acceptance.Run(receipts, candidate["manifest"])
    with run.journey("interruption") as journey:
        journey.observe("interrupted_job", "job-1")
        raise TimeoutError("updater never restarted")
    pass_everything(run, skip={"interruption"})
    assert run.results["interruption"] == "failed"
    failed = json.loads((receipts / "interruption.json").read_text())
    assert failed["missing"] == ["reboot_resume", "recovered_state", "retry_current"]
    assert "updater never restarted" in (receipts / "interruption.log").read_text()
    assert run.summary()["passed"] == sorted(set(releases.RECEIPTS) - {"interruption"})


# ── What the gate refuses


def test_a_complete_run_opens_the_gate_and_signs(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    pass_everything(acceptance.Run(receipts, candidate["manifest"]))
    accepted = acceptance.gate(receipts, candidate["manifest"])
    assert set(accepted) == releases.RECEIPTS
    manifest = {**candidate["manifest"], "receipts": accepted}
    assert releases.validate(manifest)["release"] == candidate["manifest"]["release"]


def test_a_journey_that_never_ran_is_named_not_assumed(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    pass_everything(acceptance.Run(receipts, candidate["manifest"]), skip={"cellular"})
    with pytest.raises(releases.ReleaseError, match="cellular: missing"):
        acceptance.gate(receipts, candidate["manifest"])


def test_a_recorded_skip_is_refused_exactly_like_an_absence(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    run = acceptance.Run(receipts, candidate["manifest"])
    pass_everything(run, skip={"cellular"})
    run.skip("cellular", "test phone is not wired up")
    assert json.loads((receipts / "cellular.json").read_text())["skipped"] == 1
    with pytest.raises(releases.ReleaseError, match="test phone is not wired up"):
        acceptance.gate(receipts, candidate["manifest"])


def test_an_incomplete_journey_cannot_pass_by_recording_most_of_it(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    run = acceptance.Run(receipts, candidate["manifest"])
    pass_everything(run, skip={"fleet"})
    with run.journey("fleet") as journey:
        for check in acceptance.REQUIRED["fleet"][:-1]:
            journey.observe(check, check)
    assert run.results["fleet"] == "incomplete"
    with pytest.raises(releases.ReleaseError, match="denied_authority"):
        acceptance.gate(receipts, candidate["manifest"])


def test_evidence_edited_after_the_run_stops_promotion(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    pass_everything(acceptance.Run(receipts, candidate["manifest"]))
    (receipts / "fleet.log").write_text("result: passed\n")
    with pytest.raises(releases.ReleaseError, match="fleet: evidence_changed"):
        acceptance.gate(receipts, candidate["manifest"])


def test_a_receipt_from_another_build_does_not_qualify_these_bytes(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    pass_everything(acceptance.Run(receipts, candidate["manifest"]))
    rebuilt = json.loads(json.dumps(candidate["manifest"]))
    rebuilt["components"]["client"]["sha256"] = "f" * 64
    with pytest.raises(releases.ReleaseError, match="other_artifacts"):
        acceptance.gate(receipts, rebuilt)


def test_a_hand_written_pass_without_evidence_is_refused(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    pass_everything(acceptance.Run(receipts, candidate["manifest"]), skip={"rollback"})
    (receipts / "rollback.json").write_text(json.dumps(
        {"journey": "rollback", "result": "passed", "skipped": 0,
         "artifacts": acceptance.artifact_set(candidate["manifest"]),
         "evidence_sha256": "d" * 64, "observed": list(acceptance.REQUIRED["rollback"])}))
    with pytest.raises(releases.ReleaseError, match="rollback: evidence_missing"):
        acceptance.gate(receipts, candidate["manifest"])


def test_a_passed_receipt_that_names_no_observations_is_refused(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    pass_everything(acceptance.Run(receipts, candidate["manifest"]))
    receipt = json.loads((receipts / "revocation.json").read_text())
    receipt["observed"] = ["revoked_device"]
    (receipts / "revocation.json").write_text(json.dumps(receipt))
    with pytest.raises(releases.ReleaseError, match="revocation: incomplete"):
        acceptance.gate(receipts, candidate["manifest"])


# ── The release action


def test_promotion_refuses_an_empty_receipt_directory_by_name(tmp_path, candidate):
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(releases.ReleaseError) as refusal:
        publish_release.promote(candidate["dir"], empty, tmp_path / "feed", candidate["private"])
    assert sorted(releases.RECEIPTS) == sorted(
        line.split(":")[0].strip() for line in str(refusal.value).splitlines()[1:])
    assert not (tmp_path / "feed").exists()


def test_promotion_publishes_the_candidate_with_its_evidence(tmp_path, candidate):
    receipts = tmp_path / "receipts"
    pass_everything(acceptance.Run(receipts, candidate["manifest"]))
    feed = tmp_path / "feed"
    envelope = publish_release.promote(candidate["dir"], receipts, feed, candidate["private"])
    release = envelope["manifest"]["release"]
    assert json.loads((feed / "latest.json").read_text()) == envelope
    assert releases.verify(json.loads((feed / release / "manifest.json").read_text()),
                           candidate["public"])["receipts"]["fleet"]["result"] == "passed"
    assert (feed / release / "receipts/fleet.log").is_file()


def test_qualify_reports_what_a_runner_that_died_left_behind(tmp_path, candidate, monkeypatch):
    receipts = tmp_path / "receipts"

    def half_a_run(argv, **kwargs):
        run = acceptance.Run(receipts, candidate["manifest"])
        pass_everything(run, skip={"cellular", "fleet"})
        return __import__("subprocess").CompletedProcess(argv, 1)

    monkeypatch.setattr(publish_release.subprocess, "run", half_a_run)
    state = publish_release.qualify({"acceptance": ["runner"]}, candidate["dir"], receipts,
                                    candidate["private"])
    assert state["cellular"]["state"] == "missing" and state["fleet"]["state"] == "missing"
    assert state["upgrade"]["state"] == "passed"
    with pytest.raises(releases.ReleaseError):
        publish_release.promote(candidate["dir"], receipts, tmp_path / "feed", candidate["private"])


def test_qualify_without_a_configured_runner_refuses(tmp_path, candidate):
    with pytest.raises(releases.ReleaseError, match="acceptance runner"):
        publish_release.qualify({}, candidate["dir"], tmp_path / "receipts", candidate["private"])
