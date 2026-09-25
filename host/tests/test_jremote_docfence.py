"""docfence — the only thing standing between a bearer token and the disk.

`/context/file` is a read route on a machine that also stores credentials, and
`showdoc` hands the app paths to ask back through it. Both call this module, so
these pin the rule once: markdown only, under the roots, checked *after*
symlinks resolve.

The symlink case is the one worth writing down. Half of what a jStack machine
publishes is a symlink into a plugin checkout, so following them is required —
which means a fence that checked the name it was given would be walked around
by a link whose name is innocent and whose target is not.
"""

from pathlib import Path

import pytest

from jstack_host import docfence, plugin_paths


@pytest.fixture
def fenced(monkeypatch, tmp_path):
    root = tmp_path / "roots"
    root.mkdir()
    monkeypatch.setattr(docfence, "read_roots", lambda: (root.resolve(),))
    return root


def test_markdown_under_a_root_is_allowed(fenced):
    doc = fenced / "note.md"
    doc.write_text("# hi")
    assert docfence.fenced_path(str(doc)) == doc.resolve()


def test_a_file_outside_every_root_is_refused(fenced, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# no")
    with pytest.raises(PermissionError):
        docfence.fenced_path(str(outside))


def test_a_non_markdown_file_under_a_root_is_refused(fenced):
    other = fenced / "settings.json"
    other.write_text("{}")
    with pytest.raises(PermissionError):
        docfence.fenced_path(str(other))


def test_a_symlink_out_of_the_fence_is_refused(fenced, tmp_path):
    """The name is inside; the target is not. Resolution decides."""
    secret = tmp_path / "secret.md"
    secret.write_text("# private")
    link = fenced / "innocent.md"
    link.symlink_to(secret)
    with pytest.raises(PermissionError):
        docfence.fenced_path(str(link))


def test_a_symlink_into_the_fence_is_allowed(fenced, tmp_path):
    """The mirror case, and why resolving is not simply "be stricter": a rule
    installed as a link into a marketplace checkout is the normal arrangement,
    and refusing it would empty the viewer on a working machine."""
    real = tmp_path / "checkout"
    real.mkdir()
    target = real / "rule.md"
    target.write_text("# rule")
    link = fenced / "rule.md"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        docfence.fenced_path(str(link))          # target is outside, still no
    monkey_roots = (fenced.resolve(), real.resolve())
    docfence.read_roots = lambda: monkey_roots   # restored by the fixture's patch
    assert docfence.fenced_path(str(link)) == target.resolve()


def test_a_traversal_cannot_climb_out(fenced):
    with pytest.raises(PermissionError):
        docfence.fenced_path(str(fenced / ".." / ".." / "etc" / "passwd.md"))


def test_file_text_reports_size_and_truncation(fenced, monkeypatch):
    doc = fenced / "big.md"
    doc.write_text("x" * 100)
    monkeypatch.setattr(docfence, "MAX_READ_BYTES", 10)
    out = docfence.file_text(str(doc))
    assert out["name"] == "big.md"
    assert out["bytes"] == 100 and out["truncated"] is True
    assert len(out["text"]) == 10


def test_a_missing_markdown_file_is_not_found_not_forbidden(fenced):
    """404 and 403 are different answers and the route serves them differently
    — "there is nothing there" must never read as "you may not look"."""
    with pytest.raises(FileNotFoundError):
        docfence.file_text(str(fenced / "gone.md"))


def test_the_default_roots_are_never_empty():
    """A fence that admits nothing is not safe, it is a broken viewer — and it
    is what a `read_roots()` that swallowed its own failure would produce."""
    roots = docfence.read_roots()
    assert roots and all(isinstance(r, Path) for r in roots)


def test_the_roots_admit_every_live_marketplace(monkeypatch, tmp_path):
    """A plugin installed from a *directory* marketplace publishes its rows at
    the checkout's real path, not under the plugin cache. The fence has to
    admit exactly the set resolution can produce, or the client is handed a row
    and then refused the file behind it.
    """
    live = tmp_path / "somewhere" / "Checkout"
    live.mkdir(parents=True)
    monkeypatch.setattr(plugin_paths, "live_marketplace_roots",
                        lambda: {"Checkout": live})
    assert live in docfence.read_roots()

    doc = live / "note.md"
    doc.write_text("# published from the live checkout\n")
    assert docfence.fenced_path(str(doc)) == doc.resolve()


def test_a_broken_marketplace_file_narrows_the_fence_rather_than_breaking_it():
    """`read_roots()` swallows a marketplace read failure on purpose: the
    fixed roots still answer, so a corrupt file costs a client the plugin rows
    it could read — not every document on the machine."""
    def boom():
        raise ValueError("unreadable marketplace file")

    original = plugin_paths.live_marketplace_roots
    plugin_paths.live_marketplace_roots = boom
    try:
        roots = docfence.read_roots()
    finally:
        plugin_paths.live_marketplace_roots = original
    assert roots, "a broken marketplace file emptied the fence"


def test_the_roots_admit_the_authored_plan_directory():
    """A stage row points back at the plan it was parsed from, and that plan is
    authored in `~/.claude/plans/`. Left out of the fence, every plan written
    where the engines actually write one answers 403 while its row reads fine —
    a document route that can only serve documents nobody has."""
    assert (Path.home() / ".claude" / "plans") in docfence.read_roots()
