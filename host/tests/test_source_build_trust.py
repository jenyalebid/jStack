"""A machine trusts the Hub it built — and nothing it did not build.

#146: the bundle requirement named a Developer ID team no machine but the
publisher's holds, so every other Mac built a Hub and was refused by its own
installer. What vouches for a build a machine made for itself is the key that
machine minted, pinned and signed the manifest with. What does not move is the
seal.

These drive the real `codesign` and `spctl` against ad-hoc `.app` fixtures,
because what is under test *is* the answer those two give; stubbing them would
assert this suite's idea of Gatekeeper, already known to have been wrong.
`codesign --sign -` is local, so this stays hermetic.
"""
import base64
import hashlib
import json
import plistlib
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jstack_host import app_services, install_signed, release_manifest as releases
from jstack_host.update_macos import MacBackend

HUB = "live.jstack.hub"


def bundle(path: Path, *, identifier=HUB, version="20260923", identity=None,
           sign=True, package=b"# host\n") -> Path:
    """A minimal `.app` with the two files the gate reads, ad-hoc signed.

    `identity` is the sealed `release-identity.json`: `origin` in it is the
    marker the whole exemption turns on, and it is written *before* signing so
    the signature covers it. `sign=False` is the unsigned negative.
    """
    contents = path / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": identifier, "CFBundleExecutable": "Hub",
                       "CFBundleName": "Hub", "CFBundlePackageType": "APPL",
                       "CFBundleVersion": version}, stream)
    (contents / "MacOS/Hub").write_text("#!/bin/sh\nexit 0\n")
    (contents / "MacOS/Hub").chmod(0o755)
    packages = contents / "Resources/packages/jstack_host"
    packages.mkdir(parents=True)
    (packages / "__init__.py").write_bytes(package)
    if identity is not None:
        from jstack_host.sourcestamp import fingerprint
        (packages.parent / "release-identity.json").write_text(json.dumps(
            {"package_sha256": fingerprint(packages), **identity}))
    if sign:
        subprocess.run(["/usr/bin/codesign", "--force", "--options", "runtime", "--sign", "-",
                        str(path)], check=True, capture_output=True)
    return path


def self_built(machine="this-mac") -> dict:
    """What `build_source` seals into a Hub a machine builds for itself."""
    from jstack_host.build_source import source_origin
    return {"sha": "a" * 40, "release": "2026-09-23-aaaaaaaa", "version": "9.9.9",
            "date": "2026-09-23", "github_repo": "owner/repo", "channel": "dev",
            "origin": source_origin(machine)}


def published() -> dict:
    """What the publisher's own release carries: no `origin` at all."""
    identity = self_built()
    del identity["origin"], identity["channel"]
    return identity


# ── The bundle gate: app_services.verify ────────────────────────────────────

def test_a_hub_this_machine_built_is_adopted_on_its_seal_and_its_identifier(tmp_path):
    """#146. An ad-hoc signature satisfies no signing team, and this is the
    whole of what the installer refused: the build worked, the adoption did not."""
    app_services.verify(bundle(tmp_path / "Hub.app", identity=self_built()), HUB)


def test_a_published_release_still_has_to_carry_the_publishers_team(tmp_path):
    """The negative half. Without `origin` the requirement is the one that
    shipped, so the same ad-hoc bundle is refused exactly as it was before."""
    app = bundle(tmp_path / "Hub.app", identity=published())
    with pytest.raises(releases.ReleaseError, match="code requirement"):
        app_services.verify(app, HUB)


def test_a_bundle_with_no_sealed_identity_at_all_takes_the_published_path(tmp_path):
    """Absence is not a claim. A bundle that says nothing about where it came
    from is a published release, and answers to the publisher's team."""
    app = bundle(tmp_path / "Hub.app")
    assert not app_services.source_built(app)
    with pytest.raises(releases.ReleaseError, match="code requirement"):
        app_services.verify(app, HUB)


def test_the_marker_cannot_be_added_to_a_bundle_after_it_is_signed(tmp_path):
    """The reason the decision is taken from inside the signature. Writing
    `origin` beside a signed bundle's code makes the claim and breaks the seal
    that is the only thing making it worth anything."""
    app = bundle(tmp_path / "Hub.app", identity=published())
    (app / "Contents/Resources/packages/release-identity.json").write_text(
        json.dumps(self_built()))
    assert app_services.source_built(app)
    with pytest.raises(releases.ReleaseError, match="sealed resource|invalid"):
        app_services.verify(app, HUB)


def test_a_source_build_whose_code_was_modified_is_refused(tmp_path):
    """A gate that stops checking is not a gate. With no signing team left to
    demand, the seal is the whole of what holds this path up."""
    app = bundle(tmp_path / "Hub.app", identity=self_built())
    (app / "Contents/MacOS/Hub").write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(releases.ReleaseError, match="invalid signature|modified"):
        app_services.verify(app, HUB)


def test_an_unsigned_source_build_is_refused(tmp_path):
    app = bundle(tmp_path / "Hub.app", identity=self_built(), sign=False)
    with pytest.raises(releases.ReleaseError, match="not signed"):
        app_services.verify(app, HUB)


def test_a_source_build_cannot_stand_in_for_another_bundle(tmp_path):
    """Only the signing identity moves. Which bundle this has to be is still
    demanded, or a Hub build would satisfy the gate on the menu bar's behalf."""
    app = bundle(tmp_path / "Hub.app", identifier="live.jstack.other",
                 identity=self_built())
    with pytest.raises(releases.ReleaseError, match="code requirement"):
        app_services.verify(app, HUB)


# ── The fresh install: install_signed.identity ──────────────────────────────

def assessed(monkeypatch) -> list:
    """Record what `identity()` runs itself, leaving codesign real."""
    calls = []
    monkeypatch.setattr(install_signed, "command",
                        lambda argv, **kwargs: calls.append(argv) or "")
    return calls


def test_gatekeeper_is_not_asked_of_a_bundle_this_machine_built(tmp_path, monkeypatch):
    """There is nothing for it to assess: no Developer ID, so no notarisation
    and no ticket to staple one to. Asking anyway was half the refusal."""
    calls = assessed(monkeypatch)
    app = bundle(tmp_path / "Hub.app", identity=self_built())
    assert install_signed.identity(app, HUB)["channel"] == "dev"
    assert calls == []


def test_gatekeeper_is_still_asked_of_a_published_release(tmp_path, monkeypatch):
    """The published path is not relaxed. It reaches spctl here because the
    team requirement is stubbed out of the way, not because it was dropped."""
    calls = assessed(monkeypatch)
    monkeypatch.setattr(app_services, "verify", lambda *args: None)
    app = bundle(tmp_path / "Hub.app", identity=published())
    install_signed.identity(app, HUB)
    assert calls == [["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)]]


def test_a_source_build_whose_package_fingerprint_does_not_match_is_refused(tmp_path, monkeypatch):
    """The fingerprint check stays on both paths. The bundle's sealed identity
    names the bytes of the package inside it; a build that does not hold those
    bytes is not the build that identity describes."""
    assessed(monkeypatch)
    app = bundle(tmp_path / "Hub.app", identity=self_built())
    identity = json.loads((app / "Contents/Resources/packages/release-identity.json").read_text())
    with pytest.raises(ValueError, match="differs from its sealed source identity"):
        install_signed.identity(bundle(tmp_path / "Other.app",
                                       identity={**identity, "package_sha256": "0" * 64}), HUB)
    assert identity["package_sha256"]


# ── The update gate: update_macos._check_app ────────────────────────────────

def backend(tmp_path, public_key: str) -> MacBackend:
    return MacBackend(tmp_path, {"team_id": "MZ95H77RQQ", "menubar_bundle_id": HUB,
                                 "client_bundle_id": "live.jstack.client",
                                 "client_path": str(tmp_path / "jRemote.app"),
                                 "public_key": public_key})


def envelope(key: Ed25519PrivateKey, *, origin: dict | None) -> dict:
    """A signable release: a source build carries no receipts and needs none,
    which is the exemption `origin` already bought inside the signature."""
    components = {name: {"file": name + ".zip", "version": "20260923", "bytes": 4,
                         "sha256": hashlib.sha256(name.encode()).hexdigest()}
                  for name in releases.COMPONENTS}
    manifest = {"schema": 1, "release": "2026-09-23-aaaaaaaa", "components": components,
                "sources": {"stack": "a" * 40, "client": "b" * 40},
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "13.0"}}
    if origin is None:
        manifest["receipts"] = {name: {"result": "passed", "skipped": 0, "source": "a" * 40,
                                       "evidence_sha256": "c" * 64} for name in releases.RECEIPTS}
    else:
        manifest["origin"] = origin
    return releases.sign(manifest, key.private_bytes_raw())


@pytest.fixture
def pinned():
    key = Ed25519PrivateKey.generate()
    return key, base64.b64encode(key.public_key().public_bytes_raw()).decode()


def test_a_source_build_signed_by_a_different_key_is_refused(tmp_path, pinned):
    """The marker is only worth what the signature over it is worth. A machine
    holding someone else's release reads no exemption out of it, and the
    ad-hoc bundle that release names is then refused at the bundle gate —
    which is the whole point of taking the decision from inside a signature."""
    _, public = pinned
    from jstack_host.build_source import source_origin
    job = {"envelope": envelope(Ed25519PrivateKey.generate(), origin=source_origin("some-other-mac"))}
    hub = backend(tmp_path, public)
    assert hub._built_here(job) is False
    app = bundle(tmp_path / "Hub.app", identity=self_built("some-other-mac"))
    with pytest.raises(releases.ReleaseError, match="code requirement"):
        hub._check_app(app, {"version": "20260923"}, "menubar",
                       source_build=hub._built_here(job))


def test_an_envelope_this_machine_cannot_read_grants_no_exemption(tmp_path, pinned):
    """Fail closed. A journal whose envelope is missing or unreadable is not
    an error to raise here — it is a release with nothing vouching for it,
    which is the publisher's path and its refusal."""
    _, public = pinned
    hub = backend(tmp_path, public)
    assert hub._built_here({}) is False
    assert hub._built_here({"envelope": {"manifest": {"origin": {"kind": "source-build"}}}}) is False


def test_the_pinned_key_is_what_turns_the_marker_into_an_exemption(tmp_path, pinned):
    key, public = pinned
    from jstack_host.build_source import source_origin
    assert backend(tmp_path, public)._built_here(
        {"envelope": envelope(key, origin=source_origin("this-mac"))})


def test_a_published_release_carries_no_exemption_to_read(tmp_path, pinned):
    key, public = pinned
    assert not backend(tmp_path, public)._built_here({"envelope": envelope(key, origin=None)})


def test_a_hub_built_here_stages_against_its_identifier_and_not_a_team(tmp_path, pinned):
    _, public = pinned
    app = bundle(tmp_path / "Hub.app", identity=self_built())
    backend(tmp_path, public)._check_app(app, {"version": "20260923"}, "menubar",
                                         source_build=True)


def test_a_published_menubar_still_answers_to_the_team_and_to_gatekeeper(tmp_path, pinned):
    """The same bundle, the same seal, `source_build` False: refused."""
    _, public = pinned
    app = bundle(tmp_path / "Hub.app", identity=published())
    with pytest.raises(releases.ReleaseError, match="code requirement"):
        backend(tmp_path, public)._check_app(app, {"version": "20260923"}, "menubar")


def test_the_client_keeps_the_publishers_team_inside_a_source_build(tmp_path, pinned):
    """jRemote is not built here on any path — a hub carries the publisher's
    bytes forward. Extending the exemption to it would relax a gate that has
    lost nothing."""
    _, public = pinned
    app = bundle(tmp_path / "jRemote.app", identifier="live.jstack.client",
                 identity=self_built())
    with pytest.raises(releases.ReleaseError, match="code requirement"):
        backend(tmp_path, public)._check_app(app, {"version": "20260923"}, "client",
                                             source_build=True)


@pytest.mark.parametrize("source_build", [True, False])
def test_a_broken_signature_is_refused_on_both_paths(tmp_path, pinned, source_build):
    _, public = pinned
    app = bundle(tmp_path / "Hub.app", identity=self_built() if source_build else published())
    (app / "Contents/Resources/extra.txt").write_text("added after signing")
    with pytest.raises(releases.ReleaseError, match="sealed resource|invalid|code requirement"):
        backend(tmp_path, public)._check_app(app, {"version": "20260923"}, "menubar",
                                             source_build=source_build)


@pytest.mark.parametrize("source_build", [True, False])
def test_an_unsigned_bundle_is_refused_on_both_paths(tmp_path, pinned, source_build):
    _, public = pinned
    app = bundle(tmp_path / "Hub.app", sign=False,
                 identity=self_built() if source_build else published())
    with pytest.raises(releases.ReleaseError, match="not signed|code requirement"):
        backend(tmp_path, public)._check_app(app, {"version": "20260923"}, "menubar",
                                             source_build=source_build)


def test_a_source_build_that_is_not_the_bundle_the_release_names_is_refused(tmp_path, pinned):
    """The version check is downstream of the signature and applies to both
    paths; a seal that holds over the wrong bundle is still the wrong bundle."""
    _, public = pinned
    app = bundle(tmp_path / "Hub.app", version="20260101", identity=self_built())
    with pytest.raises(releases.ReleaseError, match="bundle version differs"):
        backend(tmp_path, public)._check_app(app, {"version": "20260923"}, "menubar",
                                             source_build=True)


def test_a_source_build_artifact_that_does_not_match_its_manifest_is_refused(tmp_path, pinned):
    """What binds the bundle to the signed manifest that excused it: the
    fingerprint of the archive it was unpacked from. It is checked before
    anything is unpacked, on a source build exactly as on a publication."""
    key, public = pinned
    from jstack_host.build_source import source_origin
    manifest = releases.verify(envelope(key, origin=source_origin("this-mac")), public)
    item = manifest["components"]["menubar"]
    (tmp_path / item["file"]).write_bytes(b"not-the-signed-bytes")
    with pytest.raises(releases.ReleaseError, match="does not match signed release"):
        releases.check_artifact(tmp_path / item["file"], item)
