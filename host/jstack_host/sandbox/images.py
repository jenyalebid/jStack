"""The image library: a recipe per purpose, baked into a verified, tagged guest.

A recipe is JSON under `<state>/sandbox/recipes/<name>.json`: the base it
starts from, steps that run or copy in, secrets read from this instance's
credentials at bake time, and a verify command. The tag records the recipe's
hash, so an edited recipe makes every bake of the old one stale.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from . import client, settings


def recipes_dir() -> Path:
    return settings.state_dir() / "recipes"


def load(name: str) -> dict | None:
    try:
        return json.loads((recipes_dir() / f"{name}.json").read_text())
    except OSError:
        return None


def sha(name: str) -> str | None:
    recipe = load(name)
    if recipe is None:
        return None
    return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:16]


def names() -> list[str]:
    return sorted(p.stem for p in recipes_dir().glob("*.json")) if recipes_dir().exists() else []


def _secrets(recipe: dict) -> dict:
    if not recipe.get("secrets"):
        return {}
    from .. import hostenv
    creds = hostenv.credentials_dir()
    return {k: (creds / rel).read_text().strip() for k, rel in recipe["secrets"].items()}


def _script(lines: list[str], env: dict) -> str:
    exports = [f"export {k}={shlex.quote(v)}" for k, v in env.items()]
    return "\n".join(["set -e", *exports, *lines]) + "\n"


def _run_script(target: dict, lease: dict, text: str) -> int:
    with tempfile.TemporaryFile("w+") as fh:
        fh.write(text)
        fh.seek(0)
        return client.run(target, lease["id"], ["zsh", "-s"], interactive=True,
                          tenant=lease["tenant"], stdin=fh)


def bake(name: str, say=print) -> dict:
    """Bake on the best host that has the base; tag only after verify passes."""
    recipe = load(name)
    if recipe is None:
        raise client.SandboxError(f"no recipe {name} in {recipes_dir()}")
    env = _secrets(recipe)
    lease = client.get(recipe["base"], kind="own", say=say)
    target = client.target_of(client.held(lease["id"]))
    try:
        for i, step in enumerate(recipe.get("steps", []), 1):
            if "copy" in step:
                say(f"step {i}: copy {step['copy']} -> {step['to']}")
                code = push(lease["id"], step["copy"], step["to"])
            else:
                say(f"step {i}: {step['run']}")
                code = _run_script(target, lease, _script([step["run"]], env))
            if code:
                raise client.SandboxError(f"step {i} failed (exit {code}); nothing was tagged")
        if recipe.get("verify"):
            say(f"verify: {recipe['verify']}")
            if _run_script(target, lease, _script([recipe["verify"]], {})):
                raise client.SandboxError("verify failed; nothing was tagged")
        out = client.call(target, "image-tag", {"lease": lease["id"], "image": name,
                                                "recipe": sha(name)})
        client._forget(lease["id"])
        return {**out, "host": target["name"]}
    except BaseException:
        client.release(lease["id"])
        raise


def push(lease_id: str, src: str, dest: str) -> int:
    entry = client.held(lease_id)
    src_path = Path(src).expanduser().resolve()
    tar = subprocess.Popen(["tar", "-c", "-C", str(src_path.parent), src_path.name],
                           stdout=subprocess.PIPE)
    from .host import push_argv
    code = client.run(client.target_of(entry), lease_id, push_argv(dest),
                      interactive=True, tenant=entry["tenant"], stdin=tar.stdout)
    tar.stdout.close()
    return code or tar.wait()


def pull(lease_id: str, path: str, dest: str = ".") -> int:
    entry = client.held(lease_id)
    os.makedirs(dest, exist_ok=True)
    untar = subprocess.Popen(["tar", "-x", "-C", dest], stdin=subprocess.PIPE)
    from .host import pull_argv
    code = client.run(client.target_of(entry), lease_id, pull_argv(path),
                      tenant=entry["tenant"], stdout=untar.stdin)
    untar.stdin.close()
    return code or untar.wait()
