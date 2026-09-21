"""Where this host says it can be reached.

The failure these exist for is silent: a pairing dialog that shows an address
which cannot work from the machine being paired. Every test below names the
wrong address it keeps off that screen.
"""

from jstack_host import addresses


def test_loopback_never_appears():
    """The one address that is always wrong for a second machine, and always
    right for the machine generating the list — so it is the one that gets
    handed out by accident."""
    out = addresses.classify(["127.0.0.1", "192.168.0.106"], "mac", 9090)
    assert [a["host"] for a in out] == ["192.168.0.106", "mac.local"]


def test_lan_comes_first_and_mesh_last():
    """Order is advice, and the reader is a device that is still pairing —
    which by definition is not on the mesh yet. The mesh address is the one
    it can never reach right now, so it reads last, not first.

    It also reads a second time, under `lan`: a shipped client keeps only
    lan and local, and that duplicate is the whole point of the kind."""
    out = addresses.classify(["192.168.0.106", "10.66.0.1"], "mac", 9090)
    assert [a["kind"] for a in out] == ["lan", "lan", "local", "mesh"]
    assert [a["host"] for a in out] == ["192.168.0.106", "10.66.0.1",
                                        "mac.local", "10.66.0.1"]


def test_public_and_link_local_are_dropped():
    """A public IP on an interface here is this machine's view of itself, not
    a route anyone else has; 169.254 is what an interface holds when DHCP has
    already failed."""
    out = addresses.classify(["97.120.113.78", "169.254.3.9", "10.66.0.1"],
                             "mac", 9090)
    assert [a["kind"] for a in out] == ["lan", "local", "mesh"]
    assert {a["host"] for a in out} == {"10.66.0.1", "mac.local"}


def test_port_rides_into_every_url():
    """A host moved off 9090 would otherwise publish a list wrong in the one
    detail nobody re-reads."""
    out = addresses.classify(["192.168.0.106"], "mac", 8443)
    assert all(a["url"].endswith(":8443") for a in out)
    assert out[0]["url"] == "http://192.168.0.106:8443"


def test_hostname_becomes_a_bonjour_name():
    """`socket.gethostname()` answers all three of `mac`, `mac.local` and
    `mac.lan` depending on the network; the app needs one resolvable form."""
    for given in ("mac", "mac.local", "mac.lan", "mac."):
        out = addresses.classify([], given, 9090)
        assert [a["host"] for a in out] == ["mac.local"], given


def test_localhost_hostname_is_not_an_address():
    """A machine that answers `localhost` for its own name would otherwise
    publish loopback through the back door the first test closes."""
    assert addresses.classify([], "localhost", 9090) == []
    assert addresses.classify([], "", 9090) == []


def test_nothing_readable_is_an_empty_list_not_a_guess():
    """The screen above says 'ask the Mac' when this is empty. Inventing an
    address would be worse than admitting there isn't one."""
    assert addresses.classify([], "", 9090) == []


def test_a_vm_bridge_is_not_the_lan():
    """The address this module shipped wrong on the machine that owns the
    mesh. A Mac running VMs holds 192.168.64.1 on bridge100 — private, so it
    passed every filter here, and reachable by nothing but the guests on that
    bridge. It was published to a phone under 'works while both machines are
    on this network'."""
    out = addresses.classify(
        ["192.168.0.106", "192.168.64.1"], "mac", 9090,
        {"192.168.0.106": "en1", "192.168.64.1": "bridge100"})
    assert [a["host"] for a in out] == ["192.168.0.106", "mac.local"]


def test_the_bridge_number_is_not_what_is_excluded():
    """The kernel numbers these, so a check that knew `bridge100` would pass
    every test here and publish `bridge101` to the next device."""
    for iface in ("bridge0", "bridge100", "bridge101", "vmenet0", "vnic1",
                  "awdl0", "llw0"):
        out = addresses.classify(["192.168.64.1"], "", 9090,
                                 {"192.168.64.1": iface})
        assert out == [], iface


def test_the_mesh_survives_the_interface_filter():
    """The mesh lives on a utun, and it is excluded from `lan` by subnet then
    re-added as its own kind. An interface filter that reached it would delete
    the mesh address rather than reclassify it — so utun is not on the list,
    and this is the test that says so."""
    out = addresses.classify(
        ["192.168.0.106", "10.66.0.1"], "mac", 9090,
        {"192.168.0.106": "en1", "10.66.0.1": "utun0"})
    assert [a["kind"] for a in out] == ["lan", "lan", "local", "mesh"]
    assert out[-1]["host"] == "10.66.0.1"


def test_no_interface_map_keeps_every_address():
    """Callers that cannot name the interfaces — and a parse that failed —
    must narrow what can be excluded, never empty the list. The evidence is
    for dropping an entry, not a precondition for keeping one."""
    assert addresses.classify(["192.168.64.1"], "", 9090) != []
    assert addresses.classify(["192.168.64.1"], "", 9090, {}) != []


def test_inet_addrs_stays_unfiltered_for_mode():
    """`mode` asks this whether the machine holds the mesh gateway, and that
    question is about every interface. Narrowing it here to serve the pairing
    screen would demote this hub to 'managed' again."""
    held = addresses._inet_ifaces()
    assert set(addresses._inet_addrs()) == set(held)


def test_interfaces_are_read_off_the_real_machine():
    """The parse is the half a pure classifier cannot pin: `_inet_ifaces`
    must attribute loopback to `lo0` on any machine that runs this."""
    held = addresses._inet_ifaces()
    assert held.get("127.0.0.1") == "lo0"


def test_the_domain_leads_the_names_and_keeps_the_lan_number_first():
    """The order the app probes: a number in front of the Mac, then names for
    a device that has moved. The domain is a name, so it goes with the names —
    ahead of Bonjour, which resolves on fewer networks than DNS does."""
    out = addresses.classify(["192.168.0.10"], "mac", 9090,
                             domain="hub.jstack.live")
    assert [a["host"] for a in out] == ["192.168.0.10", "hub.jstack.live",
                                        "mac.local"]


def test_the_domain_ships_as_local_so_installed_apps_accept_it():
    """`HostStore.directURLs` filters the published list to `lan` and `local`.
    A kind of its own would be dropped by every app already on a phone, and
    the whole point is that devices paired months ago pick this up without a
    new build."""
    out = addresses.classify([], "", 9090, domain="hub.jstack.live")
    assert [a["kind"] for a in out] == ["local"]


def test_no_domain_configured_publishes_no_domain():
    """The package ships no name. A host whose owner never set one must not
    advertise a guess — an address that resolves somewhere else is worse than
    one absent."""
    out = addresses.classify(["192.168.0.10"], "mac", 9090)
    assert [a["host"] for a in out] == ["192.168.0.10", "mac.local"]


def test_a_domain_matching_the_bonjour_name_is_not_published_twice():
    """Two identical entries cost the app a duplicate probe on the path where
    probes are slowest — the one with no route at all."""
    out = addresses.classify([], "mac", 9090, domain="mac.local")
    assert [a["host"] for a in out] == ["mac.local"]


def test_the_domain_is_normalised_like_every_other_name():
    """A trailing dot and an upper-case letter are both things a hand-written
    config file carries, and neither may become a second address."""
    for given in ("HUB.jstack.live", "hub.jstack.live.", " hub.jstack.live "):
        out = addresses.classify([], "", 9090, domain=given)
        assert [a["host"] for a in out] == ["hub.jstack.live"], given


def test_reachable_runs_on_the_real_machine(monkeypatch):
    """The entry point every caller actually uses, called for real.

    Every other test here drives the pure classifier, which is why a missing
    `import os` in `hub_domain` survived the whole suite and only appeared when
    the host was asked what it publishes. `reachable` is the seam between the
    pure half and the machine, so it gets exercised as itself.
    """
    monkeypatch.setenv("JSTACK_HUB_DOMAIN", "hub.example.test")
    out = addresses.reachable(9090)
    assert out[0]["kind"] in {"lan", "local"}
    assert any(a["host"] == "hub.example.test" and a["kind"] == "local"
               for a in out)


def test_hub_domain_reads_the_environment_before_any_file(monkeypatch):
    """The override exists so a second host on this Mac — a test, a rig — can
    publish its own name without writing over the machine's file."""
    monkeypatch.setenv("JSTACK_HUB_DOMAIN", "  Rig.Example.Test  ")
    assert addresses.hub_domain() == "Rig.Example.Test"


def test_hub_domain_is_empty_when_nothing_configures_it(monkeypatch, tmp_path):
    """No name is a real answer. A host that guessed would publish an address
    resolving to somebody else's machine."""
    monkeypatch.setenv("JSTACK_HUB_DOMAIN", "")
    monkeypatch.setenv("JREMOTE_STATE_DIR", str(tmp_path))
    assert addresses.hub_domain() == ""


def test_every_entry_carries_the_shape_the_app_draws():
    out = addresses.classify(["10.66.0.1", "192.168.0.106"], "mac", 9090)
    assert out
    for a in out:
        assert set(a) == {"kind", "host", "url", "note"}
        assert a["url"].startswith("http://")
        assert a["note"]
