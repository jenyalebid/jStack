"""The updater's import freeze: nothing in its closure may import after the swap.

Once the Hub bundle is replaced under the running updater, sys.path resolves
into the NEW bundle; any first-time import mid-transaction would mix releases
inside one process. preload.MODULES is the working set loaded before a byte
moves. This test runs the preload in a clean interpreter, then walks every
import statement — module level and function local — in the closure's own
sources and fails on any target the preload did not already put in
sys.modules.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = '''
import sys
import jstack_host.preload as preload
preload.updater()
frozen = set(sys.modules)
import ast
import importlib.util
import json
from pathlib import Path

package = Path(preload.__file__).resolve().parent
closure = [name.split(".", 1)[1] for name in preload.MODULES if name.startswith("jstack_host.")]
missing = set()
for stem in closure:
    tree = ast.parse((package / (stem + ".py")).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = "jstack_host" if node.level else ""
            if node.module:
                base = base + "." + node.module if base else node.module
            targets = []
            for alias in node.names:
                candidate = base + "." + alias.name
                try:
                    is_module = importlib.util.find_spec(candidate) is not None
                except (ImportError, ValueError):
                    is_module = False
                targets.append(candidate if is_module else base)
        else:
            continue
        for target in targets:
            if target not in frozen:
                missing.add(stem + " imports " + target)
print(json.dumps(sorted(missing)))
'''


def test_every_import_in_the_update_closure_is_preloaded():
    host = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True,
                            timeout=120, env={**os.environ, "PYTHONPATH": str(host)})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_preload_covers_the_supervisors_own_module():
    from jstack_host import preload
    assert "jstack_host.update_supervisor" in preload.MODULES
    assert "jstack_host.update_app" in preload.MODULES
