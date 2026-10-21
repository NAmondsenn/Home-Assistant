"""
Network Test
Checks the assistant can tell when there's no way onto the internet, from routing
tables like the Pi's own, with the network cable in and out.
"""

from voice_assistant import network

HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
LOCAL_ONLY = "eth0\t00000A0A\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
DEFAULT_ROUTE = "eth0\t00000000\t01000A0A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"

# Tailscale's routes, and the "unreachable" default the kernel keeps on loopback -
# neither is a way onto the internet.
IPV6_WITHOUT_A_WAY_OUT = (
    "fd7a115ca1e000000000000000000053 80 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 00000400 00000005 00000000 00000001 tailscale0\n"
    "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 ffffffff 00000001 00000000 00200200       lo\n")


def test_a_default_route_means_online():
    assert network.has_default_route(HEADER + DEFAULT_ROUTE + LOCAL_ONLY, IPV6_WITHOUT_A_WAY_OUT,
                                     interface_up=lambda name: True)


def test_with_the_cable_out_there_is_no_way_online():
    # Only the local network, Tailscale's own routes and loopback's placeholder.
    assert not network.has_default_route(HEADER + LOCAL_ONLY, IPV6_WITHOUT_A_WAY_OUT,
                                         interface_up=lambda name: True)


def test_a_route_on_an_unplugged_interface_doesnt_count():
    assert not network.has_default_route(HEADER + DEFAULT_ROUTE, interface_up=lambda name: name != "eth0")


def test_without_a_routing_table_to_read_requests_are_tried(monkeypatch, tmp_path):
    # Anywhere but Linux, as always.
    monkeypatch.setattr(network, "ROUTE_FILE", str(tmp_path / "missing"))
    assert network.online()
