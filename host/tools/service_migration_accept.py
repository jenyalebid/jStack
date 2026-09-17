"""Exercise an exact legacy user-job migration on a disposable GUI Mac."""
import json
import os
from pathlib import Path
import plistlib
import subprocess

from jstack_host import install_host, migrate_services as migration


def main():
    model = subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip()
    assert model.startswith("VirtualMac") and os.geteuid() != 0
    app = Path("/Applications/jStack Hub Services.app")
    catalog = json.loads(Path("/Users/admin/service-catalog.json").read_text())
    job = catalog["acceptance"]
    path = install_host.plist_path(job["Label"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        assert plistlib.loads(path.read_bytes()) == job, "refusing to overwrite an existing definition"
    else:
        path.write_bytes(plistlib.dumps(job))
    if not install_host.is_loaded(job["Label"]):
        assert install_host.bootstrap(job["Label"], path).returncode == 0
    assert install_host.is_loaded(job["Label"])
    journal = migration.prepare(app, catalog)
    migration.apply(journal)
    assert not path.exists()
    assert not install_host.is_loaded(job["Label"])
    assert install_host.is_loaded("live.jstack.automation.acceptance")
    assert migration.load(journal)["state"] == "migrated"
    migration.rollback(journal)
    assert path.exists()
    assert install_host.is_loaded(job["Label"])
    assert not install_host.is_loaded("live.jstack.automation.acceptance")
    assert migration.load(journal)["state"] == "rolled_back"
    print(json.dumps({"migration": True, "rollback": True, "journal": str(journal)}))
    install_host._launchctl("bootout", f"{install_host._domain()}/{job['Label']}")
    assert install_host.wait_unloaded(job["Label"])
    path.rename(journal / "acceptance.finished.plist")


if __name__ == "__main__":
    main()
