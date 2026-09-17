"""The host package stands on its own, or it is not a host.

jRemote's trust story is that this package is source a user can read: the app
is closed, the host is open, and anyone who wants to know what happens to
their data can look. A reader who takes that seriously will ask the obvious
follow-up — does this actually run on *my* machine, or only on the one it was
written on? Every answer to that lives here and in `test_jremote_standalone`.

`test_jremote_standalone` proves the package *imports* with the embedding
tree removed. This file proves it never asks for that tree at call time
either, which an import probe cannot see.

Machine-specific behaviour goes through the profile seam (`hostenv.profile()`).
A feature that needs a fact about one deployment adds a profile answer — never
an import of a private module, never a literal.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "jstack_host"
PLUGIN = Path(__file__).resolve().parents[2] / "plugins/jstack"


def test_the_package_never_reaches_for_an_embedding_hosts_own_modules():
    """A call-time preference (`try: from lib import x` / a "lib.x" string
    handed to an importer) passes the import probe on a machine that happens
    to have `lib`, and still ships a dependency no user's machine can satisfy.
    The router and server both carried exactly that shape once; this pins the
    retirement."""
    pat = re.compile(r"\bfrom lib[. ]|\bimport lib\b|[\"']lib\.")
    hits = []
    for f in sorted(PKG.glob("*.py")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if pat.search(line):
                hits.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not hits, (
        "the package references an embedding host's `lib` tree — route the "
        "fact through the profile seam instead:\n" + "\n".join(hits))


def test_no_module_imports_its_way_out_of_the_package():
    """`from ..anything import x` resolves to a sibling of this package, which
    is a thing only an embedding tree has.

    This is not hypothetical: `showdoc` reached `..shared.context_inventory`
    for its read fence, which resolved happily while the package sat inside a
    dashboard and raised `ImportError: attempted relative import beyond
    top-level package` the moment it stood alone. Neither probe beside this
    one saw it — the import probe because the import was inside a function,
    the `lib` probe because the escape was spelled with dots rather than a
    module name. The fence now lives in `docfence`, in here, where it ships.

    One leading dot is the package reaching itself and is how these modules
    should talk. Two or more is a claim about what the package is installed
    *into* — which is the one claim a standalone host exists to falsify.
    """
    pat = re.compile(r"^\s*from\s+\.{2,}")
    hits = []
    for f in sorted(PKG.glob("*.py")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if pat.search(line):
                hits.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not hits, (
        "a relative import escapes the package — it resolves only while the "
        "package sits inside some larger tree, and raises ImportError on a "
        "standalone host. Move the code into the package or route the fact "
        "through the profile seam:\n" + "\n".join(hits))


def test_no_module_resolves_a_path_by_counting_parents():
    """`parents[N]` is a constant that keeps working right up until the package
    moves, and then silently picks a directory that merely exists.

    This package was two levels inside a larger tree for its whole life, so
    five modules walked `parents[2]` to find the root. After the move each of
    those resolved somewhere real and wrong — a compose shim that wasn't
    there, an APNs key directory that would have been created empty. Those
    answers belong to `hostenv`, which is the one module allowed to know where
    anything is.
    """
    pat = re.compile(r"parents\[\d+\]")
    hits = []
    for f in sorted(PKG.glob("*.py")):
        if f.name == "hostenv.py":
            continue          # the seam itself; that is its job
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if pat.search(line):
                hits.append(f"{f.name}:{i}: {line.strip()[:80]}")
    assert not hits, (
        "a module counts directory levels to find something — ask `hostenv` "
        "for it instead, so the answer moves when the package does:\n"
        + "\n".join(hits))


# ── and neither does the process these tests run in ─────────────────────────

def _scheduler_import_adds(env: dict) -> list[str]:
    """What importing jStack's scheduler appends to `sys.path`, in a fresh
    interpreter carrying `env`.

    A subprocess because the question is what an import does to a process that
    has not done it yet — asking in here would import a package this suite has
    already imported and get an empty answer whatever the truth is. Its own
    `env=`, never a mutation of this one, for the reason the whole section
    exists.
    """
    probe = ("import json,sys;"
             f"sys.path.insert(0, {str(PLUGIN)!r});"
             "before=set(sys.path);"
             "import scheduler;"
             "print(json.dumps(sorted(set(sys.path)-before)))")
    r = subprocess.run([sys.executable, "-c", probe], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def test_importing_the_scheduler_adopts_the_install_it_is_pointed_at(tmp_path):
    """The loaded gun, fired deliberately, so the test below is not vacuous.

    jStack's scheduler puts its install's `python_path` on `sys.path` as it
    imports — a daemon has to be able to load the workspace resolver the
    machine named, from any cwd, before anything asks for it. Correct there,
    and a live grenade in a test process: `sys.path` is process-global and
    nothing takes an entry back off it.
    """
    install = tmp_path / "sched"
    (install / "config").mkdir(parents=True)
    (install / "config" / "scheduler.json").write_text(
        json.dumps({"python_path": [str(tmp_path / "elsewhere")]}))
    (tmp_path / "elsewhere").mkdir()
    env = dict(os.environ, SCHEDULER_HOME=str(install))
    assert _scheduler_import_adds(env) == [str(tmp_path / "elsewhere")]


def test_this_suite_runs_against_a_scheduler_it_was_never_installed_on():
    """The 2026-09-16 failure, pinned: ten tests red in a full run and green
    alone, in two files that name neither the scheduler nor a profile.

    `test_codex_parity` imports the scheduler in-process, and on a machine
    running a host the install's `python_path` is the embedding tree. Appending
    it makes `jremote_host_profile` importable to the whole pytest process, so
    the next `reset_profile()` — twenty of them, in eight files — re-resolves
    `auto` to the machine's *live* profile and caches it there. Every later
    test that reaches a profile answer backed by the embedding dashboard then
    dies inside a tree this package is not allowed to know about.

    So the suite's environment says the scheduler is not installed, and this is
    what says it still does. Asserted through the real environment these tests
    carry — a probe that built its own would be pinning its own fixture.
    """
    assert _scheduler_import_adds(dict(os.environ)) == [], (
        "the test process adopted a machine's import root — every later "
        "profile resolution in this suite now answers about that machine")
