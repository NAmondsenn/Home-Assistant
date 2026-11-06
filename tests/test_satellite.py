"""
Satellite Test
Checks the Pi's own program - hearing the wake word, sending what's said, and playing
and stopping what the brain sends - against a fake brain over a socketpair, with
fakes in place of the microphone, speaker and wake word, so nothing is opened or
loaded. Needs numpy installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import datetime
import json
import logging
import os
import queue
import socket
import threading
import time
import wave
from functools import partial

import pytest

np = pytest.importorskip("numpy")

import satellite
from voice_assistant import fallback, paths, protocol, satellite_link
from voice_assistant.fallback import AlarmCopy, Phrase, StateCopy
from voice_assistant.wake_clips import WakeClips

PASSWORD = "the-bedroom-satellites-password"
RATE = 16000
# The fake microphone hears this many times faster than real time, so a 10 second
# recording takes one.
SPEED = 10
PHRASE = "/sounds/cant_reach_server.wav"


def samples(start, end):
    """What the fake microphone hears between two positions: each sample is its own position."""
    return (np.arange(start, end) % 32768).astype(np.int16)


def wait_for(condition, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


def connected(made):
    """
    Whether the satellite has the brain - not just whether the brain has its hello,
    which comes first. A wake word said in between is sent nowhere, and gets the phrase.
    """
    return made.link is not None and made.link.connected


class FakeBrain:
    """Admits the satellite over a socketpair, and keeps everything it sends but pings."""

    def __init__(self):
        self.up = True
        self.hellos = []
        self.received = []
        self.sockets = []
        self.stream = None
        self.lock = threading.Lock()

    def connect(self, address, timeout=None):
        """Stands in for socket.create_connection."""
        if not self.up:
            raise ConnectionRefusedError("The brain is down")
        ours, theirs = socket.socketpair()
        theirs.settimeout(timeout)
        self.sockets.append(ours)
        threading.Thread(target=self._serve, args=(ours,), daemon=True).start()
        return theirs

    def _serve(self, sock):
        stream = sock.makefile("rwb")
        try:
            hello = protocol.admit(stream, lambda name: PASSWORD)
            self.stream = stream
            self.hellos.append(hello)
            while True:
                message, payload = protocol.receive(stream)
                if message["type"] == "ping":
                    self.say("pong")
                else:
                    self.received.append((message, payload))
        except (OSError, protocol.ProtocolError, protocol.Refused):
            pass

    def say(self, message_type, payload=b"", **fields):
        with self.lock:
            protocol.send(self.stream, message_type, payload, **fields)

    def drop(self):
        """Goes down part way through, and stays down."""
        self.up = False
        self.sockets[-1].shutdown(socket.SHUT_RDWR)

    def kinds(self):
        return [message["type"] for message, _ in self.received]

    def messages(self, kind):
        return [message for message, _ in self.received if message["type"] == kind]

    def recordings(self):
        return [np.frombuffer(payload, np.int16) for message, payload in self.received if message["type"] == "audio"]

    def close(self):
        for sock in self.sockets:
            sock.close()


class FakeReader:
    def __init__(self, microphone, position):
        self.microphone = microphone
        self.position = position

    def read(self, count):
        while self.microphone.position < self.position + count:
            time.sleep(0.001)
        heard = samples(self.position, self.position + count)
        self.position += count
        return heard


class FakeMicrophone:
    """Has been listening for a second when it starts, and hears SPEED times faster than real time."""

    def __init__(self, sample_rate=48000, echo=None):
        self.started = time.monotonic()
        self.closed = False

    @property
    def position(self):
        return RATE + int((time.monotonic() - self.started) * RATE * SPEED)

    def listen(self, start=None):
        return FakeReader(self, self.position if start is None else start)

    def close(self):
        self.closed = True


class FakeWake:
    """Hears the wake word whenever the test says it - as long as it's listening for it then."""

    def __init__(self, config=None, microphone=None, playing=None):
        self.microphone = microphone
        # What the satellite says whether it's making a sound of its own with.
        self.playing = playing
        self.muted = False
        self.sensitivity = 0.5
        # A trained check, as the user's Pi has - None for none.
        self.voice_check = "a trained check"
        self.voice_check_threshold = 0.2
        self.heard_at = None
        self.listening = False
        self._said = queue.Queue()

    def set_muted(self, muted):
        self.muted = muted

    def say(self):
        """The user says the wake word. Returns the microphone position it's said at."""
        position = self.microphone.position
        self._said.put(position)
        return position

    def listen_once(self, until=None):
        # As the real one, only what's said from now on can wake it.
        now = self.microphone.position
        self.listening = True
        try:
            while True:
                if until is not None and until():
                    return None
                try:
                    position = self._said.get(timeout=0.005)
                except queue.Empty:
                    continue
                if position >= now and not self.muted:
                    self.heard_at = position
                    return 0.9
        finally:
            self.listening = False


class FakePlayback:
    """Plays until stopped, or until the test - or the end of its stream - finishes it."""

    def __init__(self, kind, volume, paused=False, **details):
        self.kind = kind
        self.volume = volume
        self.paused = paused
        self.stopped = False
        self.position = 0
        self.pieces = []
        self.filename = None
        self.__dict__.update(details)
        self._finished = threading.Event()

    @property
    def done(self):
        return self._finished.is_set()

    def wait(self, timeout=None):
        return self._finished.wait(timeout)

    def stop(self):
        self.stopped = True
        self._finished.set()

    def finish(self):
        self._finished.set()


class FakeSpeaker:
    def __init__(self, sample_rate=48000, echo_reference=None):
        self.sample_rate = sample_rate
        self.playing = []
        self.silenced = set()
        self.closed = False

    def _start(self, playback):
        if playback.kind in self.silenced:
            playback.finish()
        self.playing.append(playback)
        return playback

    def play_file(self, filename, volume=1.0, kind="reply", cache=True, start=0.0, length=None):
        return self._start(FakePlayback(kind, volume, filename=filename, start=start, length=length))

    def play_stream(self, pieces, sample_rate=None, volume=1.0, kind="reply", paused=False, most_ahead=None):
        playback = self._start(FakePlayback(kind, volume, paused, sample_rate=sample_rate))

        def feed():
            for piece in pieces:
                if playback.done:
                    break
                playback.pieces.append(piece)
                playback.position += len(piece)
            pieces.close()
            # Played out as soon as the last of it has arrived.
            playback.finish()

        threading.Thread(target=feed, daemon=True).start()
        return playback

    def sounded_within(self, seconds):
        # Only what's playing now - the real one also counts what stopped up to so many
        # seconds ago, which needs a real output stream.
        self.lingers = seconds
        return any(not playback.done and not playback.paused for playback in self.playing)

    def files(self):
        return [playback.filename for playback in self.playing if playback.filename]

    def streams(self):
        return [playback for playback in self.playing if not playback.filename]

    def stop(self, kinds=None):
        for playback in list(self.playing):
            if (kinds is None or playback.kind in kinds) and not playback.done:
                playback.stop()

    def silence(self, kind):
        self.silenced.add(kind)
        self.stop([kind])

    def allow(self, kind):
        self.silenced.discard(kind)

    def close(self):
        self.stop()
        self.closed = True


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(satellite_link, "SOCKET_TIMEOUT", 0.05)
    monkeypatch.setattr(satellite_link, "RETRY_FIRST", 0.02)
    monkeypatch.setattr(satellite_link, "RETRY_LONGEST", 0.05)
    monkeypatch.setattr(satellite, "FALLBACK_CHECK", 0.02)
    monkeypatch.setattr(satellite, "PROGRESS_EVERY", 0.02)


@pytest.fixture
def brain(monkeypatch):
    brain = FakeBrain()
    monkeypatch.setattr(satellite_link.socket, "create_connection", brain.connect)
    yield brain
    brain.close()


@pytest.fixture
def make(tmp_path, monkeypatch, make_config):
    monkeypatch.setenv("NOVA_BACKEND_TOKEN", PASSWORD)
    # Never the real data folder: running the satellite leaves a mark there.
    monkeypatch.setattr(satellite, "RUNNING_MARK", str(tmp_path / "running"))
    monkeypatch.setattr(satellite, "Microphone", FakeMicrophone)
    monkeypatch.setattr(satellite, "Speaker", FakeSpeaker)
    monkeypatch.setattr(satellite, "WakeWordDetector", FakeWake)
    monkeypatch.setattr(satellite, "make_echo_canceller", lambda config, rate: None)
    monkeypatch.setattr(satellite, "AlarmCopy", partial(AlarmCopy, str(tmp_path / "alarm_copy.json")))
    monkeypatch.setattr(satellite, "StateCopy", partial(StateCopy, str(tmp_path / "state.json")))
    monkeypatch.setattr(satellite, "Phrase", partial(Phrase, str(tmp_path / "phrase.wav"), PHRASE))
    monkeypatch.setattr(satellite, "WakeClips", partial(WakeClips, folder=str(tmp_path / "clips")))
    running = []

    def begin(made):
        def run():
            try:
                made.run()
            except SystemExit as e:
                made.exited.append(e.code)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        running.append((made, thread))

    def make(name="bedroom", start=True, **sections):
        config = make_config(audio={"sample_rate": 48000, "pre_roll": 0.3},
                             backend={"address": "nova:10400", "satellite": name}, **sections)
        made = satellite.Satellite(config)
        made.latency_file = str(tmp_path / "latency.jsonl")
        # Filled with the exit code once run() ends.
        made.exited = []
        if start:
            begin(made)
        return made

    make.begin = begin
    yield make
    for made, thread in running:
        made.stop()
        thread.join(timeout=5)


def entry(due):
    return {"id": "alarm-1@1729587600", "due": due, "kind": "alarm", "sound": "alarm_sound.wav"}


def test_the_wake_word_sends_the_pre_roll_then_the_recording(brain, make, tmp_path):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("stop_listening")

    # Saying how much of the recording is from before the wake word - see audio.pre_roll.
    assert brain.messages("wake") == [{"type": "wake", "talked_over": False, "pre_roll": 4800}]
    assert brain.kinds().index("wake") < brain.kinds().index("audio")
    pre_roll, first, second = brain.recordings()[:3]
    assert np.array_equal(pre_roll, samples(heard_at - 4800, heard_at))
    assert np.array_equal(first, samples(heard_at, heard_at + 1600))
    assert np.array_equal(second, samples(heard_at + 1600, heard_at + 3200))

    # Timed from the wake word to the reply's first sound.
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    latency = tmp_path / "latency.jsonl"
    assert wait_for(lambda: latency.exists())
    (line,) = [json.loads(text) for text in latency.read_text().splitlines()]
    assert set(line["marks_ms"]) == {"wake", "reply_audio"}
    assert line["marks_ms"]["wake"] <= line["marks_ms"]["reply_audio"]


def test_the_recording_stops_when_the_brain_says(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("stop_listening")

    assert wait_for(lambda: made.wake.listening)
    sent = len(brain.recordings())
    time.sleep(0.1)
    assert len(brain.recordings()) == sent
    assert sum(len(piece) for piece in brain.recordings()) < 10 * RATE
    assert brain.messages("audio_end") == []


def test_the_satellite_records_for_as_long_as_the_brain_says(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, longest_request=15)
    assert wait_for(lambda: made.longest_request == 15)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"), seconds=10)

    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert sum(len(piece) for piece in brain.recordings()) == 15 * RATE
    # Each message still carries no more than the protocol allows.
    assert all(len(payload) <= protocol.MAX_AUDIO for message, payload in brain.received)


def test_the_satellites_own_most_is_thirty_seconds(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, longest_request=90)
    assert wait_for(lambda: made.longest_request == 30)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"), seconds=15)

    assert sum(len(piece) for piece in brain.recordings()) == 30 * RATE


@pytest.mark.parametrize("sent", [{}, {"longest_request": "15"}, {"longest_request": True},
                                  {"longest_request": float("nan")}, {"longest_request": float("inf")},
                                  {"longest_request": 0}, {"longest_request": 1e-9}, {"longest_request": 0.5},
                                  {"longest_request": -5}, {"longest_request": None}],
                         ids=["none", "text", "bool", "nan", "inf", "zero", "tiny", "half", "negative", "null"])
def test_an_old_brain_leaves_the_satellite_at_ten_seconds(brain, make, sent):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={"general": 0.5}, muted=True, **sent)
    assert wait_for(lambda: made.wake.muted is True)

    assert made.longest_request == 10
    # And a good one afterwards still counts, so the bad one didn't wedge the state.
    brain.say("state", volumes={}, muted=False, longest_request=12)
    assert wait_for(lambda: made.longest_request == 12)
    brain.say("state", volumes={}, muted=False, **sent)
    assert wait_for(lambda: made.wake.muted is False)
    assert made.longest_request == 12


def test_the_satellite_listens_as_surely_as_the_brain_says(brain, make, caplog):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.wake.sensitivity == 0.5
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, wake_confidence=0.75)
        assert wait_for(lambda: made.wake.sensitivity == 0.75)
        # The brain repeats its state often, and the log shouldn't fill with the same value.
        brain.say("state", volumes={}, muted=False, wake_confidence=0.75)
        brain.say("state", volumes={}, muted=True, wake_confidence=0.75)
        assert wait_for(lambda: made.wake.muted is True)
    assert len([record for record in caplog.records
                if "Listening for the wake word at 0.75 now" in record.getMessage()]) == 1

    # A state without it changes nothing: an old brain.
    brain.say("state", volumes={}, muted=False)
    assert wait_for(lambda: made.wake.muted is False)
    assert made.wake.sensitivity == 0.75


@pytest.mark.parametrize("edge", [0.05, 0.99])
def test_a_wake_confidence_at_either_end_of_the_range_is_taken(brain, make, edge):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, wake_confidence=edge)
    assert wait_for(lambda: made.wake.sensitivity == edge)


@pytest.mark.parametrize("sent", [{"wake_confidence": "0.75"}, {"wake_confidence": True},
                                  {"wake_confidence": float("nan")}, {"wake_confidence": float("inf")},
                                  {"wake_confidence": 0}, {"wake_confidence": -0.1}, {"wake_confidence": 1.5},
                                  {"wake_confidence": None}, {"wake_confidence": 0.04}, {"wake_confidence": 1}],
                         ids=["text", "bool", "nan", "inf", "zero", "negative", "over-one", "null", "below-0.05",
                              "one"])
def test_a_wake_confidence_which_makes_no_sense_is_ignored(brain, make, sent):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, wake_confidence=0.75)
    assert wait_for(lambda: made.wake.sensitivity == 0.75)
    brain.say("state", volumes={}, muted=True, **sent)
    assert wait_for(lambda: made.wake.muted is True)

    assert made.wake.sensitivity == 0.75
    # And a good one afterwards still counts, so the bad one didn't wedge the state.
    brain.say("state", volumes={}, muted=False, wake_confidence=0.6)
    assert wait_for(lambda: made.wake.sensitivity == 0.6)


def test_the_satellite_checks_the_voice_as_surely_as_the_brain_says(brain, make, caplog):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.wake.voice_check_threshold == 0.2
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, voice_check=0.35)
        assert wait_for(lambda: made.wake.voice_check_threshold == 0.35)
        # The brain repeats its state often, and the log shouldn't fill with the same value.
        brain.say("state", volumes={}, muted=False, voice_check=0.35)
        brain.say("state", volumes={}, muted=True, voice_check=0.35)
        assert wait_for(lambda: made.wake.muted is True)
    assert len([record for record in caplog.records
                if "Checking it's your voice at 0.35 now" in record.getMessage()]) == 1

    # A state without it changes nothing: an old brain.
    brain.say("state", volumes={}, muted=False)
    assert wait_for(lambda: made.wake.muted is False)
    assert made.wake.voice_check_threshold == 0.35


@pytest.mark.parametrize("edge", [0, 0.0, 0.99, 0.5], ids=["int-zero", "zero", "top", "half"])
def test_a_voice_check_from_0_to_0_99_is_taken(brain, make, edge):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, voice_check=edge)
    assert wait_for(lambda: made.wake.voice_check_threshold == edge)


def test_a_voice_check_of_0_says_its_off(brain, make, caplog):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, voice_check=0)
        assert wait_for(lambda: made.wake.voice_check_threshold == 0)
    assert any("Not checking it's your voice now" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("sent", [{"voice_check": "0.3"}, {"voice_check": True}, {"voice_check": False},
                                  {"voice_check": float("nan")}, {"voice_check": float("inf")},
                                  {"voice_check": -0.1}, {"voice_check": 1}, {"voice_check": 1.5},
                                  {"voice_check": None}],
                         ids=["text", "true", "false", "nan", "inf", "negative", "one", "over-one", "null"])
def test_a_voice_check_which_makes_no_sense_is_ignored(brain, make, sent):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, voice_check=0.4)
    assert wait_for(lambda: made.wake.voice_check_threshold == 0.4)
    brain.say("state", volumes={}, muted=True, **sent)
    assert wait_for(lambda: made.wake.muted is True)

    assert made.wake.voice_check_threshold == 0.4
    # And a good one afterwards still counts, so the bad one didn't wedge the state.
    brain.say("state", volumes={}, muted=False, voice_check=0.3)
    assert wait_for(lambda: made.wake.voice_check_threshold == 0.3)


def test_before_a_check_is_trained_its_setting_says_so(brain, make, caplog):
    # Not that a check is in force, which would mislead the first look at the log.
    made = make()
    made.wake.voice_check = None
    assert wait_for(lambda: connected(made) and made.wake.listening)
    with caplog.at_level("INFO", logger="satellite"):
        brain.say("state", volumes={}, muted=False, voice_check=0.35)
        assert wait_for(lambda: made.wake.voice_check_threshold == 0.35)
    said = [record.getMessage() for record in caplog.records if "your voice" in record.getMessage()]
    assert said == ["Checking it's your voice at 0.35 once a voice check's been trained"]


def test_the_voice_check_setting_lasts_through_a_restart_while_the_brain_is_away(brain, make, tmp_path):
    # 0 is the way out if the check stops the user, and the brain being away is when the
    # wake word alone dismisses an alarm.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, voice_check=0)
    assert wait_for(lambda: made.wake.voice_check_threshold == 0)
    assert StateCopy(str(tmp_path / "state.json")).voice_check == 0

    brain.up = False
    restarted = make(start=False)
    assert restarted.wake.voice_check_threshold == 0


def test_whats_playing_is_told_to_the_wake_word(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.wake.playing() is False
    # Over the voice check's window, and a little more, after it has stopped.
    assert made.speaker.lingers == satellite.OWN_SOUND_LINGERS

    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made.speaker.streams())
    reply = made.speaker.streams()[0]
    assert made.wake.playing() is True
    reply.finish()
    assert made.wake.playing() is False

    # Paused music makes no sound.
    brain.say("play_stream", id="song-1", rate=44100, channels=2, volume=1.0, kind="music", paused=True)
    assert wait_for(lambda: len(made.speaker.streams()) == 2)
    assert made.wake.playing() is False


def test_an_alarm_ringing_from_the_copy_counts_as_its_own_sound(brain, make, tmp_path):
    # With the brain away, the wake word is the only way to dismiss it - so it's known
    # from the ringer itself, as well as from the speaker.
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)
    made.speaker.sounded_within = lambda seconds: False
    assert made.wake.playing() is True


def test_whats_playing_is_told_without_waiting_for_the_satellites_lock(brain, make):
    # The lock is held while the brain is sent to, which can take seconds - and the
    # wake word is asked on the listening thread, with the user waiting.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    told = []
    with made._lock:
        asking = threading.Thread(target=lambda: told.append(made.wake.playing()), daemon=True)
        asking.start()
        asking.join(timeout=1)
        assert told == [False]


def test_an_old_satellite_ignores_the_longest_request(tmp_path):
    copy = StateCopy(str(tmp_path / "state.json"))
    copy.update({"volumes": {"general": 0.5}, "muted": True, "longest_request": 20})

    assert copy.volume("general") == 0.5 and copy.muted is True
    assert StateCopy(str(tmp_path / "state.json")).volume("general") == 0.5


def test_the_recording_stops_after_10_seconds_until_the_brain_says_otherwise(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))

    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert sum(len(piece) for piece in brain.recordings()) == 10 * RATE
    assert wait_for(lambda: made.wake.listening)
    time.sleep(0.1)
    assert brain.kinds()[-1] == "audio_end"


def test_a_dropped_connection_mid_recording_says_the_phrase(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.drop()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    (phrase,) = [playback for playback in made.speaker.playing if playback.filename == PHRASE]
    assert phrase.kind == "reply"
    # Nothing else will let the alarms ring again now.
    assert "announcement" not in made.speaker.silenced
    assert wait_for(lambda: made.wake.listening)


def noting_when_the_brain_is_forgotten(made):
    """An Event set once the satellite has done all it does on losing the brain."""
    forgotten = threading.Event()
    forget = made._forget_the_brain

    def forget_and_note():
        forget()
        forgotten.set()

    made._forget_the_brain = forget_and_note
    return forgotten


def test_losing_the_brain_after_the_recording_says_the_phrase(brain, make):
    # The brain had heard the user, and went before it answered - which was silence.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)
    brain.drop()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    assert "announcement" not in made.speaker.silenced


def test_losing_the_brain_once_it_has_answered_says_nothing(brain, make):
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    # The end of the turn, which lets back what the wake word silenced.
    brain.say("allow", kind="announcement")
    assert wait_for(lambda: made.wake.listening and "announcement" not in made.speaker.silenced)
    brain.drop()

    assert forgotten.wait(5)
    assert PHRASE not in made.speaker.files()


def test_a_turn_the_brain_ended_as_the_recording_hit_its_limit_stays_ended(brain, make):
    # The brain says the user has stopped, and ends its turn with nothing to say, all
    # while the last piece before the limit is being sent - so the recording's own end
    # comes after the brain's.
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    send = made.link.send
    limit = round(made.longest_request * RATE)
    sent = 0

    def send_ending_the_turn_first(kind, payload=b"", /, **fields):
        nonlocal sent
        if kind == "audio":
            sent += len(payload) // 2
            if sent >= limit:
                brain.say("stop_listening")
                brain.say("allow", kind="announcement")
                assert wait_for(lambda: "announcement" not in made.speaker.silenced)
        return send(kind, payload, **fields)

    made.link.send = send_ending_the_turn_first
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))
    # Ended at the limit, so it's that branch which left the brain's turn alone.
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert wait_for(lambda: made.wake.listening)
    brain.drop()

    assert forgotten.wait(5)
    assert PHRASE not in made.speaker.files()


def test_a_recording_cut_off_at_its_limit_waits_for_the_brains_answer(brain, make):
    # Nobody said the user had stopped, so the turn is the satellite's to open - and the
    # brain going before it answers is silence, which gets the phrase.
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))
    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert wait_for(lambda: made.wake.listening)
    brain.drop()

    assert forgotten.wait(5)
    assert PHRASE in made.speaker.files()


def test_losing_the_brain_during_a_follow_up_says_the_phrase_once(brain, make):
    made = make()
    forgotten = noting_when_the_brain_is_forgotten(made)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)
    # A question, and the answer to it on its way.
    sent = len(brain.recordings())
    brain.say("listen")
    assert wait_for(lambda: len(brain.recordings()) >= sent + 2)
    brain.drop()

    assert forgotten.wait(5)
    assert wait_for(lambda: PHRASE in made.speaker.files() and made.wake.listening)
    assert made.speaker.files().count(PHRASE) == 1


def test_the_wake_word_over_a_reply_stops_it_and_says_so(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    assert wait_for(lambda: len(made.speaker.streams()) == 2 and made.speaker.streams()[1].pieces)
    music, reply = made.speaker.streams()

    made.wake.say()
    assert wait_for(lambda: brain.messages("played"))
    assert reply.stopped
    assert music.paused and not music.done
    assert "announcement" in made.speaker.silenced
    assert brain.messages("wake") == [{"type": "wake", "talked_over": True, "pre_roll": 4800}]
    # The brain hears of the wake before the end of the reply it stopped.
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    assert brain.messages("played") == [{"type": "played", "id": "reply-1", "stopped": True, "position": 3}]


def test_the_wake_word_stops_whats_playing_without_waiting_for_the_satellites_lock(brain, make):
    # The lock is held while the brain is sent to, which takes up to 2 seconds on a full
    # socket - and the reply, what rang and the music went on all that while.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.5, kind="music", paused=False)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    brain.say("play_sound", id="alarm-1", name="alarm_sound.wav", volume=1.0, kind="announcement",
              start=0.0, length=None)
    assert wait_for(lambda: len(made.speaker.streams()) == 2 and made.speaker.streams()[1].pieces
                    and made.speaker.files())
    music, reply = made.speaker.streams()
    (alarm,) = [playback for playback in made.speaker.playing if playback.filename]

    with made._lock:
        made.wake.say()
        assert wait_for(lambda: reply.stopped and alarm.stopped and music.paused, seconds=1)
        assert "announcement" in made.speaker.silenced

    # The brain still hears of the wake before the end of what it stopped.
    assert wait_for(lambda: len(brain.messages("played")) == 2)
    assert brain.messages("wake") == [{"type": "wake", "talked_over": True, "pre_roll": 4800}]
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    brain.say("stop_listening")


def test_what_the_wake_word_stops_is_told_after_the_wake_however_long_stopping_takes(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=0.8, kind="reply", paused=False)
    brain.say("stream_audio", np.array([1, 2, 3], np.int16).tobytes(), id="reply-1")
    assert wait_for(lambda: made.speaker.streams() and made.speaker.streams()[0].pieces)
    silence = made.speaker.silence

    def slowly(kind):
        silence(kind)
        # Long enough for the end of the reply, stopped just before, to be told first,
        # were nothing holding it back.
        time.sleep(0.2)

    made.speaker.silence = slowly
    made.wake.say()

    assert wait_for(lambda: brain.messages("played"))
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    brain.say("stop_listening")


def test_the_brain_asking_to_hear_an_answer_during_a_wake_sends_nothing_more(brain, make):
    # The brain finished asking a question as the wake word was said, so asked to hear
    # the answer while the wake's recording was being sent. That recording is all it
    # gets: sent again as a follow-up, it was 10 seconds of nobody talking, while the
    # wake word went unheard.
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("listen")
    brain.say("stop_listening")

    assert wait_for(lambda: made.wake.listening)
    sent = len(brain.recordings())
    time.sleep(0.1)
    assert len(brain.recordings()) == sent
    assert brain.messages("audio_end") == []


def test_a_wake_while_streaming_starts_nothing_new(brain, make):
    made = make()
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    assert not made.wake.listening
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 5)
    brain.say("stop_listening")
    assert wait_for(lambda: made.wake.listening)
    time.sleep(0.1)

    assert len(brain.messages("wake")) == 1
    # One recording, unbroken.
    recorded = np.concatenate(brain.recordings())
    assert np.array_equal(recorded, samples(heard_at - 4800, heard_at - 4800 + len(recorded)))


def test_a_stream_from_the_brain_plays_and_is_reported_done(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos)
    stereo = np.array([[1000, -1000], [2000, -2000], [3000, -3000], [4000, -4000]], np.int16)
    brain.say("play_stream", id="reply-1", rate=22050, channels=2, volume=0.5, kind="reply", paused=False)
    brain.say("stream_audio", stereo[:2].tobytes(), id="reply-1")
    brain.say("stream_audio", stereo[2:].tobytes(), id="reply-1")
    assert wait_for(lambda: made.speaker.streams() and len(made.speaker.streams()[0].pieces) == 2)
    reply = made.speaker.streams()[0]
    assert (reply.kind, reply.volume, reply.sample_rate, reply.paused) == ("reply", 0.5, 22050, False)
    assert reply.pieces[0].dtype == np.float32 and reply.pieces[0].shape == (2, 2)
    assert np.allclose(np.concatenate(reply.pieces), stereo / 32768)

    # It plays on until the brain says that's the end of it.
    time.sleep(0.05)
    assert not reply.done and brain.messages("played") == []
    brain.say("stream_end", id="reply-1")
    assert wait_for(lambda: brain.messages("played"))
    assert brain.messages("played") == [{"type": "played", "id": "reply-1", "stopped": False, "position": 4}]

    # A sound, by name, from the satellite's own.
    brain.say("play_sound", id="chime-1", name="listening.wav", volume=0.4, kind="reply", start=0.0, length=None)
    assert wait_for(lambda: made.speaker.files())
    (chime,) = [playback for playback in made.speaker.playing if playback.filename]
    assert (chime.filename, chime.volume) == (os.path.join(paths.SOUNDS_DIR, "listening.wav"), 0.4)
    chime.finish()
    assert wait_for(lambda: len(brain.messages("played")) == 2)
    assert brain.messages("played")[1] == {"type": "played", "id": "chime-1", "stopped": False, "position": 0}

    # Music says how far it's got as it plays, and is paused, turned down and stopped
    # as the brain says.
    brain.say("play_stream", id="music-1", rate=44100, channels=2, volume=0.6, kind="music", paused=False)
    assert wait_for(lambda: len(brain.messages("progress")) >= 2)
    assert brain.messages("progress")[0] == {"type": "progress", "id": "music-1", "position": 0}
    music = made.speaker.streams()[1]
    brain.say("set_playback", id="music-1", paused=True, volume=0.2)
    assert wait_for(lambda: music.paused and music.volume == 0.2)
    brain.say("stop", kinds=["music"])
    assert wait_for(lambda: len(brain.messages("played")) == 3)
    assert brain.messages("played")[2] == {"type": "played", "id": "music-1", "stopped": True, "position": 0}

    # One which has ended is the satellite's to forget - the brain's word on it is ignored.
    brain.say("set_playback", id="music-1", paused=False)
    time.sleep(0.05)
    assert music.paused


def test_the_brain_can_stop_one_playback_and_leave_the_rest_of_its_kind(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos)
    brain.say("play_sound", id="beep-1", name="volume_beep.wav", volume=0.5, kind="reply", start=0.0, length=None)
    brain.say("play_stream", id="reply-1", rate=22050, channels=1, volume=1.0, kind="reply", paused=False)
    assert wait_for(lambda: made.speaker.files() and made.speaker.streams())
    (beep,) = [playback for playback in made.speaker.playing if playback.filename]
    (reply,) = made.speaker.streams()

    brain.say("stop", id="beep-1")
    assert wait_for(lambda: brain.messages("played"))
    assert brain.messages("played") == [{"type": "played", "id": "beep-1", "stopped": True, "position": 0}]
    assert beep.stopped
    assert not reply.done

    # One which has already ended, or never played, is left as it is.
    brain.say("stop", id="beep-1")
    brain.say("stop", id="never-played")
    time.sleep(0.05)
    assert not reply.done
    assert len(brain.messages("played")) == 1


def test_with_no_brain_the_wake_word_gets_the_phrase(brain, make):
    brain.up = False
    made = make()
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    assert brain.hellos == []
    assert "announcement" not in made.speaker.silenced


def test_with_no_brain_due_alarms_ring_and_the_wake_word_dismisses_them(brain, make, tmp_path):
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    alarm = os.path.join(paths.SOUNDS_DIR, "alarm_sound.wav")
    assert wait_for(lambda: alarm in made.speaker.files())
    assert made.ringer.ringing
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()

    # Listening again, as the satellite does once it has done all the wake word does -
    # it lets the announcements back only after the ringing has stopped.
    assert wait_for(lambda: not made.ringer.ringing and made.wake.listening)
    assert made.alarms.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": ["alarm-1@1729587600"]}
    # Dismissing is all the wake word does.
    assert PHRASE not in made.speaker.files()
    assert "announcement" not in made.speaker.silenced


def test_the_away_report_is_sent_on_connecting(brain, make, tmp_path):
    brain.up = False
    # The last run never stopped properly.
    (tmp_path / "running").write_text("{}")
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)

    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    # Not dismissed, and still ringing - so the brain takes the ringing over.
    assert brain.messages("away") == [{"type": "away", "rang": ["alarm-1@1729587600"], "dismissed": [],
                                       "ringing": ["alarm-1@1729587600"]}]
    assert brain.hellos == [{"name": "bedroom", "hostname": socket.gethostname(), "sample_rate": 48000,
                             "unclean": True, "speaker": None}]


def test_the_satellite_tells_the_brain_its_spotify_speaker(brain, make):
    # The name its setup gave raspotify, from its own settings - so the brain plays
    # its Spotify there, rather than on the brain's own speaker name.
    make(spotify={"device_name": "Bedroom Pi"})
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["speaker"] == "Bedroom Pi"


@pytest.mark.parametrize("device_name", [datetime.date(2026, 10, 26), {"Bedroom Pi"}, 7, "x" * 65],
                         ids=["date", "set", "number", "too-long"])
def test_a_speaker_the_brain_cant_take_is_left_out(brain, make, caplog, device_name):
    # A hand edit such as an unquoted device_name: 2026-10-26, which YAML reads as a
    # date - which would stop the hello being sent at all, and so stop every connect.
    with caplog.at_level("WARNING", logger="satellite"):
        made = make(spotify={"device_name": device_name})
    assert wait_for(lambda: brain.hellos)

    assert brain.hellos[0]["speaker"] is None
    assert made.spotify_speaker is None
    assert len([record for record in caplog.records if "device_name" in record.getMessage()]) == 1
    # Up to 64 characters is sent as it is.
    assert make(name="kitchen", spotify={"device_name": "x" * 64}).spotify_speaker == "x" * 64


def ringing_as_the_brain_comes_back(brain, make, tmp_path):
    """
    A satellite ringing an alarm from its copy as it reaches the brain again, once it
    has told the brain so and done all it does on connecting.
    """
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)
    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    # It tells the brain under the lock, so has finished connecting once it's free.
    with made._lock:
        pass
    return made


def test_whats_ringing_is_only_handed_over_once_the_brain_says_it_has_it(brain, make, tmp_path):
    # Quietened and forgotten as soon as the brain had been told, an alarm stopped early
    # if the brain was lost before it had saved what it was told - as the next brain
    # never heard it was still ringing.
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    assert made.ringer.ringing
    assert made.alarms.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": []}

    brain.say("away_settled")
    assert wait_for(lambda: not made.ringer.ringing)
    assert made.alarms.away_report() == {"rang": [], "dismissed": []}


def test_only_what_the_brain_was_told_is_forgotten_once_it_has_it(brain, make, tmp_path):
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    # Dismissed before the brain says it has it, so after the brain was told.
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: made.alarms.away_report()["dismissed"])

    brain.say("away_settled")
    assert wait_for(lambda: not made.alarms.away_report()["rang"])
    assert made.alarms.away_report() == {"rang": [], "dismissed": ["alarm-1@1729587600"]}


def test_a_brain_lost_before_it_says_it_has_it_leaves_the_satellite_ringing(brain, make, tmp_path):
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    told = brain.messages("away")[0]
    brain.drop()
    assert wait_for(lambda: not made.link.connected)

    # As while the brain is away - so the next brain is told it all again.
    assert made.ringer.ringing
    assert made.alarms.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": []}
    brain.up = True
    assert wait_for(lambda: len(brain.messages("away")) == 2)
    assert brain.messages("away")[1] == told


def test_a_brain_which_never_says_it_has_it_is_given_up_on(brain, make, tmp_path, monkeypatch):
    # One whose wait for the report ran out before it arrived never applies it, and
    # one stuck part way through still answers pings - so neither is noticed otherwise.
    monkeypatch.setattr(satellite, "SETTLE_WAIT", 0.2)
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    told = brain.messages("away")[0]

    # Dropped, and connected again, ringing on meanwhile - and the brain told it all again.
    assert wait_for(lambda: len(brain.messages("away")) >= 2)
    assert len(brain.hellos) >= 2
    assert made.ringer.ringing
    assert brain.messages("away")[1] == told


def test_what_the_brain_took_over_rings_from_the_copy_again_if_the_brain_goes(brain, make, tmp_path):
    # The brain was ringing it too, so its copy still lists it - which kept it marked as
    # rung, and a brain lost while it rang it left it silent on both sides.
    made = ringing_as_the_brain_comes_back(brain, make, tmp_path)
    brain.say("alarms", entries=[entry(time.time() - 1)])
    brain.say("away_settled")
    assert wait_for(lambda: not made.ringer.ringing)

    brain.drop()
    assert wait_for(lambda: made.ringer.ringing_ids() == ["alarm-1@1729587600"])


def test_a_satellite_restarted_while_ringing_rings_on_and_says_so(brain, make, tmp_path):
    # Its watchdog, a crash or the power, with the brain away. It knew only that the
    # alarm had rung, so rang it no more - and the brain, told just that, neither rang
    # it nor said it had been missed.
    brain.up = False
    AlarmCopy(str(tmp_path / "alarm_copy.json")).replace([entry(time.time() - 1)])
    made = make()
    assert wait_for(lambda: made.ringer.ringing)
    made.stop()
    assert wait_for(lambda: made.exited)

    again = make()
    assert wait_for(lambda: again.ringer.ringing_ids() == ["alarm-1@1729587600"])
    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    assert brain.messages("away")[0]["ringing"] == ["alarm-1@1729587600"]


def test_a_satellite_restarted_once_its_ring_would_have_ended_doesnt_ring_again(brain, make, tmp_path):
    brain.up = False
    due = time.time() - fallback.RING_LIMIT - 1
    copy = AlarmCopy(str(tmp_path / "alarm_copy.json"))
    copy.replace([entry(due)])
    # Left going as the satellite stopped, that long ago.
    copy.rang(entry(due), due)

    made = make()
    brain.up = True
    assert wait_for(lambda: brain.messages("away"))
    assert brain.messages("away")[0]["ringing"] == [] and not made.ringer.ringing


def test_mute_from_the_brain_stops_the_wake_word(brain, make, tmp_path):
    StateCopy(str(tmp_path / "state.json")).update({"volumes": {}, "muted": True})
    made = make(start=False)
    # Muted from the start, before there's any brain to say so.
    assert made.wake.muted is True
    make.begin(made)
    assert wait_for(lambda: brain.hellos)

    brain.say("state", volumes={"general": 0.5, "alarm": 0.6, "music": 0.7}, muted=False)
    assert wait_for(lambda: made.wake.muted is False)
    brain.say("state", volumes={"general": 0.5, "alarm": 0.6, "music": 0.7}, muted=True)
    assert wait_for(lambda: made.wake.muted is True)
    assert StateCopy(str(tmp_path / "state.json")).volume("general") == 0.5

    assert wait_for(lambda: made.wake.listening)
    made.wake.say()
    time.sleep(0.1)
    assert brain.messages("wake") == []


class FakeClips:
    """Keeps what it's handed - or, once told to, raises as it's handed it."""

    def __init__(self, microphone, muted, **settings):
        self.microphone = microphone
        self.muted = muted
        self.settings = settings
        self.kept = []
        self.failure = None

    def keep(self, heard_at, score):
        self.kept.append((heard_at, score))
        if self.failure is not None:
            raise self.failure


def test_a_wake_hands_its_position_and_score_over_to_be_kept(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 7})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: made.clips.kept)
    brain.say("stop_listening")

    assert made.clips.kept == [(heard_at, 0.9)]
    assert made.clips.microphone is made.microphone
    assert made.clips.settings == {"most": 7}


@pytest.mark.parametrize("sections", [{}, {"wake_clips": {"keep": False, "most": 400}}])
def test_with_clips_switched_off_nothing_is_kept(brain, make, tmp_path, sections):
    made = make(**sections)
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.recordings())
    brain.say("stop_listening")
    # Long enough for a clip to have been finished, at the fake microphone's speed.
    time.sleep(0.2)

    assert made.clips is None
    assert not (tmp_path / "clips").exists()


def test_a_wake_keeps_a_clip_with_nothing_but_those_seconds_in_it(brain, make, tmp_path):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    # Past the first two seconds, so there's all of a clip to keep.
    assert wait_for(lambda: made.microphone.position > 3 * RATE)
    heard_at = made.wake.say()
    assert wait_for(lambda: (tmp_path / "clips").exists() and os.listdir(tmp_path / "clips"))
    brain.say("stop_listening")
    assert wait_for(lambda: not made.clips._keeping.locked())

    (name,) = os.listdir(tmp_path / "clips")
    assert name.endswith("-0.90.wav")
    with wave.open(str(tmp_path / "clips" / name), "rb") as file:
        audio = np.frombuffer(file.readframes(file.getnframes()), np.int16)
    assert np.array_equal(audio, samples(heard_at - 2 * RATE, heard_at + RATE))


def test_a_muted_satellite_keeps_nothing(brain, make, tmp_path):
    StateCopy(str(tmp_path / "state.json")).update({"volumes": {}, "muted": True})
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: made.wake.listening)
    made.wake.say()
    time.sleep(0.2)

    assert not (tmp_path / "clips").exists()


@pytest.fixture
def real_time(monkeypatch):
    """Has the fake microphone hear at the speed the real one does, so a second lasts one."""
    monkeypatch.setitem(globals(), "SPEED", 1)


def wake_clip_files(tmp_path):
    return [name for name in os.listdir(tmp_path / "clips") if name.endswith(".wav")]


def set_mute(brain, made, muted):
    brain.say("state", volumes={}, muted=muted)
    assert wait_for(lambda: made.wake.muted is muted)


def test_a_wake_just_after_unmuting_keeps_nothing_and_goes_on_as_usual(brain, make, monkeypatch, caplog, real_time):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    set_mute(brain, made, True)
    set_mute(brain, made, False)
    with caplog.at_level(logging.INFO, logger="satellite"):
        made.wake.say()
        assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")

    assert made.clips.kept == []
    assert "Wake clip not kept, muted" in [record.getMessage() for record in caplog.records]


def test_a_wake_long_enough_after_unmuting_is_kept(brain, make, monkeypatch):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    set_mute(brain, made, True)
    set_mute(brain, made, False)
    unmuted_at = made.microphone.position
    # The 2 seconds before the wake, the margin an unmute is given, and a moment.
    assert wait_for(lambda: made.microphone.position > unmuted_at + 2 * RATE + 4800 + RATE // 10)
    heard_at = made.wake.say()
    assert wait_for(lambda: made.clips.kept)
    brain.say("stop_listening")

    assert made.clips.kept == [(heard_at, 0.9)]


def test_an_unmute_is_noted_0_3_s_late_for_the_audio_still_on_its_way(brain, make, real_time):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    set_mute(brain, made, True)
    before = made.microphone.position
    set_mute(brain, made, False)
    after = made.microphone.position

    assert before + 4800 <= made._unmuted_at <= after + 4800


def test_what_counts_as_muted_ends_at_the_position_of_the_unmute(brain, make):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    assert made.clips.muted(0) is False
    set_mute(brain, made, True)
    assert made.clips.muted(made.microphone.position + RATE) is True
    set_mute(brain, made, False)
    unmuted_at = made._unmuted_at

    assert unmuted_at > 0
    assert made.clips.muted(unmuted_at - 1) is True
    assert made.clips.muted(unmuted_at) is False


def test_a_state_which_doesnt_unmute_moves_nothing(brain, make):
    # The brain sends its state whole, for a volume as much as for a mute.
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    brain.say("state", volumes={}, muted=False, longest_request=12)
    assert wait_for(lambda: made.longest_request == 12)
    assert made._unmuted_at == 0

    set_mute(brain, made, True)
    set_mute(brain, made, False)
    unmuted_at = made._unmuted_at
    brain.say("state", volumes={}, muted=False, longest_request=13)
    assert wait_for(lambda: made.longest_request == 13)

    assert made._unmuted_at == unmuted_at


def test_a_mute_in_the_second_after_a_wake_keeps_nothing(brain, make, tmp_path, real_time):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: made.clips._keeping.locked())
    set_mute(brain, made, True)
    assert wait_for(lambda: not made.clips._keeping.locked())
    brain.say("stop_listening")

    assert wake_clip_files(tmp_path) == []


def test_a_mute_whose_message_lands_just_after_the_clip_was_heard_keeps_nothing(brain, make, tmp_path, real_time):
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: made.microphone.position >= heard_at + RATE + RATE // 10)
    set_mute(brain, made, True)
    assert made.clips._keeping.locked()
    assert wait_for(lambda: not made.clips._keeping.locked())
    brain.say("stop_listening")

    assert wake_clip_files(tmp_path) == []


def test_a_satellite_keeping_clips_says_so_as_it_starts(brain, make, tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="voice_assistant.wake_clips"):
        make(start=False, wake_clips={"keep": True, "most": 400})

    assert str(tmp_path / "clips") in caplog.text


def test_a_satellite_not_keeping_clips_says_nothing_of_them(brain, make, caplog):
    with caplog.at_level(logging.INFO, logger="voice_assistant.wake_clips"):
        make(start=False)

    assert caplog.records == []


def test_a_mute_and_unmute_both_in_the_second_after_a_wake_keep_nothing(brain, make, tmp_path, real_time):
    # Unmuted again by the time the clip looks, so asking whether it's muted now isn't enough.
    made = make(wake_clips={"keep": True, "most": 400})
    assert wait_for(lambda: connected(made) and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: made.clips._keeping.locked())
    set_mute(brain, made, True)
    set_mute(brain, made, False)
    assert made.clips._keeping.locked()
    assert wait_for(lambda: not made.clips._keeping.locked())
    brain.say("stop_listening")

    assert wake_clip_files(tmp_path) == []


def test_clips_which_raise_leave_the_wake_going(brain, make, monkeypatch, caplog):
    monkeypatch.setattr(satellite, "WakeClips", FakeClips)
    made = make(wake_clips={"keep": True, "most": 400})
    made.clips.failure = RuntimeError("Nothing can be kept")
    assert wait_for(lambda: connected(made) and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.say("stop_listening")

    assert brain.messages("wake") == [{"type": "wake", "talked_over": False, "pre_roll": 4800}]
    assert np.array_equal(brain.recordings()[0], samples(heard_at - 4800, heard_at))
    assert "Nothing can be kept" in caplog.text


def test_a_restart_from_the_brain_exits_for_systemd_to_start_it_again(brain, make, tmp_path):
    made = make(name="")
    assert wait_for(lambda: brain.hellos)
    assert brain.hellos[0]["name"] == socket.gethostname()
    brain.say("restart")

    assert wait_for(lambda: made.exited)
    assert made.exited == [1]
    # Asked to, so it stopped properly.
    assert not (tmp_path / "running").exists()


def test_a_restart_from_the_brain_is_heard_while_listening_keeps_failing(brain, make):
    # Which is when Home Assistant's Restart is needed most - and it was only looked
    # for once listening had worked.
    made = make(start=False)

    def broken(until=None):
        raise RuntimeError("The microphone has gone")

    made.wake.listen_once = broken
    make.begin(made)
    assert wait_for(lambda: brain.hellos)
    brain.say("restart")

    assert wait_for(lambda: made.exited)
    assert made.exited == [1]
