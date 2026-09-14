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
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "jstack_host"


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


def test_the_suite_writes_its_state_nowhere_a_host_could_be_serving():
    """The run's state dir is disposable, and provably not this machine's.

    Every other probe in this file reads source. This one reads the running
    process, because the leak it pins is invisible in source: each module asks
    `hostenv.state_dir()`, which is correct, and the answer on an unconfigured
    checkout is `~/.local/state/jremote` — the directory a host with no
    profile resolves. So the suite minted a `host-id` there, and a second id
    for one machine is what the app reads as a second host at the same address
    (#45). On a tree where the profile does import, the same writes land in the
    live host's dir instead. Neither shows up in the report: the tests pass,
    the damage is on the machine.
    """
    from jstack_host import hostenv

    here = hostenv.state_dir().resolve()
    default = (Path.home() / ".local" / "state" / "jremote").resolve()
    assert here != default, (
        "the suite resolved the default state dir — the one a host with no "
        "profile serves out of. See conftest._STATE_DIR.")

    # And not the dir an embedded host on this machine declared, which is the
    # other half of #45: on the embedding tree the profile resolves and the
    # writes land on a host that is up and being read.
    try:
        declared = json.loads(
            (default / "embedded.json").read_text()).get("state_dir")
    except (OSError, ValueError, AttributeError):
        declared = None
    if declared:
        assert here != Path(declared).expanduser().resolve(), (
            f"the suite resolved {declared} — the state dir an embedded host "
            "on this machine declared it is serving")


def test_state_paths_bound_at_import_follow_the_suites_state_dir():
    """The constants, not just the calls — and the reason conftest sets the
    variable at module level rather than in a fixture.

    These are bound when the module is imported, which for a test module is
    collection: before any fixture has run. An autouse fixture would redirect
    every call-time reader and leave these two pointing at the machine, and
    half an isolation is the worst of the three outcomes — the suite looks
    contained and a 734K `token_usage/cache.json` still lands in somebody's
    state dir. If this fails, the isolation moved somewhere that runs too late.
    """
    from jstack_host import board, hostenv, spend

    here = hostenv.state_dir().resolve()
    for label, path in (("spend.CACHE", spend.CACHE),
                        ("board._TURN_DIR", board._TURN_DIR)):
        assert Path(path).resolve().is_relative_to(here), (
            f"{label} is {path}, outside the suite's state dir {here} — it "
            "bound before the isolation was in place")
