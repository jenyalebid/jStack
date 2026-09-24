"""A restart-independent, journaled updater. No shell commands arrive by HTTP.

Launchd runs this from a stable bootstrap copy, not from the host being
replaced. A job is re-authorized before applying. A job that cannot finish
ends failed and restores nothing: going back to a release is switching the ref
and building it, so the failure path reports which release is running rather
than putting a different one there.
"""
from __future__ import annotations

import argparse
import base64
import binascii
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
    #: Settings that name the code this process already loaded. `activate_runtime`
    #: writes them for the NEXT launch, so adopting them mid-life would point a
    #: running transaction at a runtime it did not start on.
    PINNED_SETTINGS = ("runtime_imports", "dispatcher")

    def __init__(self, root: Path, configuration: dict, backend, client=None):
        self.root, self.config, self.backend = root, configuration, backend
        self.client = client or httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
        self.journal = root / "job.json"
        self.current = json.loads(self.journal.read_text()) if self.journal.exists() else {}
        self.last_error = ""
        self.channel_error = ""
        self.build_phase = {"state": "idle"}

    def refresh_settings(self) -> None:
        """Re-read config.json each cycle, for the reason `connection` re-reads
        the adoption: this daemon outlives the settings it started with.

        A setting written while it runs stayed invisible until someone restarted
        it, and the refusal it produced named the key that was by then already
        configured — a report about state the process could not observe. In
        place, because the backend holds this same dict.
        """
        try:
            fresh = json.loads((self.root / "config.json").read_text())
        except (OSError, ValueError):
            return  # Unreadable or mid-write: keep running on what is loaded.
        if not isinstance(fresh, dict):
            return
        merged = {**fresh, **{key: self.config[key]
                              for key in self.PINNED_SETTINGS if key in self.config}}
        if merged != self.config:
            self.config.clear()
            self.config.update(merged)

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

    def abandon(self, reason: str) -> None:
        """End a job that cannot finish, without touching what runs.

        Two things can be true when a job fails, and the machine's owner has
        to be told which: either nothing was replaced and the release that was
        running still is, or the swap had already happened and the machine is
        now on the release that failed. Neither is reversed here — the backend
        only starts what it stopped and clears its own copies.
        """
        outcome = {}
        settle = getattr(self.backend, "settle", None)
        read = getattr(self.backend, "applied", None)
        # Read before settling: the swapped-out bundle is what says the swap
        # happened, and settling is what removes it.
        applied = bool(read(self.current)) if read else False
        self.save(state="failed", applied=applied, detail=f"{reason}; " + (
            f"this machine is running {self.current.get('release', 'the release that failed')}, "
            "which did not pass" if applied else
            "the release it was running is untouched and still running"))
        if settle:
            outcome = settle(self.current) or {}
        if outcome.get("error"):
            self.save(detail=self.current["detail"] + "; " + outcome["error"])

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
        observed.update(supervisor=1, updater_error=self.last_error or self.channel_error,
                        phase=self.build_phase.get("state", "idle"), build=self.build_phase)
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
        if prefix.startswith("/managed"):
            self.pin_parent_key(answer.get("public_key"))
        return answer

    def pin_parent_key(self, key: object) -> None:
        """A managed machine's trust root is its parent, not a publisher (#144).

        The key this machine verifies jobs against came from the bundle that
        installed it. The parent that adopted it signs with its own key, so
        the first build on the parent made every job unverifiable here — and
        the key that would fix it only arrived inside the update this machine
        refused. It arrives here instead, on the connection the parent
        authenticates and the job itself comes down: whatever can hand this
        machine a job can already hand it the key that job is signed with.
        Only a parent's answer reaches this; the local heartbeat never does.
        """
        if not isinstance(key, str) or not key or key == self.config.get("public_key"):
            return
        try:
            if len(base64.b64decode(key, validate=True)) != 32:
                return
        except (ValueError, binascii.Error):
            return
        path = self.root / "config.json"
        try:
            stored = json.loads(path.read_text())
            if not isinstance(stored, dict):
                stored = dict(self.config)
        except (OSError, ValueError):
            stored = dict(self.config)
        stored["public_key"] = key
        atomic_json(path, stored)
        self.config["public_key"] = key
        print("the parent hub signs with a new key; this machine now trusts it", flush=True)

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
        self.refresh_settings()
        # An apply interrupted by reboot/crash never resumes over a
        # half-replaced installation. What it did get done decides the state.
        if self.current.get("state") == "applying":
            recover = getattr(self.backend, "recovery_status", None)
            status = recover(self.current) if recover else "unknown"
            if status == "applied":
                self.save(state="verifying", verify_started=time.time())
            else:
                self.abandon("an interrupted application could not be resumed")
        if (self.current.get("state") == "verifying" and
                time.time() - self.current.get("verify_started", 0) > 180):
            # This runs before the network request: losing the parent (or
            # revocation) must not strand an unconfirmed candidate forever.
            self.abandon("verification deadline expired")
        if self.current.get("state") == "current":
            self.finalize()
        # One small answer from GitHub, and only that: nothing here downloads
        # or builds. A source-check outage must not strand an authorized job,
        # and a build in flight is already the newest answer there is.
        try:
            from . import build_source
            self.build_phase = build_source.phase(self.root)
            if self.build_phase.get("state") != "building":
                # This daemon's own client: one connection pool, and a test
                # that stubs the supervisor's transport stubs this too.
                build_source.check(self.root, self.config, client=self.client)
            status_file = self.root / "channel.json"
            status = json.loads(status_file.read_text()) if status_file.exists() else {}
            self.channel_error = ("Source check failed: " + status.get("detail", "unknown error")
                                  if status.get("status") == "failed" else "")
        except Exception as exc:
            self.channel_error = "Source check failed: " + str(exc)
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
                    self.abandon("hub did not confirm the updated host")
                return
            if time.time() - self.current["verify_started"] < 120:
                return
            self.abandon("updated components failed verification")
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
            # Preserve downloadable progress across ordinary network loss. An
            # application that lost its authority mid-way is over, not retried.
            if self.current.get("state") == "applying":
                self.abandon("authority lost during application")
            raise
        except Exception as exc:
            if self.current.get("state") in {"applying", "verifying"}:
                print(f"update application failed: {exc}", flush=True)
                self.abandon(str(exc))
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
                    restart = getattr(self.backend, "restart_required", None)
                    if not once and restart and restart(self.current):
                        # The supervisor's own bundle was replaced and the job
                        # is confirmed; exit so launchd relaunches this service
                        # from the new bundle instead of running old code on.
                        print("update: supervisor restarting from the replaced bundle", flush=True)
                        return
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
    backend = MacBackend
    if configuration.get("service_model") == "app":
        # This process replaces the bundle it imports from. Load the whole
        # working set now, while sys.path still names this process's code.
        from . import preload
        preload.updater()
        from .update_app import AppBackend
        backend = AppBackend
    Supervisor(root, configuration, backend(root, configuration)).run(once=args.once)


if __name__ == "__main__":
    main()
