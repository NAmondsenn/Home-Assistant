"""
Watchdog Module.

Tells systemd the assistant is still working, so one which has hung is restarted
rather than sitting there looking healthy.

The service is Type=notify with a WatchdogSec (see voice-assistant.service), so
systemd restarts it if it hasn't heard "WATCHDOG=1" for that long. A thread of its
own sends that every few seconds - but only while the main loop keeps checking in
with beat(), so a main loop stuck somewhere stops the pings and systemd steps in.
Outside systemd, run by hand, it does nothing.
"""

import logging
import os
import socket
import threading
import time
from typing import Optional

# Logger setup
logger = logging.getLogger(__name__)

# The longest the main loop may go without checking in and still be taken to be
# working: more than the slowest thing it legitimately waits for, a web search which
# is retried once (two lots of llm.search_timeout).
ALLOWANCE = 150.0

_last_beat = time.monotonic()


def beat(at: Optional[float] = None):
    """
    Says the main loop is still going. Cheap enough to call many times a second.

    Args:
        at: When it was last known to be getting on, by time.monotonic(), if not now -
            the brain's, which goes on listening for satellites while one of its
            sessions is stuck in a turn, says when that session last moved.
    """
    global _last_beat
    _last_beat = time.monotonic() if at is None else at


def _notify(message: str) -> bool:
    """
    Sends one message to systemd, as sd_notify does.

    Returns:
        True if it was sent - False outside systemd, or if it couldn't be.
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    # An abstract socket, named with a leading "@" in the environment.
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
        return True
    except OSError as e:
        logger.warning(f"Couldn't tell systemd '{message}': {e}")
        return False


def start():
    """
    Tells systemd the assistant has started, and keeps telling it it's alive for as
    long as the main loop checks in - see beat().
    """
    if not _notify("READY=1"):
        return
    watchdog_usec = int(os.environ.get("WATCHDOG_USEC") or 0)
    if not watchdog_usec:
        return
    beat()
    interval = watchdog_usec / 1_000_000 / 3

    def keep_telling():
        quiet_logged = False
        while True:
            time.sleep(interval)
            quiet_for = time.monotonic() - _last_beat
            if quiet_for < ALLOWANCE:
                _notify("WATCHDOG=1")
                quiet_logged = False
            elif not quiet_logged:
                logger.error(f"The main loop hasn't moved for {quiet_for:.0f}s, so systemd will restart it")
                quiet_logged = True

    threading.Thread(target=keep_telling, daemon=True, name="watchdog").start()
    logger.info(f"Watchdog on: systemd restarts the assistant if it stops for {ALLOWANCE:.0f}s")
