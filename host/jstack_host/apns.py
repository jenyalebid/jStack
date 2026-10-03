"""APNs sender — native iOS push for jRemote (notification + app badge).

Signs an ES256 provider JWT from an APNs auth key (.p8) and posts to Apple over
HTTP/2. Config + key live under `hostenv.credentials_dir()/apns/`, which the
host resolves and never keeps in the repo:

    <credentials>/apns/config.json   {"team_id","key_id","bundle_id","sandbox":true}
    <credentials>/apns/AuthKey_<key_id>.p8

`sandbox` picks the environment tried first. A device token binds to the APNs
environment of the build that minted it — development-signed installs
(`./device.sh`) register sandbox tokens, distribution-signed installs (Firebase
ad hoc, TestFlight, App Store) register production tokens — and both kinds can
be live at once across devices. A push rejected as BadDeviceToken is retried
against the other environment, and the environment that accepts a token is
remembered for it.
"""

import json
import time
from pathlib import Path

import httpx
import jwt

from . import hostenv


def _apns_dir() -> Path:
    """Resolved per call, never bound at import.

    A module constant would freeze whatever `JREMOTE_CREDENTIALS_DIR` said the
    instant this module was first imported — and the host sets its environment
    up before serving, not before importing.
    """
    return hostenv.credentials_dir() / "apns"


_PROD = "https://api.push.apple.com"
_SANDBOX = "https://api.sandbox.push.apple.com"

_token_cache: dict = {"jwt": None, "at": 0.0}
#: device token -> base URL that last accepted it; avoids re-probing every send.
_env_cache: dict = {}


def _config() -> dict | None:
    try:
        return json.loads((_apns_dir() / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def is_configured() -> bool:
    cfg = _config()
    return bool(cfg and (_apns_dir() / f"AuthKey_{cfg.get('key_id')}.p8").exists())


def _provider_token(cfg: dict) -> str:
    """ES256 JWT, valid ~1h; Apple wants it refreshed no more than hourly."""
    now = time.time()
    if _token_cache["jwt"] and now - _token_cache["at"] < 2400:
        return _token_cache["jwt"]
    key = (_apns_dir() / f"AuthKey_{cfg['key_id']}.p8").read_text()
    tok = jwt.encode(
        {"iss": cfg["team_id"], "iat": int(now)},
        key, algorithm="ES256", headers={"kid": cfg["key_id"]},
    )
    _token_cache.update(jwt=tok, at=now)
    return tok


def send(device_token: str, *, title: str, body: str,
         badge: int | None = None, session_id: str = "",
         collapse_id: str = "", extra: dict | None = None) -> tuple[bool, str]:
    """Send one push. Returns (ok, detail).

    `collapse_id`: pushes sharing one replace each other in Notification
    Center instead of piling up — a progress stream stays one banner."""
    cfg = _config()
    if not cfg or not is_configured():
        return False, "apns not configured"
    aps: dict = {"alert": {"title": title, "body": body}, "sound": "default"}
    if badge is not None:
        aps["badge"] = badge
    # `extra` carries what a tap opens when it is not a thread — a run
    # shortcut's `shortcut_run`; it never overrides `aps` or `session_id`.
    payload = {**(extra or {}), "aps": aps, "session_id": session_id}
    default = _SANDBOX if cfg.get("sandbox", True) else _PROD
    first = _env_cache.get(device_token, default)
    headers = {
        "authorization": f"bearer {_provider_token(cfg)}",
        "apns-topic": cfg["bundle_id"],
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    if collapse_id:
        # Apple caps the header at 64 bytes; over it the push is rejected.
        headers["apns-collapse-id"] = collapse_id[:64]
    try:
        with httpx.Client(http2=True, timeout=10) as client:
            for base in (first, _PROD if first == _SANDBOX else _SANDBOX):
                r = client.post(
                    f"{base}/3/device/{device_token}",
                    headers=headers,
                    content=json.dumps(payload),
                )
                if r.status_code == 200:
                    _env_cache[device_token] = base
                    return True, "sent"
                if r.status_code == 400 and "BadDeviceToken" in r.text:
                    # The token belongs to the other APNs environment.
                    continue
                return False, f"apns {r.status_code}: {r.text[:200]}"
        return False, "apns 400: BadDeviceToken in both environments"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
