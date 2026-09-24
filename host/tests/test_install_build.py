"""Installing means building a commit on a ref, not downloading a release.

Two halves. `build_source.bootstrap`/`seed` are the installer's build and its
first offer — the same manifest and the same feed writer a hub's own
`updates build` uses, fed from what an installing Mac actually has. The rest
reads `install.sh` and `app/install.sh` as text, the way the menubar script's
tests do: those two are the only door onto a fresh machine and nothing else
executes them under pytest.
"""
import json
import plistlib
import re
import tarfile
from pathlib import Path

import pytest

from jstack_host import build_hub, build_source, release_manifest as releases

REPO = Path(__file__).resolve().parents[2]
INSTALL = (REPO / "install.sh").read_text()
APP_INSTALL = (REPO / "app/install.sh").read_text()
#: The same script with its commentary taken out. What a shell script no
#: longer does cannot be asserted against a file that explains why it stopped
#: doing it — every "this used to" note reads as the thing itself.
CODE = "\n".join(line for line in INSTALL.splitlines() if not line.lstrip().startswith("#"))

HEAD = "f" * 40
CLIENT = "b" * 40


def bundle(path: Path, info: dict) -> Path:
    (path / "Contents").mkdir(parents=True)
    with (path / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump(info, stream)
    return path


@pytest.fixture
def sealed():
    """The identity `build_hub` sealed into the bundle it was asked to build.

    The bundle is archived and deleted before `bootstrap` returns, so this is
    the only place a test can read what the compiler was handed.
    """
    return {}


@pytest.fixture
def installing(tmp_path, monkeypatch, sealed):
    """A Mac part way through an install: a checkout on a ref, a client in
    /Applications, and no hub yet. Git, the Hub compiler and ditto are stubbed;
    the identity, the manifest, the signature and the tarball are the real code.
    """
    from jstack_host import app_services, build_hub, update_macos
    checkout, output, keys = tmp_path / "jStack", tmp_path / "out", tmp_path / "keys"
    client = bundle(tmp_path / "Applications/jRemote.app", {
        "CFBundleIdentifier": "live.jstack.client", "CFBundleVersion": "109",
        "LSMinimumSystemVersion": "26.0", "JStackSourceCommit": CLIENT})
    checkout.mkdir()
    calls = []

    def stack_tree(target: Path):
        (target / "plugins/jstack/.claude-plugin").mkdir(parents=True)
        (target / "plugins/jstack/.claude-plugin/plugin.json").write_text('{"version": "9.9.9"}')
        (target / "host/jstack_host").mkdir(parents=True)
        (target / "host/jstack_host/__init__.py").write_text("# host\n")

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["git", "-C"] and "worktree" in argv and "add" in argv:
            stack_tree(Path(argv[-2]))
            return ""
        if "rev-parse" in argv:
            return HEAD + "\n"
        if "rev-list" in argv:
            return "101\n"
        if argv[0] == "/usr/bin/ditto":
            import zipfile
            with zipfile.ZipFile(argv[-1], "w") as archive:
                archive.writestr(Path(argv[-2]).name + "/Contents/Info.plist", "x")
            return ""
        return ""

    def compile_hub(stack, output, version, signing, **kwargs):
        app = bundle(Path(output) / "jStack Hub.app", {
            "CFBundleIdentifier": "live.jstack.hub", "CFBundleVersion": "20260923",
            "LSMinimumSystemVersion": "13.0"})
        (app / "built").write_text(kwargs["trust_key"])
        # The real `release_identity`, on whatever `bootstrap` handed it: this
        # file is the only thing `install_signed.provision` has to read a ref
        # or an origin out of, so a stub that wrote its own would be asserting
        # the fixture. The compiler itself is what is out of reach here.
        packages = app / "Contents/Resources/packages"
        packages.mkdir(parents=True)
        sealed.update(build_hub.release_identity(
            HEAD, version, release_id=kwargs["release_id"],
            github_repo=kwargs["github_repo"], date=kwargs["date"],
            channel=kwargs.get("channel"), origin=kwargs.get("origin")))
        (packages / "release-identity.json").write_text(json.dumps(sealed))
        return app

    monkeypatch.setattr(update_macos, "command", command)
    monkeypatch.setattr(build_hub, "build", compile_hub)
    monkeypatch.setattr(app_services, "verify", lambda *a, **k: None)
    monkeypatch.setattr(build_source.subprocess, "run", lambda *a, **k: None)
    return checkout, output, keys, client, calls


def built(installing, **overrides):
    checkout, output, keys, client, _ = installing
    return build_source.bootstrap(checkout, output, keys, repo="example/stack",
                                  ref=overrides.pop("ref", "dev"),
                                  client=overrides.pop("client", client),
                                  machine="this-mac", **overrides)


# ── What the installer builds ───────────────────────────────────────────────

def test_the_installer_signs_its_build_with_the_key_this_machine_minted(installing):
    _, output, keys, _, _ = installing
    answer = built(installing)
    _, public = build_source.build_key(keys)
    manifest = releases.verify(json.loads((output / "manifest.json").read_text()), public)
    assert answer["public_key"] == public and answer["offer"] is True
    assert manifest["origin"] == {"kind": releases.SOURCE_BUILD, "machine": "this-mac"}
    assert manifest["channel"] == {"github_repo": "example/stack", "name": "dev"}
    assert manifest["sources"] == {"stack": HEAD, "client": CLIENT}
    assert manifest["sequence"] == 101 and not manifest["receipts"]


def test_the_manifest_names_the_client_this_mac_installed_and_its_commit(installing):
    """jRemote is not built here and its release is downloaded once, by the
    installer that verified it — so the bytes this hub can honestly offer are
    the ones already on the disk."""
    _, output, _, client, _ = installing
    built(installing)
    manifest = json.loads((output / "manifest.json").read_text())["manifest"]
    item = manifest["components"]["client"]
    assert item["file"] == "jRemote.zip" and item["version"] == "109"
    releases.check_artifact(output / item["file"], item)
    assert manifest["sources"]["client"] == CLIENT
    assert manifest["mobile"]["source"] == CLIENT


def test_a_client_that_does_not_record_its_commit_is_refused(installing, tmp_path):
    """Without it the manifest cannot name a client revision, and no later
    build can carry one forward."""
    anonymous = bundle(tmp_path / "Anon/jRemote.app", {
        "CFBundleIdentifier": "x", "CFBundleVersion": "1", "LSMinimumSystemVersion": "26.0"})
    with pytest.raises(releases.ReleaseError, match="commit it was built from"):
        built(installing, client=anonymous)


def test_the_release_runs_on_what_both_of_its_bundles_run_on(installing):
    """A publication states compatibility by hand. An installer reads it back
    off the two bundles, and the higher minimum is the release's."""
    _, output, _, _, _ = installing
    built(installing)
    manifest = json.loads((output / "manifest.json").read_text())["manifest"]
    assert manifest["compatibility"]["minimum_os"] == "26.0"
    assert manifest["compatibility"]["protocol"] == 1
    assert manifest["compatibility"]["rollback"] is True


def test_the_tarball_carries_the_identity_and_the_key_stage_reads_back(installing):
    _, output, keys, _, _ = installing
    release = built(installing)["release"]
    with tarfile.open(output / "stack.tar.gz") as archive:
        identity = json.loads(archive.extractfile("host/release-identity.json").read())
        trust = json.loads(archive.extractfile("host/jstack_host/release-trust.json").read())
    assert identity["release"] == release and identity["sha"] == HEAD
    assert identity["channel"] == "dev" and identity["package_sha256"]
    assert trust["public_key"] == build_source.build_key(keys)[1]


def test_the_built_hub_records_the_ref_it_was_built_from(installing, sealed):
    """#148. `install_signed.provision` reads the hub's channel out of this
    file and nothing else knows it — a fresh install refuses to find anything
    in the state dir, and the caller is not asked. While the bundle recorded
    no ref, every branch install was provisioned to follow stable."""
    built(installing, ref="feature/x")
    assert sealed["channel"] == "feature/x"
    assert build_source.channel_ref(sealed) == "feature/x"


def test_a_no_app_install_records_its_ref_too(installing, sealed):
    """The half the workaround in `seed()` could never reach: without a client
    there is no manifest and no first offer, so nothing ran after the install
    to correct the channel it had been provisioned with."""
    assert built(installing, client=None, ref="feature/x")["offer"] is False
    assert sealed["channel"] == "feature/x"


def test_the_built_hub_records_that_this_machine_built_it(installing, sealed):
    """#146. The marker the sealed installer's bundle gate reads, in the same
    spelling the signed manifest carries — one answer to what a source build
    is, sealed into the bundle by the signature over it."""
    _, output, _, _, _ = installing
    built(installing)
    manifest = json.loads((output / "manifest.json").read_text())["manifest"]
    assert sealed["origin"] == manifest["origin"] == {
        "kind": releases.SOURCE_BUILD, "machine": "this-mac"}


def test_a_build_whose_seal_does_not_hold_stops_before_applications(installing, monkeypatch):
    """`install_signed.identity` asks the same question with the bundle
    already in /Applications, nine frames down. An ad-hoc signature is enough
    for a Hub this machine built; an absent or broken one is enough for
    nobody, and that is what is still worth catching here."""
    from jstack_host import app_services

    def refuse(*args, **kwargs):
        raise releases.ReleaseError("code object is not signed at all")

    monkeypatch.setattr(app_services, "verify", refuse)
    with pytest.raises(releases.ReleaseError, match="signature does not hold"):
        built(installing)


def test_without_a_client_the_hub_is_still_built_and_the_feed_stays_empty(installing):
    """`--no-app`. A manifest needs all three components, so a feed carrying a
    component this Mac does not have is the one thing that must not happen."""
    _, output, _, _, _ = installing
    answer = built(installing, client=None)
    assert answer["offer"] is False and answer["release"]
    assert not (output / "manifest.json").exists()
    assert (output / "menubar-notarized.zip").is_file()


# ── The first offer ─────────────────────────────────────────────────────────

def seeded(installing, tmp_path, **overrides):
    _, output, keys, _, _ = installing
    built(installing, **{k: v for k, v in overrides.items() if k == "ref"})
    root, feed = tmp_path / "updates", tmp_path / "state/fleet"
    root.mkdir(parents=True)
    (root / "build-key").write_bytes((keys / "build-key").read_bytes())
    (root / "build-key").chmod(0o600)
    config = {"feed_dir": str(feed), "public_key": build_source.build_key(keys)[1]}
    config.update(overrides.get("config", {}))
    (root / "config.json").write_text(json.dumps(config))
    return root, feed, output


def test_the_install_lands_what_it_built_as_the_hubs_first_offer(installing, tmp_path):
    """`inherited()` refuses on an empty feed, so a hub that never lands its
    first release can never build a second one — and a hub with no feed serves
    no leaf."""
    root, feed, output = seeded(installing, tmp_path)
    answer = build_source.seed(root, output)
    public = build_source.build_key(root)[1]
    manifest = releases.verify(json.loads((feed / "latest.json").read_text()), public)
    assert manifest["release"] == answer["release"]
    for item in manifest["components"].values():
        releases.check_artifact(feed / manifest["release"] / item["file"], item)
    assert releases.verify(json.loads((feed / manifest["release"] / "manifest.json").read_text()),
                           public)["release"] == manifest["release"]
    # `inherited()` is the next build's first act, and it now has an answer.
    assert build_source.inherited({"feed_dir": str(feed), "public_key": public})


def test_the_seed_leaves_the_channel_the_bundle_already_answered_for(installing, tmp_path):
    """The Phase-1b workaround, gone. It wrote the ref into the hub's config
    after provisioning, which covered the installer's own path and nothing
    else: a `--no-app` install never reached it, and neither did a bundle
    reinstalled outside the installer. The bundle carries the ref now."""
    import inspect
    root, _, output = seeded(installing, tmp_path)
    build_source.seed(root, output)
    assert "channel" not in json.loads((root / "config.json").read_text())
    assert "channel" not in inspect.getsource(build_source.seed)


def test_a_hub_that_does_not_trust_the_installers_key_refuses_the_offer(installing, tmp_path):
    root, feed, output = seeded(installing, tmp_path, config={"public_key": "not-this-key"})
    with pytest.raises(releases.ReleaseError, match="does not trust the key"):
        build_source.seed(root, output)
    assert not (feed / "latest.json").exists()


def test_one_writer_lands_both_a_hubs_build_and_an_installers(installing, tmp_path):
    """Two spellings of the feed layout would be two answers to what a locally
    built release is, and only one of them is what `stage()` consumes."""
    import inspect
    source = inspect.getsource(build_source._build)
    assert "land(feed, output, envelope)" in source
    assert "assemble(" in source and "os.rename" not in source


# ── The installer's own text ────────────────────────────────────────────────

def test_install_sh_installs_a_ref_and_downloads_no_release():
    assert "--ref" in CODE and "JSTACK_REF" in CODE
    assert re.search(r"git clone --quiet [^\n]*--branch", CODE)
    assert "merge --ff-only" in CODE
    for gone in ("RELEASE_TAG", "stack-release", "releases/download", "tar xzf"):
        assert gone not in CODE, f"install.sh still downloads a release: {gone}"


def test_install_sh_clones_one_branch_so_no_binary_rides_in_on_another():
    """The app is served off a branch of this repo, and a plain clone would put
    that blob in every user's checkout and keep it there. Every clone here is
    single-branch, so a ref nobody asked for is never fetched."""
    clones = re.findall(r"git clone[^\n]*", CODE)
    assert clones
    for clone in clones:
        assert "--single-branch" in clone, f"clone fetches every branch: {clone}"


def test_install_sh_moves_a_release_snapshot_forward_instead_of_refusing_it():
    """Every Mac installed before builds replaced releases has a $CHECKOUT that
    is not a checkout: the old installer untarred a publisher snapshot there,
    so it carries host/release-identity.json and no .git. Step 2 refused
    exactly that shape, which made the first thing the new installer did on
    every deployed Mac be to die.

    It is moved aside and never deleted — it is the only copy of what the last
    release shipped, and an installer that deletes what it did not write is
    #130."""
    assert "host/release-identity.json" in CODE
    assert ".snapshot-" in CODE
    snapshot = CODE[CODE.index("host/release-identity.json"):]
    snapshot = snapshot[:snapshot.index("elif [ -e ")]
    assert 'mv "$CHECKOUT" "$ASIDE"' in snapshot
    for destructive in ("rm -rf \"$CHECKOUT\"", "rm -r \"$CHECKOUT\""):
        assert destructive not in snapshot, f"the old snapshot is deleted: {destructive}"


def test_install_sh_replaces_a_published_hub_rather_than_naming_a_verb_it_lacks():
    """A Hub that answers is left alone only when it records that this machine
    built it. A published release carries no `origin` marker, follows a release
    line nothing will publish to again, and its CLI has `updates enable` and
    `updates channel` and no `build` — so the note telling its owner to run
    `jstack-host updates build` names a verb that Hub does not have, on the one
    machine shape that cannot get it any other way."""
    assert '"kind"[[:space:]]*:[[:space:]]*"source-build"' in CODE
    guard = CODE[CODE.index("Hub already installed and answering"):]
    guard = guard[:guard.index("jStack Hub app is present but its host")]
    assert "cannot build itself forward" in guard
    assert 'rm -rf "/Applications/jStack Hub.app"' in guard


def test_the_runtime_gate_does_not_demand_a_team_a_self_built_hub_cannot_have():
    """The Hub's C runtime validates the seal before importing any module, and
    it asked for the publisher's Developer ID team unconditionally. A Hub
    compiled on the Mac that runs it is signed ad-hoc — that Mac holds no such
    identity — so every source build built cleanly and then failed its own
    sealed installer at the last step.

    The requirement is chosen at compile time, not read from Resources at
    launch: the seal covers this binary, so a bundle cannot relax its own rule
    without invalidating the signature that carries it. A marker read at launch
    could be written by whoever assembled the bundle, which is the one party
    the check answers for.
    """
    runtime = (REPO / "host/macos/Runtime.c").read_text()
    assert "#ifdef JSTACK_SOURCE_BUILD" in runtime
    assert "CFSTR(JSTACK_REQUIREMENT)" in runtime
    # The team pin is still what a published Hub demands.
    strict = runtime[runtime.index("#else"):runtime.index("#endif")]
    assert "MZ95H77RQQ" in strict and "anchor apple generic" in strict
    relaxed = runtime[runtime.index("#ifdef JSTACK_SOURCE_BUILD"):runtime.index("#else")]
    assert "MZ95H77RQQ" not in relaxed
    assert "live.jstack.hub" in relaxed


def test_a_source_build_compiles_the_runtime_that_will_accept_it():
    """Both the service runtime and the interpreter shim are built from the
    same file, so both carry the requirement and both need the define."""
    import inspect
    source = inspect.getsource(build_hub._build)
    assert 'origin = ["-DJSTACK_SOURCE_BUILD"] if isinstance(identity.get("origin"), dict) else []' in source
    compiles = [line for line in source.splitlines() if "Runtime.c" in line]
    assert len(compiles) == 2
    assert source.count("*origin") == 2


def test_install_sh_never_deletes_uncommitted_work_in_the_checkout():
    """Issue #130: it wrote a diff to the home root, then ran `checkout -- .`
    and `clean -fdq` over the tree, and a finished, tested fix was gone."""
    for destructive in ("clean -fdq", "checkout -- .", "jstack-local-changes",
                        "-source-$(date"):
        assert destructive not in CODE, f"install.sh still discards work: {destructive}"
    assert "has uncommitted work (above)" in CODE


def test_install_sh_puts_this_checkouts_adapters_on_path_not_merely_some():
    """It asked whether `log_event` resolved at all. On a Mac moved off a
    release install the answer was yes and wrong: the profile pointed into a
    release stage under the state dir, which the host step moves aside minutes
    later. The install finished green with no adapter reachable at all."""
    assert 'command -v log_event 2>/dev/null)" = "$BIN/log_event"' in CODE
    # And the stale line is taken out, or the dead stage keeps winning.
    assert "'  # jstack$'" in CODE
    assert "dropped $stale stale jstack PATH line(s)" in CODE


def test_install_sh_points_the_marketplace_at_this_checkout_not_merely_at_a_name():
    """The registration is what every rule, command and hook resolves through.
    A Mac moved off a release install carries one naming that release's stage,
    and the host step moves the stage aside minutes later — so `grep -q jStack`
    answered yes and left the whole plugin resolving from a deleted directory."""
    assert '"$REGISTERED" = "$CHECKOUT"' in CODE
    # Read from the plugin's own store, which is what `marketplace add` writes.
    assert "extraKnownMarketplaces" in CODE
    assert "marketplace jStack pointed at $REGISTERED" in CODE


def test_install_sh_repoints_a_rule_link_of_ours_that_points_somewhere_else():
    """`[ -L "$target" ]` was true of a link into a stage that no longer
    exists, and "already present" kept it that way for the life of the Mac."""
    assert 'ours "$at"' in CODE
    assert "*/rules-stage/*|*/commands-stage/*" in CODE
    assert "re-pointed" in CODE


def test_install_sh_drops_a_dead_link_of_ours_that_no_pass_would_revisit():
    """A name this checkout no longer ships is never walked by the linking
    loop, so it needs its own sweep — and only ours, never the user's."""
    assert "dead link(s) dropped" in CODE
    assert 'ours "$(readlink "$target")" || continue' in CODE


def test_install_sh_settles_build_inputs_before_it_takes_a_working_hub_apart():
    """It asked for the interpreter where the build runs, which is after the
    published Hub has been unregistered, deleted and its state moved aside. A
    Mac with no CPython 3.12 framework was left with no Hub at all, under a
    correctly worded remedy."""
    call = CODE.index("    ensure_build_inputs")
    # The teardown on the INSTALL path, not the one --uninstall does.
    replace = CODE.index("cannot build itself forward")
    teardown = CODE.index('rm -rf "/Applications/jStack Hub.app"', replace)
    assert call < teardown, "build inputs are settled after the Hub is deleted"
    # And after the checkout exists, since the venv it may find lives there.
    assert CODE.index('step "jStack source at $CHECKOUT ($REF)"') < call


def test_install_sh_verifies_the_python_package_before_it_runs_it_as_root():
    """A root package fetched over the network and run unverified is a worse
    problem than the missing interpreter it would fix."""
    assert "pkgutil --check-signature" in CODE
    assert 'Developer ID Installer: Python Software Foundation ($PSF_TEAM)' in CODE
    assert "Notarization: trusted by the Apple notary service" in CODE
    assert 'PSF_TEAM="BMM5U3QVKW"' in CODE
    # Refused on either count, rather than installed with a warning.
    assert CODE.count("refusing it") >= 2


def test_install_sh_does_not_dress_homebrews_tmux_up_as_a_pinned_signature():
    """The other two carry a Developer ID and Apple's notarization. tmux
    publishes source, so it carries neither, and the difference is said."""
    assert "no signature to pin" in INSTALL
    assert "brew install tmux" in CODE


def test_install_sh_names_a_remedy_for_every_build_input_it_requires():
    """A Mac without these cannot install, which is intended — but a traceback
    ten minutes into a build is not the way to say so."""
    for remedy in ("python.org", "xcode-select --install", "brew install tmux"):
        assert remedy in CODE, f"no remedy offered for a missing build input: {remedy}"
    assert "Python.framework/Versions/3.12" in CODE


def test_install_sh_does_not_require_a_publisher_signing_identity():
    """#146: it did, and that made the publisher's Mac the only machine the
    one install path there is could reach. A configuration that is set but
    names no file is still a mistake worth refusing over."""
    assert "JSTACK_SIGNING_CONFIG" in CODE, "the door to a notarized build is gone"
    assert "points at no file" in CODE
    for refusal in ("adopts a notarized bundle from the publisher",
                    "and notary_credentials.\"\n"):
        assert refusal not in CODE, f"install.sh still refuses an unsigned build: {refusal}"


def test_install_sh_lands_its_build_in_the_feed_through_the_one_writer():
    assert "jstack_host.build_source bootstrap" in CODE
    assert "jstack_host.build_source seed" in CODE
    assert "build-key" in CODE, "the hub must keep the key its installer signed with"


def test_jremotes_door_is_untouched():
    """Moving the client to a direct-download host is blocked on a credential
    that has not arrived; until it does, this is how a Mac gets jRemote."""
    assert 'TAG_PREFIX="mac-app-"' in APP_INSTALL
    assert 'TEAM_ID="MZ95H77RQQ"' in APP_INSTALL
    for gate in ("shasum -a 256", "codesign --verify --strict",
                 "source=Notarized Developer ID", "spctl -a -vvv -t exec"):
        assert gate in APP_INSTALL, f"the app installer lost a gate: {gate}"
    hosts = set(re.findall(r"https://([a-z0-9.-]+)/", APP_INSTALL))
    assert hosts <= {"github.com", "api.github.com", "raw.githubusercontent.com"}, hosts


def test_a_final_release_is_still_cuttable():
    """One last release has to be publishable with the machinery the deployed
    fleet is running, or that fleet can never inherit this change."""
    from jstack_host import release_channel
    assert callable(release_channel.publish)
