"""Selected-folder SMB: one observed contract from CLI through API and audit."""

import asyncio
import getpass
import platform
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jstack_host import cli, doctor, fileshare, router

_REAL_AUDIT_LOOP = fileshare.audit_loop


def _done(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(argv, code, out, err)


def _machine(tmp_path, monkeypatch, *, actual=None, account=False,
             guest=False, service=True, acl=False, smb_hash=True,
             hash_readable=True, others=None):
    """`others` is every other person's account on the machine, name -> whether
    smbd would answer its password (None = not readable from here)."""
    root = tmp_path / "stack"
    others = {} if others is None else dict(others)
    fileshare._reset_cache()
    for name in fileshare.SHARE_NAMES:
        (root / name).mkdir(parents=True)
    monkeypatch.setattr(fileshare, "_stack_root", lambda: root)
    monkeypatch.setattr(fileshare, "_root_owner", lambda path: "hostuser")
    actual = actual or {}

    def run(argv):
        if argv[:3] == [fileshare.SHARING, "-l", "-f"]:
            import json
            return _done(argv, out=json.dumps(actual))
        if argv[0] == fileshare.LS:
            text = "drwxr-xr-x+ root\n"
            if acl:
                text += f" 0: user:{fileshare.ACCOUNT} allow {','.join(fileshare.ACL_RIGHTS)}\n"
                text += f" 1: user:hostuser allow {','.join(fileshare.ACL_RIGHTS)}\n"
            return _done(argv, out=text)
        if argv[0] == fileshare.DSCL:
            if argv[1:3] == [".", "-list"]:
                rows = [f"root 0\n", f"_smbd 222\n"]
                if account:
                    rows.append(f"{fileshare.ACCOUNT} 502\n")
                rows += [f"{name} {600 + i}\n" for i, name in enumerate(others)]
                return _done(argv, out="".join(rows))
            if argv[-1] == "AuthenticationAuthority":
                user = argv[3].rsplit("/", 1)[1]
                state = others.get(user, smb_hash if hash_readable else None)
                if state is None:
                    return _done(argv, out="No such key: AuthenticationAuthority\n")
                kinds = "SALTED-SHA512-PBKDF2" + (",SMB-NT" if state else "")
                return _done(argv, out=f"AuthenticationAuthority: ;ShadowHash;HASHLIST:<{kinds}> ;SecureToken;\n")
            if account:
                return _done(argv, out=(f"NFSHomeDirectory: {fileshare.ACCOUNT_HOME}\n"
                                        f"UserShell: {fileshare.ACCOUNT_SHELL}\n"
                                        "UniqueID: 502\n"))
            return _done(argv, code=40, err="not found")
        if argv[0] == fileshare.DSEDITGROUP:
            # macOS 26's own exit code for a non-member, with its own sentence.
            return _done(argv, code=67, out="no jstackshare is NOT a member of admin")
        if argv[0] == fileshare.PWPOLICY:
            # Reading another account's hash types is root-only: it prints
            # nothing and still exits 0.
            user = argv[2]
            state = others.get(user, smb_hash if hash_readable else None)
            if state is None:
                return _done(argv)
            return _done(argv, out="SMB-NT\n" if state else
                         "SALTED-SHA512-PBKDF2\n")
        if argv[0] == fileshare.SYSADMINCTL:
            word = "enabled" if guest else "disabled"
            return _done(argv, err=f"SMB guest access {word}.")
        if argv[0] == fileshare.LAUNCHCTL:
            if argv[1:3] == ["print", "system/com.apple.smbd"]:
                return _done(argv, code=0 if service else 113)
            word = "enabled" if service else "disabled"
            return _done(argv, out=f'"com.apple.smbd" => {word}\n')
        raise AssertionError(argv)

    monkeypatch.setattr(fileshare, "_run", run)
    monkeypatch.setattr(fileshare, "_supported", lambda: True)
    return root


def _share(path, *, guest=0, read_only=0, sealed=1, shared=1):
    return {"path": str(path), "smb_guest_access": guest,
            "smb_read_only": read_only, "smb_sealed": sealed,
            "smb_shared": shared}


def test_status_rejects_home_and_accepts_exact_selected_roots(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    actual["home"] = _share(root, guest=1, sealed=0)
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True)

    observed = fileshare.status()

    assert [s["name"] for s in observed["shares"]] == list(fileshare.SHARE_NAMES)
    assert observed["unexpected"] == [{"name": "home", "path": str(root)}]
    assert observed["configured"] is True
    assert observed["secure"] is False
    assert observed["ready"] is False


def test_status_ready_means_every_security_property_was_observed(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True)

    observed = fileshare.status()

    assert observed["unexpected"] == []
    assert observed["account"]["ok"] is True
    assert observed["guest_enabled"] is False
    assert observed["secure"] is True
    assert observed["ready"] is True


def test_setup_plan_is_allowlist_and_dry_run(tmp_path, monkeypatch):
    root = _machine(tmp_path, monkeypatch, actual={
        "home": _share(tmp_path / "stack", guest=1, sealed=0),
        "Public": _share(tmp_path / "stack" / "Public", guest=1, sealed=0),
    }, guest=True)

    result = fileshare.setup()
    commands = result["commands"]

    assert result["applied"] is False
    assert any("-addUser jstackshare" in c and "-password -" in c for c in commands)
    assert any("pwpolicy -u jstackshare -sethashtypes SMB-NT on" in c
               for c in commands)
    assert any("dscl . -passwd /Users/jstackshare" in c for c in commands)
    assert any("sharing -r home" in c for c in commands)
    assert any("sharing -r Public" in c for c in commands)
    for name in fileshare.SHARE_NAMES:
        assert any(f"sharing -a {root / name}" in c and "-g 000" in c and
                   "-E 1" in c for c in commands)
        assert any("chmod +a" in c and str(root / name) in c for c in commands)
        assert any("user:hostuser allow" in c and str(root / name) in c
                   for c in commands)
    assert any("-smbGuestAccess off" in c for c in commands)


def test_setup_refuses_to_repurpose_an_existing_account(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    bad = fileshare.status()
    bad["account"] = {"exists": True, "ok": False}
    with pytest.raises(fileshare.FileShareError, match="refusing to repurpose"):
        fileshare.setup_plan(bad)


def test_empty_roots_never_plan_removal(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch, actual={"home": _share(tmp_path)})
    monkeypatch.setattr(fileshare, "desired_shares", lambda: {})
    with pytest.raises(fileshare.FileShareError, match="no selected roots"):
        fileshare.setup_plan()


def test_wrong_named_share_is_removed_only_once(tmp_path, monkeypatch):
    root = _machine(tmp_path, monkeypatch, actual={"Agents": _share(tmp_path)})
    commands = fileshare.setup_plan()
    assert commands.count([fileshare.SHARING, "-r", "Agents"]) == 1
    assert any(c[:3] == [fileshare.SHARING, "-a", str(root / "Agents")] for c in commands)
    first_share = next(i for i, c in enumerate(commands) if c[0] == fileshare.SHARING)
    assert all(c[0] == fileshare.SHARING for c in commands[first_share:])


def test_unknown_account_membership_is_not_ready(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    _machine(tmp_path, monkeypatch, actual={n: _share(root / n) for n in fileshare.SHARE_NAMES},
             account=True, acl=True)
    run = fileshare._run
    monkeypatch.setattr(fileshare, "_run",
                        lambda argv: _done(argv, code=77, err="denied")
                        if argv[0] == fileshare.DSEDITGROUP else run(argv))
    assert fileshare.status()["ready"] is False


def test_membership_is_read_from_the_sentence_not_the_exit_code():
    assert fileshare._parse_membership("no x is NOT a member of admin") is False
    assert fileshare._parse_membership("yes x is a member of admin") is True
    assert fileshare._parse_membership("") is None


@pytest.mark.skipif(not Path(fileshare.DSEDITGROUP).exists(),
                    reason="macOS membership tool absent")
def test_the_real_membership_tool_still_answers_in_those_words():
    """The probe that went blind: read the live tool, not a fixture of it."""
    for user, expected in (("nobody", False), ("root", True)):
        r = subprocess.run([fileshare.DSEDITGROUP, "-o", "checkmember",
                            "-m", user, "admin"], capture_output=True, text=True)
        assert fileshare._parse_membership(f"{r.stdout} {r.stderr}") is expected


def test_an_unreadable_hash_is_named_rather_than_blocking(tmp_path, monkeypatch):
    """Hash types are root-only and the host is sealed against root; an
    unknown there must not make `ready` unreachable in the shipped
    configuration."""
    root = tmp_path / "stack"
    _machine(tmp_path, monkeypatch, actual={n: _share(root / n) for n in fileshare.SHARE_NAMES},
             account=True, acl=True, hash_readable=False)

    observed = fileshare.status()

    assert observed["account"]["smb_hash"] is None
    assert observed["unverified"] == ["account.smb_hash"]
    assert observed["ready"] is True


def test_an_observed_missing_hash_still_blocks(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    _machine(tmp_path, monkeypatch, actual={n: _share(root / n) for n in fileshare.SHARE_NAMES},
             account=True, acl=True, smb_hash=False)
    assert fileshare.status()["ready"] is False


@pytest.mark.parametrize("entry", ["user:jstackshare_other allow", "user:jstackshare deny"])
def test_acl_matches_exact_principal_and_refuses_denials(tmp_path, monkeypatch, entry):
    text = "directory\n 0: " + entry + " " + ",".join(fileshare.ACL_RIGHTS)
    monkeypatch.setattr(fileshare, "_run", lambda argv: _done(argv, out=text))
    assert fileshare._acl_ok(tmp_path) is False


def test_root_refuses_implicit_installation(monkeypatch):
    monkeypatch.setattr(fileshare.os, "geteuid", lambda: 0)
    monkeypatch.delenv("JREMOTE_INSTANCE_ROOT", raising=False)
    with pytest.raises(fileshare.FileShareError, match="refusing to guess"):
        fileshare._stack_root()


def test_audit_does_not_block_event_loop(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()

    def probe():
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(fileshare, "audit_once", probe)

    async def exercise():
        task = asyncio.create_task(_REAL_AUDIT_LOOP())
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            assert entered.is_set()
            assert not release.is_set()
        finally:
            release.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


def test_status_endpoint_rejects_unauthenticated_http():
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app) as client:
        assert client.get("/api/jremote/v1/files/share").status_code == 401


def test_existing_account_without_smb_hash_is_rearmed(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True,
             smb_hash=False)
    commands = fileshare.setup_plan()
    assert [fileshare.PWPOLICY, "-u", fileshare.ACCOUNT, "-sethashtypes",
            "SMB-NT", "on"] in commands
    assert [fileshare.DSCL, ".", "-passwd", f"/Users/{fileshare.ACCOUNT}"] in commands


def test_acl_accepts_macos_directory_rights_normalization(tmp_path, monkeypatch):
    root = _machine(tmp_path, monkeypatch)
    text = ("drwxr-xr-x+ root\n 0: user:jstackshare allow " +
            ",".join(fileshare.ACL_OBSERVED_RIGHTS) + "\n")
    monkeypatch.setattr(fileshare, "_run", lambda argv: _done(argv, out=text))
    assert fileshare._acl_ok(root / "Agents") is True


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS ACLs")
def test_an_inherited_grant_does_not_reach_the_tree_that_predates_it(tmp_path):
    """The share mounted, listed and read, and refused every save: the grant
    was on the root, and inheritance governs only what comes after it."""
    me = getpass.getuser()
    root = tmp_path / "Agents"
    (root / "seat" / "pad").mkdir(parents=True)
    (root / "seat" / "pad" / "note.md").write_text("before the grant\n")
    (root / "top.txt").write_text("before the grant\n")
    entry = fileshare._acl_entry(me)

    subprocess.run([fileshare.CHMOD, "+a", entry, str(root)], check=True)
    assert fileshare._acl_ok(root, me) is True
    assert fileshare._children_acl_ok(root, me) is False

    subprocess.run([fileshare.CHMOD, "-R", "+a", entry, str(root)], check=True)
    assert fileshare._children_acl_ok(root, me) is True
    assert fileshare._acl_ok(root / "top.txt", me, directory=False) is True


def test_a_root_whose_tree_was_never_granted_is_not_secure(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=False)
    (root / "Agents" / "seat").mkdir(parents=True)

    observed = fileshare.status()
    agents = next(r for r in observed["shares"] if r["name"] == "Agents")

    assert agents["children_acl_ok"] is False
    assert observed["secure"] is False
    assert {"children_acl_ok", "owner_children_acl_ok"}.issubset(
        set(next(p["failed"] for p in observed["security_problems"]
                 if p.get("name") == "Agents")))


def test_setup_plans_the_tree_grant_unprivileged(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=False)
    (root / "Agents" / "seat").mkdir(parents=True)

    plan = fileshare.setup(apply=False)

    assert any(c.startswith(f"{fileshare.CHMOD} -R +a") and c.endswith("Agents")
               for c in plan["tree_commands"])
    # It belongs to the owner, not to the administrator prompt.
    assert not any("-R" in c for c in plan["commands"])


def test_a_file_the_owner_cannot_relabel_is_named_not_fatal():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return _done(argv, code=1, err="chmod: Failed to set ACL on file a: Permission denied\n")

    out = fileshare._apply_tree([[fileshare.CHMOD, "-R", "+a", "ace", "/x"]], runner)

    assert len(calls) == 1
    assert out[0]["returncode"] == 1 and out[0]["refused"] == 1
    assert "Permission denied" in out[0]["first_refusal"]


def test_audit_alerts_once_per_distinct_unsafe_state(monkeypatch):
    alerts = []
    states = [
        {"unexpected": [{"name": "home", "path": "/Users/x"}]},
        {"unexpected": [{"name": "home", "path": "/Users/x"}]},
        {"unexpected": []},
    ]
    monkeypatch.setattr(fileshare, "status", lambda: states.pop(0))
    monkeypatch.setattr(fileshare.hostenv, "security_alert", alerts.append)
    monkeypatch.setattr(fileshare, "_last_alert", "")

    fileshare.audit_once()
    fileshare.audit_once()
    fileshare.audit_once()

    assert len(alerts) == 1
    assert "home=/Users/x" in alerts[0]


def test_secure_configuration_is_not_ready_until_service_is_loaded(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True,
             service=False)
    observed = fileshare.status()
    assert observed["secure"] is True
    assert observed["ready"] is False


def test_router_lifespan_runs_and_cancels_the_package_audit(monkeypatch):
    events = []

    async def watch():
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(fileshare, "audit_loop", watch)
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app):
        assert events == ["started"]
    assert events == ["started", "stopped"]


def test_authenticated_status_route_and_feature_probe(tmp_path, monkeypatch):
    root = tmp_path / "stack"
    actual = {name: _share(root / name) for name in fileshare.SHARE_NAMES}
    _machine(tmp_path, monkeypatch, actual=actual, account=True, acl=True)
    monkeypatch.setattr(router, "require_token", lambda: "device")

    # The router's own route table is stable across starlette versions;
    # the app's list nests included routers from starlette 1.6.
    route = next(r for r in router.router.routes if r.path == "/api/jremote/v1/files/share")
    assert route.endpoint()["ready"] is True
    assert router._probe("file_sharing") is True


def test_doctor_fails_an_enabled_server_with_undeclared_shares(monkeypatch):
    monkeypatch.setattr(fileshare, "status", lambda: {
        "available": True, "configured": False, "ready": False,
        "unexpected": [{"name": "home", "path": "/Users/x"}],
        "service_enabled": True,
    })
    result = doctor.check_file_sharing()
    assert result["grade"] == doctor.FAIL
    assert "home" in result["detail"]


def test_cli_files_subcommands_dispatch(monkeypatch, capsys):
    monkeypatch.setattr(fileshare, "status", lambda: {"ready": True})
    assert cli.main(["files", "status"]) == 0
    assert '"ready": true' in capsys.readouterr().out


# --- applying without root: the administrator prompt ----------------------------
#
# The sealed runtime refuses to start as root, so `sudo jstack-host files
# setup --apply` cannot exist.  Applying as the logged-in user hands the plan —
# fixed system tools only — to the OS administrator prompt.

def _apply_rig(monkeypatch, commands, *, secure=True):
    monkeypatch.setattr(fileshare.os, "geteuid", lambda: 501)
    monkeypatch.setattr(fileshare, "setup_plan", lambda observed=None: commands)
    monkeypatch.setattr(fileshare, "status", lambda: {"secure": secure, "ready": False})
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw.get("input", "")))
        return _done(argv, out="")
    return calls, runner


def test_apply_without_root_goes_through_the_administrator_prompt(monkeypatch, tmp_path):
    commands = [
        [fileshare.SYSADMINCTL, "-addUser", fileshare.ACCOUNT, "-fullName",
         "jStack File Sharing", "-shell", fileshare.ACCOUNT_SHELL, "-home",
         fileshare.ACCOUNT_HOME, "-password", "-"],
        [fileshare.PWPOLICY, "-u", fileshare.ACCOUNT, "-sethashtypes", "SMB-NT", "on"],
        [fileshare.DSCL, ".", "-passwd", f"/Users/{fileshare.ACCOUNT}"],
        [fileshare.SHARING, "-r", "hostuser’s Public Folder"],
        [fileshare.SHARING, "-a", str(tmp_path / "Agents"), "-n", "Agents", "-g", "000", "-E", "1"],
    ]
    calls, runner = _apply_rig(monkeypatch, commands)
    monkeypatch.setattr(fileshare, "_collect_password", lambda: "s3cret pass")
    written = []
    real = fileshare._private_password_file

    def spy(password):
        path = real(password)
        written.append(path)
        return path
    monkeypatch.setattr(fileshare, "_private_password_file", spy)

    result = fileshare.setup(apply=True, runner=runner)

    assert result["applied"] is True
    [(argv, source)] = calls
    assert argv == [fileshare.OSASCRIPT, "-"]
    assert source.startswith("do shell script ")
    assert "with administrator privileges" in source
    # the password travels by file, never in the script or on argv
    assert "s3cret" not in source
    assert f'pw=$({fileshare.CAT} ' in source
    assert '-password \\"$pw\\"' in source
    assert f'-passwd /Users/{fileshare.ACCOUNT} \\"$pw\\"' in source
    assert "-password -" not in source
    assert "sharing -r 'hostuser’s Public Folder'" in source
    assert "set -eu" in source
    # and the file is gone once the prompt has closed
    assert written and not written[0].exists() and not written[0].parent.exists()


def test_apply_without_root_and_without_a_password_step_asks_for_none(monkeypatch, tmp_path):
    commands = [[fileshare.SHARING, "-a", str(tmp_path / "Agents"), "-n", "Agents"]]
    calls, runner = _apply_rig(monkeypatch, commands)
    monkeypatch.setattr(fileshare, "_collect_password",
                        lambda: (_ for _ in ()).throw(AssertionError("asked")))
    result = fileshare.setup(apply=True, runner=runner)
    assert result["applied"] is True
    assert "pw=" not in calls[0][1]


def test_apply_reports_a_refused_or_failed_prompt(monkeypatch, tmp_path):
    commands = [[fileshare.SHARING, "-a", str(tmp_path / "Agents"), "-n", "Agents"]]
    monkeypatch.setattr(fileshare.os, "geteuid", lambda: 501)
    monkeypatch.setattr(fileshare, "setup_plan", lambda observed=None: commands)
    monkeypatch.setattr(fileshare, "status", lambda: {"secure": False})

    def refused(argv, **kw):
        return _done(argv, code=1, err="User canceled. (-128)")
    with pytest.raises(fileshare.FileShareError, match="administrator approval"):
        fileshare.setup(apply=True, runner=refused)


def test_apply_without_root_still_demands_a_secure_result(monkeypatch, tmp_path):
    commands = [[fileshare.SHARING, "-a", str(tmp_path / "Agents"), "-n", "Agents"]]
    calls, runner = _apply_rig(monkeypatch, commands, secure=False)
    with pytest.raises(fileshare.FileShareError, match="still not secure"):
        fileshare.setup(apply=True, runner=runner)


def test_off_without_root_removes_through_the_administrator_prompt(monkeypatch):
    monkeypatch.setattr(fileshare.os, "geteuid", lambda: 501)
    monkeypatch.setattr(fileshare, "status", lambda: {
        "secure": False, "shares": [{"name": "Agents", "present": True}],
        "unexpected": [{"name": "home"}]})
    calls = []

    def runner(argv, **kw):
        calls.append(kw.get("input", ""))
        return _done(argv)
    result = fileshare.off(apply=True, runner=runner)
    assert result["applied"] is True
    [source] = calls
    assert "sharing -r Agents" in source and "sharing -r home" in source
    assert "with administrator privileges" in source


def test_administrator_script_refuses_a_password_step_with_no_password():
    with pytest.raises(fileshare.FileShareError, match="none was collected"):
        fileshare._administrator_script(
            [[fileshare.DSCL, ".", "-passwd", f"/Users/{fileshare.ACCOUNT}"]], None)


# --- the server speaks only to the sharing account -----------------------------
#
# Share-point settings gate three folders.  They do not gate who may log in to
# smbd: any local account with an SMB hash can, and an administrator who does
# may mount the whole startup disk.  So every other account is observed, and
# setup switches its SMB credential off.

def _ready_machine(tmp_path, monkeypatch, **kw):
    root = tmp_path / "stack"
    _machine(tmp_path, monkeypatch, actual={n: _share(root / n) for n in fileshare.SHARE_NAMES},
             account=True, acl=True, **kw)
    return root


def test_another_account_smbd_answers_for_is_not_secure(tmp_path, monkeypatch):
    _ready_machine(tmp_path, monkeypatch, others={"owner": True, "guest2": False})

    observed = fileshare.status()

    assert observed["accounts"] == [
        {"name": "guest2", "uid": 601, "smb_hash": False},
        {"name": "owner", "uid": 600, "smb_hash": True},
    ]
    assert observed["secure"] is False
    assert observed["ready"] is False
    assert {"kind": "account_speaks_smb", "name": "owner"} in observed["security_problems"]


def test_setup_switches_every_other_account_off_the_server(tmp_path, monkeypatch):
    _ready_machine(tmp_path, monkeypatch, others={"owner": True, "quiet": False,
                                                  "unread": None})

    commands = fileshare.setup_plan()

    off = [c[2] for c in commands if c[:2] == [fileshare.PWPOLICY, "-u"]
           and c[-2:] == ["SMB-NT", "off"]]
    # An unreadable account is switched off too: dropping a hash that was
    # never there costs nothing, and a guess would leave a door open.
    assert off == ["owner", "unread"]
    assert fileshare.ACCOUNT not in off
    assert [fileshare.PWPOLICY, "-u", fileshare.ACCOUNT, "-sethashtypes",
            "SMB-NT", "on"] not in commands


def test_an_unreadable_other_account_is_named_and_does_not_block(tmp_path, monkeypatch):
    _ready_machine(tmp_path, monkeypatch, others={"unread": None})

    observed = fileshare.status()

    assert observed["accounts"] == [{"name": "unread", "uid": 600, "smb_hash": None}]
    assert "accounts.unread.smb_hash" in observed["unverified"]
    assert observed["ready"] is True


def test_system_accounts_and_the_sharing_account_are_not_other_accounts(tmp_path, monkeypatch):
    _ready_machine(tmp_path, monkeypatch)
    assert fileshare.status()["accounts"] == []


def test_hash_types_fall_back_to_the_directory_record(monkeypatch):
    """`pwpolicy` is silent for anyone but root and the caller; the record's
    HASHLIST says the same thing, and the real hub reads its own that way."""
    calls = []

    def run(argv):
        calls.append(argv[0])
        if argv[0] == fileshare.PWPOLICY:
            return _done(argv)
        return _done(argv, out="AuthenticationAuthority: ;ShadowHash;HASHLIST:"
                               "<SALTED-SHA512-PBKDF2,SMB-NT> ;SecureToken;\n")
    monkeypatch.setattr(fileshare, "_run", run)

    assert fileshare._hash_types("someone") == {"SALTED-SHA512-PBKDF2", "SMB-NT"}
    assert calls == [fileshare.PWPOLICY, fileshare.DSCL]


def test_doctor_fails_a_server_another_account_can_log_in_to(monkeypatch):
    monkeypatch.setattr(fileshare, "status", lambda: {
        "available": True, "configured": True, "ready": False, "secure": False,
        "unexpected": [], "service_enabled": True, "shares": [],
        "security_problems": [{"kind": "account_speaks_smb", "name": "owner"}],
    })
    result = doctor.check_file_sharing()
    assert result["grade"] == doctor.FAIL
    assert "owner" in result["detail"]
    assert "files setup --apply" in result["hint"]


def test_doctor_only_warns_where_no_server_is_declared(monkeypatch):
    monkeypatch.setattr(fileshare, "status", lambda: {
        "available": True, "configured": False, "ready": False, "secure": False,
        "unexpected": [], "service_enabled": False, "shares": [],
        "security_problems": [{"kind": "account_speaks_smb", "name": "owner"}],
    })
    assert doctor.check_file_sharing()["grade"] == doctor.WARN


# --- the capability probe answers from one reading (jStack#325) ---------------

def test_the_probe_answers_several_askers_from_one_scan(tmp_path, monkeypatch):
    import threading
    _ready_machine(tmp_path, monkeypatch)
    scans = []
    real = fileshare._observe

    def counted():
        scans.append(1)
        return real()
    monkeypatch.setattr(fileshare, "_observe", counted)

    answers = []
    threads = [threading.Thread(target=lambda: answers.append(fileshare.serves_files()))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert answers == [True] * 5
    assert len(scans) == 1
    assert router._probe("file_sharing") is True
    assert len(scans) == 1


def test_a_fresh_status_renews_what_the_probe_answers_from(tmp_path, monkeypatch):
    _ready_machine(tmp_path, monkeypatch)
    assert fileshare.serves_files() is True

    # The share points vanish; the probe still answers from its reading until
    # someone takes a fresh one, which every default `status()` call is.
    monkeypatch.setattr(fileshare, "_actual_shares", lambda: {})
    assert fileshare.serves_files() is True
    assert fileshare.status()["ready"] is False
    assert fileshare.serves_files() is False


def test_the_probe_reading_expires(tmp_path, monkeypatch):
    _ready_machine(tmp_path, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(fileshare.time, "monotonic", lambda: now[0])
    assert fileshare.serves_files() is True
    monkeypatch.setattr(fileshare, "_actual_shares", lambda: {})
    now[0] += fileshare.PROBE_TTL + 0.1
    assert fileshare.serves_files() is False
