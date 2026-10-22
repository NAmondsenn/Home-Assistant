"""
Satellite Test
Checks the Pi's own program - hearing the wake word, sending what's said, and playing
and stopping what the brain sends - against a fake brain over a socketpair, with
fakes in place of the microphone, speaker and wake word, so nothing is opened or
loaded. Needs numpy installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import json
import os
import queue
import socket
import threading
import time
from functools import partial

import pytest

np = pytest.importorskip("numpy")

import satellite
from voice_assistant import paths, protocol, satellite_link
from voice_assistant.fallback import AlarmCopy, Phrase, StateCopy

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

    def __init__(self, config=None, microphone=None):
        self.microphone = microphone
        self.muted = False
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

    def make(name="bedroom", start=True):
        config = make_config(audio={"sample_rate": 48000, "pre_roll": 0.3},
                             backend={"address": "nova:10400", "satellite": name})
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
    assert wait_for(lambda: brain.hellos and made.wake.listening)
    heard_at = made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("stop_listening")

    assert brain.messages("wake") == [{"type": "wake", "talked_over": False}]
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
    assert wait_for(lambda: brain.hellos and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 3)
    brain.say("stop_listening")

    assert wait_for(lambda: made.wake.listening)
    sent = len(brain.recordings())
    time.sleep(0.1)
    assert len(brain.recordings()) == sent
    assert sum(len(piece) for piece in brain.recordings()) < 10 * RATE
    assert brain.messages("audio_end") == []


def test_the_recording_stops_after_10_seconds_whatever_the_brain_says(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: brain.messages("audio_end"))

    assert brain.messages("audio_end") == [{"type": "audio_end", "reason": "limit"}]
    assert sum(len(piece) for piece in brain.recordings()) == 10 * RATE
    assert wait_for(lambda: made.wake.listening)
    time.sleep(0.1)
    assert brain.kinds()[-1] == "audio_end"


def test_a_dropped_connection_mid_recording_says_the_phrase(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos and made.wake.listening)
    made.wake.say()
    assert wait_for(lambda: len(brain.recordings()) >= 2)
    brain.drop()

    assert wait_for(lambda: PHRASE in made.speaker.files())
    (phrase,) = [playback for playback in made.speaker.playing if playback.filename == PHRASE]
    assert phrase.kind == "reply"
    # Nothing else will let the alarms ring again now.
    assert "announcement" not in made.speaker.silenced
    assert wait_for(lambda: made.wake.listening)


def test_the_wake_word_over_a_reply_stops_it_and_says_so(brain, make):
    made = make()
    assert wait_for(lambda: brain.hellos and made.wake.listening)
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
    assert brain.messages("wake") == [{"type": "wake", "talked_over": True}]
    # The brain hears of the wake before the end of the reply it stopped.
    assert brain.kinds().index("wake") < brain.kinds().index("played")
    assert brain.messages("played") == [{"type": "played", "id": "reply-1", "stopped": True, "position": 3}]


def test_the_brain_asking_to_hear_an_answer_during_a_wake_sends_nothing_more(brain, make):
    # The brain finished asking a question as the wake word was said, so asked to hear
    # the answer while the wake's recording was being sent. That recording is all it
    # gets: sent again as a follow-up, it was 10 seconds of nobody talking, while the
    # wake word went unheard.
    made = make()
    assert wait_for(lambda: brain.hellos and made.wake.listening)
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
    assert wait_for(lambda: brain.hellos and made.wake.listening)
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

    assert wait_for(lambda: not made.ringer.ringing)
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
    # Not dismissed, but rung - so the brain takes the ringing over.
    assert brain.messages("away") == [{"type": "away", "rang": ["alarm-1@1729587600"], "dismissed": []}]
    assert not made.ringer.ringing
    assert made.alarms.away_report() == {"rang": [], "dismissed": []}
    assert brain.hellos == [{"name": "bedroom", "hostname": socket.gethostname(), "sample_rate": 48000,
                             "unclean": True}]


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
