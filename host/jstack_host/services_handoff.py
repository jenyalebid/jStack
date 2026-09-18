"""Hub-owned execution of Services replacement, with durable crash recovery.

This controller has no network or shell-command interface. It accepts only a
verified Services candidate and uses the existing sealed-owner transaction.
Fleet update orchestration must coordinate its own journal before submitting.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from . import app_services, service_settings, services_update
from .migrate_services import exclusive, migration_root
from .update_app import control
from .update_supervisor import atomic_json

ROLE = "services-handoff"
TERMINAL = {"updated", "rolled_back", "failed"}


def request_path() -> Path:
    return migration_root() / "services-handoff.json"


def submit(candidate: Path, *, request_id: str | None = None) -> dict:
    """Queue an explicit update; never enable a denied maintenance service."""
    settings = service_settings.read()
    hub = Path(settings["app"])
    app_services.verify(hub)
    state = control(hub, "status").get(ROLE)
    if state not in {"enabled", "not_registered", "not_found"}:
        raise ValueError("independent maintenance service is unavailable or denied")
    candidate = candidate.resolve(strict=True)
    candidate_seal = services_update.seal(candidate)
    with exclusive(migration_root() / "handoff-lock"):
        path = request_path()
        if path.exists() and json.loads(path.read_text()).get("state") not in TERMINAL:
            raise ValueError("unfinished Services handoff requires recovery")
        request_id = request_id or uuid.uuid4().hex
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise ValueError("invalid Services handoff identity")
        request = {"schema": 1, "id": request_id, "state": "pending",
                   "candidate": str(candidate), "candidate_seal": candidate_seal,
                   "settings": services_update.digest(settings)}
        atomic_json(path, request)
        if state in {"not_registered", "not_found"}:
            try:
                result = control(hub, "register", ROLE)
                if result.get("status") != "enabled":
                    raise ValueError("maintenance service did not become enabled")
            except Exception:
                request.update(state="failed", detail="maintenance service did not become enabled")
                atomic_json(path, request)
                raise
        return request


def request_rollback(request_id: str) -> dict:
    settings = service_settings.read()
    hub = Path(settings["app"])
    state = control(hub, "status").get(ROLE)
    if state != "enabled":
        raise ValueError("independent maintenance service is not running")
    with exclusive(migration_root() / "handoff-lock"):
        path = request_path()
        request = json.loads(path.read_text())
        if request.get("id") != request_id or request.get("state") not in {"updated", "rollback_pending", "rolled_back"}:
            raise ValueError("Services rollback does not match its completed handoff")
        if request["state"] != "rolled_back":
            request["state"] = "rollback_pending"
            atomic_json(path, request)
        return request


def reconcile() -> dict | None:
    # Refuse a manual launch from Services: stopping it would kill recovery.
    settings, _ = services_update.context()
    with exclusive(migration_root() / "handoff-lock"):
        path = request_path()
        if not path.exists():
            return None
        request = json.loads(path.read_text())
        if (request.get("schema") != 1 or request.get("settings") != services_update.digest(settings)
                or not isinstance(request.get("id"), str)
                or len(request["id"]) != 32 or any(c not in "0123456789abcdef" for c in request["id"])):
            raise ValueError("invalid or changed Services handoff")
        if request.get("state") in TERMINAL and request.get("state") != "updated":
            return request
        if request.get("state") not in {"pending", "applying", "updated", "rollback_pending"}:
            raise ValueError("unknown Services handoff state")
        name = "services-update-" + request["id"]
        journal = migration_root() / name / "journal.json"
        try:
            if not journal.exists():
                if request["state"] != "pending":
                    raise ValueError("Services handoff lost its recovery journal")
                candidate = Path(request["candidate"])
                if services_update.seal(candidate) != request["candidate_seal"]:
                    raise ValueError("Services candidate changed after handoff")
                services_update.prepare(candidate, transaction_name=name)
            value, _ = services_update.load(journal)
            if value["candidate_seal"] != request["candidate_seal"]:
                raise ValueError("handoff does not match the prepared candidate")
            action = request["state"]
            if action != "rollback_pending":
                request["state"] = "applying"
                atomic_json(path, request)
            if action == "rollback_pending":
                services_update.rollback(journal)
            elif value["state"] == "prepared":
                services_update.apply(journal)
            elif value["state"] not in {"updated", "rolled_back"}:
                services_update.rollback(journal)
            final, owner = services_update.load(journal)
            if final["state"] not in {"updated", "rolled_back"}:
                raise ValueError("Services handoff did not reach a recoverable outcome")
            expected = final["candidate_seal" if final["state"] == "updated" else "previous_seal"]
            if services_update.seal(owner) != expected:
                raise ValueError("installed Services owner differs from the handoff outcome")
            services_update.observe(owner, final["roles"])
            request["state"] = final["state"]
            atomic_json(path, request)
            return request
        except Exception as exc:
            # Once a transaction exists, keep the request recoverable. A
            # transient observation failure must not strand stopped services.
            if not journal.exists():
                request["state"] = "failed"
            request["detail"] = str(exc)
            atomic_json(path, request)
            raise


def main():
    # SMAppService owns this process and its children; launchd restarts it
    # after interruption, even if the Services updater is presently stopped.
    while True:
        try:
            reconcile()
        except Exception as exc:
            print(f"Services handoff: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
