"""hostenv — the one seam between the jRemote host API and this machine.

Everything else in this package is generic. The roster is not: which
agents exist, where their workspaces are, and which agent owns a Claude
project dir are facts about the instance. These tests pin both profiles and,
more importantly, pin the seam itself — a direct `lib.agents` import anywhere
in the package silently re-couples the host to this Mac, and the guard at the
bottom is what catches that.
"""

import json
from pathlib import Path

import pytest

from jstack_host import hostenv


@pytest.fixture(autouse=True)
def _clean_profile():
    """Every test resolves its own profile; none leaks into the next."""
    hostenv.reset_profile()
    yield
    hostenv.reset_profile()


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A standalone instance root — agents are directories, nothing else."""
    root = tmp_path / "Agents"
    (root / "Iris" / "chat" / "reminder").mkdir(parents=True)
    (root / "Iris" / "pm").mkdir()
    (root / "Iris" / "pad").mkdir()          # reserved — never a sub-mode
    (root / "Iris" / ".claude").mkdir()      # hidden — never a sub-mode
    (root / "Nova" / "chat").mkdir(parents=True)
    (root / "web" / "chat").mkdir(parents=True)
    (root / "web-2" / "chat").mkdir(parents=True)   # hyphen + digit, and a prefix
                                                    # of it is itself an agent
    (root / ".git").mkdir()                   # hidden — never an agent
    (root / "pad").mkdir()                    # reserved — never an agent
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(root))
    hostenv.reset_profile()
    return root


# ── profile selection ──

@pytest.fixture
def machine_profile(monkeypatch, tmp_path):
    """A machine that supplies its own profile module, written for the test.

    The real one on any given host names that host's plumbing, so asserting
    against it pins a machine rather than the seam. What the seam promises is
    narrower and testable anywhere: *if* the module is importable, `auto`
    resolves to it, and the answers come back untouched.
    """
    mod = tmp_path / "probe_host_profile.py"
    mod.write_text(
        "from pathlib import Path\n"
        "class P:\n"
        "    name = 'probe'\n"
        "    def active_agents(self): return {'sentinel': {}}\n"
        "    def workspace(self, a): return Path('/sentinel') / a\n"
        "def make_profile(): return P()\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("JREMOTE_PROFILE_MODULE", "probe_host_profile")
    hostenv.reset_profile()


def test_auto_prefers_the_machines_profile_module(monkeypatch, machine_profile):
    """A machine that supplies a profile module gets it, not the fallback."""
    monkeypatch.delenv("JREMOTE_HOST_PROFILE", raising=False)
    assert hostenv.profile().name == "probe"


def test_external_profile_delegates_verbatim(monkeypatch, machine_profile):
    """The seam reinterprets nothing — the profile's answer is the answer."""
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "external")
    assert hostenv.active_agents() == {"sentinel": {}}
    assert hostenv.workspace("x") == Path("/sentinel/x")


def _block_lib(monkeypatch):
    """Make this look like a machine that has never had the embedding tree."""
    real_import = __import__

    def no_lib(name, *args, **kwargs):
        if name == "lib" or name.startswith("lib."):
            raise ImportError("no lib on this machine")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_lib)


def test_explicit_external_raises_when_the_module_cannot_load(monkeypatch):
    """A demanded profile that cannot load is an error, never a silent
    downgrade.

    Same rule as the engine resolver: running as something other than what
    was asked for surfaces much later, in a board nobody can explain.
    """
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "external")
    monkeypatch.setenv("JREMOTE_PROFILE_MODULE", "absent_test_host_profile")
    _block_lib(monkeypatch)
    with pytest.raises(ImportError):
        hostenv.profile()


def test_auto_falls_back_to_default_without_lib(monkeypatch, tmp_path):
    """A machine with no embedding tree gets a working roster, not an exception."""
    monkeypatch.delenv("JREMOTE_HOST_PROFILE", raising=False)
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(tmp_path))
    # Imported embedding profiles survive in sys.modules between tests.
    monkeypatch.setenv("JREMOTE_PROFILE_MODULE", "absent_test_host_profile")
    _block_lib(monkeypatch)
    assert hostenv.profile().name == "default"


# ── instance_root resolution: the blank-thread bug ──

def test_instance_root_derives_agents_from_jstack_root(monkeypatch, tmp_path):
    """With no explicit override, the agents tree is `$JSTACK_ROOT/Agents` —
    the one place the installer, the plugin and doctor all resolve against."""
    monkeypatch.delenv("JREMOTE_INSTANCE_ROOT", raising=False)
    monkeypatch.setenv("JSTACK_ROOT", str(tmp_path / "jstack-root"))
    assert hostenv.instance_root() == tmp_path / "jstack-root" / "Agents"


def test_instance_root_override_beats_jstack_root(monkeypatch, tmp_path):
    """An explicit `JREMOTE_INSTANCE_ROOT` wins — it is what the plist pins."""
    monkeypatch.setenv("JSTACK_ROOT", str(tmp_path / "jstack-root"))
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(tmp_path / "explicit"))
    assert hostenv.instance_root() == tmp_path / "explicit"


def test_instance_root_never_falls_back_to_a_bare_home(monkeypatch, tmp_path):
    """The blank-thread bug: with no override and no `$JSTACK_ROOT`, the last
    resort must be `$HOME/Agents` — even absent — never the home directory
    itself. A missing agents tree is zero agents; a bare `$HOME` made every
    folder in it an agent, and `welcome` opened its first session in one."""
    monkeypatch.delenv("JREMOTE_INSTANCE_ROOT", raising=False)
    monkeypatch.delenv("JSTACK_ROOT", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / "Desktop").mkdir(parents=True)
    (fake_home / "actions-runner").mkdir()
    monkeypatch.setattr(hostenv, "HOME", fake_home)
    root = hostenv.instance_root()
    assert root == fake_home / "Agents"
    assert not root.exists()
    # And the roster read off that absent tree is empty, not the home folders.
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(root))
    monkeypatch.setenv("JREMOTE_HOST_PROFILE", "default")
    hostenv.reset_profile()
    assert hostenv.active_agents() == {}


# ── the default profile: the filesystem is the roster ──

def test_roster_is_the_directories(instance):
    roster = hostenv.active_agents()
    assert set(roster) == {"iris", "nova", "web", "web-2"}
    assert roster["iris"]["name"] == "Iris"
    assert roster["iris"]["workspace"] == str(instance / "Iris")


def test_reserved_and_hidden_dirs_are_not_agents(instance):
    """`pad` is a seat's scratchpad and `.git` is a repo — neither is an agent."""
    assert "pad" not in hostenv.active_agents()
    assert ".git" not in hostenv.active_agents()


def test_workspace_resolves_base_and_submode(instance):
    assert hostenv.workspace("iris") == instance / "Iris"
    assert hostenv.workspace("iris-pm") == instance / "Iris" / "pm"


def test_nested_submode_walks_hyphens_into_dirs(instance):
    """`chat-reminder` is one id and two directories."""
    assert hostenv.split_id("iris-chat-reminder") == ("iris", "chat/reminder")
    assert hostenv.workspace("iris-chat-reminder") == (
        instance / "Iris" / "chat" / "reminder")


def test_unknown_base_raises(instance):
    with pytest.raises(KeyError):
        hostenv.workspace("nobody")


def test_submode_dirs_excludes_reserved(instance):
    assert hostenv.submode_dirs("iris") == ["chat", "pm"]


def test_umbrella_dir_name_is_the_real_case(instance):
    """The id is lowercased; the directory is not."""
    assert hostenv.umbrella_dir_name("iris") == "Iris"
    assert hostenv.umbrella_dir_name("nobody") is None


# ── project dirs: reverse Claude's own encoding, not a hardcoded path ──

def test_project_dir_decodes_against_the_instance_root(instance):
    encoded = str(instance).replace("/", "-") + "-Iris-chat"
    assert hostenv.project_dir_to_agent(encoded) == ("iris", "chat")


def test_project_dir_base_only_reads_as_default_mode(instance):
    encoded = str(instance).replace("/", "-") + "-Nova"
    assert hostenv.project_dir_to_agent(encoded) == ("nova", "default")


def test_project_dir_handles_a_name_the_legacy_regex_cannot(instance):
    """`web-2` has a hyphen and a digit.

    The legacy decoder matches `[A-Z][a-z]+` after a hardcoded
    `-Users-nova-Agents-` prefix, so it cannot express this name or any
    root but this Mac's. Resolving the base against directories that exist
    can.
    """
    encoded = str(instance).replace("/", "-") + "-web-2-chat"
    assert hostenv.project_dir_to_agent(encoded) == ("web-2", "chat")


def test_project_dir_outside_the_root_is_not_ours(instance):
    """The root is the anchor, not a hint.

    Another machine's tree can hold an agent with the same name; claiming it
    would attribute a foreign session to a local agent. Nothing but a dir that
    actually descends from this instance root resolves.
    """
    assert hostenv.project_dir_to_agent("-Users-someone-else-Projects-App") is None
    assert hostenv.project_dir_to_agent("-Users-someone-Agents-Iris-chat") is None
    assert hostenv.project_dir_to_agent(str(instance).replace("/", "-")) is None
    # An absolute path whose first component happens to be an agent name.
    assert hostenv.project_dir_to_agent("-Iris-chat") is None


def test_a_sibling_root_cannot_claim_our_agents(instance):
    """`~/Agents` must not swallow `~/AgentsIris`.

    The encoded root has to match at a path boundary. Without that, the
    sibling's dir strips to `Iris-chat` and files a foreign machine's session
    under our Iris.
    """
    sibling = str(instance.parent / "AgentsIris").replace("/", "-")
    assert hostenv.project_dir_to_agent(sibling + "-chat") is None


def test_project_dir_for_an_unknown_agent_is_none(instance):
    encoded = str(instance).replace("/", "-") + "-Nobody-chat"
    assert hostenv.project_dir_to_agent(encoded) is None


# ── the two profiles agree about the same machine ──

def _lib_agents():
    try:
        from lib import agents
        return agents
    except Exception:
        return None


@pytest.mark.skipif(_lib_agents() is None, reason="no embedding tree on this machine")
def test_both_profiles_read_this_tree_the_same_way(monkeypatch):
    """The fallback is not a different semantics — it is the same one, derived.

    `~/Agents` is a tree both profiles can read: the registry knows it from
    `agents.json`, the default profile from the directories. Everything below
    is a pure filesystem fact, so the two must not disagree — a divergence here
    means a standalone host would file sessions under different agent ids than
    this Mac does for the identical layout, and every board row, workspace path
    and deep link would drift with it.

    The roster's `active` flag is deliberately not compared: activation exists
    only in the registry, and a directory cannot know it.
    """
    jj = _lib_agents()
    root = Path.home() / "Agents"
    default = hostenv.DefaultProfile(root)

    assert set(default.active_agents()) == set(jj.active_agents())

    for base in sorted(jj.active_agents()):
        assert default.umbrella_dir_name(base) == jj.umbrella_dir_name(base), base
        assert default.submode_dirs(base) == jj.submode_dirs(base), base
        encoded = str(root).replace("/", "-") + "-" + jj.umbrella_dir_name(base)
        assert default.project_dir_to_agent(encoded) == jj.project_dir_to_agent(encoded)
        for mode in jj.submode_dirs(base):
            agent_id = f"{base}-{mode}"
            assert default.split_id(agent_id) == jj.split_id(agent_id), agent_id
            assert default.workspace(agent_id) == jj.workspace(agent_id), agent_id
            enc = encoded + "-" + mode
            assert (default.project_dir_to_agent(enc)
                    == jj.project_dir_to_agent(enc)), enc


# ── the seam itself ──

def test_no_module_in_the_package_imports_lib_agents_directly():
    """The guard that keeps the host portable.

    One `from lib.agents import …` slipped back into any module here and the
    package is silently embedding-tree-only again — every endpoint still answers on this
    Mac, so nothing fails until a second instance exists. Docstrings may name
    it; code may not.
    """
    pkg = Path(hostenv.__file__).parent
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        if path.name == "hostenv.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "lib.agents" in code and "import" in code:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "import lib.agents through hostenv:\n" + "\n".join(offenders)


# ── minting an identity into a dir no host serves (#45) ──

def _declare(marker: Path, state: Path) -> None:
    """Write an embed marker naming `state` — an embedded host on this
    machine saying, in writing, where it lives."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(
        {"server": "the dashboard", "port": 9090, "profile": "jj",
         "state_dir": str(state), "token_path": str(state / "api-token")}))


def test_host_id_refuses_to_mint_beside_a_host_that_declared_elsewhere(
        monkeypatch, tmp_path):
    """The whole of #45 in one assertion.

    Something on the machine resolved the default state dir — a hook with a
    hardcoded path, a test suite with no profile importable — and minted a
    second `host-id` for one Mac. The app routes on that id, so it reads a
    second host at the same address: every session on its board real, none of
    them the ones asked for.

    `JREMOTE_STATE_DIR` is deliberately absent: the stray got here by
    *resolving*, not by being told. A process that set the variable itself is
    the next test.
    """
    stray, served = tmp_path / "stray", tmp_path / "served"
    _declare(tmp_path / "embedded.json", served)
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(tmp_path / "embedded.json"))
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    monkeypatch.delenv("JREMOTE_HOST_ID", raising=False)
    monkeypatch.setattr(hostenv, "state_dir", lambda: stray)

    with pytest.raises(hostenv.SecondIdentity) as caught:
        hostenv.host_id()
    assert str(served) in str(caught.value), "the refusal must name the real dir"
    assert not (stray / "host-id").exists(), "it minted anyway"
    assert not stray.exists(), "a refused call left the directory behind"


def test_an_explicit_state_dir_is_consent_to_be_a_second_host(
        monkeypatch, tmp_path):
    """`JREMOTE_STATE_DIR` is the remedy the refusal's own message prescribes,
    and `state_dir()` documents it as how a second host on this same Mac — a
    test, a second instance — gets its own state. A process minting into the
    very dir it explicitly named is not a stray, even beside an embedded host
    that declared elsewhere; refusing it would make the documented second host
    unbootable on exactly the machines that have a first one."""
    second, served = tmp_path / "second", tmp_path / "served"
    _declare(tmp_path / "embedded.json", served)
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(tmp_path / "embedded.json"))
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(second))
    monkeypatch.delenv("JREMOTE_HOST_ID", raising=False)

    minted = hostenv.host_id()
    assert minted and hostenv.host_id() == minted, "the id must be stable"


def test_host_id_still_answers_from_the_dir_the_marker_names(
        monkeypatch, tmp_path):
    """Serving is what the marker asserts, and a host serving here mints here.
    The refusal is about the dir being *wrong*, never about a marker existing."""
    served = tmp_path / "served"
    _declare(tmp_path / "embedded.json", served)
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(tmp_path / "embedded.json"))
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(served))
    monkeypatch.delenv("JREMOTE_HOST_ID", raising=False)

    minted = hostenv.host_id()
    assert minted and hostenv.host_id() == minted, "the id must be stable"


def test_an_id_already_on_disk_is_read_back_even_from_a_stray_dir(
        monkeypatch, tmp_path):
    """Reading is not claiming. A dir that already holds an id answers for
    whoever put it there, and refusing that would break every reader of a
    second host's dir to prevent a write that is not happening."""
    stray, served = tmp_path / "stray", tmp_path / "served"
    stray.mkdir()
    (stray / "host-id").write_text("already-here")
    _declare(tmp_path / "embedded.json", served)
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(tmp_path / "embedded.json"))
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(stray))
    monkeypatch.delenv("JREMOTE_HOST_ID", raising=False)

    assert hostenv.host_id() == "already-here"


def test_no_marker_is_no_opinion_and_a_fresh_host_mints(monkeypatch, tmp_path):
    """A machine that never embedded a host has nothing to disagree with, and
    a first install has to mint or it cannot come up at all. Refusing on
    absence would protect a new host from a second one it does not have."""
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(tmp_path / "absent.json"))
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path / "fresh"))
    monkeypatch.delenv("JREMOTE_HOST_ID", raising=False)

    assert hostenv.host_id(), "a host with no rival could not name itself"
