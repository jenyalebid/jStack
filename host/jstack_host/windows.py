"""Windows on a machine's desk, asked under the one permission holder.

macOS bills a privacy request to the **responsible process** of the session
that made it, never to whoever holds the credential. So a window verb run over
ssh — `ssh mac 'osascript …'`, `ssh mac 'swift …'` — asks for a grant on the
ssh session binary (`sshd-keygen-wrapper` before OpenSSH 9.8,
`com.apple.sshd-session` after), is denied because nobody ever granted that,
and if it is impatient enough to call `AXIsProcessTrustedWithOptions` it puts a
dialog on the user's screen naming sshd. That dialog is jStack#239, and it is
what a whole afternoon of remote attempts produces: the Hub holds the grant the
entire time and none of it is reachable.

The fix is not a remote shell that runs as the Hub — that hands every caller
the permission holder's identity, and the service catalog refuses privileged
and shell jobs for exactly that reason. It is a **named, typed capability**:
`Contents/MacOS/JStackWindows`, sealed inside the bundle, spawned by the Hub's
own services so the responsible process is `live.jstack.hub`. A parent does not
reach across a shell; it asks the leaf's Hub, and the leaf's Hub spends the
leaf's own grant.

One grant, one holder, one dialog per machine — and `authorize` is the only
thing on this path that raises it, from inside the bundle, refusing outright
when it can tell the prompt would name something else.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

#: Where the sealed helper sits inside a holder bundle.
HELPER = "Contents/MacOS/JStackWindows"

#: The helper's exit codes, as the reason a caller can branch on. `misattributed`
#: is the one that matters: the machine is fine, the *asker* is in the wrong
#: session, and no amount of retrying from there will ever work.
REASONS = {64: "usage", 69: "gone", 70: "refused", 77: "privileged",
           78: "untrusted", 79: "misattributed"}

VERBS = frozenset({"minimize", "unminimize", "raise"})

#: What each refusal reads as over HTTP, so a parent asking a leaf gets the
#: same `reason` the leaf's own caller would. 412 is the honest code for "the
#: machine is fine, the grant is not held yet"; 421 (Misdirected Request) is
#: the honest code for "you asked from a session macOS attributes elsewhere" —
#: retrying from there never works, and a 500 would invite exactly that.
STATUS = {"absent": 503, "untrusted": 412, "misattributed": 421, "gone": 404,
          "refused": 409, "usage": 400, "unreachable": 502, "timeout": 504,
          "failed": 500}


def reason_for(status: int) -> str:
    """The refusal kind a status code came from — the inverse of `STATUS`."""
    return next((reason for reason, code in STATUS.items() if code == status), "failed")


class WindowsError(Exception):
    """A window verb did not happen. `reason` says which kind, so a route can
    pick its status and a person can tell "not granted" from "not here"."""

    def __init__(self, message: str, reason: str = "failed"):
        super().__init__(message)
        self.reason = reason


def _bundle() -> Path | None:
    """The holder bundle this code is running out of, if any.

    Preferred over the installation's declared app: a Hub serving a request is
    already inside the bundle whose grant it is about to spend, and reading the
    path off `__file__` cannot drift from the process that will do the asking.
    """
    for parent in Path(__file__).resolve().parents:
        if parent.suffix == ".app" and (parent / HELPER).is_file():
            return parent
    return None


def helper() -> Path:
    """The sealed helper to run, or a refusal naming why there isn't one."""
    bundle = _bundle()
    if bundle is not None:
        return bundle / HELPER
    from . import service_settings
    installation = service_settings.read()
    if not installation.get("app"):
        raise WindowsError("this machine has no installed jStack Hub, so there is no "
                           "permission holder to ask under", "absent")
    app = Path(installation["app"])
    if not (app / HELPER).is_file():
        raise WindowsError(f"the Hub installed at {app} predates the window capability "
                           "— update it", "absent")
    from .app_services import verify
    verify(app)
    return app / HELPER


def _run(*arguments: str, timeout: float = 30.0) -> dict:
    try:
        result = subprocess.run([str(helper()), *arguments], capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WindowsError("the window helper did not answer in time", "timeout")
    except OSError as exc:
        raise WindowsError(f"the window helper could not be run: {exc}", "absent")
    if result.returncode != 0:
        raise WindowsError((result.stderr or "").strip() or "the window helper failed",
                           REASONS.get(result.returncode, "failed"))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise WindowsError("the window helper answered something that is not JSON", "failed")


def trust() -> dict:
    """Does the Hub hold Accessibility on this machine — asked, never read.

    Non-prompting. The ancestry rides along because the answer is a property of
    *this* process's session, not of the machine: a `false` from under an ssh
    session and a `false` on a machine nobody ever granted read identically
    until you can see what asked.
    """
    return _run("trust")


def authorize() -> dict:
    """Raise the Accessibility request from inside the Hub, once per machine.

    A no-op when the grant is already held (`prompted: false`), so this is safe
    to call from a doctor rung or a route without turning into a dialog every
    caller can fire at the user's screen.
    """
    return _run("authorize", timeout=60.0)


def listing() -> dict:
    """Every application with windows, and every window's title, state and
    rectangle. The rectangle is not decoration: "I moved it to another display"
    is a claim only a position can settle."""
    return _run("list")


def act(verb: str, pid: int, index: int, title: str) -> dict:
    """Act on one named window. The caller supplies the title it read from
    `listing()`; the helper refuses if that window has moved underneath it."""
    if verb not in VERBS:
        raise WindowsError(f"unsupported window verb: {verb}", "usage")
    return _run(verb, str(int(pid)), str(int(index)), title)


def restore(index: int, title: str) -> dict:
    """Bring a minimized window back from the Dock.

    Its own application may have stopped listing it the moment it was
    minimized — Notes does exactly that — so the Dock is the only handle left.
    macOS does not say whose window each Dock item is, so these are named by
    index and title out of `listing()["minimized"]`, never guessed onto a pid.
    """
    return _run("restore", str(int(index)), title)


def set_hidden(pid: int, hidden: bool) -> dict:
    """Hide or unhide a whole application. Reports what the desk shows
    afterwards, never what was requested — an accessory application accepts the
    message and stays exactly where it was, and calling that success is how a
    window nobody hid gets reported as hidden."""
    return _run("hide" if hidden else "unhide", str(int(pid)))


# ── the same capability, one machine over ───────────────────────────────────

#: The leaf route a parent asks. Named for the capability, not the transport:
#: whoever calls it is asking a Hub to spend its own machine's grant.
LEAF_PATH = "/api/jremote/v1/windows"


def on_host(host_key: str, action: str, payload: dict | None = None,
            *, poster=None) -> dict:
    """Ask `host_key`'s Hub to run a window verb on its own machine.

    The parent spends its delegated grant to mint a credential on the leaf and
    posts to the leaf's own route, so the work happens inside the leaf's sealed
    bundle under the leaf's own Accessibility grant. Nothing about this path
    crosses a shell, which is the whole point: a shell would move the request
    into a session macOS holds sshd responsible for, and the leaf's grant would
    be unreachable from the very machine that owns it.
    """
    from . import grants
    from .store import get_store
    row = get_store().host_row(host_key)
    if row is None or row["deleted"]:
        raise WindowsError(f"unknown machine: {host_key}", "gone")
    try:
        access = grants.mint_on(dict(row), "Windows", poster=poster,
                                owner_id="host-internal")
        status, body = (poster or grants._httpx_post)(
            f"http://{access['address']}:{access['port']}{LEAF_PATH}/{action}",
            payload or {}, access["token"])
    except grants.GrantError as exc:
        raise WindowsError(str(exc), "unreachable")
    if status != 200:
        raise WindowsError(str(body.get("detail") or f"the machine answered {status}"),
                           reason_for(status))
    return body
