"""
Remote Satellite Test
Checks the brain's side of one satellite - playing, stopping and hearing through it -
against a fake satellite over a socketpair, which reads what the brain sends with the
real protocol. Needs numpy installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import logging
import select
import socket
import struct
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


def connect(stream_class=satellite_link.Stream):
    """The brain's view of a satellite which has just been admitted, serving on a thread of its own."""
    ours, theirs = socket.socketpair()
    # Short, so the brain's reading wakes often to check on the satellite.
    ours.settimeout(0.05)
    satellite = RemoteSatellite(stream_class(ours), dict(HELLO))
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


class Failing(Pieces):
    """A stream's pieces which fail once the ones given are used up, as speech can part way through a reply."""

    def __next__(self):
        try:
            return super().__next__()
        except StopIteration:
            raise RuntimeError("the voice failed") from None


class Chunky(satellite_link.Stream):
    """
    A stream which writes in small pieces, as it does a big message on a busy connection,
    giving another thread the chance to write in between.
    """

    def write(self, data):
        for begin in range(0, len(data), 100):
            super().write(data[begin:begin + 100])
            time.sleep(0)


class Breakable:
    """A stream which takes what's written until it's broken, then fails as a hung-up connection does."""

    def __init__(self):
        self.broken = False

    def write(self, data):
        if self.broken:
            raise BrokenPipeError("hung up")

    def flush(self):
        pass


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


def test_a_wake_says_how_much_of_its_recording_is_from_before_it(pair):
    # In samples - or nothing, from a satellite which doesn't say, when the session
    # goes by its own setting.
    satellite, fake, serving = pair
    fake.say("wake", talked_over=False, pre_roll=4800)
    fake.say("wake", talked_over=False)
    fake.say("wake", talked_over=False, pre_roll=-1)
    fake.say("wake", talked_over=False, pre_roll=True)
    assert [satellite.wakes.get(timeout=2)["pre_roll"] for _ in range(4)] == [4800, None, None, None]


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


def test_a_late_played_from_an_old_connection_finishes_nothing_on_the_new_one():
    # The satellite reports a sound's end on whichever link it has by then, so an id which
    # the new connection reused would end the new connection's sound.
    old_connection = connect()
    old = old_connection[0].speaker.play_file("chime.wav")
    disconnect(*old_connection)

    satellite, fake, serving = connect()
    try:
        new = satellite.speaker.play_file("chime.wav")
        assert new.id != old.id

        fake.say("played", id=old.id, stopped=False, position=100)
        fake.say("ping")
        assert wait_for(lambda: fake.messages("pong"))
        assert not new.done

        fake.say("played", id=new.id, stopped=False, position=200)
        assert new.wait(2)
    finally:
        disconnect(satellite, fake, serving)


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


@pytest.mark.parametrize("stopping", [
    lambda speaker: speaker.stop(),
    lambda speaker: speaker.stop(["reply"]),
    lambda speaker: speaker.silence("reply"),
], ids=["everything", "a kind", "silence"])
def test_a_sound_started_while_a_stop_is_sent_isnt_finished_by_it(stopping):
    # Another thread's reply starts after the satellite was told to stop, so it plays
    # there - and mustn't be taken as stopped here, where it would be waited on no more.
    started = []

    def send(kind, payload=b"", /, **fields):
        if kind in ("stop", "silence") and not started:
            started.append(speaker.play_file("late.wav"))
        return True

    speaker = remote.RemoteSpeaker(send, 48000)
    earlier = speaker.play_file("earlier.wav")

    stopping(speaker)

    assert earlier.done
    assert not started[0].done


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


def test_the_longest_request_goes_with_the_state_only_when_given(pair):
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}

    satellite.set_state(volumes, False)
    satellite.set_state(volumes, False, 20.0)

    assert wait_for(lambda: len(fake.received) == 2)
    assert fake.received == [({"type": "state", "volumes": volumes, "muted": False}, b""),
                             ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0}, b"")]


def test_the_wake_confidence_goes_with_the_state_only_when_given(pair):
    # Just another field, so an older satellite's state is as it was, and one which doesn't know it ignores it.
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}

    satellite.set_state(volumes, False, 20.0)
    satellite.set_state(volumes, False, 20.0, wake_confidence=0.75)
    satellite.set_state(volumes, True, wake_confidence=0.5)

    assert wait_for(lambda: len(fake.received) == 3)
    assert fake.received == [
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0}, b""),
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0, "wake_confidence": 0.75}, b""),
        ({"type": "state", "volumes": volumes, "muted": True, "wake_confidence": 0.5}, b"")]


def test_the_voice_check_goes_with_the_state_only_when_given(pair):
    # Just another field, as the wake confidence is, so an older satellite ignores it.
    satellite, fake, serving = pair
    volumes = {"general": 0.5, "alarm": 1.0, "music": 0.3}

    satellite.set_state(volumes, False, 20.0, wake_confidence=0.75)
    satellite.set_state(volumes, False, 20.0, wake_confidence=0.75, voice_check=0.2)
    satellite.set_state(volumes, True, voice_check=0)

    assert wait_for(lambda: len(fake.received) == 3)
    assert fake.received == [
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0, "wake_confidence": 0.75}, b""),
        ({"type": "state", "volumes": volumes, "muted": False, "longest_request": 20.0, "wake_confidence": 0.75,
          "voice_check": 0.2}, b""),
        ({"type": "state", "volumes": volumes, "muted": True, "voice_check": 0}, b"")]


def test_messages_sent_from_several_threads_arrive_whole_and_in_order():
    # Music, speech and the session's own messages are all sent from threads of their own.
    satellite, fake, serving = connect(Chunky)
    sent = []

    def send_many(number):
        for n in range(20):
            sent.append(satellite.send("stream_audio", bytes([number]) * 1500, id=str(number), n=n))

    try:
        threads = [threading.Thread(target=send_many, args=(number,)) for number in range(1, 5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert len(sent) == 80 and all(sent)
        assert wait_for(lambda: len(fake.received) == 80)
        for number in range(1, 5):
            mine = [(message, payload) for message, payload in fake.received if message["id"] == str(number)]
            assert [message["n"] for message, _ in mine] == list(range(20))
            assert all(payload == bytes([number]) * 1500 for _, payload in mine)
    finally:
        disconnect(satellite, fake, serving)


def test_a_send_which_fails_ends_what_was_playing_and_whatever_is_started_after():
    stream = Breakable()
    satellite = RemoteSatellite(stream, dict(HELLO))
    speaker = satellite.speaker
    # Endless, and held back by most_ahead, so only the satellite going can end it.
    song = Pieces(np.zeros((100, 2), dtype=np.float32) for _ in iter(int, 1))
    music = speaker.play_stream(song, sample_rate=1000, kind="music", most_ahead=0.5)
    chime = speaker.play_file("chime.wav")
    assert satellite.connected and not music.done and not chime.done

    stream.broken = True
    later = speaker.play_file("later.wav")

    # The failed send is the brain finding the satellite gone: nothing waits on it any more.
    assert not satellite.connected
    assert later.done and chime.done and music.wait(2)
    assert song.closed.wait(2)
    assert satellite.send("restart") is False


def test_a_stream_whose_pieces_fail_part_way_still_ends_where_it_got_to(pair, caplog):
    satellite, fake, serving = pair
    pieces = Failing([np.zeros(2205, dtype=np.float32)])

    with caplog.at_level(logging.ERROR, logger=remote.__name__):
        playback = satellite.speaker.play_stream(pieces, sample_rate=22050)
        assert wait_for(lambda: fake.messages("stream_end"))

    assert [message["type"] for message, _ in fake.received] == ["play_stream", "stream_audio", "stream_end"]
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.ERROR, "A streamed reply sound failed part way: the voice failed")]
    assert pieces.closed.is_set()
    # Not lost with it: the satellite plays what it has, and says so.
    assert satellite.connected and not playback.done
    fake.say("played", id=playback.id, stopped=False, position=2205)
    assert playback.wait(2)


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
    fake.say("away", rang=["kitchen@1700000000", "wake-up@1700003600"], dismissed=["wake-up@1700003600"],
             ringing=["kitchen@1700000000"])
    # Or without what's still ringing, from a satellite which doesn't say.
    fake.say("away", rang=["kitchen@1700000000"], dismissed=[])
    assert satellite.away_reports.get(timeout=2) == {"rang": ["kitchen@1700000000", "wake-up@1700003600"],
                                                     "dismissed": ["wake-up@1700003600"],
                                                     "ringing": ["kitchen@1700000000"]}
    assert satellite.away_reports.get(timeout=2) == {"rang": ["kitchen@1700000000"], "dismissed": [], "ringing": []}


def test_pings_are_answered(pair):
    satellite, fake, serving = pair
    fake.say("ping")
    assert wait_for(lambda: fake.messages("pong"))


def closed(ours, theirs):
    theirs.close()


def reset(ours, theirs):
    # Closing with no time to linger resets the connection rather than closing it.
    theirs.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    theirs.close()
    # The reset has arrived once there's something to read.
    assert select.select([ours], [], [], 5)[0]


@pytest.mark.parametrize("hang_up, family", [(closed, socket.AF_UNIX), (reset, socket.AF_INET)],
                         ids=["broken pipe", "reset"])
def test_a_send_which_finds_the_satellite_already_gone_says_so_plainly(hang_up, family, caplog):
    # As after the brain was paused: the satellite gave up on it and hung up, leaving
    # pings for the brain to answer once it carried on - with a pipe broken, or reset.
    caplog.set_level(logging.INFO, logger=remote.__name__)
    if family == socket.AF_UNIX:
        ours, theirs = socket.socketpair()
    else:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            theirs = socket.create_connection(listener.getsockname(), timeout=5)
            ours, _ = listener.accept()
    ours.settimeout(5)
    satellite = RemoteSatellite(satellite_link.Stream(ours), dict(HELLO))
    hang_up(ours, theirs)
    try:
        assert satellite.send("pong") is False
    finally:
        ours.close()
    assert not satellite.connected
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, "The bedroom satellite had already hung up, so 'pong' wasn't sent")]


def test_a_send_which_fails_otherwise_is_a_warning(caplog):
    class Stalled:
        def write(self, data):
            raise TimeoutError("timed out")

        def flush(self):
            pass

    satellite = RemoteSatellite(Stalled(), dict(HELLO))
    assert satellite.send("restart") is False
    assert not satellite.connected
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "Couldn't send 'restart' to the bedroom satellite (timed out)")]


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
