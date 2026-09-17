"""Explicit optional automation capabilities; no shell or privileged jobs.

The signed catalog fixes each allowed launch definition. Machine-local
settings retain the original arguments/environment without publishing them.
Signing this catalog does not attest imported code in an external checkout.
"""
import hashlib
import json
from pathlib import Path
import re

TRIGGERS = {"KeepAlive", "RunAtLoad", "StartInterval", "StartCalendarInterval", "WatchPaths",
            "ThrottleInterval", "ProcessType", "Nice", "ExitTimeOut", "AbandonProcessGroup"}
JOB_KEYS = TRIGGERS | {"Label", "Program", "ProgramArguments", "WorkingDirectory",
                       "EnvironmentVariables", "StandardOutPath", "StandardErrorPath"}


def digest(job: dict) -> str:
    return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(slug: str, job: dict):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", slug):
        raise ValueError("invalid capability identifier")
    if not isinstance(job, dict) or set(job) - JOB_KEYS:
        raise ValueError("unsupported or privileged launch definition")
    if not isinstance(job.get("Label"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", job["Label"]):
        raise ValueError("invalid legacy label")
    argv = job.get("ProgramArguments")
    if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
        raise ValueError("capability requires an explicit argument vector")
    executable = job.get("Program", argv[0])
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise ValueError("capability executable must be absolute")
    for field in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        if field in job and (not isinstance(job[field], str) or not Path(job[field]).is_absolute()):
            raise ValueError("capability paths must be absolute")
    environment = job.get("EnvironmentVariables", {})
    if not isinstance(environment, dict) or any(
            not isinstance(key, str) or not key or "=" in key or "\x00" in key or
            not isinstance(value, str) or "\x00" in value for key, value in environment.items()):
        raise ValueError("invalid capability environment")
    if any("\x00" in arg for arg in argv):
        raise ValueError("invalid capability argument")


def definitions(catalog: dict) -> tuple[dict, dict]:
    if not catalog or not isinstance(catalog, dict):
        raise ValueError("empty automation catalog")
    plists, manifest = {}, {}
    labels = set()
    for slug, job in catalog.items():
        validate(slug, job)
        if job["Label"] in labels:
            raise ValueError("duplicate legacy service in catalog")
        labels.add(job["Label"])
        label = "live.jstack.automation." + slug
        definition = {key: value for key, value in job.items() if key in TRIGGERS}
        definition.update(Label=label, BundleProgram="Contents/MacOS/JStackRuntime",
                          ProgramArguments=["JStackRuntime", "local", slug],
                          AssociatedBundleIdentifiers=["live.jstack.automation"])
        plists[label + ".plist"] = definition
        manifest[slug] = {"plist": label + ".plist", "legacy_label": job["Label"], "job_sha256": digest(job)}
    return plists, manifest
