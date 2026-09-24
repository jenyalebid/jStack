import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jstack_host import build_hub, publish_release, release_channel, release_manifest as releases
from jstack_host.update_macos import MacBackend


def manifest():
    components = {name: {"file": name + ".zip", "version": "18", "bytes": 1,
                         "sha256": "a" * 64} for name in releases.COMPONENTS}
    return {"schema": releases.SCHEMA, "release": "18-source", "components": components,
            "sources": {"stack": "a" * 40, "client": "b" * 40},
            "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                              "architecture": "arm64", "minimum_os": "26.0"},
            "receipts": {name: {"result": "passed", "skipped": 0, "source": "a" * 40,
                                "evidence_sha256": "b" * 64} for name in releases.RECEIPTS}}


def test_receipts_bind_the_exact_commit():
    """Not the artifact bytes: every Mac builds the Hub itself, so two honest
    installs of one commit never share a digest and binding on bytes would
    refuse a fleet its own receipts. The commit still has to match."""
    value = manifest()
    releases.validate(value)
    value["components"]["menubar"]["sha256"] = "c" * 64
    releases.validate(value)
    value["sources"]["stack"] = "f" * 40
    with pytest.raises(ValueError, match="receipt"):
        releases.validate(value)


def test_retired_services_owner_cannot_reenter_the_feed():
    value = manifest()
    value["components"]["services"] = dict(value["components"]["menubar"], file="services.zip")
    with pytest.raises(ValueError, match="components"):
        releases.validate(value, promoted=False)
    del value["components"]["services"]
    value["schema"] = 2
    with pytest.raises(ValueError, match="schema"):
        releases.validate(value, promoted=False)


def test_owner_artifacts_cannot_share_a_download_filename():
    value = manifest()
    value["components"]["client"]["file"] = value["components"]["menubar"]["file"]
    with pytest.raises(ValueError, match="distinct"):
        releases.validate(value, promoted=False)


def test_legacy_backend_refuses_a_superseded_native_schema(tmp_path):
    with pytest.raises(ValueError, match="schema"):
        MacBackend(tmp_path, {}).compatible({**manifest(), "schema": 2})


def test_qualified_native_release_reaches_publication_and_receipt_gates(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    envelope = releases.sign(manifest(), key.private_bytes_raw())
    for name in ("manifest.json", "candidate.json"):
        (tmp_path / name).write_text(json.dumps(envelope))
    def publication_reached(*args, **kwargs):
        raise RuntimeError("publication reached")
    def receipt_gate_reached(*args, **kwargs):
        raise RuntimeError("receipt gate reached")
    monkeypatch.setattr(release_channel.releases, "check_artifact", lambda *args: None)
    monkeypatch.setattr(release_channel.subprocess, "run", publication_reached)
    with pytest.raises(RuntimeError, match="publication reached"):
        release_channel.publish(tmp_path, "example/stack", base64.b64encode(key.public_key().public_bytes_raw()).decode())
    monkeypatch.setattr(publish_release.acceptance, "gate", receipt_gate_reached)
    with pytest.raises(RuntimeError, match="receipt gate reached"):
        publish_release.promote(tmp_path, tmp_path / "receipts", tmp_path / "feed", key.private_bytes_raw())
    assert not (tmp_path / "feed").exists()


def test_publisher_binds_the_public_hub_to_its_release_identity(tmp_path, monkeypatch):
    stack, output = tmp_path / "stack", tmp_path / "output"
    (stack / "host").mkdir(parents=True)
    output.mkdir()
    identity = {"release": "2026-09-21-source", "github_repo": "example/stack", "date": "2026-09-21"}
    (stack / "host/release-identity.json").write_text(json.dumps(identity))
    capabilities = {"dashboard": {"label": "live.jstack.automation.dashboard"}}
    catalog = tmp_path / "automation-catalog.json"
    catalog.write_text(json.dumps(capabilities))
    calls = []

    def build(source, destination, version, config, **kwargs):
        calls.append((source, version, kwargs))
        destination.mkdir()
        return destination / "Hub.app"

    def notarize(app, destination, config):
        (destination / "hub-notarized.zip").write_bytes(destination.name.encode())

    monkeypatch.setattr(build_hub, "build", build)
    monkeypatch.setattr(build_hub, "notarize", notarize)
    publish_release.sign_hub(stack, output, "0.70.0", {"local_catalog": str(catalog)})
    # The public feed artifact carries an empty catalog; the publisher's own
    # capability definitions reach only the machine-local variant.
    assert [c[2]["catalog"] for c in calls] == [None, capabilities]
    for source, version, kwargs in calls:
        assert source == stack and version == "0.70.0"
        assert kwargs["release_id"] == identity["release"]
        assert kwargs["github_repo"] == identity["github_repo"]
        assert kwargs["date"] == "2026-09-21"
    assert (output / "menubar-notarized.zip").read_bytes() != (output / "hub-catalog.zip").read_bytes()
