"""Destructive lifecycle checks, refused outside a disposable macOS VM.

Run with the candidate's JStackPython. No production paths or credentials
are transferred into the guest. Reboot is a separate, observable phase.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


def run(*arguments):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"{Path(arguments[0]).name}: {result.stderr[-2000:]}")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["start", "reboot", "remove"])
    parser.add_argument("--app", type=Path, default=Path("/Applications/jStack Hub.app"))
    args = parser.parse_args()
    model = run("/usr/sbin/sysctl", "-n", "hw.model").strip()
    if not model.startswith("VirtualMac"):
        raise SystemExit("REFUSED: lifecycle acceptance requires a disposable VirtualMac")
    app = args.app
    controller = str(app / "Contents/MacOS/JStackHub")
    runtime = str(app / "Contents/MacOS/JStackRuntime")
    state = Path.home() / ".local/state/jremote/service-acceptance"
    settings = state.parent / "service-settings.json"
    receipts = Path.home() / "service-acceptance-receipts"
    receipts.mkdir(exist_ok=True)
    evidence = {"phase": args.phase, "model": model,
                "executable_sha256": hashlib.sha256(Path(runtime).read_bytes()).hexdigest()}
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", str(app))
    run("/usr/sbin/spctl", "--assess", "--type", "execute", str(app))
    evidence["runtime"] = json.loads(run(runtime, "self-test"))
    assert evidence["runtime"]["isolated"] is True
    assert evidence["runtime"]["stdio"] == "utf-8"
    assert str(app) in evidence["runtime"]["prefix"]
    assert str(app) in evidence["runtime"]["package"]
    if args.phase == "start":
        if settings.exists():
            previous = json.loads(settings.read_text())
            if previous.get("environment", {}).get("JREMOTE_STATE_DIR") != str(state):
                raise RuntimeError("refusing to replace another fixture's service settings")
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"schema": 1, "app": str(app), "port": 9391, "bind": "127.0.0.1",
            "environment": {"JREMOTE_STATE_DIR": str(state), "JREMOTE_HOST_PROFILE": "default",
                            "JREMOTE_TMUX_SOCK": "service-identity-accept"}}))
        settings.chmod(0o600)
        for role in ("host", "menu"):
            answer = json.loads(run(controller, "register", role))
            assert answer["status"] == "enabled", answer
    if args.phase in ("start", "reboot"):
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen("http://127.0.0.1:9391/api/health", timeout=2) as response:
                    health = json.load(response)
                break
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    raise RuntimeError("service did not become healthy")
                time.sleep(0.2)
        assert health["service"] == "jremote-host"
        assert health["source"]["sha"] and health["source"]["dirty"] is False
        evidence["health"] = health
        for role in ("host", "menu"):
            observed = run("/bin/launchctl", "print", f"gui/{os.getuid()}/live.jstack.hub.{role}")
            assert "state = running" in observed
            assert "parent bundle identifier = live.jstack.hub" in observed
            evidence[role] = observed
        # No credential appears in a receipt. Verify the real authenticated
        # sessions endpoint using only the guest's newly minted local token.
        token_paths = list(state.rglob("internal-token"))
        assert len(token_paths) == 1
        token = token_paths[0].read_text().strip()
        url = "http://127.0.0.1:9391/api/jremote/v1/sessions/active"
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert isinstance(json.load(response)["sessions"], list)
        try:
            urllib.request.urlopen(url, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("unauthenticated sessions request was accepted")
        evidence["authenticated_sessions"] = "passed"
        evidence["unauthenticated_sessions"] = 401
    else:
        for role in ("menu", "host"):
            run(controller, "unregister", role)
        deadline = time.monotonic() + 30
        while True:
            loaded = [role for role in ("host", "menu") if subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/live.jstack.hub.{role}"],
                capture_output=True).returncode == 0]
            if not loaded:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("unregistered service is still loaded")
            time.sleep(0.2)
        evidence["services_absent"] = True
        assert state.is_dir(), "uninstall must retain identity and data"
    (receipts / f"{args.phase}.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"phase": args.phase, "result": "passed", "receipt": str(receipts / f"{args.phase}.json")}))


if __name__ == "__main__":
    main()
