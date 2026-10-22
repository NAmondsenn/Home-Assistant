"""
End-to-End Test
Checks a satellite and the brain together - the real Satellite, Brain and Session,
over a real connection on this machine - with stand-ins only for what can't be had
here: the Pi's microphone, speaker and wake word, and the brain's Nemotron, Piper and
Claude. Each side's own tests check it against a fake of the other; these check that
the two really agree. Needs numpy and soxr installed (as they are on the Pi), and is
skipped anywhere they aren't.
"""

import queue
import threading
import time
from functools import partial
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

import satellite as satellite_module
from voice_assistant import brain as brain_module
from voice_assistant import clock as clock_module
from voice_assistant import memory as memory_module
from voice_assistant import paths, satellite_link
from voice_assistant import routines as routines_module
from voice_assistant import session as session_module
from voice_assistant.fallback import AlarmCopy, Phrase, StateCopy
from voice_assistant.satellite_passwords import SatellitePasswords
from voice_assistant.vad import LoudnessDetector

PASSWORD = "the-bedroom-satellites-password"
RATE = 16000
# The microphone hears this many times faster than real time, so a 10 second recording
# takes two and a half.
SPEED = 4
# How long one of the satellite's own sounds plays for, in real seconds.
SOUND_SECONDS = 0.05


def wait_for(condition, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


# The Pi's devices

class Microphone:
    """Hears silence but for the user talking, when the test says - SPEED times faster than real time."""

    def __init__(self, sample_rate=48000, echo=None):
        self.started = time.monotonic()
        self.lock = threading.Lock()
        # Where the user talked, as (start, end) positions.
        self.said = []

    @property
    def position(self):
        return RATE + int((time.monotonic() - self.started) * RATE * SPEED)

    def say(self, seconds, after=0.0):
        """The user talks for this long, starting this long from now."""
        start = self.position + round(after * RATE)
        with self.lock:
            self.said.append((start, start + round(seconds * RATE)))

    def heard(self, start, end):
        audio = np.zeros(end - start, np.int16)
        with self.lock:
            said = list(self.said)
        for begin, finish in said:
            if max(begin, start) < min(finish, end):
                audio[max(begin, start) - start:min(finish, end) - start] = 8000
        return audio

    def listen(self, start=None):
        return Reader(self, self.position if start is None else start)

    def close(self):
        pass


class Reader:
    def __init__(self, microphone, position):
        self.microphone = microphone
        self.position = position

    def read(self, count):
        while self.microphone.position < self.position + count:
            time.sleep(0.001)
        heard = self.microphone.heard(self.position, self.position + count)
        self.position += count
        return heard


class Wake:
    """Hears the wake word whenever the test says - as long as it's listening for it then."""

    def __init__(self, config=None, microphone=None):
        self.microphone = microphone
        self.heard_at = None
        self.listening = False
        self._said = queue.Queue()

    def set_muted(self, muted):
        pass

    def say(self):
        self._said.put(self.microphone.position)

    def listen_once(self, until=None):
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
                if position >= now:
                    self.heard_at = position
                    return 0.9
        finally:
            self.listening = False


class Playback:
    """Plays until stopped, or the end of its sound or stream."""

    def __init__(self, kind, filename=None):
        self.kind = kind
        self.filename = filename
        self.paused = False
        self.volume = 1.0
        self.stopped = False
        self.position = 0
        self.pieces = []
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


class Speaker:
    """Plays each of the satellite's own sounds for SOUND_SECONDS, and each stream until the last of it arrives."""

    def __init__(self, sample_rate=48000, echo_reference=None):
        self.sample_rate = sample_rate
        self.lock = threading.Lock()
        self.playing = []
        self.silenced = set()

    def _start(self, playback):
        with self.lock:
            if playback.kind in self.silenced:
                playback.finish()
            self.playing.append(playback)
        return playback

    def play_file(self, filename, volume=1.0, kind="reply", cache=True, start=0.0, length=None):
        playback = self._start(Playback(kind, filename))
        timer = threading.Timer(SOUND_SECONDS, playback.finish)
        timer.daemon = True
        timer.start()
        return playback

    def play_stream(self, pieces, sample_rate=None, volume=1.0, kind="reply", paused=False, most_ahead=None):
        playback = self._start(Playback(kind))

        def feed():
            for piece in pieces:
                if playback.done:
                    break
                playback.pieces.append(piece)
                playback.position += len(piece)
            pieces.close()
            playback.finish()

        threading.Thread(target=feed, daemon=True).start()
        return playback

    def streams(self, kind):
        with self.lock:
            return [playback for playback in self.playing if playback.filename is None and playback.kind == kind]

    def stop(self, kinds=None):
        with self.lock:
            playing = list(self.playing)
        for playback in playing:
            if kinds is None or playback.kind in kinds:
                playback.stop()

    def silence(self, kind):
        with self.lock:
            self.silenced.add(kind)
        self.stop([kind])

    def allow(self, kind):
        with self.lock:
            self.silenced.discard(kind)

    def close(self):
        self.stop()


# The brain's models

class Piper:
    """Says everything as a tenth of a second at half volume, keeping what it was asked to say."""

    sample_rate = 22050

    def __init__(self):
        self.said = []

    def stream(self, message, on_start=None, remember=True):
        self.said.append(message)

        def pieces():
            if on_start:
                on_start()
            yield np.full(2205, 0.5, np.float32)
        return pieces()

    def close(self):
        pass


class Transcription:
    def __init__(self, text):
        self.text = text
        self.heard = None

    def add(self, audio):
        pass

    def finish(self, audio):
        self.heard = audio
        return {"text": self.text}

    def cancel(self):
        pass


class Nemotron:
    """Hears in each recording the next of the texts the test gives."""

    loaded = True

    def __init__(self):
        self.texts = []
        self.made = []

    def listen(self):
        self.made.append(Transcription(self.texts.pop(0) if self.texts else ""))
        return self.made[-1]


class Claude:
    """Answers each request with the next of the replies the test gives, keeping what it was asked."""

    def __init__(self, **kwargs):
        self.replies = []
        self.asked = []

    def forget_stale_history(self):
        pass

    def process_query(self, text, context=None, lights=None):
        self.asked.append(text)
        return {"response": self.replies.pop(0), "actions": [], "success": True}

    def remember(self, text, reply):
        pass

    def record_reply(self, text, spoken, tool_answers=()):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch, make_config):
    """
    Makes brains and satellites which keep everything in the test's own folder, and
    lets them all go after the test. A brain made again with the same port is the
    first restarted, with everything it saved.
    """
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(tmp_path / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(tmp_path / "alarms.json"))
    monkeypatch.setattr(memory_module, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(routines_module, "ROUTINES_FILE", str(tmp_path / "routines.json"))
    monkeypatch.setattr(brain_module, "RUNNING_MARK", str(tmp_path / "brain-running"))
    passwords = SatellitePasswords(str(tmp_path / "satellites.json"))
    passwords.add("bedroom", PASSWORD)
    monkeypatch.setattr(brain_module, "SatellitePasswords", lambda: passwords)
    # Short, so a brain which has gone is noticed - and found again - quickly.
    monkeypatch.setattr(brain_module, "SOCKET_TIMEOUT", 0.1)
    monkeypatch.setattr(brain_module, "STOP_CHECK", 0.02)
    monkeypatch.setattr(satellite_link, "SOCKET_TIMEOUT", 0.1)
    monkeypatch.setattr(satellite_link, "RETRY_FIRST", 0.02)
    monkeypatch.setattr(satellite_link, "RETRY_LONGEST", 0.05)
    monkeypatch.setattr(satellite_module, "FALLBACK_CHECK", 0.02)

    # The brain's models.
    piper, nemotron, claude = Piper(), Nemotron(), Claude()
    monkeypatch.setattr(brain_module, "TextToSpeech", lambda config: piper)
    monkeypatch.setattr(brain_module, "_speech_to_text", lambda config: nemotron)
    monkeypatch.setattr(session_module, "LLMHandler", lambda **kwargs: claude)
    monkeypatch.setattr(session_module, "make_detector", lambda config: LoudnessDetector())
    monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume: None)

    # The Pi's devices, and what it keeps.
    monkeypatch.setenv("NOVA_BACKEND_TOKEN", PASSWORD)
    monkeypatch.setattr(satellite_module, "RUNNING_MARK", str(tmp_path / "satellite-running"))
    monkeypatch.setattr(satellite_module, "Microphone", Microphone)
    monkeypatch.setattr(satellite_module, "Speaker", Speaker)
    monkeypatch.setattr(satellite_module, "WakeWordDetector", Wake)
    monkeypatch.setattr(satellite_module, "make_echo_canceller", lambda config, rate: None)
    monkeypatch.setattr(satellite_module, "AlarmCopy", partial(AlarmCopy, str(tmp_path / "alarm_copy.json")))
    monkeypatch.setattr(satellite_module, "StateCopy", partial(StateCopy, str(tmp_path / "state.json")))
    monkeypatch.setattr(satellite_module, "Phrase", partial(Phrase, str(tmp_path / "phrase.wav")))

    brains, satellites = [], []

    def make_brain(port=0, clock=False):
        """
        A brain, listening for satellites - its clock ringing what comes due, if asked,
        started first, as Brain.run starts it.
        """
        config = make_config(assistant={"name": "Nova", "wake_word": "Hey Nova"}, backend={"port": port},
                             timers={"alarm_ring_duration": 10})
        made = brain_module.Brain(config)
        brains.append(made)
        if clock:
            made.clock.start()
        made.serving = threading.Thread(target=made.serve, daemon=True)
        made.serving.start()
        assert wait_for(lambda: made._server is not None)
        return made

    def make_satellite(brain):
        """The bedroom's satellite, running, and connecting to the brain - noting everything the brain sends it."""
        config = make_config(audio={"sample_rate": 48000, "pre_roll": 0.3},
                             backend={"address": f"127.0.0.1:{brain.port}", "satellite": "bedroom"})
        made = satellite_module.Satellite(config)
        made.latency_file = str(tmp_path / "satellite-latency.jsonl")
        made.from_brain = []
        for kind, handler in list(made._handlers.items()):
            def noting(message, payload, handler=handler):
                made.from_brain.append(message)
                handler(message, payload)
            made._handlers[kind] = noting

        def run():
            try:
                made.run()
            except SystemExit:
                pass

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        satellites.append((made, thread))
        return made

    yield SimpleNamespace(make_brain=make_brain, make_satellite=make_satellite, piper=piper, nemotron=nemotron,
                          claude=claude)
    for made, thread in satellites:
        made.stop()
        thread.join(5)
    for made in brains:
        made.clock.stop()
        made.clock.dismiss()
        made.stop()


def caught_up(brain, name="bedroom"):
    """Whether the satellite has connected, and said what happened while it was away."""
    return brain.session_for(name) is not None and name in brain._caught_up


def test_a_question_is_asked_and_its_answer_heard(world):
    # The wake word, what's said after it, the reply's audio reaching the satellite -
    # and a question in it, whose answer the satellite sends when the brain asks to hear
    # it: how Nova hears the answer to a question.
    brain = world.make_brain()
    satellite = world.make_satellite(brain)
    assert wait_for(lambda: caught_up(brain) and satellite.wake.listening)
    world.nemotron.texts = ["What should I cook tonight?", "Pasta."]
    world.claude.replies = ["Pasta or curry?", "Pasta it is."]

    # The user answers a moment after they're asked.
    listen = satellite._handlers["listen"]

    def asked(message, payload):
        listen(message, payload)
        satellite.microphone.say(0.5, after=0.3)

    satellite._handlers["listen"] = asked

    # "Hey Nova, what should I cook tonight?"
    satellite.microphone.say(0.8, after=0.2)
    satellite.wake.say()

    replies = satellite.speaker.streams
    assert wait_for(lambda: len(replies("reply")) == 2 and all(reply.done for reply in replies("reply")))
    assert wait_for(lambda: satellite.wake.listening)

    # Both heard, the second as the answer, from what the satellite sent when asked.
    assert world.claude.asked == ["What should I cook tonight?", "Pasta."]
    assert [message["type"] for message in satellite.from_brain].count("listen") == 1
    heard = [transcription.heard for transcription in world.nemotron.made]
    assert len(heard) == 2 and np.count_nonzero(heard[0]) == round(0.8 * RATE)
    assert np.count_nonzero(heard[1]) == round(0.5 * RATE)
    # Both said, and played on the satellite as they arrived.
    assert world.piper.said[1:] == ["Pasta or curry?", "Pasta it is."]
    assert all(reply.pieces and not reply.stopped for reply in replies("reply"))


def test_an_alarm_rung_while_the_brain_restarted_rings_on_from_it(world):
    # The brain restarts as the alarm comes due - a crash, the watchdog or an update -
    # so the satellite rings it from its copy, and then the brain, back, takes it over.
    # It used to stop half a minute in, as the satellite reached the restarted brain,
    # which knew only that it had been missed - and said nothing more of it.
    first = world.make_brain()
    satellite = world.make_satellite(first)
    assert wait_for(lambda: caught_up(first))
    first.clock.set_alarm(7, 0, satellite="bedroom")
    due = time.time() + 0.5
    with first.clock._changing():
        first.clock._alarms[1]["due_at"] = due
        first.clock._save_alarms()
    alarm = f"alarm-1@{int(due)}"
    assert wait_for(lambda: [entry["id"] for entry in satellite.alarms.due(due)] == [alarm])

    # Gone, and its port free again. Its clock never ran, so it never rang the alarm.
    first.stop()
    first.serving.join(5)
    assert not first.serving.is_alive()
    assert wait_for(lambda: satellite.ringer.ringing_ids() == [alarm])

    # Back, with everything it saved, where the satellite knows to find it.
    restarted = world.make_brain(port=first.port, clock=True)
    assert wait_for(lambda: caught_up(restarted))

    assert wait_for(lambda: restarted.clock.is_ringing("bedroom"))
    assert wait_for(lambda: not satellite.ringer.ringing)
    # Rung now by the brain, through the satellite's speaker.
    assert wait_for(lambda: any(message["type"] == "play_sound" and message["name"] == "alarm_sound.wav"
                                for message in satellite.from_brain))
    assert restarted.clock._missed == []


def test_an_alarm_ringing_on_the_brain_when_it_restarts_rings_on_from_the_restarted_one(world):
    # The brain is ringing the alarm as it goes - the watchdog, say. The restarted brain
    # knows only from its files, and from the satellite, which went on ringing it from
    # its copy: it used to find nothing to ring and stop the satellite for good.
    first = world.make_brain(clock=True)
    satellite = world.make_satellite(first)
    assert wait_for(lambda: caught_up(first))
    first.clock.set_alarm(7, 0, "daily", satellite="bedroom")
    due = time.time() + 0.5
    with first.clock._changing():
        first.clock._alarms[1]["due_at"] = due
        first.clock._save_alarms()
    alarm = f"alarm-1@{int(due)}"
    assert wait_for(lambda: first.clock.is_ringing("bedroom"))
    # The copy it was sent holds the alarm as ringing, due from when it began.
    assert wait_for(lambda: alarm in [entry["id"] for entry in satellite.alarms.due(time.time())])

    # Gone while it rings, and its port free again.
    first.clock.stop()
    first.stop()
    first.serving.join(5)
    assert not first.serving.is_alive()
    assert wait_for(lambda: satellite.ringer.ringing_ids() == [alarm])
    heard = len(satellite.from_brain)

    restarted = world.make_brain(port=first.port, clock=True)
    assert wait_for(lambda: caught_up(restarted))

    assert wait_for(lambda: restarted.clock.is_ringing("bedroom"))
    assert wait_for(lambda: not satellite.ringer.ringing)
    # Rung now by the brain, through the satellite's speaker.
    assert wait_for(lambda: any(message["type"] == "play_sound" and message["name"] == "alarm_sound.wav"
                                for message in satellite.from_brain[heard:]))
    assert restarted.clock._missed == []
    # Still set for the next day, the once.
    assert restarted.clock.list_alarms()["message"] == "One alarm: 7:00 AM alarm, every day."
