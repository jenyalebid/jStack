"""Explicit optional automation capabilities; no shell or privileged jobs.

The signed catalog fixes each allowed launch definition. Machine-local
settings retain the original arguments/environment without publishing them.
Signing this catalog does not attest imported code in an external checkout.
"""
import hashlib
import json
from pathlib import Path
import re

TRIGGERS = {"KeepAlive", "RunAtLoad", "StartInterval", "StartCalendarInterval",
            "ThrottleInterval", "ProcessType", "Nice", "ExitTimeOut", "AbandonProcessGroup", "LimitLoadToSessionType"}
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
    # Only public scheduling metadata goes into the notarized bundle. Paths,
    # legacy identifiers, arguments and environment stay machine-local.
    for key in ("RunAtLoad", "AbandonProcessGroup"):
        if key in job and type(job[key]) is not bool:
            raise ValueError("invalid boolean trigger")
    if job.get("AbandonProcessGroup"):
        raise ValueError("capabilities must retain launchd process-group cleanup")
    for key in ("StartInterval", "ThrottleInterval", "ExitTimeOut", "Nice"):
        if key in job and (type(job[key]) is not int or job[key] < (-20 if key == "Nice" else 0)):
            raise ValueError("invalid numeric trigger")
    if "ProcessType" in job and (not isinstance(job["ProcessType"], str) or job["ProcessType"] not in {"Standard", "Background", "Interactive", "Adaptive"}):
        raise ValueError("invalid process type")
    if "LimitLoadToSessionType" in job:
        sessions = job["LimitLoadToSessionType"]
        sessions = sessions if isinstance(sessions, list) else [sessions]
        if not sessions or any(not isinstance(item, str) or item not in {"Aqua", "Background", "LoginWindow", "StandardIO", "System"} for item in sessions):
            raise ValueError("invalid session type")
    if "KeepAlive" in job and type(job["KeepAlive"]) is not bool:
        keep = job["KeepAlive"]
        if (not isinstance(keep, dict) or set(keep) - {"SuccessfulExit", "Crashed", "NetworkState"} or
                any(type(value) is not bool for value in keep.values())):
            raise ValueError("unsupported keepalive policy")
    if "StartCalendarInterval" in job:
        intervals = job["StartCalendarInterval"]
        intervals = intervals if isinstance(intervals, list) else [intervals]
        if not intervals:
            raise ValueError("empty calendar trigger")
        bounds = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31), "Weekday": (0, 7), "Month": (1, 12)}
        for interval in intervals:
            if (not isinstance(interval, dict) or not interval or set(interval) - bounds.keys() or
                    any(type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]
                        for key, value in interval.items())):
                raise ValueError("invalid calendar trigger")


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
        manifest[slug] = {"plist": label + ".plist", "job_sha256": digest(job)}
    return plists, manifest
