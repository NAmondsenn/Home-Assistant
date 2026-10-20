"""
Watchdog Test
Checks what the assistant tells systemd, using a socket of the test's own in place
of systemd's.
"""

import shutil
import socket
import tempfile
import time

import pytest

from voice_assistant import watchdog


@pytest.fixture
def systemd(monkeypatch):
    """A socket standing in for systemd's, which collects what's sent to it."""
    # In a short folder of its own, since a socket's path can't be much over 100
    # characters, and the usual temporary folder on a Mac is nearly that already.
    folder = tempfile.mkdtemp(dir="/tmp")
    path = folder + "/notify"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(path)
    listener.settimeout(0.5)
    monkeypatch.setenv("NOTIFY_SOCKET", path)

    def received(within=0.5):
        messages, deadline = [], time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                messages.append(listener.recv(100).decode())
            except socket.timeout:
                break
        return messages

    yield received
    listener.close()
    shutil.rmtree(folder)


def test_outside_systemd_it_does_nothing(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    watchdog.start()


def test_it_says_its_ready_and_keeps_saying_its_alive(systemd, monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "150000")
    watchdog.start()
    assert systemd(0.1)[0] == "READY=1"
    watchdog.beat()
    assert "WATCHDOG=1" in systemd(0.3)


def test_a_main_loop_which_stops_moving_stops_the_pings(systemd, monkeypatch):
    # A hung assistant used to look healthy to systemd for as long as it was left.
    monkeypatch.setenv("WATCHDOG_USEC", "150000")
    monkeypatch.setattr(watchdog, "ALLOWANCE", 0.2)
    watchdog.start()
    # Nothing checks in from here - including microphones other tests left waiting.
    monkeypatch.setattr(watchdog, "beat", lambda: None)
    time.sleep(0.3)
    # What was sent before the allowance ran out is still waiting to be read.
    systemd(0.05)
    assert systemd(0.3) == []
