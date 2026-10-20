"""
Network Module.

Whether the Pi has a way onto the internet at all, found by reading its routing
table - which is instant, and sends nothing anywhere.

Found testing on the Pi with the network cable out: everything which went to
Claude, Spotify or the weather sat waiting out its timeouts first - over half a
minute for one request to Claude, with its retry - so the assistant seemed to have
stopped working, when the timers and alarms it handles itself could have carried
on straight away.
"""

import os
from typing import Callable

ROUTE_FILE = "/proc/net/route"
IPV6_ROUTE_FILE = "/proc/net/ipv6_route"

# Route flags, from the kernel's route.h.
RTF_UP = 0x0001
RTF_REJECT = 0x0200


def _interface_up(interface: str) -> bool:
    """
    Whether a network interface is actually connected - a route can outlive its
    cable being unplugged for a while. Assumed up if it can't be read.
    """
    try:
        with open(f"/sys/class/net/{interface}/operstate") as f:
            return f.read().strip() in ("up", "unknown")
    except OSError:
        return True


def has_default_route(ipv4_routes: str, ipv6_routes: str = "",
                      interface_up: Callable[[str], bool] = _interface_up) -> bool:
    """
    Whether either routing table has a default route - a way off the local network -
    on an interface which is connected. It goes when the cable is unplugged or the
    Wi-Fi drops.

    Args:
        ipv4_routes: The text of /proc/net/route.
        ipv6_routes: The text of /proc/net/ipv6_route.
        interface_up: Checks an interface is connected - only replaced by the tests.
    """
    for line in ipv4_routes.splitlines()[1:]:
        fields = line.split()
        if (len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & RTF_UP
                and interface_up(fields[0])):
            return True

    for line in ipv6_routes.splitlines():
        fields = line.split()
        # Not the "unreachable" placeholder the kernel keeps on loopback.
        if (len(fields) >= 10 and fields[0] == "0" * 32 and fields[1] == "00" and fields[9] != "lo"
                and not int(fields[8], 16) & RTF_REJECT and interface_up(fields[9])):
            return True
    return False


def online() -> bool:
    """
    Whether there's any point trying to reach the internet right now.

    Returns:
        False only when there's definitely no way off the local network. Where the
        routing table can't be read - anywhere but Linux - it's True, and requests
        are tried as they always were.
    """
    try:
        with open(ROUTE_FILE) as f:
            ipv4 = f.read()
    except OSError:
        return True

    ipv6 = ""
    if os.path.exists(IPV6_ROUTE_FILE):
        try:
            with open(IPV6_ROUTE_FILE) as f:
                ipv6 = f.read()
        except OSError:
            pass
    return has_default_route(ipv4, ipv6)
