"""Tests for the satellite's connection to its brain, against a fake brain on a real socket."""

import logging
import re
import select
import socket
import struct
import threading
import time

import pytest

from voice_assistant import protocol, satellite_link
from voice_assistant.config import ConfigError
from voice_assistant.satellite_link import SatelliteLink, Stream

PASSWORD = "hunter2"


class FakeBrain:
    """A listening socket which admits satellites, or refuses them, as told."""

    def __init__(self, refuse=False, answer_pings=True, host="127.0.0.1"):
        self.host = host
        self.listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        try:
            self.listener.bind((host, 0))
        except OSError:
            self.listener.close()
            raise
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
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

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

    def make(address, password=PASSWORD, unclean=True, **speaker):
        events, messages = [], []
        link = SatelliteLink(address, "bedroom", "pi", password, 48000, unclean,
                             lambda message, payload: messages.append((message["type"], payload)),
                             events.append, **speaker)
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


def test_the_link_says_its_spotify_speaker_each_time_it_connects(brain, make):
    link, events, messages = make(brain.address, speaker="Bedroom Pi")
    link.start()
    assert wait_for(lambda: len(brain.hellos) == 1)
    brain.drop_everyone()
    assert wait_for(lambda: len(brain.hellos) == 2)
    assert [hello["speaker"] for hello in brain.hellos] == ["Bedroom Pi", "Bedroom Pi"]


def test_the_link_connects_and_reports_it(brain, make):
    link, events, messages = make(brain.address)
    link.start()
    assert wait_for(lambda: events == [True] and len(brain.hellos) == 1)
    assert link.connected
    assert brain.hellos == [{"name": "bedroom", "hostname": "pi", "sample_rate": 48000, "unclean": True,
                             "speaker": None}]

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


def test_a_stop_just_as_the_brain_accepts_tells_nothing(brain, make):
    # Neither connected nor lost: the satellite never heard it had the brain, so hearing
    # it had lost it at shutdown would be news of nothing.
    link, events, messages = make(brain.address)
    connect = link._connect

    def connect_then_stop():
        connect()
        # As stop() does, but from here, so it comes in just after the brain has
        # accepted the satellite. Joining is the test's to do.
        link._stopping.set()
        link._close()

    link._connect = connect_then_stop
    link.start()
    link._thread.join(timeout=5)
    assert not link._thread.is_alive()
    assert wait_for(lambda: len(brain.hellos) == 1)
    assert events == []
    assert not link.connected


def test_the_brain_can_be_an_ipv6_address_in_brackets(make):
    try:
        brain = FakeBrain(host="::1")
    except OSError:
        pytest.skip("This machine has no IPv6 loopback")
    try:
        link, events, messages = make(brain.address)
        assert link.address == ("::1", brain.port)
        link.start()
        assert wait_for(lambda: events == [True] and len(brain.hellos) == 1)
    finally:
        brain.close()


@pytest.mark.parametrize("address, host, port", [
    ("nova:10400", "nova", 10400),
    # As it always was, so an address which works now still does - the port is after
    # the last colon.
    ("2001:db8::1:10400", "2001:db8::1", 10400),
    ("[2001:db8::1]:10400", "2001:db8::1", 10400),
])
def test_the_address_is_a_name_and_a_port(make, address, host, port):
    link, events, messages = make(address)
    assert link.address == (host, port)


@pytest.mark.parametrize("address", ["nova", "nova:", "nova:port", ":10400", "nova:0", "nova:65536",
                                     "[::1]", "[::1]10400", "[]:10400", "[::1]:"])
def test_an_address_which_cant_be_one_is_refused_naming_the_setting(make, address):
    with pytest.raises(ConfigError, match=rf"backend\.address.*not {re.escape(repr(address))}"):
        make(address)


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


def test_a_brain_gone_silent_is_logged_as_silent(make, monkeypatch, caplog):
    # It was logged as "The connection was closed", which it never was.
    brain = FakeBrain(answer_pings=False)
    now = [1000.0]
    monkeypatch.setattr(satellite_link, "monotonic", lambda: now[0])
    try:
        link, events, messages = make(brain.address)
        with caplog.at_level(logging.WARNING):
            link.start()
            assert wait_for(lambda: link.connected)
            now[0] += 15.5
            assert wait_for(lambda: events[:2] == [True, False])
    finally:
        brain.close()

    lost = [record.message for record in caplog.records if record.message.startswith("Lost the brain")]
    assert lost[0] == "Lost the brain at 127.0.0.1 (Nothing heard from the brain for 15 seconds)"


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


# The stream

def loopback():
    """Both ends of a real connection on this machine: the far end's socket, and a Stream on the near end's."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        far = socket.create_connection(listener.getsockname(), timeout=5)
        near, _ = listener.accept()
    near.settimeout(5)
    return far, Stream(near)


def arrived(stream):
    """Waits until there's something to read on the stream's socket - more bytes, or the end."""
    assert select.select([stream._socket], [], [], 5)[0]


def test_the_stream_tells_a_hang_up_without_waiting():
    far, stream = loopback()
    started = time.monotonic()
    assert stream.hung_up() is False
    # Not the socket's five seconds, and the timeout is left as it was.
    assert time.monotonic() - started < 1
    assert stream._socket.gettimeout() == 5

    far.sendall(b'{"type": "hello"}\n')
    far.close()
    assert protocol.receive(stream)[0]["type"] == "hello"
    arrived(stream)
    assert stream.hung_up() is True
    assert stream._socket.gettimeout() == 5
    stream._socket.close()


def test_the_stream_takes_a_reset_for_a_hang_up():
    far, stream = loopback()
    # Closing with no time to linger resets the connection rather than closing it.
    far.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    far.close()
    arrived(stream)
    assert stream.hung_up() is True
    assert stream._socket.gettimeout() == 5
    stream._socket.close()


def test_the_stream_doesnt_take_bytes_still_to_read_for_a_hang_up():
    far, stream = loopback()
    # Both in one go, so the second is read into the stream's buffer with the first -
    # with the hang-up behind it on the socket.
    far.sendall(b'{"type": "hello"}\n{"type": "ping"}\n')
    far.close()
    assert protocol.receive(stream)[0]["type"] == "hello"
    assert stream._buffer
    arrived(stream)
    assert stream.hung_up() is False
    assert protocol.receive(stream)[0]["type"] == "ping"
    assert stream.hung_up() is True
    stream._socket.close()

    # And still on the socket, unread, ahead of the hang-up.
    far, stream = loopback()
    far.sendall(b"{")
    far.close()
    arrived(stream)
    assert stream.hung_up() is False
    assert stream.read(1) == b"{"
    arrived(stream)
    assert stream.hung_up() is True
    stream._socket.close()
