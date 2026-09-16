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
RECEIPTS = {"fresh_install", "upgrade", "fleet", "offline_catchup", "session_survival",
            "interruption", "rollback", "revocation", "cellular"}
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
        raise ReleaseError("a release must include stack, menubar and client")
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
    if promoted:
        receipts = manifest.get("receipts", {})
        artifact_set = hashlib.sha256(canonical(components)).hexdigest()
        for name in RECEIPTS:
            receipt = receipts.get(name, {})
            if (receipt.get("result") != "passed" or receipt.get("skipped") != 0
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
