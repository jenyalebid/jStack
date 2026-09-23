"""The setup validator — does this machine have what a jRemote host needs?

`python3 -m jstack_host.install_host doctor`, and the tail of every
install. The contract a host stands on is small and every part of it is
observable, so this checks each part the way the host actually uses it and
says what to do about the ones that fail — rather than leaving a fresh
install to be diagnosed screen by screen from a phone.

Three grades. **fail** is something the host cannot serve without: no
`claude`, no `tmux`, no WebSocket server, no token. **warn** is a screen that
will be honest about being absent until the thing arrives: no jStack timeline
yet, no registry, an allowance nobody has sampled. **ok** is ok. The exit
status is the worst grade — a script can gate on it, a person can read it.

Every check is a function of the same seams the host reads at runtime
(`hostenv`, the spawn path, the feature modules), so a green doctor and a
working host are the same fact. A check that raises is a check that fails:
the validator must never be the thing that hides a broken machine.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import hostenv

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}


def _check(name: str, grade: str, detail: str, hint: str = "") -> dict:
    return {"name": name, "grade": grade, "detail": detail, "hint": hint}


def _which(binary: str) -> str | None:
    """Resolved against the host's spawn path, NOT this shell's — a binary
    only the shell can see is exactly the failure this exists to catch."""
    return shutil.which(binary, path=hostenv.spawn_path())


def _version(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


# ── the checks ──

def check_python() -> dict:
    v = sys.version_info
    if v < (3, 11):
        return _check("python", FAIL, f"{v.major}.{v.minor} — needs 3.11 or newer",
                      "brew install python@3.12 and re-run the installer")
    return _check("python", OK, f"{v.major}.{v.minor}.{v.micro} ({sys.executable})")


def check_claude() -> dict:
    path = _which("claude")
    if not path:
        return _check("claude", FAIL, "not on the host's spawn path",
                      f"install Claude Code, or symlink it into ~/.local/bin — "
                      f"the host looks in: {hostenv.spawn_path()}")
    return _check("claude", OK, f"{path} — {_version([path, '--version']) or 'version unknown'}")


def check_tmux() -> dict:
    path = _which("tmux")
    if not path:
        return _check("tmux", FAIL, "not on the host's spawn path",
                      "brew install tmux — every chat runs inside it")
    return _check("tmux", OK, f"{path} — {_version([path, '-V']) or 'version unknown'}")


def check_websocket() -> dict:
    for mod in ("wsproto", "websockets"):
        try:
            m = importlib.import_module(mod)
            return _check("websocket", OK,
                          f"{mod} {getattr(m, '__version__', '')}".strip())
        except ImportError:
            continue
    return _check("websocket", FAIL, "no WebSocket server library in the venv",
                  "pip install wsproto — without it every terminal attach is a 404")


def check_fd_limit() -> dict:
    try:
        import resource
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, ValueError, OSError):
        return _check("open files", WARN, "limit unreadable")
    if soft < 1024:
        return _check("open files", WARN, f"soft limit {soft} in this shell",
                      "the host raises its own to 8192 at startup; nothing to do")
    return _check("open files", OK, f"soft limit {soft}")


def check_token() -> dict:
    path = hostenv.token_path()
    if not path.exists():
        return _check("token", FAIL, f"missing at {path}",
                      "run the installer — it mints one")
    return _check("token", OK, f"present at {path}")


def check_profile() -> dict:
    p = hostenv.profile()
    if p.name == "default":
        root = getattr(p, "root", None)
        return _check("profile", OK, f"default — agents root {root}")
    return _check("profile", OK, f"{p.name}")


def check_agents() -> dict:
    agents = hostenv.active_agents()
    if not agents:
        return _check("agents", WARN, "no agent directories found",
                      "every directory under the agents root is an agent; "
                      "point JREMOTE_INSTANCE_ROOT at the tree that holds them")
    drawn = sum(1 for a in agents.values() if a.get("emoji"))
    names = ", ".join(sorted(agents))
    return _check("agents", OK, f"{len(agents)} ({names}); {drawn} with an emoji")


# A warning is a thing to go and do. None of the five checks below used to be.
#
# A fresh install ended on five yellow lines whose own hints said they were
# normal — "the first session that logs an entry creates it", "the board fills
# in as Claude Code sessions run", "optional", "open any interactive claude
# session once". Nothing to fix, nothing broken, and the reader learns in the
# first minute that yellow means nothing here. The next warning that does mean
# something gets skimmed past with the rest.
#
# So: state that arrives by itself with normal use grades OK and still says
# what is absent. WARN is kept for the cases that genuinely stay broken —
# log_event not installed at all, a registry file present but empty.


def check_registry() -> dict:
    p = hostenv.profile()
    if p.name != "default":
        return _check("registry", OK, f"the {p.name} profile's own")
    path = p.registry_path()
    if not path.exists():
        return _check("registry", OK, f"no agents.json at {path} yet — "
                      "agents show as bare directories until one exists")
    reg = p._registry()
    if not reg:
        return _check("registry", WARN, f"{path} holds no agent entries",
                      "each key is an agent id: {name, emoji, workspace, repos}")
    return _check("registry", OK, f"{path} — {len(reg)} entries")


def check_timeline() -> dict:
    from . import timeline
    db = hostenv.timeline_db()
    binary = timeline.log_event_bin()
    if binary is None:
        return _check("timeline", WARN, "jStack's log_event not installed",
                      "install the jStack plugin — the Timeline tab and tags "
                      "read its store")
    if not db.exists():
        return _check("timeline", OK, f"log_event at {binary}, no store yet at "
                      f"{db} — the first session that logs an entry creates it")
    return _check("timeline", OK, f"{db}")


def check_transcripts() -> dict:
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return _check("transcripts", OK, f"none yet at {root} — the board "
                      "fills in as Claude Code sessions run")
    n = sum(1 for _ in root.glob("*/*.jsonl"))
    return _check("transcripts", OK, f"{n} under {root}")


def check_scheduler() -> dict:
    d = hostenv.scheduler_config_dir()
    if not (d / "schedule.json").exists():
        return _check("scheduler", OK, f"no schedule at {d} yet — optional; "
                      "its journal feeds the Runs source of the Timeline")
    return _check("scheduler", OK, f"{d}")


def check_allowance() -> dict:
    """What the Usage bars would draw right now, and why if the answer is
    nothing.

    Reads the merged store rather than one tier: this check used to look only
    at the Claude CLI's cache, so on a host whose bars were being kept fresh
    by the status-line sampler it reported "no usage reading cached yet" while
    the app drew a live meter — a check that says the opposite of the thing it
    checks. It also has to name the missing writer, because "no reading" and
    "no sampler installed" are repaired by completely different actions.
    """
    import time
    from . import allowance
    from .claude_settings import statusline_state

    state = allowance.read()["providers"]
    lines, missing = [], []
    for pid, p in state.items():
        if p is None:
            missing.append(pid)
            continue
        pcts = ", ".join(f"{w['label']} {w['pct']:.0f}%"
                         for w in p["windows"] if w.get("pct") is not None)
        age = int(p.get("age_seconds") or 0)
        lines.append(f"{p['label']} {pcts or 'no windows'} "
                     f"({p.get('source')}, {age}s ago"
                     f"{', STALE' if p.get('stale') else ''})")

    wired, detail = statusline_state()
    if "claude" in missing and not wired:
        return _check("allowance", WARN,
                      "no Claude reading, and nothing on this machine samples "
                      "one", f"{detail} — run host/tools/claude_setup.py to "
                      "wire the status-line sampler, or the Usage bars stay "
                      "empty until someone runs /usage by hand")
    if not lines:
        return _check("allowance", OK, "sampler wired, no reading yet — the "
                      "Usage bars fill in on the next status-line render")
    note = f"not connected: {', '.join(missing)}" if missing else ""
    return _check("allowance", OK, "; ".join(lines), note)


def check_repos() -> dict:
    repos = hostenv.repos()
    if not repos:
        return _check("repos", WARN, "no git checkouts found",
                      "the Commits source of the Timeline scans the repo root "
                      "(JSTACK_REPO_ROOT, default: parent of the agents root)")
    return _check("repos", OK, f"{len(repos)} checkouts")


def check_service() -> dict:
    """Only meaningful once installed — reported as-is, never as a failure of
    the machine: the doctor also runs before the LaunchAgent exists. A profile
    may declare the host runs embedded in another server (`embedded_in`), and
    then there is no LaunchAgent to look for."""
    from . import install_host
    embedded = getattr(hostenv.profile(), "embedded_in", "")
    if embedded:
        return _check("service", OK, f"embedded in {embedded}")
    if not install_host.plist_path().exists():
        return _check("service", WARN, "LaunchAgent not installed")
    # The agent's own port, never the default. Probing DEFAULT_PORT graded a
    # host installed on 9099 by whatever held 9090 — on this machine the
    # dashboard — and passed it as `answering on 9090`. A check that grades the
    # wrong program is worse than no check: it reported every check passed on a
    # machine whose host it had never contacted.
    port = install_host.installed_port() or install_host.DEFAULT_PORT
    served = install_host.health(port)
    if not served:
        return _check("service", FAIL, f"installed but nothing answers on {port}",
                      "launchctl kickstart -k gui/$(id -u)/com.jremote.host; "
                      "then read logs/host.err in the state dir")
    if served.get("service") != "jremote-host":
        return _check("service", FAIL,
                      f"something that is not this host answers on {port}",
                      f"another program holds {port} — reinstall on a free port: "
                      "jstack-host install --port <port>")
    return _check("service", OK, f"answering on {port}, "
                  f"profile {served.get('profile')}")


def _behind(root: str, old: str, new: str) -> str:
    """How many commits `new` is ahead of `old` in the checkout at `root`,
    as a string — or '' when git cannot relate them (a sha the checkout has
    never seen relates to nothing, and guessing a number would be worse)."""
    try:
        r = subprocess.run(["git", "-C", root, "rev-list", "--count",
                            f"{old}..{new}"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def check_source() -> dict:
    """Does the running host serve the bytes the tree holds?

    The host is an editable install: what a process serves is the tree as of
    its own startup, and the tree moves on underneath it with the version
    label never changing. This machine holds four copies of jStack — checkout,
    plugin cache, running host, app build — and the running host was the one
    whose identity could only be guessed. The serving side comes from the
    stamp the process recorded as it started (the embed marker for an
    embedded host, `/api/health` for a standalone one); the tree side is
    computed fresh right here. Every mismatch is a thing to go and do, so
    every mismatch is a WARN with the action in the hint.
    """
    from . import embed, install_host, sourcestamp
    tree = sourcestamp.capture()
    if not tree["sha"]:
        return _check("source", OK, "not served from a git checkout — a real "
                      "install cannot drift from a tree it does not have")
    now = sourcestamp.describe(tree)
    embedded = embed.server()
    if embedded:
        serving, where = embed.read().get("source"), f"embedded in {embedded}"
    elif install_host.plist_path().exists():
        port = install_host.installed_port() or install_host.DEFAULT_PORT
        served = install_host.health(port)
        if served is None:
            return _check("source", WARN, f"tree at {now}; the installed host "
                          f"answers nothing on {port}, so what it serves is "
                          "unknowable",
                          "the service check owns the outage — restarting it "
                          "deploys the tree as a side effect")
        serving, where = served.get("source"), f"answering on {port}"
    else:
        return _check("source", OK, f"tree at {now} — no host on this machine, "
                      "nothing serving to drift")
    if not isinstance(serving, dict) or not serving.get("sha"):
        return _check("source", WARN, f"tree at {now}; the serving process "
                      "predates the source stamp",
                      "restart the host — its next startup records what it serves")
    sha, dirty = str(serving["sha"]), bool(serving.get("dirty"))
    if dirty:
        return _check("source", WARN, f"serving {sha[:12]}+dirty ({where}) — "
                      "the running code is UNCOMMITTED bytes, not any commit",
                      "commit what is meant to run and restart on a clean tree; "
                      "code that answers requests must be reproducible from git")
    if sha != tree["sha"]:
        n = _behind(tree["root"], sha, tree["sha"])
        gap = (f"{n} commit(s) not deployed" if n else
               "a sha this checkout does not contain")
        detail = f"serving {sha[:12]}, tree at {now} — {gap}"
        hint = "restart the host to deploy the tree"
        if tree["dirty"]:
            hint += " — but it is dirty right now, and a restart ships those " \
                    "uncommitted bytes too; commit first"
        return _check("source", WARN, detail, hint)
    if tree["dirty"]:
        return _check("source", WARN, f"serving {sha[:12]} clean ({where}), but "
                      "the served package has uncommitted edits — the next "
                      "restart ships them",
                      "commit or drop them; what runs must be a commit")
    return _check("source", OK, f"serving {sha[:12]}, in step with the tree "
                  f"({where})")


def check_app() -> dict:
    """The fourth copy — the Mac app build, against the feed this hub publishes.

    Only a publishing hub can grade this locally; any other Mac gets its app
    build reported as a fact and no verdict, because comparing against a feed
    that lives on another machine would mean guessing.
    """
    import plistlib
    from . import desk, releases
    info_plist = Path(desk.APP) / "Contents" / "Info.plist"
    if not info_plist.exists():
        return _check("app", OK, f"no app at {desk.APP} — not an app Mac")
    try:
        with open(info_plist, "rb") as fh:
            info = plistlib.load(fh)
        build = int(info.get("CFBundleVersion") or 0)
        version = str(info.get("CFBundleShortVersionString") or "?")
    except (OSError, ValueError, plistlib.InvalidFileException) as e:
        return _check("app", WARN, f"cannot read {info_plist}: {e}",
                      "reinstall the app — an unreadable bundle is not a version")
    if not releases.publishes():
        return _check("app", OK, f"build {build} ({version}) installed — this "
                      "machine does not publish the feed, no local truth to "
                      "compare against")
    try:
        latest = releases.latest()
    except releases.ReleaseError as e:
        return _check("app", WARN, f"build {build} installed; release feed "
                      f"broken: {e}",
                      "republish — every app on the fleet updates from this feed")
    if latest is None:
        return _check("app", OK, f"build {build} ({version}) installed; feed "
                      "empty — nothing published yet")
    if latest["build"] == build:
        return _check("app", OK, f"build {build} ({version}) — matches the "
                      "published release")
    if latest["build"] > build:
        return _check("app", WARN, f"installed build {build}, feed publishes "
                      f"{latest['build']} — the app is behind its own feed",
                      "the app self-updates on launch; if it stays behind, "
                      "the updater is the bug")
    return _check("app", WARN, f"installed build {build} is AHEAD of the "
                  f"published {latest['build']} — a local build nobody published",
                  "publish it or reinstall the released app; an unpublished "
                  "binary is exactly the unaccounted-for copy")


def check_file_sharing() -> dict:
    """An optional surface is quiet when absent and loud when unsafe."""
    from . import fileshare
    observed = fileshare.status()
    if not observed["available"]:
        return _check("files", OK, observed.get("reason", "not available"))
    unexpected = observed["unexpected"]
    if unexpected:
        names = ", ".join(row["name"] for row in unexpected)
        grade = FAIL if observed.get("service_enabled") else WARN
        return _check("files", grade, f"undeclared SMB share point(s): {names}",
                      "run `jstack-host files status`, then "
                      "`sudo jstack-host files setup --apply`")
    if not observed["configured"]:
        return _check("files", OK, "selected-folder sharing not configured")
    if observed["ready"]:
        names = ", ".join(row["name"] for row in observed["shares"])
        return _check("files", OK, f"ready: {names}")
    if observed.get("secure") and not observed.get("service_enabled"):
        return _check("files", WARN, "selected shares are secure but File Sharing is off",
                      "enable File Sharing in System Settings")
    return _check("files", WARN, "selected shares are configured but drifted",
                  "run `jstack-host files status`, then "
                  "`sudo jstack-host files setup --apply`")


CHECKS = (check_python, check_claude, check_tmux, check_websocket, check_fd_limit,
          check_token, check_profile, check_agents, check_registry, check_timeline,
          check_transcripts, check_scheduler, check_allowance, check_repos,
          check_service, check_source, check_app, check_file_sharing)


def checks() -> list[dict]:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:                              # noqa: BLE001
            name = fn.__name__.removeprefix("check_").replace("_", " ")
            out.append(_check(name, FAIL, f"check crashed: {type(e).__name__}: {e}"))
    return out


def worst(results: list[dict]) -> str:
    return max((r["grade"] for r in results), key=lambda g: _RANK[g], default=OK)


def report(out=None) -> int:
    """Print the table; exit status 0 ok, 1 warnings only, 2 something the
    host cannot serve without."""
    out = out or sys.stdout
    results = checks()
    mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    for r in results:
        print(f"{mark[r['grade']]}  {r['name']:<12} {r['detail']}", file=out)
        if r["hint"] and r["grade"] != OK:
            print(f"      → {r['hint']}", file=out)
    grade = worst(results)
    summary = {OK: "every check passed", WARN: "serving; some screens wait on the warnings above",
               FAIL: "the host cannot serve chats until the failures above are fixed"}
    print(f"\n{summary[grade]}", file=out)
    return _RANK[grade]


if __name__ == "__main__":
    raise SystemExit(report())
