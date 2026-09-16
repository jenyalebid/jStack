"""Three independent host processes, real HTTP, persisted hub-owned policy.

This proves the authority chain, not a WireGuard handshake. The installer is
the only replaced edge: OS-level installation belongs to the clean-Mac rig.
No live registry, token, tunnel, agent or session is read or changed.
"""

import contextlib
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import httpx


def _serve_node(folder, key, port, pipe):
    os.environ.update(JREMOTE_HOST_PROFILE="default", JREMOTE_STATE_DIR=folder,
                      JREMOTE_INSTANCE_ROOT=str(Path(folder) / "Agents"),
                      JREMOTE_HOST_ID=key, JREMOTE_HOST_NAME=key,
                      JREMOTE_TOKEN_PATH=str(Path(folder) / "unused-token"))
    from jstack_host import attach_parent, devices, enrolment, grants, hostenv, mode, store, tunnel
    from jstack_host.server import create_app
    import uvicorn

    hostenv.reset_profile()
    store._store = store.SessionStore(db_path=Path(folder) / "registry.sqlite")
    mode.current = lambda: {"mode": "managed" if attach_parent.parent_record() else "open"}
    mode.is_hub = lambda: not bool(attach_parent.parent_record())
    hostenv.security_alert = lambda _: None
    enrolment._own_mesh_address = lambda: "127.0.0.1"
    tunnel.can_pair = lambda: True
    bundle = {name: "test installer boundary\n" for name in tunnel.LEAF_FILES}
    bundle["leaf.env"] = "WG_ADDR=127.0.0.1/32\n"
    tunnel.issue = lambda name, leaf=False: {"device": name, "bundle": bundle, "created": True}
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                           lifespan="off", log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    if not server.started:
        pipe.send({"error": "server did not start"})
        return
    pipe.send({"token": devices.internal_token()})
    try:
        while True:
            command = pipe.recv()
            if command["op"] == "stop":
                break
            if command["op"] == "attach":
                try:
                    result = attach_parent.attach(command["code"], command["parent"],
                        host_key=key, port=port, sudo=False,
                        runner=lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))
                    pipe.send({"attached": result["delegated"], "token": result["token"]})
                except Exception as exc:
                    pipe.send({"error": type(exc).__name__ + ": " + str(exc)})
            elif command["op"] == "count":
                pipe.send({"devices": len(store.get_store().list_devices())})
            elif command["op"] == "detach":
                grants.revoke_issued()
                pipe.send({"revoked": True})
    finally:
        server.should_exit = True
        thread.join(timeout=5)


class Node:
    def __init__(self, folder, key):
        self.folder, self.key = str(folder), key
        folder.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None

    def start(self):
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_serve_node,
            args=(self.folder, self.key, self.port, child))
        self.process.start()
        assert self.pipe.poll(15), "isolated host startup timed out"
        response = self.pipe.recv()
        assert "error" not in response, response.get("error")
        self.token = response["token"]

    def stop(self):
        if self.process is None:
            return
        if self.process.is_alive():
            self.pipe.send({"op": "stop"})
            self.process.join(timeout=10)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=5)
        self.pipe.close()
        self.process = None

    def command(self, op, **data):
        self.pipe.send({"op": op, **data})
        assert self.pipe.poll(15), "isolated host command timed out"
        result = self.pipe.recv()
        assert "error" not in result, result.get("error")
        return result

    def call(self, method, path, *, token=None, body=None):
        return httpx.request(method, self.url + "/api/jremote/v1" + path,
            headers={"Authorization": "Bearer " + (token or self.token)},
            json=body, timeout=10, trust_env=False)


def test_pair_adopt_restrict_revoke_restart_over_real_http(tmp_path):
    hub = Node(tmp_path / "hub", "hub-main")
    first = Node(tmp_path / "first", "leaf-first")
    second = Node(tmp_path / "second", "leaf-second")
    with contextlib.ExitStack() as cleanup:
        for node in (hub, first, second):
            cleanup.callback(node.stop)
            node.start()

        # Pair through the same one-time-code endpoints the app uses.
        code = hub.call("POST", "/enrolment/codes", body={"name": "phone"}).json()["code"]
        response = hub.call("POST", "/enrolment/redeem", body={"code": code, "identity": "phone-one"})
        assert response.status_code == 200
        phone, phone_id = response.json()["token"], response.json()["device"]["id"]

        leaf_tokens = {}
        for leaf in (first, second):
            code = hub.call("POST", "/enrolment/codes",
                            body={"name": leaf.key, "kind": "host"}).json()["code"]
            result = leaf.command("attach", code=code, parent=hub.url)
            assert result["attached"] is True
            leaf_tokens[leaf.key] = result["token"]
        assert {r["key"] for r in hub.call("GET", "/hosts", token=phone).json()["hosts"]} == {
            first.key, second.key}
        visible = first.call("GET", "/hosts")
        assert visible.status_code == 200, visible.text
        assert {r["key"] for r in visible.json()["hosts"]} == {
            hub.key, second.key}
        assert first.call("GET", "/devices").json() == {"devices": []}
        assert first.call("POST", "/devices", body={"name": "forbidden"}).status_code == 403

        granted = hub.call("POST", f"/hosts/{first.key}/grant", token=phone, body={}).json()["token"]
        assert first.call("GET", "/agents", token=granted).status_code == 200
        count = first.command("count")["devices"]
        for _ in range(4):
            again = hub.call("POST", f"/hosts/{first.key}/grant", token=phone, body={}).json()["token"]
            assert again == granted
        assert first.command("count")["devices"] == count
        sibling = first.call("POST", f"/hosts/{second.key}/grant", body={}).json()["token"]
        assert second.call("GET", "/agents", token=sibling).status_code == 200

        # Both flags, every combination. Cached credentials are checked too.
        for home, others in [(False, True), (True, False), (False, False), (True, True)]:
            changed = hub.call("POST", f"/hosts/{first.key}/visibility",
                              body={"sees_home": home, "sees_leaves": others})
            assert changed.status_code == 200
            expected = ({hub.key} if home else set()) | ({second.key} if others else set())
            assert {r["key"] for r in first.call("GET", "/hosts").json()["hosts"]} == expected
            assert hub.call("GET", "/agents", token=leaf_tokens[first.key]).status_code == (200 if home else 403)
            assert second.call("GET", "/agents", token=sibling).status_code == (200 if others else 403)

        first.stop()
        first.start()
        assert first.call("GET", "/agents", token=granted).status_code == 200
        hub.stop()
        assert first.call("GET", "/agents", token=granted).status_code == 403
        hub.start()
        assert first.call("GET", "/agents", token=granted).status_code == 200
        assert hub.call("POST", f"/devices/{phone_id}/revoke", body={}).status_code == 200
        assert first.call("GET", "/agents", token=granted).status_code == 403
        second.command("detach")
        assert second.call("GET", "/agents", token=sibling).status_code == 403
