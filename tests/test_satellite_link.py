"""Tests for the satellite's connection to its brain, against a fake brain on a real socket."""

import logging
import socket
import threading
import time

import pytest

from voice_assistant import protocol, satellite_link
from voice_assistant.satellite_link import SatelliteLink

PASSWORD = "hunter2"


class FakeBrain:
    """A listening socket which admits satellites, or refuses them, as told."""

    def __init__(self, refuse=False, answer_pings=True):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.refuse = refuse
        self.answer_pings = answer_pings
        self.hellos = []
        self.received = []
        self.streams = []
        self.accepted = 0
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def address(self):
        return f"127.0.0.1:{self.port}"

    def _accept(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        stream = sock.makefile("rwb")
        self.streams.append((sock, stream))
        try:
            if self.refuse:
                protocol.receive(stream)
                protocol.send(stream, "refused")
                sock.close()
                return
            self.hellos.append(protocol.admit(stream, lambda name: PASSWORD))
            while True:
                message, payload = protocol.receive(stream)
                self.received.append(message["type"])
                if message["type"] == "ping" and self.answer_pings:
                    protocol.send(stream, "pong")
        except (OSError, protocol.ProtocolError, protocol.Refused):
            pass

    def say(self, kind, **fields):
        sock, stream = self.streams[-1]
        protocol.send(stream, kind, **fields)

    def drop_everyone(self):
        for sock, _ in self.streams:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def close(self):
        self.listener.close()
        self.drop_everyone()


def wait_for(condition, seconds=5):
    # Counted in loops, not read from a clock, as a test may patch the link's clock.
    for _ in range(int(seconds / 0.01)):
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(satellite_link, "SOCKET_TIMEOUT", 0.05)
    monkeypatch.setattr(satellite_link, "RETRY_FIRST", 0.02)
    monkeypatch.setattr(satellite_link, "RETRY_LONGEST", 0.08)


@pytest.fixture
def make():
    made = []

    def make(address, password=PASSWORD, unclean=True):
        events, messages = [], []
        link = SatelliteLink(address, "bedroom", "pi", password, 48000, unclean,
                             lambda message, payload: messages.append((message["type"], payload)),
                             events.append)
        made.append(link)
        return link, events, messages

    yield make
    for link in made:
        link.stop()


@pytest.fixture
def brain():
    brain = FakeBrain()
    yield brain
    brain.close()


def test_the_link_connects_and_reports_it(brain, make):
    link, events, messages = make(brain.address)
    link.start()
    assert wait_for(lambda: events == [True] and len(brain.hellos) == 1)
    assert link.connected
    assert brain.hellos == [{"name": "bedroom", "hostname": "pi", "sample_rate": 48000, "unclean": True}]

    brain.say("listen")
    brain.say("phrase", rate=22050)
    assert wait_for(lambda: len(messages) == 2)
    assert [m[0] for m in messages] == ["listen", "phrase"]

    assert link.send("wake", talked_over=False) is True
    assert wait_for(lambda: "wake" in brain.received)
    link.stop()
    assert not link.connected
    assert events == [True, False]


def test_the_link_reconnects_after_the_brain_goes(brain, make):
    link, events, messages = make(brain.address)
    link.start()
    assert wait_for(lambda: link.connected)
    brain.drop_everyone()
    assert wait_for(lambda: events == [True, False, True])
    assert wait_for(lambda: len(brain.hellos) == 2)
    assert link.connected
    # The brain has heard the first hello's word on it already.
    assert [h["unclean"] for h in brain.hellos] == [True, False]


def test_pings_are_answered_inside_the_link(brain, make, monkeypatch):
    monkeypatch.setattr(satellite_link, "PING_EVERY", 0.05)
    link, events, messages = make(brain.address)
    link.start()
    assert wait_for(lambda: brain.received.count("ping") >= 2)
    assert link.connected
    assert messages == []


def test_refusals_are_logged_once_and_backed_off(make, caplog):
    brain = FakeBrain(refuse=True)
    waits = []

    class Recording(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return super().wait(timeout)

    try:
        link, events, messages = make(brain.address)
        link._stopping = Recording()
        with caplog.at_level(logging.WARNING):
            link.start()
            assert wait_for(lambda: brain.accepted >= 5)
        link.stop()
    finally:
        brain.close()
    assert len([r for r in caplog.records if "Can't connect" in r.message]) == 1
    assert waits[:4] == pytest.approx([0.02, 0.04, 0.08, 0.08])
    assert events == []


def test_an_unreachable_brain_is_logged_once(make, caplog):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    link, events, messages = make(f"127.0.0.1:{port}")
    with caplog.at_level(logging.WARNING):
        link.start()
        time.sleep(0.4)
    link.stop()
    assert len([r for r in caplog.records if "Can't connect" in r.message]) == 1


def test_nothing_heard_for_15_seconds_counts_as_dropped(make, monkeypatch):
    brain = FakeBrain(answer_pings=False)
    now = [1000.0]
    monkeypatch.setattr(satellite_link, "monotonic", lambda: now[0])
    try:
        link, events, messages = make(brain.address)
        link.start()
        assert wait_for(lambda: link.connected)
        now[0] += 14.0
        time.sleep(0.2)
        assert link.connected
        now[0] += 1.5
        assert wait_for(lambda: events[:2] == [True, False])
    finally:
        brain.close()


def test_send_says_false_while_not_connected(brain, make):
    link, events, messages = make(brain.address)
    assert link.send("wake") is False
    link.start()
    assert wait_for(lambda: link.connected)
    assert link.send("wake") is True
    link.stop()
    assert link.send("wake") is False


def test_a_message_can_carry_a_field_named_kind(brain, make):
    link, events, messages = make(brain.address)
    link.start()
    assert wait_for(lambda: link.connected)
    assert link.send("note", kind="reply") is True
    assert wait_for(lambda: "note" in brain.received)


def test_pings_go_out_while_the_brain_keeps_talking(brain, make, monkeypatch):
    monkeypatch.setattr(satellite_link, "PING_EVERY", 0.1)
    link, events, messages = make(brain.address)
    link.start()
    assert wait_for(lambda: link.connected)
    # Pieces arriving faster than the socket timeout, so no read ever times out.
    for _ in range(40):
        brain.say("stream_audio", id="a")
        time.sleep(0.01)
    assert brain.received.count("ping") >= 2
