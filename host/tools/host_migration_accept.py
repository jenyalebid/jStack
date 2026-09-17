"""Released standalone host migration evidence on a disposable GUI VirtualMac.

Baseline runs with the released package on PYTHONPATH and its own interpreter.
Later phases run with the candidate package. Never uses production credentials.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["baseline", "prepare", "apply", "verify", "rollback"])
    parser.add_argument("--embedded", action="store_true")
    args = parser.parse_args()
    assert run("/usr/sbin/sysctl", "-n", "hw.model").strip().startswith("VirtualMac")
    assert run("/usr/bin/stat", "-f", "%Su", "/dev/console").strip() == os.environ["USER"]
    assert set(run("/usr/sbin/spctl", "--status", "--verbose").splitlines()) == {
        "assessments enabled", "developer id enabled"}
    home = Path.home()
    state = home / ".local/state/migration-lab"
    evidence = home / "migration-receipts"
    evidence.mkdir(mode=0o700, exist_ok=True)
    os.environ.update(JREMOTE_STATE_DIR=str(state), JREMOTE_HOST_PROFILE="default",
                      JREMOTE_INSTANCE_ROOT=str(home / "lab-agents"))
    from jstack_host import install_host, release_manifest
    from jstack_host.update_supervisor import atomic_json
    public = json.loads((home / "release76/host/jstack_host/release-trust.json").read_text())["public_key"]
    manifest = release_manifest.verify(json.loads((home / "candidate.json").read_text()), public, promoted=False)
    for component in manifest["components"].values():
        release_manifest.check_artifact(home / component["file"], component)
    receipt = {"phase": args.phase, "prior_release": manifest["release"]}
    if args.phase == "baseline":
        assert not state.exists(), "baseline already provisioned"
        for app in (Path("/Applications/JStack Host.app"), Path("/Applications/jRemote.app")):
            run("/usr/bin/codesign", "--verify", "--deep", "--strict", "-R",
                '=anchor apple generic and certificate leaf[subject.OU] = "MZ95H77RQQ"', str(app))
            run("/usr/sbin/spctl", "--assess", "--type", "execute", str(app))
        assert install_host.install(port=9392, bind="127.0.0.1", state_dir=state, out=io.StringIO()) == 0
        job = {"Label": "com.jremote.menubar", "ProgramArguments": [
            "/Applications/JStack Host.app/Contents/MacOS/JStackHostBar"], "RunAtLoad": True,
            "KeepAlive": True, "EnvironmentVariables": {"JREMOTE_STATE_DIR": str(state)}}
        path = install_host.plist_path(job["Label"])
        path.write_bytes(plistlib.dumps(job))
        run("/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path))
        from jstack_host.install_updater import bootstrap
        bootstrap(public, state_dir=state, candidate_test=True)
        receipt["identity_hashes"] = {name: hashlib.sha256((state / name).read_bytes()).hexdigest()
                                      for name in ("host-id", "internal-token")}
    else:
        from jstack_host import migrate_host, service_settings
        root = home / "migration-private"
        if args.phase == "prepare":
            jobs = {role: plistlib.loads(install_host.plist_path(label).read_bytes()) for role, label in {
                "host": "com.jremote.host", "updater": "com.jremote.updater", "menu": "com.jremote.menubar"}.items()}
            settings = {"schema": 1, "app": "/Applications/jStack Hub.app",
                        "services_app": "/Applications/jStack Hub Services.app", "port": 9392,
                        "bind": "127.0.0.1", "migration_dir": str(root),
                        "environment": install_host.carried_environment(jobs["host"]["EnvironmentVariables"])}
            if args.embedded:
                settings.update(host_capability="dashboard", automation_settings=str(root / "automation.json"))
            request = {"settings": settings, "jobs": jobs,
                       "provenance": {role: migrate_host.provenance(install_host.plist_path(job["Label"]))
                                      for role, job in jobs.items()}}
            journal = migrate_host.prepare(request, root)
            atomic_json(evidence / "journal-path.json", {"path": str(journal)})
        else:
            journal = Path(json.loads((evidence / "journal-path.json").read_text())["path"])
            if args.phase == "apply":
                migrate_host.apply(journal)
            elif args.phase == "rollback":
                migrate_host.rollback(journal)
                restored = migrate_host.load(journal)
                assert restored["state"] == "rolled_back"
                assert not service_settings.read()
                for record in restored["files"]:
                    assert migrate_host.file_hash(Path(record["path"])) == record["before"]
                for record in restored["records"]:
                    assert migrate_host.file_hash(Path(record["path"])) == record["sha256"]
                    assert install_host.is_loaded(record["label"])
                assert install_host.wait_for_health(9392)
                deadline = time.monotonic() + 30
                while True:
                    observed = json.loads((state / "updates/observed.json").read_text())
                    if observed.get("updater_source", {}).get("sha") == manifest["sources"]["stack"]:
                        break
                    assert time.monotonic() < deadline, "legacy recovery source did not return"
                    time.sleep(0.25)
                receipt["exact_configuration_and_recovery_restored"] = True
            else:
                boot = run("/usr/sbin/sysctl", "-n", "kern.boottime")
                assert int(re.search(r"sec = (\d+)", boot)[1]) > (evidence / "apply.json").stat().st_mtime
                receipt["verified_after_reboot"] = True
                migrate_host.verify(migrate_host.load(journal))
            baseline = json.loads((evidence / "baseline.json").read_text())
            assert all(hashlib.sha256((state / name).read_bytes()).hexdigest() == digest
                       for name, digest in baseline["identity_hashes"].items())
            receipt["identity_preserved"] = True
            receipt["journal_state"] = migrate_host.load(journal)["state"]
    import httpx
    if args.phase != "prepare":
        token = (state / "internal-token").read_text().strip()
        with httpx.Client(timeout=5, trust_env=False) as client:
            base = "http://127.0.0.1:9392/api/jremote/v1"
            assert client.get(base + "/host").status_code == 401
            response = client.get(base + "/host", headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            receipt["source"] = response.json()["source"]
            if args.embedded:
                response = client.get("http://127.0.0.1:9392/fixture/import")
                response.raise_for_status()
                embedding = response.json()
                assert embedding["profile"] == "embedding-lab"
                assert embedding["state"] == str(state)
                expected = str(home / "release76/host") if args.phase == "rollback" else "/Applications/jStack Hub.app/Contents/Resources/packages"
                assert embedding["package"].startswith(expected + "/")
                receipt["embedding"] = embedding
            if args.phase in {"baseline", "rollback"}:
                assert receipt["source"]["release"] == manifest["release"]
                assert receipt["source"]["sha"] == manifest["sources"]["stack"]
    receipt.update(passed=True, timestamp=time.time())
    atomic_json(evidence / (args.phase + ".json"), receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
