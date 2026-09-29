"""No module in the package defines the same top-level name twice.

Python keeps the last definition silently, so a duplicate is not an error —
it is a working import with the wrong body behind the name. Two branches each
added a `build_client` to `publish_release` (one for a stack publication's
client, one for the standalone client road); each was green alone, and the
merge of both left every caller of the first one passing its arguments to the
second. The symptom was a `TypeError` deep in an unrelated line, and the road
it broke was the one that builds a release.

A grep cannot see this and a type checker is not run on the gate, so the
package asserts it about itself.
"""
import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "jstack_host"


def _duplicate_top_level_names(source: str) -> list[str]:
    tree = ast.parse(source)
    seen, duplicates = {}, []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in seen:
                duplicates.append(f"{node.name} (lines {seen[node.name]} and {node.lineno})")
            seen[node.name] = node.lineno
    return duplicates


@pytest.mark.parametrize("module", sorted(PACKAGE.rglob("*.py")), ids=lambda p: p.name)
def test_no_module_defines_a_name_twice(module):
    duplicates = _duplicate_top_level_names(module.read_text())
    assert not duplicates, (
        f"{module.name} defines the same top-level name twice: {'; '.join(duplicates)} "
        "— the later one silently wins, so callers written for the earlier "
        "signature pass their arguments to the wrong body"
    )


def test_the_check_sees_a_duplicate_when_there_is_one():
    """The guard above is only worth having if it can go red."""
    assert _duplicate_top_level_names("def f():\n    pass\n\n\ndef f():\n    pass\n")
    assert not _duplicate_top_level_names("def f():\n    pass\n\n\ndef g():\n    pass\n")
