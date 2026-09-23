"""Every non-Python file under jstack_host ships, or the build silently drops it.

`[tool.setuptools.packages.find]` finds PACKAGES. A directory with no `__init__.py`
is not one, so nothing under it is collected no matter what the wheel is built from —
`jstack_host/bin/jremote-compose-editor` was dropped from every build ever made, and
the failure surfaced not as a build error but as an installed bundle whose `bin/` was
empty while the checkout's was not. Eighteen tests in the Infrastructure suite went red
against that bundle, which reads identically to a real regression.

This is the guard that makes the next such file fail here instead of in a release.
"""
from pathlib import Path
import subprocess
import tomllib

HOST = Path(__file__).resolve().parents[1]
PKG = HOST / "jstack_host"


def shipped_patterns():
    data = tomllib.loads((HOST / "pyproject.toml").read_text())
    return data["tool"]["setuptools"]["package-data"]["jstack_host"]


def tracked_data_files():
    """Non-Python files git tracks under jstack_host, relative to the package root."""
    out = subprocess.run(["git", "ls-files", "jstack_host"], cwd=HOST,
                         capture_output=True, text=True, check=True).stdout.split()
    return [Path(p).relative_to("jstack_host") for p in out if not p.endswith(".py")]


def test_every_tracked_data_file_is_covered_by_package_data():
    patterns = shipped_patterns()
    missing = [str(f) for f in tracked_data_files()
               if not any(f.match(p) for p in patterns)]
    assert not missing, (
        f"these ship in no wheel because package-data does not name them: {missing}")


def test_the_compose_editor_is_named_specifically():
    """The one this guard was written for — a regression here is invisible until install."""
    assert (PKG / "bin" / "jremote-compose-editor").exists()
    f = Path("bin/jremote-compose-editor")
    assert any(f.match(p) for p in shipped_patterns())
