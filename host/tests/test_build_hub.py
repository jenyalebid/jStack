import json
import subprocess
import sys
from pathlib import Path

import pytest

from jstack_host import build_hub


@pytest.mark.parametrize("role", build_hub.ROLES)
def test_startup_targets_are_owned_by_the_app(role):
    definition = build_hub.service_plist(role)
    assert definition["Label"] == f"live.jstack.hub.{role}"
    assert definition["BundleProgram"].startswith("Contents/MacOS/JStack")
    assert "Program" not in definition
    assert "UserName" not in definition
    assert "EnvironmentVariables" not in definition
    assert definition["AssociatedBundleIdentifiers"] == ["live.jstack.hub"]


def test_unknown_service_cannot_be_rendered():
    with pytest.raises(KeyError):
        build_hub.service_plist("arbitrary")


def test_relocation_rejects_external_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr(build_hub, "command", lambda argv: "binary:\n\t/opt/homebrew/lib/foreign.dylib (version 1)\n")
    with pytest.raises(ValueError, match="unbundled dependency"):
        build_hub.relocate(tmp_path / "runtime", Path("/source"), tmp_path)


def test_relocation_requires_bundled_target(monkeypatch, tmp_path):
    monkeypatch.setattr(build_hub, "command", lambda argv: "binary:\n\t/source/lib/missing.dylib (version 1)\n")
    with pytest.raises(ValueError, match="missing bundled library"):
        build_hub.relocate(tmp_path / "runtime", Path("/source"), tmp_path)


def test_relocation_is_relative_to_each_binary(monkeypatch, tmp_path):
    (tmp_path / "Python").touch()
    calls = []

    def command(argv):
        calls.append(argv)
        return "binary:\n\t/source/Python (version 1)\n\t/usr/lib/libSystem.B.dylib (version 1)\n"

    monkeypatch.setattr(build_hub, "command", command)
    build_hub.relocate(tmp_path / "lib/module.so", Path("/source"), tmp_path)
    assert calls[1] == ["/usr/bin/install_name_tool", "-change", "/source/Python",
                        "@loader_path/../Python", str(tmp_path / "lib/module.so")]


def test_macho_does_not_follow_symlinks(tmp_path):
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    link = tmp_path / "link"
    link.symlink_to(binary)
    assert build_hub.macho(binary)
    assert not build_hub.macho(link)


def test_release_bundle_uses_the_manifest_identity():
    identity = build_hub.release_identity("a" * 40, "0.70.0", release_id="2026-09-21-aaaaaaaa",
                                          github_repo="https://github.com/owner/repo.git", date="2026-09-21")
    assert identity == {"sha": "a" * 40, "build": "2026-09-21-aaaaaaaa",
                        "release": "2026-09-21-aaaaaaaa", "version": "0.70.0",
                        "github_repo": "owner/repo", "date": "2026-09-21"}


def test_the_bundle_answers_for_its_ref_and_for_who_built_it():
    """#148 and #146. Nothing downstream can ask either question anywhere
    else: `install_signed.provision` writes a fresh hub's channel from this
    file, and `app_services.verify` takes the source-build exemption from it.
    A publication passes neither, so it keeps the team requirement."""
    from jstack_host.build_source import source_origin
    identity = build_hub.release_identity(
        "a" * 40, "0.70.0", release_id="2026-09-21-aaaaaaaa", github_repo="owner/repo",
        date="2026-09-21", channel="feature/x", origin=source_origin("this-mac"))
    assert identity["channel"] == "feature/x"
    assert identity["origin"] == {"kind": "source-build", "machine": "this-mac"}


@pytest.mark.parametrize("value", ["../escape", "-flag", "a" * 80, "feature/../main", ""])
def test_a_ref_a_hub_will_not_follow_cannot_be_sealed_into_a_bundle(value):
    """The bundle's channel is pasted into a git command line and compared
    against signed content by whoever reads it back, so it is bounded where it
    is written, not only where it is used."""
    with pytest.raises(ValueError):
        build_hub.release_identity("a" * 40, "0.70.0", release_id="2026-09-21-aaaaaaaa",
                                   github_repo="owner/repo", date="2026-09-21", channel=value)


@pytest.mark.parametrize("origin", [{"kind": "published"}, {"machine": "x"}, "source-build", {}])
def test_only_the_marker_a_manifest_carries_can_be_sealed_as_an_origin(origin):
    """One spelling of "a machine built this". A bundle that records anything
    else records nothing the installer's gate will read."""
    with pytest.raises(ValueError):
        build_hub.release_identity("a" * 40, "0.70.0", release_id="2026-09-21-aaaaaaaa",
                                   github_repo="owner/repo", date="2026-09-21", origin=origin)


@pytest.mark.parametrize("extra", [{"channel": "dev"}, {"origin": {"kind": "source-build"}}])
def test_a_dev_build_claims_no_release_line_and_no_origin(extra):
    """The fallback identity names no release and no repo; a ref or an origin
    on it would be a claim about a build nobody cut."""
    with pytest.raises(ValueError):
        build_hub.release_identity("a" * 40, "0.70.0", **extra)


@pytest.mark.parametrize("arguments", [
    {"release_id": "2026-09-21-aaaaaaaa"},
    {"release_id": "2026-09-21-aaaaaaaa", "github_repo": "owner/repo", "date": ""},
    # A counter is no longer an identity, whatever it counts.
    {"release_id": "2026-09-21-aaaaaaaa", "github_repo": "owner/repo", "date": 77},
    # A day that never happened is a typo, not a release date.
    {"release_id": "2026-09-21-aaaaaaaa", "github_repo": "owner/repo", "date": "2026-02-31"},
    {"release_id": "2026-09-21-aaaaaaaa", "github_repo": "owner/repo", "date": "21-09-2026"},
    {"release_id": "../escape", "github_repo": "owner/repo", "date": "2026-09-21"},
    {"release_id": "2026-09-21-aaaaaaaa", "github_repo": "https://foreign.invalid/repo", "date": "2026-09-21"},
])
def test_release_identity_rejects_incomplete_or_invalid_inputs(arguments):
    with pytest.raises(ValueError):
        build_hub.release_identity("a" * 40, "0.70.0", **arguments)


def test_the_built_package_tree_can_mint_a_peer(tmp_path, monkeypatch):
    """The whole bug in one assertion: a shipped Hub must be able to pair.

    `pip install --target` carries the `jstack_host` package and nothing else
    under `host/`, so for every release up to 0.69.9 the staged package tree
    had no `wg_peer.py` — `can_pair()` was False inside the signed app on the
    machine that owns the mesh, taking device pairing, `adopt` and every leaf
    bundle with it. Asserted against the staged tree rather than against the
    repo, because the repo always had the file; only the bundle did not.
    """
    from jstack_host import hostenv, tunnel
    packages = tmp_path / "packages"
    packages.mkdir()
    build_hub.stage_mesh_tools(Path(__file__).resolve().parents[2], packages)
    (packages / "Credentials/wireguard").mkdir(parents=True)
    (packages / "Credentials/wireguard/wg0.conf").write_text("[Interface]\n")

    monkeypatch.setattr(hostenv, "package_root", lambda: packages)
    monkeypatch.delenv("WG_PEER_DIR", raising=False)
    monkeypatch.delenv("JREMOTE_PEER_SCRIPT", raising=False)
    tunnel.rebind()
    try:
        assert tunnel.PEER_SCRIPT == packages / "scripts/wireguard/wg_peer.py"
        assert tunnel.can_pair()
        assert tunnel.missing_for_pairing() == []
    finally:
        tunnel.rebind()


def test_staging_refuses_a_tree_without_the_mesh_tooling(tmp_path):
    stack = tmp_path / "stack"
    (stack / "host/scripts/wireguard").mkdir(parents=True)
    peer = stack / "host/scripts/wireguard/wg_peer.py"
    peer.write_text("#\n")
    peer.chmod(0o755)
    with pytest.raises(ValueError, match="could not pair"):
        build_hub.stage_mesh_tools(stack, tmp_path / "packages")


def test_staging_refuses_a_script_that_cannot_be_run(tmp_path):
    """A staged-but-unrunnable script fails the same way a missing one does —
    `wg_peer.py` is spawned, and the leaf installers are run on the far Mac."""
    stack = tmp_path / "stack"
    (stack / "host/scripts/wireguard").mkdir(parents=True)
    for name in ("wg_peer.py", "install_hub.sh", "install_leaf.sh", "wg_up.sh",
                 "wg_leaf_watch.sh", "wg_sync.sh"):
        path = stack / "host/scripts/wireguard" / name
        path.write_text("#\n")
        path.chmod(0o755)
    (stack / "host/scripts/wireguard/wg_up.sh").chmod(0o644)
    with pytest.raises(ValueError, match="executable bit"):
        build_hub.stage_mesh_tools(stack, tmp_path / "packages")


def test_a_present_peer_table_is_not_blamed_for_a_missing_tool(tmp_path, monkeypatch):
    """The refusal must name the absent file, not the one it expected to be."""
    from jstack_host import tunnel
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\n")
    monkeypatch.setattr(tunnel, "HUB_CONF", conf)
    monkeypatch.setattr(tunnel, "PEER_SCRIPT", tmp_path / "gone/wg_peer.py")
    assert tunnel.missing_for_pairing() == [tmp_path / "gone/wg_peer.py"]


def test_a_hub_rebuilding_itself_locates_the_framework_it_builds_around(monkeypatch, tmp_path):
    """The interpreter running the build is never assumed to be the one built around.

    `jstack-host` runs from the checkout venv, which the installer makes on
    whatever python3 the Mac already had — 3.14 from Homebrew on a stock
    machine — and the sealed Hub runs JStackPython, which is 3.12 with no pip.
    Both failed: the first on the version guard, the second on "No module
    named pip". Install worked either way, so every machine could install and
    none could update itself.
    """
    from jstack_host import build_hub

    def answering(**overrides):
        answer = {"prefix": str(framework), "version": "3.12.10", "pip": True} | overrides
        return json.dumps(answer)

    framework = tmp_path / "Library/Frameworks/Python.framework/Versions/3.12"
    framework.mkdir(parents=True)
    (framework / "Python").write_bytes(b"\xcf\xfa\xed\xfe")
    asked, answers = [], {}

    def probe(argv, **kwargs):
        asked.append(argv[0])
        if argv[0] not in answers:
            return subprocess.CompletedProcess(argv, 1, "", "no such file")
        return subprocess.CompletedProcess(argv, 0, answers[argv[0]], "")

    monkeypatch.setattr(build_hub.subprocess, "run", probe)
    monkeypatch.delenv("JSTACK_BUILD_PYTHON", raising=False)

    # The running interpreter is a pipless 3.12 and a Homebrew 3.14 at once —
    # neither is what the bundle embeds, and the framework is found regardless.
    answers[build_hub.FRAMEWORK_PYTHON] = answering()
    answers[sys.executable] = answering(version="3.14.7", pip=True, prefix=str(tmp_path / "brew"))
    found = build_hub.build_interpreter()
    assert found == build_hub.Interpreter(build_hub.FRAMEWORK_PYTHON, framework, "3.12.10")
    assert asked[0] == build_hub.FRAMEWORK_PYTHON

    # JSTACK_BUILD_PYTHON is asked first and wins when it qualifies.
    monkeypatch.setenv("JSTACK_BUILD_PYTHON", "/opt/python3")
    answers["/opt/python3"] = answering()
    asked.clear()
    assert build_hub.build_interpreter().executable == "/opt/python3"
    assert asked == ["/opt/python3"]

    # A 3.12 that is not a framework build, and one without pip, are both
    # refused — and the refusal says which was which.
    del answers["/opt/python3"], answers[build_hub.FRAMEWORK_PYTHON]
    answers["/opt/python3"] = answering(prefix=str(tmp_path / "plain"))
    answers[sys.executable] = answering(pip=False)
    with pytest.raises(RuntimeError) as refused:
        build_hub.build_interpreter()
    assert "/opt/python3 is not a framework build" in str(refused.value)
    assert "has no pip" in str(refused.value)
    assert "3.12" in str(refused.value)


def test_a_build_input_is_found_where_it_lives_not_only_on_path(monkeypatch, tmp_path):
    """A build runs from launchd or over ssh, with PATH=/usr/bin:/bin:/usr/sbin:/sbin.

    tmux comes from Homebrew because the installer put it there, and none of
    Homebrew is on that PATH — so `shutil.which` answered no about a binary
    sitting at /opt/homebrew/bin/tmux and the build called it missing.
    """
    brew = tmp_path / "opt/homebrew/bin"
    brew.mkdir(parents=True)
    tmux = brew / "tmux"
    tmux.write_text("#!/bin/sh\n")
    tmux.chmod(0o755)
    monkeypatch.setattr(build_hub, "TOOL_DIRS", (str(brew), "/usr/bin"))
    monkeypatch.setattr(build_hub.shutil, "which", lambda name: None)
    monkeypatch.delenv("JSTACK_BUILD_TMUX", raising=False)
    assert build_hub.build_tool("tmux", "JSTACK_BUILD_TMUX") == tmux

    # PATH still wins when it has an answer, and the override wins over both.
    on_path = tmp_path / "path/tmux"
    on_path.parent.mkdir()
    on_path.write_text("#!/bin/sh\n")
    on_path.chmod(0o755)
    monkeypatch.setattr(build_hub.shutil, "which", lambda name: str(on_path))
    assert build_hub.build_tool("tmux", "JSTACK_BUILD_TMUX") == on_path
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("#!/bin/sh\n")
    elsewhere.chmod(0o755)
    monkeypatch.setenv("JSTACK_BUILD_TMUX", str(elsewhere))
    assert build_hub.build_tool("tmux", "JSTACK_BUILD_TMUX") == elsewhere

    # A directory named like the tool is not the tool, and neither is a file
    # without its executable bit.
    monkeypatch.delenv("JSTACK_BUILD_TMUX")
    monkeypatch.setattr(build_hub.shutil, "which", lambda name: None)
    tmux.chmod(0o644)
    with pytest.raises(ValueError, match="not on PATH and not in"):
        build_hub.build_tool("tmux", "JSTACK_BUILD_TMUX")
