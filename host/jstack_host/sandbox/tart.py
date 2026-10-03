"""The guest driver: tart, scoped to one tenant's own TART_HOME.

Every guest boots with a window on the host's display. `tart run` from an ssh
shell has no WindowServer, so the run goes through LaunchServices (`open`),
which places it in the console user's GUI session.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path


class TartError(RuntimeError):
    pass


class Tart:
    def __init__(self, binary: str, home: Path):
        self.binary = binary
        self.home = home

    def env(self) -> dict:
        return {**os.environ, "TART_HOME": str(self.home)}

    def run(self, *args: str, check: bool = True, timeout: float | None = None,
            **kw) -> subprocess.CompletedProcess:
        self.home.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([self.binary, *args], env=self.env(), text=True,
                              capture_output=True, timeout=timeout, **kw)
        if check and proc.returncode:
            raise TartError(f"tart {' '.join(args)}: "
                            f"{(proc.stderr or proc.stdout).strip()}")
        return proc

    def list(self) -> list[dict]:
        if not self.home.exists():
            return []
        return json.loads(self.run("list", "--format", "json").stdout or "[]")

    def names(self) -> set[str]:
        return {vm["Name"] for vm in self.list()}

    def running(self, name: str) -> bool:
        return any(vm["Name"] == name and vm.get("State") == "running"
                   for vm in self.list())

    def clone(self, src: str, dst: str) -> None:
        self.run("clone", src, dst, timeout=3600)

    def configure(self, name: str, cpu: int, mem_gb: int) -> None:
        self.run("set", name, "--cpu", str(cpu), "--memory", str(mem_gb * 1024))

    def app(self) -> str:
        """The tart.app bundle behind the binary, read through any wrapper."""
        real = Path(self.binary).resolve()
        if ".app/" in str(real):
            return str(real).split(".app/")[0] + ".app"
        try:
            text = real.read_text(errors="ignore")
        except OSError:
            text = ""
        found = re.search(r'"?([^"\s]+\.app)/Contents/MacOS/tart', text)
        if not found:
            raise TartError(f"cannot find tart.app behind {self.binary}")
        return os.path.expandvars(found.group(1))

    def boot(self, name: str, wait: float, args: list[str] = ()) -> str:
        """Start `name` in a GUI window and return its address once it answers."""
        subprocess.run(["open", "-n", "-a", self.app(), "--env",
                        f"TART_HOME={self.home}", "--args", "run", name, *args],
                       check=True, capture_output=True, text=True)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.running(name):
                ip = self.run("ip", name, "--wait", "30", check=False)
                if ip.returncode == 0 and ip.stdout.strip():
                    if self.exec(name, ["true"], check=False,
                                 timeout=30).returncode == 0:
                        return ip.stdout.strip()
            time.sleep(3)
        raise TartError(f"{name} did not come up within {int(wait)}s")

    def exec(self, name: str, argv: list[str], interactive: bool = False,
             tty: bool = False, check: bool = True, capture: bool = True,
             timeout: float | None = None, input: str | None = None,
             stdin=None, stdout=None):
        flags = (["-i"] if interactive or input is not None else []) + (["-t"] if tty else [])
        if not capture:
            return subprocess.run([self.binary, "exec", *flags, name, *argv],
                                  env=self.env(), timeout=timeout,
                                  stdin=stdin, stdout=stdout)
        return self.run("exec", *flags, name, *argv, check=check,
                        timeout=timeout, input=input)

    def ip(self, name: str, wait: float) -> str:
        """The guest's address now. A shared-network guest's lease can move
        across boots and is read stale just after one, so this asks again
        until the address answers."""
        deadline = time.time() + wait
        while True:
            out = self.run("ip", name, "--wait", "30", check=False)
            addr = out.stdout.strip()
            if out.returncode == 0 and addr and self.exec(
                    name, ["sh", "-c", f"ifconfig | grep -qw {addr}"],
                    check=False, timeout=30).returncode == 0:
                return addr
            if time.time() > deadline:
                raise TartError(f"{name} has no address: "
                                f"{(out.stderr or out.stdout).strip()}")
            time.sleep(3)

    def stop(self, name: str) -> None:
        self.run("stop", name, "--timeout", "30", check=False)

    def delete(self, name: str) -> None:
        if self.running(name):
            self.stop(name)
        self.run("delete", name, check=False)

    def pull(self, ref: str) -> None:
        self.run("pull", ref, timeout=6 * 3600)
