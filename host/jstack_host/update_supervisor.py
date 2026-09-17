"""A restart-independent, journaled updater. No shell commands arrive by HTTP.

Launchd runs this from a stable bootstrap copy, not from the host being
replaced. A job is re-authorized before applying. Interrupted application is
rolled back from the saved transaction before accepting another job.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from pathlib import Path

import httpx

from . import fleet_updates as fleet, release_manifest as releases


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Supervisor:
    def __init__(self, root: Path, configuration: dict, backend, client=None):
        self.root, self.config, self.backend = root, configuration, backend
        self.client = client or httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
        self.journal = root / "job.json"
        self.current = json.loads(self.journal.read_text()) if self.journal.exists() else {}
        self.last_error = ""

    def save(self, **changes):
        self.current.update(changes)
        atomic_json(self.journal, self.current)

    def finalize(self) -> None:
        if self.current.get("finalized"):
            return
        finish = getattr(self.backend, "finalize", None)
        if finish:
            finish(self.current)
        self.save(finalized=True)

    def connection(self) -> tuple[str, str, str]:
        # Reread the adoption on EVERY request. Detach/re-attach invalidates an
        # outstanding job; an old token cached in a long-running daemon must
        # not silently authorize it after the machine has changed owners.
        parent = self.root.parent / "parent.json"
        if parent.exists():
            record = json.loads(parent.read_text())
            base = (f"http://{record['parent_address']}:{int(record.get('parent_port') or 9090)}"
                    if record.get("parent_address") else record.get("parent_url", ""))
            return base, record.get("token", ""), "/managed/updates"
        if self.config.get("managed"):
            raise releases.ReleaseError("machine was detached; queued update authority cancelled")
        return self.config["local_url"], Path(self.config["token_path"]).read_text().strip(), "/updates"

    def heartbeat(self) -> dict:
        base, token, prefix = self.connection()
        if not token or not base:
            raise releases.ReleaseError("no update authority connection")
        observed = self.backend.observe(self.current)
        observed.update(supervisor=1, updater_error=self.last_error)
        atomic_json(self.root / "observed.json", observed)
        body = {"observation": observed}
        if self.current:
            body.update(job_id=self.current["id"], state=self.current["state"],
                        detail=self.current.get("detail", ""))
        if prefix.startswith("/managed"):
            # The leaf's own menu can display progress during a parent outage.
            try:
                local_token = Path(self.config["token_path"]).read_text().strip()
                self.client.post(self.config["local_url"] + "/api/jremote/v1/updates/heartbeat",
                                 json={"observation": observed},
                                 headers={"Authorization": "Bearer " + local_token}).raise_for_status()
            except (httpx.HTTPError, OSError):
                pass  # parent remains the authority; failure never completes a job
        response = self.client.post(base.rstrip("/") + "/api/jremote/v1" + prefix + "/heartbeat",
                                    json=body, headers={"Authorization": "Bearer " + token})
        response.raise_for_status()
        answer = response.json()
        return answer

    def authorize(self) -> None:
        response = self.heartbeat()
        job = response.get("job") or {}
        if job.get("id") != self.current["id"] or job.get("state") not in fleet.ACTIVE:
            raise releases.ReleaseError("update job is no longer authorized")

    def download(self, manifest: dict) -> Path:
        directory = self.root / "releases" / manifest["release"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        base, token, prefix = self.connection()
        for component in manifest["components"].values():
            target = directory / component["file"]
            if target.exists():
                try:
                    releases.check_artifact(target, component)
                    continue
                except releases.ReleaseError:
                    pass
            partial = target.with_name(target.name + ".partial")
            url = base.rstrip("/") + "/api/jremote/v1" + prefix + "/artifact/" + manifest["release"] + "/" + component["file"]
            with self.client.stream("GET", url, headers={"Authorization": "Bearer " + token}) as response:
                response.raise_for_status()
                received = 0
                with partial.open("wb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        received += len(chunk)
                        if received > component["bytes"]:
                            raise releases.ReleaseError("download exceeds signed artifact size")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            releases.check_artifact(partial, component)
            os.replace(partial, target)
        return directory

    def tick(self) -> None:
        # An apply interrupted by reboot/crash never starts from scratch over
        # a half-replaced installation. Recover its exact prior transaction.
        if self.current.get("state") == "applying":
            self.backend.rollback(self.current)
            self.save(state="rolled_back", detail="recovered interrupted application")
        if (self.current.get("state") == "verifying" and
                time.time() - self.current.get("verify_started", 0) > 180):
            # This runs before the network request: losing the parent (or
            # revocation) must not strand an unconfirmed candidate forever.
            self.backend.rollback(self.current)
            self.save(state="rolled_back", detail="verification deadline expired")
        if self.current.get("state") == "current":
            self.finalize()
        reply = self.heartbeat()
        job = reply.get("job")
        if (job and job.get("id") == self.current.get("id") and job.get("state") == "current"
                and self.current.get("state") == "verifying" and self.backend.verify(self.current)):
            # The hub may have committed confirmation just before this process
            # died. Adopt that durable answer instead of timing out a success.
            self.save(state="current", verified=True)
            self.finalize()
        if not job or job.get("state") not in fleet.ACTIVE:
            return
        if self.current.get("id") != job["id"]:
            self.current = dict(job)
            self.save(state="pending")
        if self.current["state"] in fleet.TERMINAL:
            return
        if self.current["state"] == "verifying":
            if self.backend.verify(self.current):
                self.save(state="verifying", verified=True)
                confirmed = self.heartbeat().get("job") or {}
                if confirmed.get("state") == "current":
                    self.save(state="current")
                    self.finalize()
                elif time.time() - self.current["verify_started"] > 180:
                    self.backend.rollback(self.current)
                    self.save(state="rolled_back", detail="hub did not confirm the updated host")
                return
            if time.time() - self.current["verify_started"] < 120:
                return
            self.backend.rollback(self.current)
            self.save(state="rolled_back", detail="updated components failed verification")
            self.heartbeat()
            return
        try:
            manifest = releases.verify(self.current["envelope"], self.config["public_key"],
                                       promoted=not self.config.get("candidate_test", False))
            self.backend.compatible(manifest)
            self.save(state="downloading")
            self.authorize()
            directory = self.download(manifest)
            transaction = self.backend.stage(manifest, directory)
            # The recoverable transaction is durable BEFORE any running byte
            # is replaced. stage may create files but cannot restart anything.
            self.save(transaction=transaction)
            self.authorize()
            self.save(state="applying")
            self.heartbeat()
            self.backend.apply(self.current)
            self.save(state="verifying", verify_started=time.time())
            self.heartbeat()
        except httpx.HTTPError:
            # Preserve downloadable progress across ordinary network loss.
            # If the host disappeared during apply, restore before retrying.
            if self.current.get("state") == "applying":
                self.backend.rollback(self.current)
                self.save(state="rolled_back", detail="authority lost during application")
            raise
        except Exception as exc:
            if self.current.get("state") in {"applying", "verifying"}:
                self.save(detail=str(exc))
                print(f"update application failed: {exc}", flush=True)
                self.backend.rollback(self.current)
                self.save(state="rolled_back", detail=str(exc))
            else:
                self.save(state="failed", detail=str(exc))
            raise

    def run(self, *, once=False):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "supervisor.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            delay = 5
            while True:
                try:
                    self.tick()
                    activate = getattr(self.backend, "activate_runtime", None)
                    if not once and activate and activate(self.current):
                        import sys
                        # The dispatcher is stable; its chosen runtime moves
                        # only after the successful transaction is durable.
                        # flock would survive exec, so release before replacing
                        # this process and let the new copy take the same lock.
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        os.execv(sys.executable, [sys.executable, self.config["dispatcher"],
                                                 "--state-dir", str(self.root.parent)])
                    self.last_error = ""
                    delay = 5
                except Exception as exc:
                    self.last_error = str(exc)
                    print(f"update reconciliation: {type(exc).__name__}: {exc}", flush=True)
                    delay = min(60, delay * 2)
                if once:
                    return
                time.sleep(delay)


def main():
    from . import sourcestamp
    sourcestamp.capture()  # pin this updater's loaded source at process startup
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    root = args.state_dir / "updates"
    configuration = json.loads((root / "config.json").read_text())
    from .update_macos import MacBackend
    Supervisor(root, configuration, MacBackend(root, configuration)).run(once=args.once)


if __name__ == "__main__":
    main()
