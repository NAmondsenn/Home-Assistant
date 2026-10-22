"""
Remote Satellite Test
Checks the brain's side of one satellite - playing, stopping and hearing through it -
against a fake satellite over a socketpair, which reads what the brain sends with the
real protocol. Needs numpy installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import socket
import threading
import time

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import protocol, remote, satellite_link
from voice_assistant.remote import RemoteSatellite

HELLO = {"name": "bedroom", "hostname": "pi", "sample_rate": 48000, "unclean": False}


def wait_for(condition, seconds=5):
    # Counted in loops, not read from a clock, as a test may patch the brain's clock.
    for _ in range(int(seconds / 0.005)):
        if condition():
            return True
        time.sleep(0.005)
    return False


def ends_at_once(feed):
    """Whether reading on from the end of a feed returns straight away, as it does once the feed has ended."""
    started = time.monotonic()
    feed.listen().read(100)
    return time.monotonic() - started < 1.0


class FakeSatellite:
    """The Pi's end of the connection: keeps everything the brain sends, and says what it's told to."""

    def __init__(self, sock):
        self.socket = sock
        self.stream = sock.makefile("rwb")
        self.received = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                self.received.append(protocol.receive(self.stream))
        except (OSError, protocol.ProtocolError):
            pass

    def say(self, kind, payload=b"", **fields):
        with self.lock:
            protocol.send(self.stream, kind, payload, **fields)

    def messages(self, kind):
        return [message for message, _ in self.received if message["type"] == kind]

    def payloads(self, kind):
        return [payload for message, payload in self.received if message["type"] == kind]

    def go(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


def connect():
    """The brain's view of a satellite which has just been admitted, serving on a thread of its own."""
    ours, theirs = socket.socketpair()
    # Short, so the brain's reading wakes often to check on the satellite.
    ours.settimeout(0.05)
    satellite = RemoteSatellite(satellite_link._Stream(ours), dict(HELLO))
    fake = FakeSatellite(theirs)
    serving = threading.Thread(target=satellite.serve, daemon=True)
    serving.start()
    return satellite, fake, serving


def disconnect(satellite, fake, serving):
    satellite.close()
    fake.go()
    serving.join(2)


@pytest.fixture
def pair():
    made = connect()
    yield made
    disconnect(*made)


class Pieces:
    """A stream's pieces, which notes being closed - as a generator making speech would be."""

    def __init__(self, pieces):
        self.pieces = iter(pieces)
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.pieces)

    def close(self):
        self.closed.set()


def test_the_satellite_is_known_by_its_hello(pair):
    satellite, fake, serving = pair
    assert (satellite.name, satellite.hostname, satellite.sample_rate) == ("bedroom", "pi", 48000)
    assert satellite.speaker.sample_rate == 48000
    assert satellite.connected


def test_a_stream_reaches_the_satellite_as_16_bit_pieces(pair):
    satellite, fake, serving = pair
    speech = np.linspace(-1, 1, 5000, dtype=np.float32)
    playback = satellite.speaker.play_stream(iter([speech[:3000], speech[3000:]]), sample_rate=22050, volume=0.5)

    assert wait_for(lambda: fake.messages("stream_end"))
    assert fake.messages("play_stream") == [{"type": "play_stream", "id": playback.id, "rate": 22050, "channels": 1,
                                             "volume": 0.5, "kind": "reply", "paused": False}]
    kinds = [message["type"] for message, _ in fake.received]
    assert kinds[0] == "play_stream" and kinds[-1] == "stream_end"
    assert set(kinds[1:-1]) == {"stream_audio"}
    assert all(message["id"] == playback.id for message, _ in fake.received)
    # Each piece is at most 100ms of it.
    pieces = fake.payloads("stream_audio")
    assert all(len(piece) <= 2205 * 2 for piece in pieces)
    sent = np.concatenate([np.frombuffer(piece, "<i2") for piece in pieces])
    assert len(sent) == 5000
    assert np.abs(sent / 32768 - speech).max() < 1 / 16384
    assert playback.duration == pytest.approx(5000 / 22050)


def test_music_goes_in_stereo_and_keeps_close_to_what_has_played(pair):
    satellite, fake, serving = pair
    song = np.tile(np.array([[0.25, -0.25]], dtype=np.float32), (500, 1))
    playback = satellite.speaker.play_stream((song for _ in range(10)), sample_rate=1000, kind="music",
                                             most_ahead=1.0)

    # A second of it, and the piece which went past that, then nothing more until
    # the satellite has played some.
    assert wait_for(lambda: len(fake.messages("stream_audio")) == 11)
    time.sleep(0.2)
    assert len(fake.messages("stream_audio")) == 11
    assert fake.messages("play_stream")[0]["channels"] == 2
    first = np.frombuffer(fake.payloads("stream_audio")[0], "<i2").reshape(-1, 2)
    assert len(first) == 100
    assert (first[:, 0] == 8192).all() and (first[:, 1] == -8192).all()

    # A second played, at the satellite's own rate.
    fake.say("progress", id=playback.id, position=48000)
    assert wait_for(lambda: len(fake.messages("stream_audio")) == 21)
    time.sleep(0.2)
    assert len(fake.messages("stream_audio")) == 21
    assert playback.position == 48000


def test_waiting_ends_when_the_satellite_says_played(pair):
    satellite, fake, serving = pair
    playback = satellite.speaker.play_file("/home/nova/sounds/timer.wav", volume=0.4, kind="announcement",
                                           start=2.0, length=3.0)

    assert wait_for(lambda: fake.messages("play_sound"))
    assert fake.messages("play_sound") == [{"type": "play_sound", "id": playback.id, "name": "timer.wav",
                                            "volume": 0.4, "kind": "announcement", "start": 2.0, "length": 3.0}]
    assert playback.wait(0.1) is False
    assert not playback.done

    fake.say("played", id=playback.id, stopped=False, position=144000)
    assert playback.wait(2) is True
    assert playback.done
    assert playback.position == 144000


def test_a_wake_brings_a_feed_of_the_recording(pair):
    satellite, fake, serving = pair
    heard = (np.arange(4800) % 1000).astype(np.int16)

    fake.say("wake", talked_over=True)
    for piece in np.split(heard, 3):
        fake.say("audio", piece.tobytes())
    fake.say("audio_end", reason="limit")

    wake = satellite.wakes.get(timeout=2)
    assert wake["talked_over"] is True
    assert np.array_equal(wake["feed"].listen(start=0).read(4800), heard)
    assert ends_at_once(wake["feed"])


def test_a_follow_up_is_heard_until_the_brain_stops_listening(pair):
    satellite, fake, serving = pair
    answer = np.arange(1600, dtype=np.int16)

    feed = satellite.listen()
    assert wait_for(lambda: fake.messages("listen"))
    fake.say("audio", answer.tobytes())
    assert np.array_equal(feed.listen(start=0).read(1600), answer)

    satellite.stop_listening()
    assert wait_for(lambda: fake.messages("stop_listening"))
    # Already on its way as the brain stopped listening, so it goes nowhere.
    fake.say("audio", answer.tobytes())
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))
    assert feed.position == 1600
    assert ends_at_once(feed)


def test_pausing_music_tells_the_satellite(pair):
    satellite, fake, serving = pair
    song = np.zeros((100, 2), dtype=np.float32)
    playback = satellite.speaker.play_stream(iter([song]), kind="music", paused=True)
    assert wait_for(lambda: fake.messages("play_stream"))
    assert fake.messages("play_stream")[0]["paused"] is True

    playback.paused = False
    playback.volume = 0.3
    playback.paused = True

    assert wait_for(lambda: len(fake.messages("set_playback")) == 3)
    assert fake.messages("set_playback") == [{"type": "set_playback", "id": playback.id, "paused": False},
                                             {"type": "set_playback", "id": playback.id, "volume": 0.3},
                                             {"type": "set_playback", "id": playback.id, "paused": True}]
    assert playback.paused is True
    assert playback.volume == 0.3


def test_stopping_one_sound_stops_only_that_one(pair):
    satellite, fake, serving = pair
    beep = satellite.speaker.play_file("volume_beep.wav", volume=0.5)
    reply = satellite.speaker.play_file("reply.wav")

    beep.stop()
    assert beep.done and not reply.done
    # Already stopped, so there's nothing more to say.
    beep.stop()

    assert wait_for(lambda: fake.messages("stop"))
    time.sleep(0.05)
    assert fake.messages("stop") == [{"type": "stop", "id": beep.id}]


def test_stopping_tells_the_satellite_what_to_stop(pair):
    satellite, fake, serving = pair
    speaker = satellite.speaker
    reply = speaker.play_file("reply.wav")
    alarm = speaker.play_file("alarm.wav", kind="announcement")

    speaker.stop(["reply"])
    assert reply.done and not alarm.done
    speaker.silence("announcement")
    assert alarm.done
    speaker.allow("announcement")
    speaker.stop()

    assert wait_for(lambda: len(fake.received) == 6)
    assert [message for message, _ in fake.received[2:]] == [{"type": "stop", "kinds": ["reply"]},
                                                              {"type": "silence", "kind": "announcement"},
                                                              {"type": "allow", "kind": "announcement"},
                                                              {"type": "stop", "kinds": None}]


def test_state_alarms_the_phrase_and_restarts_reach_the_satellite(pair):
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}
    entries = [{"id": "kitchen@1700000000", "due": 1700000000.0, "kind": "timer", "sound": "timer.wav"}]
    phrase = np.arange(100, dtype=np.int16).tobytes()

    satellite.set_state(volumes, True)
    satellite.send_alarms(entries)
    satellite.send_phrase(phrase, 22050)
    satellite.restart()

    assert wait_for(lambda: len(fake.received) == 4)
    assert fake.received == [({"type": "state", "volumes": volumes, "muted": True}, b""),
                             ({"type": "alarms", "entries": entries}, b""),
                             ({"type": "phrase", "rate": 22050, "bytes": 200}, phrase),
                             ({"type": "restart"}, b"")]


def test_audio_too_long_for_the_satellite_isnt_sent(pair):
    satellite, fake, serving = pair
    # The satellite would drop the connection over it.
    assert satellite.send_phrase(b"\x00" * (protocol.MAX_AUDIO + 2), 22050) is False
    satellite.restart()
    assert wait_for(lambda: fake.received)
    assert [message["type"] for message, _ in fake.received] == ["restart"]
    assert satellite.connected


def test_what_rang_while_away_is_passed_on(pair):
    satellite, fake, serving = pair
    fake.say("away", rang=["kitchen@1700000000"], dismissed=["wake-up@1700003600"])
    assert satellite.away_reports.get(timeout=2) == {"rang": ["kitchen@1700000000"],
                                                     "dismissed": ["wake-up@1700003600"]}


def test_pings_are_answered(pair):
    satellite, fake, serving = pair
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))


def test_when_the_satellite_goes_every_playback_and_feed_ends(pair):
    satellite, fake, serving = pair
    speaker = satellite.speaker
    # Endless, and held back by most_ahead, so only the satellite going can end it.
    song = Pieces(np.zeros((100, 2), dtype=np.float32) for _ in iter(int, 1))
    music = speaker.play_stream(song, sample_rate=1000, kind="music", most_ahead=0.5)
    chime = speaker.play_file("chime.wav")
    fake.say("wake", talked_over=False)
    feed = satellite.wakes.get(timeout=2)["feed"]
    assert wait_for(lambda: fake.messages("play_sound"))

    fake.go()

    serving.join(2)
    assert not serving.is_alive()
    assert not satellite.connected
    assert music.done and chime.done
    assert music.wait(0) and chime.wait(0)
    assert song.closed.wait(2)
    assert ends_at_once(feed)


def test_nothing_heard_for_15_seconds_counts_as_the_satellite_gone(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(remote, "monotonic", lambda: now[0])
    satellite, fake, serving = connect()
    try:
        now[0] += 14.0
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
        # Fourteen seconds on from the ping, which was the last heard.
        now[0] += 14.0
        time.sleep(0.2)
        assert satellite.connected
        now[0] += 1.5
        serving.join(2)
        assert not serving.is_alive()
        assert not satellite.connected
    finally:
        disconnect(satellite, fake, serving)


def test_closing_lets_the_satellite_go(pair):
    satellite, fake, serving = pair
    satellite.close()
    serving.join(2)
    assert not serving.is_alive()
    assert not satellite.connected


def test_nothing_plays_on_a_satellite_which_isnt_connected(pair):
    satellite, fake, serving = pair
    speaker = satellite.speaker
    satellite.close()
    serving.join(2)

    speech = Pieces([np.zeros(100, dtype=np.float32)])
    played = [speaker.play(np.zeros(100, dtype=np.float32)), speaker.play_stream(speech),
              speaker.play_file("chime.wav")]
    # Already finished, as Speaker's are with no speaker to play on, so whatever
    # waits on one doesn't need to check for None first.
    assert all(playback.done and playback.wait(0) and playback.duration == 0 for playback in played)
    # Closed, so whatever was making it stops.
    assert speech.closed.is_set()
    for playback in played:
        playback.paused = True
        playback.stop()
    assert satellite.send("restart") is False
    assert ends_at_once(satellite.listen())
    time.sleep(0.1)
    assert fake.received == []
