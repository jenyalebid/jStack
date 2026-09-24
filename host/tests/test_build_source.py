"""Following a ref: what a check costs, and what a build actually produces.

The regression the whole module exists for is in the first test: a check used
to stream every artifact of a release before it would offer one, so the tick
that asks "is there an update" downloaded the update. A check that touches an
artifact byte here fails.
"""
import base64
import hashlib
import json
import os
import tarfile
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jstack_host import build_source, release_manifest as releases
from jstack_host.update_supervisor import Supervisor, atomic_json

HEAD = "f" * 40
OLD = "a" * 40


@pytest.fixture
def publisher():
    """A key, and the published release a hub is holding when it starts."""
    key = Ed25519PrivateKey.generate()
    return key, base64.b64encode(key.public_key().public_bytes_raw()).decode()


def published(feed: Path, key, *, stack=OLD, sequence=5, ref="stable", client=b"client-bytes"):
    components = {}
    for name in releases.COMPONENTS:
        body = client if name == "client" else (name + "-bytes").encode()
        components[name] = {"file": name + ".zip", "version": "1",
                            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    manifest = {"schema": 1, "release": "published-1", "components": components,
                "sequence": sequence, "channel": {"github_repo": "example/stack", "name": ref},
                "sources": {"stack": stack, "client": "b" * 40},
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "13.0"},
                "receipts": {name: {"result": "passed", "skipped": 0, "source": stack,
                                    "evidence_sha256": "c" * 64} for name in releases.RECEIPTS}}
    envelope = releases.sign(manifest, key.private_bytes_raw())
    (feed / manifest["release"]).mkdir(parents=True, exist_ok=True)
    for name, item in components.items():
        body = client if name == "client" else (name + "-bytes").encode()
        (feed / manifest["release"] / item["file"]).write_bytes(body)
    atomic_json(feed / manifest["release"] / "manifest.json", envelope)
    atomic_json(feed / "latest.json", envelope)
    return envelope


def wired(sha=HEAD, status=200):
    """A GitHub that answers the one question a check is allowed to ask."""
    seen = []

    def transport(request):
        seen.append(request)
        if status != 200:
            return httpx.Response(status, text="no such ref")
        return httpx.Response(200, text=sha)
    return seen, httpx.MockTransport(transport)


def setup(tmp_path, publisher, **overrides):
    root, feed = tmp_path / "updates", tmp_path / "feed"
    root.mkdir(parents=True)
    published(feed, publisher[0])
    config = {"github_repo": "example/stack", "feed_dir": str(feed),
              "public_key": publisher[1], "machine": "this-mac", **overrides}
    return root, feed, config


def test_a_check_asks_one_question_and_downloads_no_artifact_byte(tmp_path, publisher):
    root, feed, config = setup(tmp_path, publisher)
    before = {path: path.stat().st_mtime_ns for path in feed.rglob("*") if path.is_file()}
    seen, transport = wired()
    with httpx.Client(transport=transport) as client:
        answer = build_source.check(root, config, client=client, now=1000)
    assert [str(request.url) for request in seen] == [
        "https://api.github.com/repos/example/stack/commits/main"]
    assert seen[0].headers["accept"] == "application/vnd.github.sha"
    # Forty bytes of sha is the entire cost of knowing whether this hub is
    # behind. The old channel paid for the release to find out.
    assert len(HEAD) == 40 and answer["head"] == HEAD
    assert {path: path.stat().st_mtime_ns for path in feed.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("head,expected", [(OLD, "current"), (HEAD, "behind")])
def test_a_check_compares_head_against_the_build_this_hub_holds(tmp_path, publisher, head, expected):
    root, _, config = setup(tmp_path, publisher)
    _, transport = wired(head)
    with httpx.Client(transport=transport) as client:
        build_source.check(root, config, client=client, now=1000)
    status = json.loads((root / "channel.json").read_text())
    assert status["status"] == expected and status["release"] == "published-1"
    assert status["ref"] == "stable" and status["checked"] == 1000


def test_a_check_keeps_the_three_hundred_second_floor(tmp_path, publisher):
    root, _, config = setup(tmp_path, publisher)
    seen, transport = wired()
    with httpx.Client(transport=transport) as client:
        build_source.check(root, config, client=client, now=1000)
        build_source.check(root, config, client=client, now=1000 + build_source.FLOOR - 1)
        assert len(seen) == 1
        build_source.check(root, config, client=client, now=1000 + build_source.FLOOR)
    assert len(seen) == 2


@pytest.mark.parametrize("excluded", ["managed", "candidate_test", "parent_record", "no_repo"])
def test_a_check_never_runs_for_a_leaf_a_managed_mac_or_a_candidate_hub(tmp_path, excluded):
    config = {"github_repo": "example/stack"}
    if excluded == "parent_record":
        (tmp_path / "parent.json").write_text("{}")
    elif excluded == "no_repo":
        config.pop("github_repo")
    else:
        config[excluded] = True
    seen, transport = wired()
    with httpx.Client(transport=transport) as client:
        assert build_source.check(tmp_path / "updates", config, client=client) is None
    assert not seen and not (tmp_path / "updates" / "channel.json").exists()


def test_a_failed_check_is_recorded_where_the_window_reads_it_and_raised(tmp_path, publisher):
    root, _, config = setup(tmp_path, publisher)
    _, transport = wired(status=404)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            build_source.check(root, config, client=client, now=1000)
    status = json.loads((root / "channel.json").read_text())
    assert status["status"] == "failed" and "404" in status["detail"]


def test_a_commit_object_is_read_rather_than_refused(tmp_path, publisher):
    """A proxy that ignores the media type hands back the whole commit. That
    is a working answer to the question asked, so it is used."""
    root, _, config = setup(tmp_path, publisher)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"sha": HEAD}))
    with httpx.Client(transport=transport) as client:
        assert build_source.check(root, config, client=client, now=1000)["head"] == HEAD


def test_an_answer_that_is_not_a_commit_is_refused(tmp_path, publisher):
    root, _, config = setup(tmp_path, publisher)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(releases.ReleaseError, match="commit sha"):
            build_source.check(root, config, client=client, now=1000)


@pytest.mark.parametrize("name", ["../etc", "-rf", "a b", "x" * 200])
def test_a_ref_that_is_not_a_branch_is_refused(name):
    with pytest.raises(releases.ReleaseError):
        build_source.channel_ref({"channel": name})


def test_no_ref_configured_is_the_stable_line_and_stable_is_main():
    assert build_source.channel_ref({}) == releases.STABLE_CHANNEL
    assert build_source.branch(releases.STABLE_CHANNEL) == "main"
    assert build_source.branch("feature/x") == "feature/x"


def test_the_publisher_and_the_builder_share_one_ref_vocabulary():
    """Two copies of this regex would be two answers to "may a hub follow
    this name", and only one of them is the one a signature is compared to."""
    from jstack_host import release_channel
    assert release_channel.CHANNEL is build_source.CHANNEL
    assert release_channel.channel_name is build_source.channel_ref
    assert release_channel.repository is build_source.repository


# ── The tick ────────────────────────────────────────────────────────────────

def supervisor(tmp_path, config):
    token = tmp_path / "token"
    token.write_text("test-token")
    seen = []

    class Backend:
        def observe(self, job):
            return {"release": None, "verified": False}

    def transport(request):
        seen.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(200, text=OLD)
        return httpx.Response(200, json={"job": None})

    client = httpx.Client(transport=httpx.MockTransport(transport))
    settings = {"local_url": "http://hub", "token_path": str(token), **config}
    return Supervisor(tmp_path / "updates", settings, Backend(), client), seen


def test_the_tick_checks_and_never_downloads(tmp_path, publisher):
    root, _, config = setup(tmp_path, publisher)
    daemon, seen = supervisor(tmp_path, config)
    daemon.tick()
    assert [url for url in seen if "github" in url] == [
        "https://api.github.com/repos/example/stack/commits/main"]
    assert json.loads((root / "channel.json").read_text())["status"] == "current"
    assert daemon.channel_error == ""


def test_a_failed_check_reaches_the_window_without_stranding_the_tick(tmp_path, publisher):
    root, _, config = setup(tmp_path, publisher)
    atomic_json(root / "channel.json", {"checked": time.time(), "status": "failed",
                                        "detail": "no such ref"})
    daemon, _ = supervisor(tmp_path, config)
    daemon.tick()
    assert daemon.channel_error == "Source check failed: no such ref"


def test_a_build_in_flight_is_reported_and_suspends_checking(tmp_path, publisher):
    root, _, config = setup(tmp_path, publisher)
    atomic_json(root / "build.json", {"state": "building", "ref": "main", "started": time.time()})
    daemon, seen = supervisor(tmp_path, config)
    daemon.tick()
    assert not [url for url in seen if "github" in url]
    observed = json.loads((root / "observed.json").read_text())
    assert observed["phase"] == "building" and observed["build"]["ref"] == "main"


def test_a_build_record_left_by_a_dead_process_stops_claiming_to_build(tmp_path):
    root = tmp_path / "updates"
    root.mkdir()
    assert build_source.phase(root) == {"state": "idle"}
    atomic_json(root / "build.json", {"state": "building", "started": time.time() - 7 * 3600})
    assert build_source.phase(root)["state"] == "stalled"
    atomic_json(root / "build.json", {"state": "built", "release": "r"})
    assert build_source.phase(root)["state"] == "built"


# ── Trust ───────────────────────────────────────────────────────────────────

def test_the_build_key_is_minted_once_kept_private_and_stable(tmp_path):
    root = tmp_path / "updates"
    private, public = build_source.build_key(root)
    assert (root / "build-key").stat().st_mode & 0o777 == 0o600
    assert build_source.build_key(root) == (private, public)
    signed = releases.sign({**_minimal(), "origin": {"kind": releases.SOURCE_BUILD}}, private)
    assert releases.verify(signed, public)["release"] == "local-1"
    os.chmod(root / "build-key", 0o640)
    with pytest.raises(releases.ReleaseError, match="beyond this account"):
        build_source.build_key(root)


def _minimal() -> dict:
    return {"schema": 1, "release": "local-1",
            "components": {name: {"file": name + ".zip", "version": "1", "bytes": 3,
                                  "sha256": hashlib.sha256(b"abc").hexdigest()}
                           for name in releases.COMPONENTS},
            "sources": {"stack": OLD, "client": "b" * 40},
            "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                              "architecture": "arm64", "minimum_os": "13.0"},
            "receipts": {}}


def test_a_source_build_verifies_without_receipts_but_never_while_carrying_them():
    """The exemption is inside the signed bytes, so only the pinned key grants
    it — and a manifest may not claim both a local build and a publication."""
    manifest = {**_minimal(), "origin": {"kind": releases.SOURCE_BUILD, "machine": "this-mac"}}
    assert releases.validate(manifest)["release"] == "local-1"
    with pytest.raises(releases.ReleaseError, match="acceptance evidence"):
        releases.validate(_minimal())
    with pytest.raises(releases.ReleaseError, match="cannot carry acceptance receipts"):
        releases.validate({**manifest, "receipts": {
            name: {"result": "passed", "skipped": 0, "source": manifest["sources"]["stack"],
                   "evidence_sha256": "c" * 64} for name in releases.RECEIPTS}})


# ── Building ────────────────────────────────────────────────────────────────

@pytest.fixture
def builder(tmp_path, publisher, monkeypatch):
    """A build with git and the Hub compiler replaced, and nothing else.

    Everything this stubs is a subprocess that would reach the network or the
    Swift toolchain. The manifest, the signature, the tarball and the landing
    are the real code under test.
    """
    from jstack_host import build_hub, update_macos
    root, feed, config = setup(tmp_path, publisher)
    calls = []

    def stack_tree(target: Path):
        (target / "plugins/jstack/.claude-plugin").mkdir(parents=True)
        (target / "plugins/jstack/.claude-plugin/plugin.json").write_text('{"version": "9.9.9"}')
        (target / "host/jstack_host").mkdir(parents=True)
        (target / "host/jstack_host/__init__.py").write_text("# host\n")
        (target / "host/jstack_host/release-trust.json").write_text('{"public_key": "old"}')

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["git", "-C"] and "worktree" in argv and "add" in argv:
            stack_tree(Path(argv[-2]))
            return ""
        if "rev-list" in argv:
            return "101\n"
        if argv[0] == "/usr/bin/ditto":
            with zipfile.ZipFile(argv[-1], "w") as bundle:
                bundle.writestr("jStack Hub.app/Contents/Info.plist", "hub")
            return ""
        return ""

    def compile_hub(stack, output, version, signing, **kwargs):
        assert kwargs["trust_key"] and kwargs["date"] and kwargs["release_id"]
        app = Path(output) / "jStack Hub.app"
        app.mkdir(parents=True)
        (app / "built").write_text(kwargs["trust_key"])
        return app

    monkeypatch.setattr(update_macos, "command", command)
    monkeypatch.setattr(build_hub, "build", compile_hub)
    monkeypatch.setattr(build_source, "fetch", lambda *args: HEAD)
    monkeypatch.setattr(build_source.subprocess, "run", lambda *a, **k: None)
    # The grid this hub adopted into, per test. The package-level store is one
    # file for the whole session, so a leaf another module enrolled would
    # otherwise decide whether these builds are allowed to run.
    from jstack_host import store as stores
    from jstack_host.store import SessionStore
    grid = SessionStore(db_path=tmp_path / "grid.sqlite")
    monkeypatch.setattr(stores, "get_store", lambda: grid)
    return root, feed, config, calls


def _adopt(key="leaf-one", name="Office Mac", device="device-1"):
    from jstack_host import store as stores
    grid = stores.get_store()
    grid.upsert_host(key, name, "10.66.0.9")
    grid.bind_host_device(key, device)
    return grid


def test_a_hub_that_adopted_machines_will_not_rotate_the_key_under_them(builder):
    """#144: a build mints this hub's own key and `_offer` rotates
    `public_key` to it, while a leaf verifies every job against the key it
    pinned at install. The first build under a leaf makes it refuse the very
    update that would re-key it, so until #144 is fixed the build is refused."""
    root, feed, config, calls = builder
    _adopt()
    with pytest.raises(releases.ReleaseError, match="#144"):
        build_source.build(root, config)
    # Refused before anything started: no marker, no git, no feed movement.
    assert not (root / "build.json").exists() and not calls
    assert json.loads((feed / "latest.json").read_text())["manifest"]["release"] == "published-1"


def test_a_forgotten_or_unbound_machine_is_not_a_leaf_that_blocks_a_build(builder):
    """Exactly the rows a job is sent to. A tombstone takes no job, and a row
    with no credential has no updater to strand."""
    root, _, config, _ = builder
    grid = _adopt("leaf-gone")
    grid.forget_host("leaf-gone")
    grid.upsert_host("leaf-unbound", "Never Paired", "10.66.0.10")
    assert build_source.adopted() == []
    assert build_source.build(root, config)["release"]


def test_the_hatch_past_the_leaf_refusal_is_typed_and_never_the_default(builder, monkeypatch):
    root, _, config, _ = builder
    _adopt()
    assert build_source.build_refusal(root, config)
    monkeypatch.setenv(build_source.DESPITE_LEAVES, "1")
    assert build_source.build_refusal(root, config) == ""
    assert build_source.build(root, config)["release"]


def test_a_machine_with_nowhere_to_build_from_says_so_instead_of_raising_a_key_error(builder):
    root, _, config, _ = builder
    assert build_source.build_refusal(root, {}) == "this host has no source repository to build from"
    assert build_source.build_refusal(root, {**config, "managed": True}) == \
        "a managed machine takes its builds from its parent"
    assert build_source.build_refusal(root, config) == ""


def test_a_build_lands_an_offer_this_hub_signed_itself(builder, publisher):
    root, feed, config, _ = builder
    atomic_json(root / "config.json", dict(config))
    result = build_source.build(root, config, ref="feature/x")
    _, public = build_source.build_key(root)
    envelope = json.loads((feed / "latest.json").read_text())
    manifest = releases.verify(envelope, public)
    assert manifest["release"] == result["release"] == f"{build_source.release_date()}-{HEAD[:8]}-" \
        + manifest["release"].rsplit("-", 1)[1]
    assert manifest["sources"]["stack"] == HEAD
    assert manifest["origin"] == {"kind": releases.SOURCE_BUILD, "machine": "this-mac"}
    assert manifest["channel"] == {"github_repo": "example/stack", "name": "feature/x"}
    assert manifest["sequence"] == 101 and not manifest["receipts"]
    # The publisher's key is gone: this hub verifies what it builds itself.
    assert json.loads((root / "config.json").read_text())["public_key"] == public
    with pytest.raises(releases.ReleaseError, match="not trusted"):
        releases.verify(envelope, publisher[1])
    assert json.loads((root / "build.json").read_text())["state"] == "built"


def test_a_build_lands_the_shape_stage_already_consumes(builder):
    root, feed, config, _ = builder
    release = build_source.build(root, config)["release"]
    manifest = releases.verify(json.loads((feed / release / "manifest.json").read_text()),
                               build_source.build_key(root)[1])
    for item in manifest["components"].values():
        releases.check_artifact(feed / release / item["file"], item)
    with tarfile.open(feed / release / "stack.tar.gz") as bundle:
        identity = json.loads(bundle.extractfile("host/release-identity.json").read())
        renamed = json.loads(bundle.extractfile("host/build-identity.json").read())
        trust = json.loads(bundle.extractfile("host/jstack_host/release-trust.json").read())
    # stage() reads exactly these three facts back out of the tarball — the
    # stage() of the machine being updated, which may predate the rename and
    # know only the old file name.
    assert identity == renamed and identity["build"] == release
    assert identity["release"] == release and identity["sha"] == manifest["sources"]["stack"]
    assert identity["package_sha256"] and identity["channel"] == "stable"
    assert trust["public_key"] == build_source.build_key(root)[1]


def test_a_build_carries_the_client_artifact_forward_byte_for_byte(builder):
    """jRemote's Mac app is not built here, so the honest thing to name is the
    exact client this hub already holds and has already verified."""
    root, feed, config, _ = builder
    previous = json.loads((feed / "latest.json").read_text())["manifest"]
    release = build_source.build(root, config)["release"]
    manifest = releases.verify(json.loads((feed / "latest.json").read_text()),
                               build_source.build_key(root)[1])
    assert manifest["components"]["client"] == previous["components"]["client"]
    assert manifest["sources"]["client"] == previous["sources"]["client"]
    assert (feed / release / "client.zip").read_bytes() == b"client-bytes"


def test_a_hub_with_nothing_to_carry_forward_refuses_rather_than_half_builds(builder):
    root, feed, config, calls = builder
    (feed / "latest.json").unlink()
    with pytest.raises(releases.ReleaseError, match="carry a client artifact"):
        build_source.build(root, config)
    assert not any("worktree" in " ".join(argv) for argv in calls)
    assert json.loads((root / "build.json").read_text())["state"] == "failed"


def test_rebuilding_the_same_sources_reoffers_those_exact_bytes(builder):
    root, feed, config, calls = builder
    first = build_source.build(root, config)["release"]
    bytes_before = (feed / first / "stack.tar.gz").read_bytes()
    calls.clear()
    assert build_source.build(root, config)["release"] == first
    assert (feed / first / "stack.tar.gz").read_bytes() == bytes_before
    assert not any("worktree" in " ".join(argv) for argv in calls)


def test_a_ref_force_pushed_behind_the_held_build_is_refused(builder, publisher):
    root, feed, config, _ = builder
    published(feed, publisher[0], sequence=500, ref="stable")
    with pytest.raises(releases.ReleaseError, match="behind the build"):
        build_source.build(root, config)
    # A different ref is a rollback, not a downgrade: its count means nothing
    # against the line this hub was on.
    assert build_source.build(root, config, ref="feature/x")["release"]


@pytest.mark.parametrize("excluded", ["managed", "parent_record"])
def test_a_leaf_never_builds(builder, excluded):
    root, _, config, calls = builder
    if excluded == "parent_record":
        (root.parent / "parent.json").write_text("{}")
    else:
        config["managed"] = True
    with pytest.raises(releases.ReleaseError, match="from its parent"):
        build_source.build(root, config)
    assert not calls
