import json
import plistlib

from jstack_host import sos


def test_client_identity_comes_from_the_install_record_and_app(tmp_path):
    state = tmp_path / "state"
    (state / "updates").mkdir(parents=True)
    (state / "updates/config.json").write_text(json.dumps({"client_bundle_id": "example.client"}))
    app = tmp_path / "Applications/jRemote.app/Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "example.other"}))
    found = sos.client_identifiers({"JREMOTE_STATE_DIR": str(state)}, tmp_path)
    assert found[:1] == ["example.client"]
    assert "example.other" in found


def test_client_identity_rejects_paths_and_garbage(tmp_path):
    state = tmp_path / "state"
    (state / "updates").mkdir(parents=True)
    for value in ("../escape", "a/b.c", "", 7, "nodot"):
        (state / "updates/config.json").write_text(json.dumps({"client_bundle_id": value}))
        assert value not in sos.client_identifiers({"JREMOTE_STATE_DIR": str(state)}, tmp_path)
    (state / "updates/config.json").write_text("not json")
    sos.client_identifiers({"JREMOTE_STATE_DIR": str(state)}, tmp_path)
