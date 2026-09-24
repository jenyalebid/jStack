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
#: What a machine is running: the day it was made, the commit it came from and
#: the sources it was made out of. It was called a release while jStack
#: published them, and the identifier itself has not changed — only the word.
KEY = "build"
#: The name that key had before. Machines built before the rename write it and
#: read nothing else, and a hub and a leaf sit on either side of the rename for
#: as long as it takes the fleet to come across. Both names are written until
#: no machine in the fleet still reads this one (jStack #177).
LEGACY_KEY = "release"
#: The file a built bundle carries its identity in, beside the packages it
#: ships. An installed Hub predating the rename has the old name on disk and
#: is read by whatever installer or updater arrives next, so both are read.
IDENTITY_FILE = "build-identity.json"
LEGACY_IDENTITY_FILE = "release-identity.json"


class ReleaseError(ValueError):
    pass


def identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ReleaseError("invalid build identifier")
    return value


def build_id(value: object) -> str:
    """The build a manifest, an identity file or a machine's report names.

    Either name answers. Anything that crosses machines writes both, so what
    comes back is whichever name the far side's own code knows.
    """
    if not isinstance(value, dict):
        return ""
    return str(value.get(KEY) or value.get(LEGACY_KEY) or "")


def named(build: str) -> dict:
    """One build under both its names, for anything another machine reads."""
    return {KEY: build, LEGACY_KEY: build}


def identity_file(directory: Path) -> Path:
    """Where a built bundle records what it is, under whichever name it used.

    Returns the current name when neither exists, so a writer gets the name to
    write and a reader gets the name that is actually there.
    """
    legacy = directory / LEGACY_IDENTITY_FILE
    current = directory / IDENTITY_FILE
    return legacy if legacy.is_file() and not current.is_file() else current


def write_identity(directory: Path, identity: dict) -> dict:
    """Lay a bundle's identity down under both names, both keys inside.

    The machine that installs a bundle runs the code it had before, not the
    code inside the bundle: a Hub on the last release reads
    `release-identity.json` out of the tarball and out of the app it staged,
    and refuses the update when the file is not there. So the first build
    after the rename is exactly the one that must still carry the old name.
    """
    identity = {**identity, **named(build_id(identity))}
    text = json.dumps(identity) + "\n"
    for name in (IDENTITY_FILE, LEGACY_IDENTITY_FILE):
        (directory / name).write_text(text)
    return identity


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate(manifest: dict, *, promoted: bool = True) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ReleaseError("unsupported release schema")
    identifier(build_id(manifest))
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
        # Every receipt the release carries, against this exact commit — and
        # not a demand for one particular set of journey NAMES. This code
        # also runs on the machine taking the update, where it is years older
        # than the release it is judging: an updater that insisted on the names
        # it shipped with refused every release that renamed or retired a
        # journey, and the machine stayed joined, online and un-updatable
        # forever (#123). Which journeys a candidate must survive is decided
        # where the names are current — `acceptance.gate`, at promotion.
        receipts = manifest.get("receipts", {})
        source = manifest["sources"]["stack"]
        if not isinstance(receipts, dict) or not receipts:
            raise ReleaseError("a promoted release carries no acceptance evidence")
        for name, receipt in sorted(receipts.items()):
            if (not isinstance(receipt, dict) or receipt.get("result") != "passed"
                    or receipt.get("skipped") != 0
                    or receipt.get("source") != source
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
