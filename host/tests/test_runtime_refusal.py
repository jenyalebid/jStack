"""#285: a Hub whose seal broke died at launch saying nothing anyone could find.

The runtime verifies the bundle's resource seal before it imports anything, and
refused with exit 78 — but its one stderr line went nowhere, because the sealed
LaunchAgent names no StandardErrorPath. A `__pycache__` written into
`Contents/Resources/packages` by some other Python was enough to kill every Hub
service with empty logs.

These drive the real `codesign` and the real `Runtime.c`, compiled against the
interpreter running the suite, over an ad-hoc `.app` — the refusal under test
is Security.framework's answer, and a stub would only assert this file's idea
of it.
"""
import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from jstack_host import doctor

REPO = Path(__file__).resolve().parents[2]
HUB = "live.jstack.hub"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS code signing")


def bundle(root: Path, executable: Path | None = None, *, barred: bool = False) -> Path:
    """An ad-hoc signed Hub-shaped app with one package in it."""
    app = root / "jStack Hub.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": HUB, "CFBundleExecutable": "Runtime",
                       "CFBundlePackageType": "APPL"}, stream)
    runtime = contents / "MacOS/Runtime"
    if executable:
        shutil.copy2(executable, runtime)
    else:
        runtime.write_text("#!/bin/sh\nexit 0\n")
        runtime.chmod(0o755)
    package = contents / "Resources/packages/httpx"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# httpx\n")
    (package / "_sub").mkdir()
    (package / "_sub/core.py").write_text("VALUE = 1\n")
    if barred:
        from jstack_host.build_hub import bar_bytecode
        bar_bytecode(contents / "Resources")
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
                   check=True, capture_output=True)
    return app


def write_bytecode(app: Path) -> Path:
    cache = app / "Contents/Resources/packages/httpx/__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-312.pyc").write_bytes(b"\0")
    return cache


@pytest.fixture(scope="module")
def runtime(tmp_path_factory) -> Path:
    """`Runtime.c` as a source-built Hub compiles it, against this interpreter."""
    include = sysconfig.get_config_var("INCLUDEPY")
    library = sysconfig.get_config_var("LDLIBRARY") or ""
    candidates = [Path(sysconfig.get_config_var(key) or "/nonexistent") / library
                  for key in ("PYTHONFRAMEWORKPREFIX", "LIBDIR")]
    linked = next((path for path in candidates if path.is_file()), None)
    if not shutil.which("xcrun") or not include or not linked:
        pytest.skip("no clang or no linkable Python on this machine")
    out = tmp_path_factory.mktemp("runtime") / "Runtime"
    subprocess.run(["xcrun", "clang", "-O2", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=13.0",
                    "-Wl,-w", "-framework", "Security", "-framework", "CoreFoundation",
                    "-DJSTACK_SOURCE_BUILD", "-I" + include, str(REPO / "host/macos/Runtime.c"),
                    str(linked), "-o", str(out)], check=True, capture_output=True)
    return out


def launch(app: Path, home: Path) -> subprocess.CompletedProcess:
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run([str(app / "Contents/MacOS/Runtime"), "host"], env=environment,
                          capture_output=True, text=True, timeout=60)


def test_refusal_names_the_bytecode_and_leaves_it_where_it_can_be_read(tmp_path, runtime):
    app = bundle(tmp_path, runtime)
    cache = write_bytecode(app)
    home = tmp_path / "home"
    home.mkdir()

    refused = launch(app, home)

    assert refused.returncode == 78
    added = "added: Contents/Resources/packages/httpx/__pycache__/__init__.cpython-312.pyc"
    assert added in refused.stderr
    left = home / "Library/Logs/jStack/runtime.log"
    report = left.read_text()
    assert added in report and "refused to start Runtime host" in report
    assert "Cause: Python bytecode" in report
    assert "-name __pycache__" in report and "live.jstack.hub.host" in report

    # The remedy it names is the remedy: the bundle verifies again, and the
    # refusal does not outlive the fault it described.
    shutil.rmtree(cache)
    launch(app, home)
    assert not left.exists()


def test_a_bundle_altered_beyond_bytecode_is_told_to_reinstall(tmp_path, runtime):
    app = bundle(tmp_path, runtime)
    (app / "Contents/Resources/packages/httpx/__init__.py").write_text("# changed\n")
    home = tmp_path / "home"
    home.mkdir()

    refused = launch(app, home)

    assert refused.returncode == 78
    assert "altered: Contents/Resources/packages/httpx/__init__.py" in refused.stderr
    assert "Remedy: reinstall jStack Hub" in refused.stderr
    assert "__pycache__" not in refused.stderr


def use(monkeypatch, app: Path):
    from jstack_host import service_settings
    monkeypatch.setattr(service_settings, "read", lambda: {"app": str(app)})


def test_doctor_finds_bytecode_in_the_hub_before_its_next_launch(tmp_path, monkeypatch):
    app = bundle(tmp_path)
    use(monkeypatch, app)
    assert doctor.check_hub_seal()["grade"] == doctor.OK

    write_bytecode(app)
    result = doctor.check_hub_seal()

    assert result["grade"] == doctor.FAIL
    assert "added Contents/Resources/packages/httpx/__pycache__/__init__.cpython-312.pyc" in result["detail"]
    assert "exits 78" in result["detail"]
    assert "-name __pycache__" in result["hint"] and str(app) in result["hint"]


def test_doctor_sends_an_altered_hub_to_a_reinstall(tmp_path, monkeypatch):
    app = bundle(tmp_path)
    use(monkeypatch, app)
    (app / "Contents/Resources/packages/httpx/__init__.py").write_text("# changed\n")

    result = doctor.check_hub_seal()

    assert result["grade"] == doctor.FAIL
    assert "modified Contents/Resources/packages/httpx/__init__.py" in result["detail"]
    assert "reinstall" in result["hint"] and "__pycache__" not in result["hint"]


def test_a_built_hub_keeps_its_seal_when_another_python_imports_from_it(tmp_path, monkeypatch):
    app = bundle(tmp_path, barred=True)
    use(monkeypatch, app)

    imported = subprocess.run([sys.executable, "-c", "import httpx, httpx._sub.core as c; print(c.VALUE)"],
                              env={**{k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"},
                                   "PYTHONPATH": str(app / "Contents/Resources/packages")},
                              capture_output=True, text=True, timeout=60)

    assert imported.returncode == 0 and imported.stdout.strip() == "1", imported.stderr
    assert not list(app.rglob("*.pyc"))
    assert doctor.check_hub_seal()["grade"] == doctor.OK


def test_bytecode_already_in_the_tree_is_refused_not_sealed(tmp_path):
    from jstack_host.build_hub import bar_bytecode
    (tmp_path / "pkg/__pycache__").mkdir(parents=True)
    (tmp_path / "pkg/m.py").write_text("")
    with pytest.raises(ValueError, match="pkg/__pycache__"):
        bar_bytecode(tmp_path)


def test_doctor_without_a_hub_has_nothing_to_grade(tmp_path, monkeypatch):
    use(monkeypatch, tmp_path / "absent.app")
    assert doctor.check_hub_seal()["grade"] == doctor.OK


def test_the_menu_bar_reads_the_file_the_runtime_writes():
    """The menu bar is the one surface still alive when every Hub service is
    refusing; it only says why if both sides agree on the file and its lines."""
    runtime = (REPO / "host/macos/Runtime.c").read_text()
    menu = (REPO / "host/menubar/JStackHostBar.swift").read_text()
    assert '"%s/Library/Logs/jStack"' in runtime and '"%s/runtime.log"' in runtime
    assert '"Library/Logs/jStack/runtime.log"' in menu
    assert '"Cause: Python bytecode' in runtime and '"Cause: Python bytecode"' in menu
    assert 'snprintf(line, sizeof line, "  %s: %s\\n"' in runtime and '$0.hasPrefix("  ")' in menu
