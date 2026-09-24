"""Immutable, signed fleet releases. Publishing and deploying are separate.

The trust key comes from local bootstrap configuration, never from the feed.
The same validator runs at promotion and on the machine doing the install.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

SCHEMA = 1
COMPONENTS = {"stack", "menubar", "client"}
#: The journeys promotion requires, checked by `acceptance.gate`. The set moves
#: as journeys are added, renamed and retired, which is why an updater never
#: insists on it: see `validate`.
RECEIPTS = {"fresh_install", "upgrade", "fleet", "offline_catchup", "session_survival",
            "interruption", "revocation", "off_network"}
#: The line a hub follows when nobody chose one. Named rather than empty so a
#: manifest built before channels existed can be read as belonging to it: the
#: releases already published came off main, which is what this names.
STABLE_CHANNEL = "stable"
#: A manifest a hub built for itself from a commit, rather than one a
#: publisher cut. It has no publication and so no acceptance evidence; the
#: marker is inside the signed manifest so only the pinned key can grant the
#: exemption below, and never alongside receipts.
SOURCE_BUILD = "source-build"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")


class ReleaseError(ValueError):
    pass


def identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ReleaseError("invalid release identifier")
    return value


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate(manifest: dict, *, promoted: bool = True) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ReleaseError("unsupported release schema")
    identifier(manifest.get("release"))
    components = manifest.get("components")
    if not isinstance(components, dict) or set(components) != COMPONENTS:
        raise ReleaseError("release components do not match its schema")
    for name, item in components.items():
        if not isinstance(item, dict):
            raise ReleaseError(f"invalid {name} artifact")
        identifier(item.get("file"))
        identifier(item.get("version"))
        if not HEX.fullmatch(str(item.get("sha256", ""))):
            raise ReleaseError(f"invalid {name} checksum")
        size = item.get("bytes")
        if type(size) is not int or not 0 < size <= 4 * 1024**3:
            raise ReleaseError(f"invalid {name} size")
    if len({item["file"] for item in components.values()}) != len(components):
        raise ReleaseError("release artifacts require distinct filenames")
    compatibility = manifest.get("compatibility", {})
    if (compatibility.get("protocol") != 1 or compatibility.get("rollback") is not True
            or compatibility.get("platform") != "macos"
            or compatibility.get("architecture") not in ("arm64", "x86_64", "universal")):
        raise ReleaseError("unsupported compatibility or non-reversible migration")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", str(compatibility.get("minimum_os", ""))):
        raise ReleaseError("minimum macOS version required")
    sources = manifest.get("sources", {})
    if set(sources) != {"stack", "client"} or any(
            not re.fullmatch(r"[a-f0-9]{40}", str(v)) for v in sources.values()):
        raise ReleaseError("exact committed source revisions required")
    origin = manifest.get("origin")
    built = isinstance(origin, dict) and origin.get("kind") == SOURCE_BUILD
    if built and manifest.get("receipts"):
        raise ReleaseError("a source build cannot carry acceptance receipts")
    if promoted and not built:
        # Every receipt the release carries, against these exact artifacts —
        # and not a demand for one particular set of journey NAMES. This code
        # also runs on the machine taking the update, where it is years older
        # than the release it is judging: an updater that insisted on the names
        # it shipped with refused every release that renamed or retired a
        # journey, and the machine stayed joined, online and un-updatable
        # forever (#123). Which journeys a candidate must survive is decided
        # where the names are current — `acceptance.gate`, at promotion.
        receipts = manifest.get("receipts", {})
        artifact_set = hashlib.sha256(canonical(components)).hexdigest()
        if not isinstance(receipts, dict) or not receipts:
            raise ReleaseError("a promoted release carries no acceptance evidence")
        for name, receipt in sorted(receipts.items()):
            if (not isinstance(receipt, dict) or receipt.get("result") != "passed"
                    or receipt.get("skipped") != 0
                    or receipt.get("artifacts") != artifact_set
                    or not HEX.fullmatch(str(receipt.get("evidence_sha256", "")))):
                raise ReleaseError(f"missing or mismatched acceptance receipt: {name}")
    return manifest


def verify(envelope: dict, public_key: str, *, promoted: bool = True) -> dict:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        manifest = envelope["manifest"]
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(envelope["signature"], validate=True), canonical(manifest))
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ReleaseError("release signature not trusted") from exc
    return validate(manifest, promoted=promoted)


def sign(manifest: dict, private_key: bytes, *, promoted: bool = True) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    validate(manifest, promoted=promoted)
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(canonical(manifest))
    return {"manifest": manifest, "signature": base64.b64encode(signature).decode()}


def check_artifact(path: Path, item: dict) -> None:
    if not path.is_file() or path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
        raise ReleaseError(f"artifact does not match signed release: {item['file']}")
