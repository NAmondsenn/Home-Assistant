"""
Backend Test
Checks Nova's backend hears what a Pi sends it, and turns away a Pi without the
password, and that a Pi uses the backend while it answers and its own speech-to-text
when it doesn't - over real connections on this machine, with stand-ins for Nemotron
and for the Pi's own model. Needs numpy installed (as it is on the Pi), and is
skipped anywhere it isn't.
"""

import socket
import threading
import time

import pytest

np = pytest.importorskip("numpy")

import backend
from voice_assistant import protocol, speech_to_text
from voice_assistant.speech_to_text import BackendSpeechToText

TOKEN = "correct-horse-battery-staple"


class FakeNemotron:
    """Stands in for Nemotron, hearing whatever the test sets, and keeping each recording it was given."""

    def __init__(self, heard="Set a timer for ten minutes.", slow=0.0):
        self.heard = heard
        self.slow = slow
        self.given = []

    def start(self):
        return []

    def add(self, stream, audio):
        stream.append(audio)

    def finish(self, stream):
        time.sleep(self.slow)
        self.given.append(np.concatenate(stream) if stream else np.zeros(0, np.float32))
        return self.heard


@pytest.fixture
def serve():
    """Starts backends on this machine, stopping them again after the test."""
    servers = []

    def start(nemotron=None, port=0):
        server = backend.Server(port, TOKEN, nemotron or FakeNemotron())
        # Checking for being stopped more often than the half second it usually
        # would, so each test isn't kept waiting at the end.
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _connect(server, token=TOKEN):
    """Connects to a backend as a Pi would, giving it a password."""
    stream = socket.create_connection(server.server_address, timeout=2).makefile("rwb")
    protocol.send(stream, "hello", token=token)
    return stream


# The backend

def test_a_pi_without_the_password_is_turned_away(serve):
    stream = _connect(serve(), token="not-the-password-at-all")
    assert protocol.receive(stream)[0]["type"] == "refused"
    with pytest.raises(ConnectionError):
        protocol.receive(stream)


def test_a_recording_is_heard_as_its_sent_and_the_words_come_back(serve):
    nemotron = FakeNemotron()
    stream = _connect(serve(nemotron))
    assert protocol.receive(stream)[0]["type"] == "ready"

    protocol.send(stream, "listen")
    protocol.send(stream, "audio", np.array([0, 16384], "<i2").tobytes())
    protocol.send(stream, "audio", np.array([-32768, 32767], "<i2").tobytes())
    protocol.send(stream, "finish")

    assert protocol.receive(stream)[0] == {"type": "text", "text": "Set a timer for ten minutes."}
    assert np.allclose(nemotron.given[0], [0, 0.5, -1, 32767 / 32768])


def test_its_checked_on_with_a_ping(serve):
    stream = _connect(serve())
    protocol.receive(stream)
    protocol.send(stream, "ping")
    assert protocol.receive(stream)[0]["type"] == "pong"


def test_a_message_out_of_turn_ends_the_connection(serve):
    stream = _connect(serve())
    protocol.receive(stream)
    # Finished, without a recording ever having been started.
    protocol.send(stream, "finish")
    with pytest.raises(ConnectionError):
        protocol.receive(stream)


def test_the_backend_wont_start_with_a_password_easy_to_guess():
    with pytest.raises(ValueError):
        backend.Server(0, "letmein", FakeNemotron())


# A Pi using it

class FakeLocal:
    """Stands in for the Pi's own speech-to-text."""

    def __init__(self):
        self.transcribed = None

    def transcribe(self, audio):
        self.transcribed = len(audio)
        return {"text": "Heard on the Pi."}

    def listen(self):
        return "listening on the Pi"


@pytest.fixture
def local():
    """Loads the Pi's own speech-to-text when asked, keeping each one loaded."""
    loaded = []

    def load():
        loaded.append(FakeLocal())
        return loaded[-1]

    return loaded, load


def _address(server):
    return f"127.0.0.1:{server.server_address[1]}"


def _nothing_listening():
    """An address nothing is listening on."""
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{spare.getsockname()[1]}"


def _wait_for(condition, timeout=3.0):
    stop = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < stop, "it never happened"
        time.sleep(0.01)


def test_the_backend_hears_for_the_pi_while_its_answering(serve, local):
    server = serve()
    loaded, load = local
    stt = BackendSpeechToText(_address(server), TOKEN, load)
    try:
        listening = stt.listen()
        listening.add(np.full(480, 0.25, np.float32))
        listening.add(np.full(480, -0.5, np.float32))
        assert listening.finish(np.zeros(960, np.float32))["text"] == "Set a timer for ten minutes."
        assert np.allclose(server.nemotron.given[0], [0.25] * 480 + [-0.5] * 480)
        # The Pi's own was never needed, so it was never loaded.
        assert loaded == []
    finally:
        stt.close()


def test_with_the_backend_down_the_pis_own_is_loaded_straight_away(local):
    loaded, load = local
    stt = BackendSpeechToText(_nothing_listening(), TOKEN, load)
    try:
        assert len(loaded) == 1 and not stt.reachable
        assert stt.listen() == "listening on the Pi"
    finally:
        stt.close()


def test_a_pi_with_the_wrong_password_hears_for_itself(serve, local):
    loaded, load = local
    stt = BackendSpeechToText(_address(serve()), "not-the-password-at-all", load)
    try:
        assert stt.listen() == "listening on the Pi"
    finally:
        stt.close()


def test_a_recording_the_backend_doesnt_answer_in_time_is_transcribed_on_the_pi(serve, local):
    loaded, load = local
    stt = BackendSpeechToText(_address(serve(FakeNemotron(slow=1.0))), TOKEN, load, answer_within=0.2)
    try:
        listening = stt.listen()
        listening.add(np.full(480, 0.1, np.float32))
        started = time.monotonic()
        assert listening.finish(np.zeros(16000, np.float32))["text"] == "Heard on the Pi."
        # From the whole recording, after waiting only as long as it was told to.
        assert loaded[0].transcribed == 16000 and time.monotonic() - started < 0.8
        assert not stt.reachable
    finally:
        stt.close()


def test_the_backend_going_and_coming_back_is_noticed(serve, local):
    server = serve()
    port = server.server_address[1]
    loaded, load = local
    stt = BackendSpeechToText(_address(server), TOKEN, load, check_every=0.05)
    try:
        assert stt.reachable and loaded == []
        server.shutdown()
        server.server_close()
        # As if the backend's machine had gone, and its connection with it.
        with stt._busy:
            stt._drop()
        # The Pi's own is loaded as soon as that's noticed, before it's needed.
        _wait_for(lambda: loaded)
        serve(port=port)
        _wait_for(lambda: stt.reachable)
    finally:
        stt.close()


def test_what_the_backend_imagines_in_noise_is_thrown_away(serve, local):
    loaded, load = local
    stt = BackendSpeechToText(_address(serve(FakeNemotron(heard="Thank you."))), TOKEN, load)
    try:
        listening = stt.listen()
        listening.add(np.full(480, 0.1, np.float32))
        assert listening.finish(np.zeros(480, np.float32))["text"] == ""
    finally:
        stt.close()


def test_a_cancelled_recording_leaves_the_backend_free_for_the_next(serve, local):
    loaded, load = local
    stt = BackendSpeechToText(_address(serve()), TOKEN, load)
    try:
        stt.listen().cancel()
        listening = stt.listen()
        listening.add(np.full(480, 0.1, np.float32))
        assert listening.finish(np.zeros(480, np.float32))["text"] == "Set a timer for ten minutes."
    finally:
        stt.close()


def test_the_backend_is_used_once_config_says_where_it_is(serve, make_config, monkeypatch):
    monkeypatch.setenv("NOVA_BACKEND_TOKEN", TOKEN)
    stt = speech_to_text.make_speech_to_text(make_config(backend={"address": _address(serve())}))
    try:
        assert isinstance(stt, BackendSpeechToText) and stt.reachable
    finally:
        stt.close()
