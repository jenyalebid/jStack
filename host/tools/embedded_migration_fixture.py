"""Turn a disposable released-host fixture into a private embedding app.

The fixture preserves the released state and interpreter. It mounts the same
public routers a private dashboard uses and reports its real import/profile.
"""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time


def main():
    assert subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).startswith("VirtualMac")
    assert os.geteuid() != 0
    home = Path.home()
    assert (home / "migration-receipts/rollback.json").exists(), "requires the restored released fixture"
    from jstack_host import install_host
    path = install_host.plist_path()
    job = plistlib.loads(path.read_bytes())
    assert job["Label"] == "com.jremote.host" and "jstack_host.server" in job["ProgramArguments"]
    root = home / "embedding-lab"
    root.mkdir(mode=0o700)
    package = root / "lab_dashboard"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "profile.py").write_text('''from jstack_host.hostenv import DefaultProfile, instance_root
class Profile(DefaultProfile):
    name = "embedding-lab"
    embedded_in = "Fixture dashboard"
def make_profile():
    return Profile(instance_root())
''')
    (package / "app.py").write_text('''import sys
import jstack_host
from fastapi import FastAPI
from jstack_host import embed, hostenv
from jstack_host.router import router, unauthenticated_router, grant_router
from jstack_host.pty import ws_router
app = FastAPI()
for route in (router, unauthenticated_router, grant_router, ws_router):
    app.include_router(route)
@app.get("/api/health")
def health():
    return {"status": "ok", "embedding": True}
@app.get("/fixture/import")
def imported():
    return {"package": jstack_host.__file__, "profile": hostenv.profile().name,
            "interpreter": sys.executable, "state": str(hostenv.state_dir())}
if __name__ == "__main__":
    import uvicorn
    embed.declare(port=9392)
    uvicorn.run("lab_dashboard.app:app", host="127.0.0.1", port=9392)
''')
    (root / "original-host.plist").write_bytes(path.read_bytes())
    job["ProgramArguments"] = [job["ProgramArguments"][0], "-m", "lab_dashboard.app"]
    job["WorkingDirectory"] = str(root)
    env = job["EnvironmentVariables"]
    env.update(JREMOTE_HOST_PROFILE="external", JREMOTE_PROFILE_MODULE="lab_dashboard.profile",
               JREMOTE_TOKEN_PATH=str(home / ".local/state/migration-lab/api-token"),
               PYTHONPATH=str(home / "release76/host"))
    install_host._launchctl("bootout", f"{install_host._domain()}/{job['Label']}")
    assert install_host.wait_unloaded(job["Label"])
    path.write_bytes(plistlib.dumps(job))
    assert install_host.bootstrap(job["Label"], path).returncode == 0
    import httpx
    deadline = time.monotonic() + 30
    while True:
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                response = client.get("http://127.0.0.1:9392/fixture/import")
                response.raise_for_status()
                observed = response.json()
                assert observed["profile"] == "embedding-lab"
                assert observed["package"].startswith(str(home / "release76/host"))
                break
        except httpx.HTTPError:
            assert time.monotonic() < deadline, "embedding fixture did not start"
            time.sleep(0.25)
    catalog = root / "catalog.json"
    catalog.write_text(json.dumps({"dashboard": job}, indent=2))
    catalog.chmod(0o600)
    print(json.dumps({"fixture": "embedded released host", "observed": observed, "catalog": str(catalog)}))


if __name__ == "__main__":
    main()
