"""Copy native tools and their dylib closure into a signed candidate."""
import hashlib
import os
from pathlib import Path
import shutil

from .update_macos import command


def dependencies(path: Path) -> list[str]:
    return list(dict.fromkeys(line.strip().split(" (", 1)[0]
        for line in command(["/usr/bin/otool", "-L", str(path)]).splitlines()
        if line.startswith("\t")))


def bundle(source: Path, destination: Path, libraries: Path, licenses: Path) -> list[Path]:
    """Reject unresolvable loader-relative inputs rather than guessing."""
    mapping = {source.resolve(): destination}
    pending = [source.resolve()]
    while pending:
        original = pending.pop()
        target = mapping[original]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        notices = []
        for parent in original.parents:
            notices = [parent / name for name in ("COPYING", "LICENSE", "LICENSE.md") if (parent / name).is_file()]
            if notices:
                break
        if not notices:
            raise ValueError(f"no license notice found for build input: {original.name}")
        licenses.mkdir(parents=True, exist_ok=True)
        for notice in notices:
            shutil.copy2(notice, licenses / f"{target.name}-{notice.name}")
        changes = []
        for dependency in dependencies(original):
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if not dependency.startswith("/"):
                raise ValueError(f"unresolved tool library: {dependency}")
            path = Path(dependency).resolve(strict=True)
            if path == original:
                continue  # LC_ID_DYLIB, not a dependent library
            if path not in mapping:
                # A source digest in the basename makes collisions explicit,
                # independent of which Cellar symlink was used to find it.
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                mapping[path] = libraries / f"{digest}-{path.name}"
                pending.append(path)
            replacement = "@loader_path/" + os.path.relpath(mapping[path], target.parent)
            changes += ["-change", dependency, replacement]
        if original.suffix == ".dylib":
            changes = ["-id", "@rpath/" + target.name, *changes]
        if changes:
            command(["/usr/bin/install_name_tool", *changes, str(target)])
    return list(mapping.values())
