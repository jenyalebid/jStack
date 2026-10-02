"""Remove owned shell/SSH declarations without replacing unrelated settings."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile


def strip_blocks(text: str, begin: str, end: str) -> str:
    kept, inside = [], False
    for line in text.splitlines(keepends=True):
        marker = line.strip()
        if marker == begin:
            if inside:
                raise ValueError("nested managed block")
            inside = True
        elif marker == end:
            if not inside:
                raise ValueError("unmatched managed block end")
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise ValueError("unterminated managed block")
    return "".join(kept)


def rewrite(path: Path, transform) -> None:
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise ValueError("symlink in settings ancestry")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    with os.fdopen(fd, "r") as stream:
        before = os.fstat(stream.fileno())
        if before.st_uid != os.getuid() or not stat.S_ISREG(before.st_mode):
            raise ValueError("settings are not an owned regular file")
        original = stream.read()
    updated = transform(original)
    if original == updated:
        return
    fd, temporary = tempfile.mkstemp(prefix=".sos-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(before.st_mode))
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size):
            raise ValueError("settings changed during cleanup")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run() -> int:
    if os.geteuid() == 0 or not Path("/private/var/db/live.jstack.sos").exists():
        raise ValueError("user cleanup requires an active approved wipe")
    home = Path.home()
    for name, kind in (("authorized_keys", "keys"), ("config", "hosts")):
        begin, end = f"# >>> jremote managed {kind} >>>", f"# <<< jremote managed {kind} <<<"
        rewrite(home / ".ssh" / name, lambda text: strip_blocks(text, begin, end))
    owned = re.compile(r"^\s*export\s+(?:PATH=.*# jstack\s*$|JSTACK_ROOT=)")
    for name in (".zshrc", ".zprofile", ".bash_profile", ".bashrc", ".profile"):
        rewrite(home / name, lambda text: "".join(line for line in text.splitlines(keepends=True)
                                                 if not owned.match(line)))
    return 0
