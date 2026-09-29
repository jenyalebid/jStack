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
            "interruption", "revocation", "off_network",
            "shell_adopt", "shell_flip", "shell_detach", "delegate_leaf"}
#: The line a hub follows when nobody chose one. Named rather than empty so a
#: manifest built before channels existed can be read as belonging to it: the
#: releases already published came off main, which is what this names. It was
#: spelled `stable` until every machine could choose its line; `channel_ref`
#: still reads that spelling, as main, wherever a config or a sealed identity
#: carries it.
STABLE_CHANNEL = "main"
#: The release lines. Every machine is on exactly one; a release build is made
#: only off one of these, and each has its own offer in a hub's feed. Any other
#: branch is a debug build: it can be built and installed on purpose, never
#: released.
LINES = ("main", "dev")
#: A manifest a hub built for itself from a commit, rather than one a
#: publisher cut. It has no publication and so no acceptance evidence; the
#: marker is inside the signed manifest so only the pinned key can grant the
#: exemption below, and never alongside receipts.
SOURCE_BUILD = "source-build"
#: Where a feed shelves client (jRemote.app) publications, beside the releases.
#: The client is released on its own clock — one signed build at a time, each
#: naming the oldest stack it runs on — and every hub build carries the newest
#: one its stack version satisfies (`build_source.carried_client`). Not a
#: release directory: nothing in here is offered to a machine directly.
CLIENTS = "clients"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
VERSION = re.compile(r"\d+(?:\.\d+)*\Z")


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


def artifact(name: str, item: object) -> dict:
    """One signed component: a file name, a version, and the bytes it must be."""
    if not isinstance(item, dict):
        raise ReleaseError(f"invalid {name} artifact")
    identifier(item.get("file"))
    identifier(item.get("version"))
    if not HEX.fullmatch(str(item.get("sha256", ""))):
        raise ReleaseError(f"invalid {name} checksum")
    size = item.get("bytes")
    if type(size) is not int or not 0 < size <= 4 * 1024**3:
        raise ReleaseError(f"invalid {name} size")
    return item


def version_key(version: str) -> tuple[int, ...]:
    """`26.9.5` orders after `26.9.10`? No — as numbers, never as text; and
    `1.0` is `1`, so a floor of either reads the same against a stack of both."""
    if not VERSION.fullmatch(str(version)):
        raise ReleaseError(f"not a stack version: {version!r}")
    parts = [int(part) for part in str(version).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def validate_client(manifest: dict) -> dict:
    """A client publication: one build of jRemote.app, and the oldest stack it
    runs on. Signed by the publisher, read by the hub that carries it."""
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA \
            or manifest.get("kind") != "client":
        raise ReleaseError("unsupported client publication schema")
    build = manifest.get("build")
    if type(build) is not int or build <= 0:
        raise ReleaseError("a client publication names its build number")
    item = artifact("client", manifest.get("component"))
    if item["version"] != str(build):
        raise ReleaseError("client artifact version is not its build number")
    if not SHA.fullmatch(str(manifest.get("source", ""))):
        raise ReleaseError("exact committed client source revision required")
    packages = manifest.get("packages", {})
    if not isinstance(packages, dict) or any(
            not IDENTIFIER.fullmatch(str(k)) or not SHA.fullmatch(str(v)) for k, v in packages.items()):
        raise ReleaseError("exact committed client package revisions required")
    compatibility = manifest.get("compatibility", {})
    if not isinstance(compatibility, dict) or compatibility.get("protocol") != 1:
        raise ReleaseError("unsupported client protocol")
    version_key(compatibility.get("stack_minimum", ""))
    return manifest


def client_runs_on(manifest: dict, version: str) -> bool:
    """Whether a client publication runs on a stack of `version`."""
    compatibility = manifest["compatibility"]
    return compatibility.get("protocol") == 1 and \
        version_key(compatibility["stack_minimum"]) <= version_key(version)


def validate(manifest: dict, *, promoted: bool = True) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ReleaseError("unsupported release schema")
    identifier(manifest.get("release"))
    components = manifest.get("components")
    if not isinstance(components, dict) or set(components) != COMPONENTS:
        raise ReleaseError("release components do not match its schema")
    for name, item in components.items():
        artifact(name, item)
    if len({item["file"] for item in components.values()}) != len(components):
        raise ReleaseError("release artifacts require distinct filenames")
    compatibility = manifest.get("compatibility", {})
    # `rollback` promised a bundle restore that no longer exists — a failed
    # update leaves the running machine alone, and a release is taken back by
    # switching the ref. Deployed updaters still REQUIRE the field to be True,
    # so the writers keep emitting it; this reader accepts a manifest without
    # it, and refuses only the one value that would claim a mechanism is off
    # (#150). Once no deployed updater predates this, the writers drop it.
    if compatibility.get("rollback", True) is not True:
        raise ReleaseError("unsupported compatibility or non-reversible migration")
    if (compatibility.get("protocol") != 1
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
        # A receipt written before the build model bound the artifact bytes,
        # not the commit, and every release published under the publisher's
        # key carries that shape. A hub's first build carries its client
        # forward from exactly one of those, and a Mac years behind is judged
        # by this code too — so the older binding is still read, on its own
        # terms: the digest of the components the receipt was written against.
        artifact_set = hashlib.sha256(canonical(components)).hexdigest()
        if not isinstance(receipts, dict) or not receipts:
            raise ReleaseError("a promoted release carries no acceptance evidence")
        for name, receipt in sorted(receipts.items()):
            if not isinstance(receipt, dict):
                raise ReleaseError(f"missing or mismatched acceptance receipt: {name}")
            bound = (receipt.get("source") == source if "source" in receipt
                     else receipt.get("artifacts") == artifact_set)
            if (receipt.get("result") != "passed" or receipt.get("skipped") != 0 or not bound
                    or not HEX.fullmatch(str(receipt.get("evidence_sha256", "")))):
                raise ReleaseError(f"missing or mismatched acceptance receipt: {name}")
    return manifest


def signed_by(envelope: dict, public_key: str) -> dict:
    """The manifest inside an envelope this key signed, and nothing else."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        manifest = envelope["manifest"]
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(envelope["signature"], validate=True), canonical(manifest))
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ReleaseError("release signature not trusted") from exc
    return manifest


def verify(envelope: dict, public_key: str, *, promoted: bool = True) -> dict:
    return validate(signed_by(envelope, public_key), promoted=promoted)


def verify_client(envelope: dict, public_key: str) -> dict:
    return validate_client(signed_by(envelope, public_key))


def _seal(manifest: dict, private_key: bytes) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(canonical(manifest))
    return {"manifest": manifest, "signature": base64.b64encode(signature).decode()}


def sign(manifest: dict, private_key: bytes, *, promoted: bool = True) -> dict:
    validate(manifest, promoted=promoted)
    return _seal(manifest, private_key)


def sign_client(manifest: dict, private_key: bytes) -> dict:
    validate_client(manifest)
    return _seal(manifest, private_key)


def check_artifact(path: Path, item: dict) -> None:
    if not path.is_file() or path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
        raise ReleaseError(f"artifact does not match signed release: {item['file']}")
