"""Stable, stdlib-only entry point for the managed updater.

An in-flight transaction keeps its original runtime. Only a confirmed update
may move runtime_imports; launchd/reboot then starts that same confirmed copy.
"""
import json
import sys
from pathlib import Path


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    root = (args.state_dir / "updates").resolve()
    configuration = json.loads((root / "config.json").read_text())
    paths = [Path(value).resolve() for value in configuration["runtime_imports"]]
    if not paths or any(not path.is_relative_to(root) or not path.is_dir() for path in paths):
        raise RuntimeError("updater runtime must be an installed copy inside its state directory")
    sys.path[:0] = [str(path) for path in paths]
    from jstack_host.update_supervisor import main as supervise
    supervise()


if __name__ == "__main__":
    main()
