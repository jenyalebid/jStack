"""Release trust, durable jobs, authority, and restart recovery contracts."""
# NOTE — destructive install/uninstall paths: never run these for real on the home
# machine (the production Hub). Every launchd / JStackHub / sudo boundary must be
# stubbed; conftest fails the test if one is reached. Real proofs run in lab guests.
import base64
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from jstack_host import devices, hostenv, managed_access, mode
from jstack_host import attach_parent, fleet_updates as fleet, release_manifest as releases
from jstack_host import update_routes, store as stores
from jstack_host.update_supervisor import Supervisor, atomic_json


@pytest.fixture
def release():
    key = Ed25519PrivateKey.generate()
    components = {name: {"file": name + ".zip", "sha256": hashlib.sha256(b"artifact").hexdigest(),
                         "bytes": 8, "version": "1"} for name in releases.COMPONENTS}
    manifest = {"schema": 1, "release": "test-1", "components": components,
                "sources": {"stack": "a" * 40, "client": "b" * 40},
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "13.0"},
                "receipts": {name: {"result": "passed", "skipped": 0, "source": "a" * 40,
                                    "evidence_sha256": "c" * 64} for name in releases.RECEIPTS}}
    envelope = releases.sign(manifest, key.private_bytes_raw())
    return key, base64.b64encode(key.public_key().public_bytes_raw()).decode(), envelope


@pytest.mark.parametrize("corrupt_upload", [False, True])
@pytest.mark.parametrize("existing", [None, "draft", "published"])
def test_publication_checks_uploaded_bytes_before_exposing_release(tmp_path, release, monkeypatch, corrupt_upload, existing):
    import shutil
    import subprocess
    from jstack_host import release_channel, update_macos
    _, public, envelope = release
    atomic_json(tmp_path / "manifest.json", envelope)
    for item in envelope["manifest"]["components"].values():
        (tmp_path / item["file"]).write_bytes(b"artifact")
    monkeypatch.setattr(release_channel.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0,
                            json.dumps({"draft": existing == "draft", "assets": [{"name": "manifest.json"}]}), "")
                        if existing else subprocess.CompletedProcess(a[0], 1, "", "HTTP 404"))
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["release", "download"]:
            from pathlib import Path
            name = argv[argv.index("--pattern") + 1]
            target = Path(argv[argv.index("--dir") + 1]) / name
            shutil.copy2(tmp_path / name, target)
            if corrupt_upload and name == "client.zip":
                target.write_bytes(b"corrupt")
        return ""
    monkeypatch.setattr(update_macos, "command", command)
    if corrupt_upload:
        with pytest.raises(releases.ReleaseError):
            release_channel.publish(tmp_path, "example/stack", public)
        assert not any(c[1:3] == ["release", "edit"] for c in calls)
    else:
        release_channel.publish(tmp_path, "example/stack", public)
        if existing == "published":
            assert all(c[1:3] == ["release", "download"] for c in calls)
        else:
            stack_calls = [c for c in calls if c[3] == "stack-release-test-1"]
            assert stack_calls[-1][1:3] == ["release", "edit"]
        if existing is None:
            assert calls[0][calls[0].index("--target") + 1] == "a" * 40


def test_signature_rejects_payload_and_trust_key_substitution(release):
    key, public, envelope = release
    assert releases.verify(envelope, public)["release"] == "test-1"
    wrong = Ed25519PrivateKey.generate()
    envelope["public_key"] = base64.b64encode(wrong.public_key().public_bytes_raw()).decode()
    assert releases.verify(envelope, public)["release"] == "test-1"
    envelope["manifest"]["release"] = "tampered"
    with pytest.raises(releases.ReleaseError, match="signature"):
        releases.verify(envelope, public)
    with pytest.raises(releases.ReleaseError, match="signature"):
        releases.verify(releases.sign(envelope["manifest"], wrong.private_bytes_raw()), public)


@pytest.mark.parametrize("mutation", ["none", "skipped", "stale", "wrong_digest", "failed"])
def test_every_receipt_a_release_carries_is_a_pass_for_this_exact_commit(release, mutation):
    _, _, envelope = release
    manifest = envelope["manifest"]
    receipt = manifest["receipts"]["off_network"]
    if mutation == "none":
        manifest["receipts"] = {}
    elif mutation == "skipped":
        receipt["skipped"] = 1
    elif mutation == "stale":
        manifest["sources"]["stack"] = "f" * 40
    elif mutation == "wrong_digest":
        receipt["evidence_sha256"] = ""
    else:
        receipt["result"] = "failed"
    with pytest.raises(releases.ReleaseError, match="receipt|acceptance evidence"):
        releases.validate(manifest)


def test_a_release_that_retired_a_journey_still_installs_on_an_older_updater(release):
    """An updater cannot be taught a journey invented after it shipped. One
    that insisted on the exact set of names it knew refused every release that
    renamed or retired one, forever (#123); promotion is where the set lives."""
    manifest = release[2]["manifest"]
    del manifest["receipts"]["off_network"]
    manifest["receipts"]["a_journey_this_build_never_heard_of"] = dict(
        manifest["receipts"]["interruption"])
    assert releases.validate(manifest) is manifest


@pytest.mark.parametrize("filename", ["../escape", "/absolute", "a/b", "", ".", "..", "a\\b"])
def test_artifact_paths_are_bare_safe_names(release, filename):
    release[2]["manifest"]["components"]["stack"]["file"] = filename
    with pytest.raises(releases.ReleaseError):
        releases.validate(release[2]["manifest"])


def test_job_survives_reopen_duplicate_delivery_and_rejects_overlap(tmp_path, release):
    path = tmp_path / "jobs.sqlite"
    job = fleet.FleetStore(path).queue("leaf", "credential", release[2], "click-one")
    reopened = fleet.FleetStore(path)
    assert reopened.queue("leaf", "credential", release[2], "click-one")["id"] == job["id"]
    assert reopened.queue("leaf", "credential", release[2], "click-two")["id"] == job["id"]
    release[2]["manifest"]["release"] = "test-2"
    with pytest.raises(releases.ReleaseError):
        reopened.queue("leaf", "credential", release[2], "click-two")
    with pytest.raises(releases.ReleaseError):
        reopened.transition(job["id"], "other-leaf", "downloading")
    with pytest.raises(releases.ReleaseError):
        reopened.transition(job["id"], "leaf", "current")


def test_current_requires_verification_and_expired_inventory_is_unknown(tmp_path, release):
    store = fleet.FleetStore(tmp_path / "jobs.sqlite")
    job = store.queue("leaf", "cred", release[2], "click")
    for state in ("downloading", "applying", "verifying"):
        store.transition(job["id"], "leaf", state)
    with pytest.raises(releases.ReleaseError, match="verification"):
        store.transition(job["id"], "leaf", "current")
    store.transition(job["id"], "leaf", "current", verified=True)
    store.report("leaf", {"release": "test-1", "verified": True, "supervisor": 1})
    assert store.inventory("leaf", "Office", "test-1")["state"] == "current"
    assert store.inventory("leaf", "Office", "test-2")["state"] == "available"
    with store.connection() as db:
        db.execute("UPDATE reports SET seen=?", (time.time() - 100,))
    assert store.inventory("leaf", "Office", "test-1")["state"] == "unknown/offline"


def test_fresh_install_at_the_desired_release_is_current_not_available(tmp_path):
    # No job ever ran and nothing was verified: the installer put the release
    # there. Running exactly the desired release means there is no update to
    # offer — the menu read "Update Available" on every fresh install
    # (proven 2026-09-22, guest vfy-full-instances, release ffe85dc9).
    store = fleet.FleetStore(tmp_path / "jobs.sqlite")
    store.report("leaf", {"release": "test-1", "verified": False, "supervisor": 1})
    assert store.inventory("leaf", "Office", "test-1")["state"] == "current"
    assert store.inventory("leaf", "Office", "test-2")["state"] == "available"
    assert store.inventory("leaf", "Office", None)["state"] == "not_published"


def test_parent_accepts_a_leaf_rollback_after_restart_during_download(tmp_path, release):
    store = fleet.FleetStore(tmp_path / "jobs.sqlite")
    job = store.queue("leaf", "cred", release[2], "click")
    store.transition(job["id"], "leaf", "downloading")

    recovered = store.transition(
        job["id"], "leaf", "rolled_back", "authority lost during application")

    assert recovered["state"] == "rolled_back"


@pytest.fixture
def offers(release):
    """What this hub's feed offers per line: main carries the release, dev
    nothing until a test offers it one."""
    return {"main": release[2], "dev": None}


@pytest.fixture
def rig(tmp_path, monkeypatch, app, release, offers):
    state = tmp_path / "state"
    monkeypatch.setattr(hostenv, "state_dir", lambda: state)
    monkeypatch.setattr(hostenv, "host_id", lambda: "hub-main")
    monkeypatch.setattr(hostenv, "host_name", lambda: "Home")
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {})
    monkeypatch.setattr(mode, "is_managed", lambda: False)
    monkeypatch.setattr(mode, "is_hub", lambda: True)
    monkeypatch.setattr(fleet, "offer", lambda line="main": offers[line])
    store = stores.get_store()
    monkeypatch.setattr(devices, "_store", lambda: store)
    internal = devices.internal_token()
    console = TestClient(app, client=("127.0.0.1", 4000),
                         headers={"Authorization": "Bearer " + internal})
    phone, token = devices.mint("Phone")
    remote = TestClient(app, client=("10.66.0.8", 4001),
                        headers={"Authorization": "Bearer " + token})
    return store, console, remote


def test_phone_cannot_queue_updates_even_from_loopback(rig):
    _, console, remote = rig
    body = {"target": "all", "request_id": "click"}
    assert remote.post("/api/jremote/v1/updates/queue", json=body).status_code == 403
    assert console.post("/api/jremote/v1/updates/queue", headers=remote.headers,
                        json=body).status_code == 403
    assert console.post("/api/jremote/v1/updates/queue", json=body).status_code == 200
    assert remote.post("/api/jremote/v1/managed/updates/heartbeat", json={}).status_code == 403


def test_offline_job_delivered_on_reconnect_and_revocation_refuses_it(rig):
    store, console, remote = rig
    row, token = devices.mint("Office")
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    store.bind_host_device("leaf-one", row["id"])
    store.set_host_visibility("leaf-one", sees_home=False, sees_leaves=False)
    queued = console.post("/api/jremote/v1/updates/queue",
                          json={"target": "leaf-one", "request_id": "click"})
    assert queued.status_code == 200, queued.text
    inventory = console.get("/api/jremote/v1/updates/inventory").json()
    assert inventory["machines"][1]["state"] == "pending/offline"
    headers = {"Authorization": "Bearer " + token}
    path = "/api/jremote/v1/managed/updates/heartbeat"
    atomic_json(fleet.root() / "config.json", {"public_key": "hub-key"})
    response = remote.post(path, headers=headers, json={"observation": {"supervisor": 1}})
    assert response.status_code == 200, response.text
    assert response.json()["job"]["id"] == queued.json()["jobs"][0]["id"]
    # The key the job is signed with rides beside it (#144).
    assert response.json()["public_key"] == "hub-key"
    assert remote.post("/api/jremote/v1/managed/updates/check", headers=headers).status_code == 200
    devices.revoke(row["id"])
    assert remote.post(path, headers=headers, json={}).status_code == 401


def test_job_cannot_be_claimed_by_another_machine(rig):
    store, console, remote = rig
    tokens = []
    for key in ("leaf-one", "leaf-two"):
        row, token = devices.mint(key)
        store.upsert_host(key, key, "10.66.0.9")
        store.bind_host_device(key, row["id"])
        tokens.append(token)
    job = console.post("/api/jremote/v1/updates/queue",
                       json={"target": "leaf-one", "request_id": "click"}).json()["jobs"][0]
    response = remote.post("/api/jremote/v1/managed/updates/heartbeat",
                           headers={"Authorization": "Bearer " + tokens[1]},
                           json={"job_id": job["id"], "state": "current",
                                 "observation": {"release": "test-1", "verified": True}})
    assert fleet.FleetStore().latest("leaf-one")["state"] == "pending"
    assert response.json().get("job") is None


class Backend:
    def __init__(self):
        self.events = []
        self.healthy = True

    def compatible(self, manifest):
        self.events.append("compatible")

    def stage(self, manifest, directory):
        self.events.append("stage")
        return {"previous": "old"}

    def apply(self, job):
        assert job["state"] == "applying" and job["transaction"]["previous"] == "old"
        self.events.append("apply")

    def applied(self, job):
        return bool(job.get("transaction", {}).get("swapped"))

    def settle(self, job):
        self.events.append("settle")
        return {"error": ""}

    def finalize(self, job):
        self.events.append("finalize")

    def verify(self, job):
        return self.healthy

    def observe(self, job):
        return {"release": job.get("release"), "verified": bool(job.get("verified"))}


def supervisor(tmp_path, release, backend=None):
    root = tmp_path / "updates"
    root.mkdir(exist_ok=True)
    token = tmp_path / "token"
    token.write_text("test-token")
    configuration = {"local_url": "http://hub", "token_path": str(token), "public_key": release[1]}
    job = {"id": "job-1", "state": "pending", "release": "test-1", "envelope": release[2]}

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"artifact")
        body = json.loads(request.content)
        if body.get("state"):
            job["state"] = body["state"]
        if body.get("observation", {}).get("verified"):
            job["state"] = "current"
        return httpx.Response(200, json={"job": job})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    return Supervisor(root, configuration, backend or Backend(), client), job


def managed_supervisor(tmp_path, release, *, pinned: str, answer: dict):
    """A leaf: its parent on file, `pinned` as the key it installed with, and a
    parent that answers every heartbeat with `answer` beside the job."""
    daemon, job = supervisor(tmp_path, release)
    (tmp_path / "parent.json").write_text(json.dumps(
        {"parent_url": "http://parent:9090", "token": "leaf-token"}))
    daemon.config["public_key"] = pinned
    atomic_json(daemon.root / "config.json", dict(daemon.config))

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"artifact")
        body = json.loads(request.content)
        if body.get("state"):
            job["state"] = body["state"]
        if body.get("observation", {}).get("verified"):
            job["state"] = "current"
        return httpx.Response(200, json={"job": job, **answer})
    daemon.client = httpx.Client(transport=httpx.MockTransport(transport))
    return daemon, job


def test_a_leaf_takes_its_parents_key_from_the_heartbeat_and_verifies_the_job_with_it(tmp_path, release):
    """#144. The leaf installed from a bundle signed by one key; the parent
    that adopted it builds and signs with its own. The job it sends is
    unverifiable against the pinned key — and the key that fixes that comes
    down the same authenticated connection, before the job is verified."""
    stale = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    daemon, job = managed_supervisor(tmp_path, release, pinned=stale,
                                     answer={"public_key": release[1]})
    daemon.tick()
    assert daemon.backend.events == ["compatible", "stage", "apply"], daemon.current
    assert daemon.config["public_key"] == release[1]
    assert json.loads((daemon.root / "config.json").read_text())["public_key"] == release[1]
    # Durable across the daemon's own re-read of its settings.
    daemon.refresh_settings()
    assert daemon.config["public_key"] == release[1]


@pytest.mark.parametrize("offered", ["", "not a key", base64.b64encode(b"short").decode(), 7, None])
def test_a_leaf_keeps_its_key_when_the_parent_offers_nothing_usable(tmp_path, release, offered):
    daemon, _ = managed_supervisor(tmp_path, release, pinned=release[1],
                                   answer={"public_key": offered} if offered is not None else {})
    daemon.tick()
    assert daemon.config["public_key"] == release[1]
    assert daemon.backend.events == ["compatible", "stage", "apply"]


def test_a_hub_never_takes_a_key_from_its_own_local_heartbeat(tmp_path, release):
    """Only a parent is a trust root. The hub's local heartbeat answers with
    the same field, and a hub must not re-pin itself off it."""
    daemon, job = supervisor(tmp_path, release)
    foreign = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"artifact")
        return httpx.Response(200, json={"job": job, "public_key": foreign})
    daemon.client = httpx.Client(transport=httpx.MockTransport(transport))
    daemon.tick()
    assert daemon.config["public_key"] == release[1]


def test_supervisor_stages_before_apply_then_requires_hub_confirmation(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    assert daemon.backend.events == ["compatible", "stage", "apply"]
    assert daemon.current["state"] == "verifying"
    daemon.tick()
    assert daemon.current["state"] == "current"
    assert daemon.current["finalized"] is True
    assert daemon.backend.events[-1] == "finalize"
    daemon.tick()
    assert daemon.backend.events.count("apply") == 1
    assert daemon.backend.events.count("finalize") == 1


def test_crash_mid_apply_fails_the_job_and_restores_nothing(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.current = {**job, "state": "applying", "transaction": {"previous": "old"}}
    daemon.save()
    restarted = Supervisor(daemon.root, daemon.config, Backend(), daemon.client)
    restarted.tick()
    assert restarted.backend.events == ["settle"]
    assert restarted.current["state"] == "failed"
    assert restarted.current["applied"] is False
    assert "untouched and still running" in restarted.current["detail"]


@pytest.mark.parametrize("status,expected", [("applied", "verifying"), ("unknown", "failed")])
def test_an_interrupted_application_is_judged_by_what_it_finished(tmp_path, release, status, expected):
    backend = Backend()
    backend.healthy = False
    backend.recovery_status = lambda job: status
    daemon, job = supervisor(tmp_path, release, backend)
    daemon.current = {**job, "state": "applying", "transaction": {"native": True}}
    daemon.save()
    daemon.tick()
    assert daemon.current["state"] == expected
    assert ("settle" in backend.events) is (expected == "failed")


def test_a_failure_after_the_swap_says_the_machine_runs_the_release_that_failed(tmp_path, release):
    backend = Backend()
    backend.healthy = False
    backend.recovery_status = lambda job: "unknown"
    daemon, job = supervisor(tmp_path, release, backend)
    daemon.current = {**job, "state": "applying", "transaction": {"swapped": True}}
    daemon.save()
    daemon.tick()
    assert daemon.current["state"] == "failed" and daemon.current["applied"] is True
    assert "running test-1" in daemon.current["detail"]


def test_failed_verification_fails_the_job(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    daemon.backend.healthy = False
    daemon.save(verify_started=time.time() - 150)
    daemon.tick()
    assert daemon.current["state"] == "failed"
    assert daemon.current["detail"].startswith("updated components failed verification")


def test_tampered_download_never_reaches_apply(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    original = daemon.client

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"TAMPERED")
        return original.send(request)
    daemon.client = httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(releases.ReleaseError):
        daemon.tick()
    assert "apply" not in daemon.backend.events
    assert daemon.current["state"] == "failed"


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_stack_archive_rejects_links_and_devices(tmp_path, kind):
    from jstack_host.update_macos import safe_tar
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("link")
        member.type, member.linkname = kind, "../../outside"
        bundle.addfile(member)
    with pytest.raises(releases.ReleaseError):
        safe_tar(archive, tmp_path / "unpacked")


def test_verification_timeout_settles_without_network(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    daemon.save(verify_started=time.time() - 181)
    def offline(request):
        raise httpx.ConnectError("offline", request=request)
    daemon.client = httpx.Client(transport=httpx.MockTransport(offline))
    with pytest.raises(httpx.ConnectError):
        daemon.tick()
    assert daemon.backend.events[-1] == "settle"
    assert daemon.current["state"] == "failed"


def test_restart_adopts_hub_confirmation_before_local_commit(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    daemon.save(verified=True)
    restarted = Supervisor(daemon.root, daemon.config, daemon.backend, daemon.client)
    restarted.tick()
    assert restarted.current["state"] == "current"
    assert restarted.current["finalized"] is True
    assert restarted.backend.events.count("apply") == 1


def test_confirmed_transaction_removes_temporary_app_backups(tmp_path):
    from jstack_host.update_macos import MacBackend
    target = tmp_path / "Client.app"
    target.mkdir()
    backup = tmp_path / "Client.app.previous-release-stage-123"
    backup.mkdir()
    (backup / "old").write_text("old")
    job = {"transaction": {"apps": {"client": {
        "target": str(target), "backup": str(backup)}}}}
    backend = MacBackend(tmp_path, {})
    backend.finalize(job)
    assert target.is_dir()
    assert not backup.exists()
    backend.finalize(job)


def test_finalize_leaves_nothing_of_this_updaters_beside_the_app(tmp_path):
    """Three full Hub copies were found in /Applications after one acceptance
    run, each visible in Finder and Launchpad (#118)."""
    from jstack_host.update_macos import MacBackend
    target = tmp_path / "Hub.app"
    target.mkdir()
    for leftover in ("Hub.app.previous-release-stage-1", "Hub.app.failed-old-job",
                     "Hub.app.incoming-old-job"):
        (tmp_path / leftover).mkdir()
    keep = tmp_path / "Hub.app.notes"
    keep.mkdir()
    job = {"transaction": {"apps": {"menubar": {
        "target": str(target), "backup": str(tmp_path / "Hub.app.previous-release-stage-1")}}}}
    MacBackend(tmp_path, {}).finalize(job)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Hub.app", "Hub.app.notes"]


def test_staging_prunes_the_release_trees_nothing_is_running_from(tmp_path, monkeypatch):
    """Every generation staged a full source tree plus its dependencies and
    none was ever removed, so the host's import path grew one per update
    (#129). The tree the loaded runtime imports from is the exception."""
    from jstack_host import update_plugins
    from jstack_host.update_macos import MacBackend
    root = tmp_path / "updates"
    releases_dir = root / "releases"
    live, superseded, incoming = (releases_dir / name for name in ("live", "old", "new"))
    for directory in (live / "stage-a/stack/host", superseded / "stage-b", incoming):
        directory.mkdir(parents=True)
    backend = MacBackend(root, {"runtime_imports": [str(live / "stage-a/stack/host")]})
    monkeypatch.setattr(update_plugins, "discover", lambda: [])
    backend._prune_releases(incoming)
    assert sorted(path.name for path in releases_dir.iterdir()) == ["live", "new"]


def test_staging_keeps_the_tree_the_installed_host_service_runs_from(tmp_path, monkeypatch):
    """`runtime_imports` only advances on a confirmed job, so after a failed
    one the host runs out of a tree no configuration names. Its plist does."""
    import plistlib
    from jstack_host import update_plugins
    from jstack_host.update_macos import MacBackend
    root = tmp_path / "updates"
    failed, incoming = (root / "releases" / name for name in ("failed", "new"))
    (failed / "stage-a/stack/host").mkdir(parents=True)
    incoming.mkdir(parents=True)
    plist = tmp_path / "host.plist"
    plist.write_bytes(plistlib.dumps({"Label": "live.jstack.host", "EnvironmentVariables": {
        "PYTHONPATH": str(failed / "stage-a/stack/host") + ":" + str(failed / "stage-a/dependencies")}}))
    monkeypatch.setattr(update_plugins, "discover", lambda: [])
    MacBackend(root, {"host_plist": str(plist)})._prune_releases(incoming)
    assert sorted(path.name for path in (root / "releases").iterdir()) == ["failed", "new"]


def test_staging_keeps_the_tree_the_agent_marketplace_is_registered_against(tmp_path, monkeypatch):
    """The marketplace is a directory registration inside a stage that the
    agent CLIs re-read on every run; a superseded release tree costs less than
    a registration pointing at nothing."""
    from jstack_host import update_plugins
    from jstack_host.update_macos import MacBackend
    root = tmp_path / "updates"
    registered, incoming = (root / "releases" / name for name in ("prior", "new"))
    (registered / "stage-a/stack/plugins/jstack").mkdir(parents=True)
    incoming.mkdir(parents=True)
    monkeypatch.setattr(update_plugins, "discover",
                        lambda: [{"kind": "claude", "root": str(registered / "stage-a/stack")}])
    MacBackend(root, {})._prune_releases(incoming)
    assert sorted(path.name for path in (root / "releases").iterdir()) == ["new", "prior"]


def test_finalize_refuses_a_path_outside_the_updaters_backup_shape(tmp_path):
    from jstack_host.update_macos import MacBackend
    target = tmp_path / "Client.app"
    target.mkdir()
    unrelated = tmp_path / "Keep.app"
    unrelated.mkdir()
    job = {"transaction": {"apps": {"client": {
        "target": str(target), "backup": str(unrelated)}}}}
    with pytest.raises(releases.ReleaseError, match="unexpected recovery bundle"):
        MacBackend(tmp_path, {}).finalize(job)
    assert unrelated.is_dir()


def test_completed_job_accepts_repeated_current_heartbeats(rig):
    _, console, remote = rig
    job = console.post("/api/jremote/v1/updates/queue",
                       json={"target": "self", "request_id": "click"}).json()["jobs"][0]
    store = fleet.FleetStore()
    for state in ("downloading", "applying", "verifying"):
        store.transition(job["id"], "hub-main", state)
    store.transition(job["id"], "hub-main", "current", verified=True)
    for _ in range(2):
        result = console.post("/api/jremote/v1/updates/heartbeat",
                              json={"job_id": job["id"], "state": "current"})
        assert result.status_code == 200, result.text
        assert result.json()["job"]["state"] == "current"


def test_revocation_cancels_pending_inventory_job(rig):
    store, console, remote = rig
    row, token = devices.mint("Office")
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    store.bind_host_device("leaf-one", row["id"])
    response = console.post("/api/jremote/v1/updates/queue",
                            json={"target": "leaf-one", "request_id": "click"})
    assert response.status_code == 200
    devices.revoke(row["id"])
    assert fleet.FleetStore().inventory("leaf-one", "Office", "test-1")["state"] == "cancelled"


def test_atomic_replacement_preserves_executable_mode_and_symlink_target(tmp_path):
    from jstack_host.update_macos import atomic_bytes
    original, launcher = tmp_path / "original", tmp_path / "launcher"
    original.write_bytes(b"old")
    original.chmod(0o755)
    launcher.symlink_to(original)
    atomic_bytes(launcher, b"new")
    assert not launcher.is_symlink()
    assert launcher.read_bytes() == b"new"
    assert original.read_bytes() == b"old"
    assert launcher.stat().st_mode & 0o777 == 0o755


def test_promote_checks_evidence_bytes_and_preserves_prior_feed(tmp_path, release):
    from jstack_host.publish_release import promote
    key, public, envelope = release
    candidate, evidence, feed = (tmp_path / name for name in ("candidate", "evidence", "feed"))
    for directory in (candidate, evidence, feed):
        directory.mkdir()
    for item in envelope["manifest"]["components"].values():
        (candidate / item["file"]).write_bytes(b"artifact")
    atomic_json(candidate / "candidate.json", envelope)
    atomic_json(feed / "latest.json", {"prior": "untouched"})
    for name, receipt in envelope["manifest"]["receipts"].items():
        log = evidence / (name + ".log")
        log.write_text("synthetic UNIT TEST evidence, never production acceptance")
        atomic_json(evidence / (name + ".json"), {**receipt, "evidence_sha256": releases.digest(log)})
    (evidence / "off_network.log").write_text("changed")
    with pytest.raises(releases.ReleaseError, match="evidence"):
        promote(candidate, evidence, feed, key.private_bytes_raw())
    assert json.loads((feed / "latest.json").read_text()) == {"prior": "untouched"}


@pytest.mark.parametrize("state,verified,changes", [("verifying", True, False),
                                                 ("current", False, False), ("current", True, True)])
def test_updater_runtime_moves_only_after_confirmed_transaction(tmp_path, state, verified, changes):
    from jstack_host.update_macos import MacBackend
    stack = tmp_path / "release/stack"
    (stack / "host/jstack_host").mkdir(parents=True)
    (stack / "host/jstack_host/update_dispatcher.py").write_text("# test fixture")
    configuration = {"dispatcher": str(tmp_path / "stable-dispatcher"), "runtime_imports": ["old"]}
    atomic_json(tmp_path / "config.json", configuration)
    backend = MacBackend(tmp_path, configuration)
    job = {"state": state, "verified": verified,
           "transaction": {"stack": str(stack), "stage": str(tmp_path / "release")}}
    assert backend.activate_runtime(job) is changes
    updated = json.loads((tmp_path / "config.json").read_text())
    assert updated["dispatcher"] == configuration["dispatcher"]
    assert (updated["runtime_imports"] != ["old"]) is changes


def test_launchd_replacement_waits_for_service_removal(tmp_path, monkeypatch):
    import subprocess
    from jstack_host import update_macos
    calls = []
    remaining = iter([0, 0, 113])
    def run(argv, **kwargs):
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, next(remaining) if argv[1] == "print" else 0)
    monkeypatch.setattr(update_macos.subprocess, "run", run)
    monkeypatch.setattr(update_macos.time, "sleep", lambda _: None)
    update_macos.MacBackend(tmp_path, {"host_label": "lab-host"})._unload("host")
    assert calls == ["bootout", "print", "print", "print"]


def test_inventory_observes_updater_source_and_requires_live_menubar(tmp_path, monkeypatch):
    from jstack_host import update_macos, update_plugins, sourcestamp
    token = tmp_path / "token"
    token.write_text("test")
    config = {"client_path": "Client", "menubar_path": "Menu", "token_path": str(token),
              "local_url": "http://test"}
    monkeypatch.setattr(update_macos, "bundle_info", lambda _: {"CFBundleVersion": "1"})
    monkeypatch.setattr(update_plugins, "discover", lambda: [])
    monkeypatch.setattr(update_plugins, "observed", lambda _: {})
    monkeypatch.setattr(sourcestamp, "capture", lambda: {"sha": "actual-updater"})
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"source": {
        "release": "r1", "dirty": False}}))
    real_client = httpx.Client
    monkeypatch.setattr(update_macos.httpx, "Client", lambda **kwargs: real_client(transport=transport))
    job = {"verified": True, "state": "current", "release": "r1", "envelope": {"manifest": {
        "components": {"client": {"version": "1"}, "menubar": {"version": "1"}}}}}
    backend = update_macos.MacBackend(tmp_path, config)
    monkeypatch.setattr(update_macos, "running", lambda _: [12])
    assert backend.observe(job)["verified"] is True
    assert backend.observe(job)["updater_source"] == {"sha": "actual-updater"}
    monkeypatch.setattr(update_macos, "running", lambda _: [])
    assert backend.observe(job)["verified"] is False


def test_running_observes_exec_after_cached_process_scan(tmp_path):
    """A launchd-style same-PID exec must not stay cached as its launcher."""
    import plistlib
    import subprocess
    import time
    import psutil
    from jstack_host.update_macos import running

    app = tmp_path / "Fixture.app"
    # Keep Apple's platform binary in place; copying it would test signature
    # enforcement instead of the process exec transition.
    executable = Path("/bin/sleep")
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": str(executable)}))
    process = subprocess.Popen(["/bin/sh", "-c", 'read ready; exec "$1" 30',
                                "fixture-launcher", str(executable)], stdin=subprocess.PIPE)
    try:
        # Seed the precise stale cache that launch-time process scanning makes.
        assert process.pid in [p.pid for p in psutil.process_iter(["pid", "exe"])]
        assert process.pid not in running(app)
        process.stdin.write(b"go\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while psutil.Process(process.pid).exe() != str(executable):
            assert time.monotonic() < deadline, "fixture did not exec"
            time.sleep(0.01)
        assert process.pid in running(app)
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdin.close()


def _channel_cli(state, *argv):
    import io
    import contextlib
    from jstack_host import cli
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["updates", "channel", *argv, "--state-dir", str(state)])
    return code, out.getvalue().strip(), err.getvalue().strip()


def test_switching_a_hub_to_a_branch_is_one_command(tmp_path, monkeypatch):
    """A hub is switched by naming a branch, not by editing JSON. The write
    goes to the same config `refresh` reads and a reinstall carries forward."""
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    config = tmp_path / "updates" / "config.json"
    atomic_json(config, {"github_repo": "example/stack"})
    assert _channel_cli(tmp_path)[:2] == (0, releases.STABLE_CHANNEL)
    assert _channel_cli(tmp_path, "feature/x")[:2] == (0, "feature/x")
    assert json.loads(config.read_text())["channel"] == "feature/x"
    assert json.loads(config.read_text())["github_repo"] == "example/stack"
    assert _channel_cli(tmp_path)[:2] == (0, "feature/x")
    assert _channel_cli(tmp_path, "stable")[:2] == (0, releases.STABLE_CHANNEL)


def test_a_mistyped_branch_name_is_a_sentence_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    atomic_json(tmp_path / "updates" / "config.json", {"github_repo": "example/stack"})
    code, _, err = _channel_cli(tmp_path, "../etc")
    assert code == 1 and "must name a branch" in err
    assert "channel" not in json.loads((tmp_path / "updates" / "config.json").read_text())


def test_asking_before_updates_are_enabled_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    code, _, err = _channel_cli(tmp_path)
    assert code == 1 and "updates enable" in err


@pytest.mark.parametrize("initial_state", ["pending", "current"])
def test_reused_request_keeps_its_job_after_state_and_inventory_change(tmp_path, release, initial_state):
    path = tmp_path / "fleet.sqlite"
    store = fleet.FleetStore(path)
    job = store.queue("leaf", "credential", release[2], "original")
    if initial_state == "current":
        for state in ("downloading", "applying", "verifying", "current"):
            store.transition(job["id"], "leaf", state, verified=True)
        store.report("leaf", {"verified": True, "release": "test-1"})
    assert store.queue("leaf", "credential", release[2], "update-all")["id"] == job["id"]
    if initial_state == "pending":
        store.transition(job["id"], "leaf", "failed")
    store.report("leaf", {"verified": False, "release": "old"})
    reopened = fleet.FleetStore(path)
    assert reopened.queue("leaf", "credential", release[2], "update-all")["id"] == job["id"]
    with pytest.raises(releases.ReleaseError, match="different update"):
        reopened.queue("leaf", "other-credential", release[2], "update-all")


# ── Lines: every machine takes its own line's offer ─────────────────────────

def _dev_offer(release):
    manifest = {**release[2]["manifest"], "release": "dev-1"}
    return releases.sign(manifest, release[0].private_bytes_raw())


def _leaf(store, key):
    row, token = devices.mint(key)
    store.upsert_host(key, key, "10.66.0.9")
    store.bind_host_device(key, row["id"])
    return {"Authorization": "Bearer " + token}


def test_the_feed_keeps_main_where_old_leaves_read_it_and_dev_beside_it(tmp_path, monkeypatch):
    from jstack_host import releases as app_releases
    monkeypatch.setattr(app_releases, "RELEASE_DIR", tmp_path / "releases/mac")
    assert fleet.latest_path("main") == tmp_path / "releases/fleet/latest.json"
    assert fleet.latest_path() == fleet.latest_path("main")
    assert fleet.latest_path("dev") == tmp_path / "releases/fleet/latest-dev.json"
    assert fleet.offer("dev") is None
    assert fleet.machine_line({}) == "main" and fleet.machine_line({"line": "stable"}) == "main"
    assert fleet.machine_line({"line": "dev"}) == "dev"
    assert fleet.machine_line({"line": "feature/x"}) == "main"


def test_inventory_measures_each_machine_against_its_own_line(rig, release, offers):
    store, console, remote = rig
    offers["dev"] = _dev_offer(release)
    headers = _leaf(store, "leaf-dev")
    _leaf(store, "leaf-main")
    beat = "/api/jremote/v1/managed/updates/heartbeat"
    answer = remote.post(beat, headers=headers,
                         json={"observation": {"supervisor": 1, "line": "dev"}})
    assert answer.status_code == 200, answer.text
    # The heartbeat answers with the reporting machine's own line's offer.
    assert answer.json()["offer"]["manifest"]["release"] == "dev-1"
    inventory = console.get("/api/jremote/v1/updates/inventory").json()
    assert inventory["lines"] == {"main": "test-1", "dev": "dev-1"}
    rows = {row["machine"]: row for row in inventory["machines"]}
    assert rows["hub-main"]["line"] == "main" and rows["hub-main"]["desired"] == "test-1"
    assert rows["leaf-dev"]["line"] == "dev" and rows["leaf-dev"]["desired"] == "dev-1"
    # A machine that never said which line it is on is on main.
    assert rows["leaf-main"]["line"] == "main" and rows["leaf-main"]["desired"] == "test-1"


def test_a_queue_hands_each_machine_its_own_lines_release(rig, release, offers):
    store, console, remote = rig
    offers["dev"] = _dev_offer(release)
    headers = _leaf(store, "leaf-dev")
    _leaf(store, "leaf-main")
    remote.post("/api/jremote/v1/managed/updates/heartbeat", headers=headers,
                json={"observation": {"supervisor": 1, "line": "dev"}})
    queued = console.post("/api/jremote/v1/updates/queue",
                          json={"target": "all", "request_id": "click"})
    assert queued.status_code == 200, queued.text
    jobs = {job["machine"]: job["release"] for job in queued.json()["jobs"]}
    assert jobs == {"hub-main": "test-1", "leaf-dev": "dev-1", "leaf-main": "test-1"}


def test_a_line_with_nothing_offered_queues_nothing_for_its_machines(rig):
    store, console, remote = rig
    headers = _leaf(store, "leaf-dev")
    remote.post("/api/jremote/v1/managed/updates/heartbeat", headers=headers,
                json={"observation": {"supervisor": 1, "line": "dev"}})
    queued = console.post("/api/jremote/v1/updates/queue",
                          json={"target": "leaf-dev", "request_id": "click"})
    assert queued.status_code == 409
    assert "no complete release has been offered on dev" in queued.text
    own = remote.post("/api/jremote/v1/managed/updates/request", headers=headers,
                      json={"request_id": "own"})
    assert own.status_code == 409 and "on dev" in own.text


def test_a_leaf_asks_for_its_line_and_an_old_leaf_asking_nothing_gets_main(rig, release, offers):
    store, _, remote = rig
    offers["dev"] = _dev_offer(release)
    headers = _leaf(store, "leaf-one")
    check = "/api/jremote/v1/managed/updates/check"
    assert remote.post(check, headers=headers).json()["offer"]["manifest"]["release"] == "test-1"
    assert remote.post(check, headers=headers, json={}).json()["offer"]["manifest"]["release"] == "test-1"
    assert remote.post(check, headers=headers,
                       json={"line": "dev"}).json()["offer"]["manifest"]["release"] == "dev-1"
    assert remote.post(check, headers=headers, json={"line": "feature/x"}).status_code == 400


def test_a_leaf_names_its_line_when_it_asks_its_parent(monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(managed_access, "is_leaf", lambda: True)
    monkeypatch.setattr(managed_access, "_post_parent",
                        lambda route, body: asked.append((route, body)) or {"offer": None})
    monkeypatch.setattr(fleet, "config", lambda: {"channel": "dev"})
    assert update_routes.offered() is None
    monkeypatch.setattr(fleet, "config", lambda: {"channel": "stable"})
    update_routes.offered()
    assert asked == [("updates/check", {"line": "dev"}), ("updates/check", {"line": "main"})]


def test_the_heartbeat_carries_the_line_this_machine_is_on(tmp_path):
    from jstack_host.update_macos import MacBackend
    for channel, line in (("dev", "dev"), ("stable", "main"), (None, "main"), ("feature/x", "main")):
        config = {} if channel is None else {"channel": channel}
        assert MacBackend(tmp_path, config).line() == line
