import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jstack_host import build_hub, publish_release, release_channel, release_manifest as releases
from jstack_host.update_macos import MacBackend


def manifest():
    components = {name: {"file": name + ".zip", "version": "18", "bytes": 1,
                         "sha256": "a" * 64} for name in releases.NATIVE_COMPONENTS}
    artifacts = hashlib.sha256(releases.canonical(components)).hexdigest()
    return {"schema": 2, "release": "18-source", "components": components,
            "sources": {"stack": "a" * 40, "client": "b" * 40},
            "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                              "architecture": "arm64", "minimum_os": "26.0"},
            "receipts": {name: {"result": "passed", "skipped": 0, "artifacts": artifacts,
                                "evidence_sha256": "b" * 64} for name in releases.RECEIPTS}}


def test_native_receipts_bind_the_independent_recovery_artifact():
    value = manifest()
    releases.validate(value)
    value["components"]["services"]["sha256"] = "c" * 64
    with pytest.raises(ValueError, match="receipt"):
        releases.validate(value)


def test_native_format_cannot_be_misrepresented_as_legacy():
    value = manifest()
    value["schema"] = 1
    with pytest.raises(ValueError, match="components"):
        releases.validate(value, promoted=False)
    del value["components"]["services"]
    releases.validate(value, promoted=False)


def test_owner_artifacts_cannot_share_a_download_filename():
    value = manifest()
    value["components"]["services"]["file"] = value["components"]["menubar"]["file"]
    with pytest.raises(ValueError, match="distinct"):
        releases.validate(value, promoted=False)


def test_legacy_backend_refuses_native_owner_update_before_platform_work(tmp_path):
    with pytest.raises(ValueError, match="self-update"):
        MacBackend(tmp_path, {}).compatible(manifest())


def test_native_publication_is_closed_even_with_complete_receipts(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    envelope = releases.sign(manifest(), key.private_bytes_raw())
    for name in ("manifest.json", "candidate.json"):
        (tmp_path / name).write_text(json.dumps(envelope))
    monkeypatch.setattr(release_channel.subprocess, "run", lambda *a, **k: pytest.fail("must not access publication"))
    with pytest.raises(ValueError, match="self-update"):
        release_channel.publish(tmp_path, "example/stack", base64.b64encode(key.public_key().public_bytes_raw()).decode())
    with pytest.raises(ValueError, match="self-update"):
        publish_release.promote(tmp_path, tmp_path / "receipts", tmp_path / "feed", key.private_bytes_raw())
    assert not (tmp_path / "feed").exists()


def test_publisher_binds_both_public_owners_to_its_release_identity(tmp_path, monkeypatch):
    stack, output = tmp_path / "stack", tmp_path / "output"
    (stack / "host").mkdir(parents=True)
    output.mkdir()
    identity = {"release": "18-source", "github_repo": "example/stack", "build": 18}
    (stack / "host/release-identity.json").write_text(json.dumps(identity))
    calls = []

    def build(source, destination, version, config, **kwargs):
        calls.append((source, version, kwargs))
        destination.mkdir()
        return destination / "Owner.app"

    def notarize(app, destination, config):
        (destination / "hub-notarized.zip").write_bytes(destination.name.encode())

    monkeypatch.setattr(build_hub, "build", build)
    monkeypatch.setattr(build_hub, "notarize", notarize)
    publish_release.sign_service_owners(stack, output, "0.70.0", {})
    assert [c[2]["recovery"] for c in calls] == [False, True]
    for source, version, kwargs in calls:
        assert source == stack and version == "0.70.0"
        assert kwargs["release_id"] == identity["release"]
        assert kwargs["github_repo"] == identity["github_repo"]
        assert kwargs["build_number"] == 18 and "catalog" not in kwargs
    assert (output / "menubar-notarized.zip").read_bytes() != (output / "services-notarized.zip").read_bytes()
