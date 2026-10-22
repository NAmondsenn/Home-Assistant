"""
Session Test
Checks one satellite's Nova on the brain: each wake word the satellite hears recorded,
heard and answered through it, its mute and volumes kept and sent to it, and its own
device in Home Assistant - against a fake Pi over a socketpair, which reads what the
brain sends with the real protocol, and stand-ins for Piper and Nemotron. Needs numpy
and soxr installed (as they are on the Pi), and is skipped anywhere they aren't.
"""

import importlib
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import clock as clock_module
from voice_assistant import ha_device, paths, protocol, satellite_link
from voice_assistant import session as session_module
from voice_assistant.actions import Action
from voice_assistant.clock import Clock
from voice_assistant.conversation import Reply
from voice_assistant.recording import RATE, AudioFeed
from voice_assistant.remote import RemoteSatellite
from voice_assistant.routines import Routines
from voice_assistant.vad import LoudnessDetector


def wait_for(condition, seconds=5):
    for _ in range(int(seconds / 0.005)):
        if condition():
            return True
        time.sleep(0.005)
    return False


def said(seconds=0.5, before=0.3, after=1.0):
    """
    What a satellite sends for one request, as 16-bit samples: the moment before it was
    woken, the user talking, then quiet.
    """
    return np.concatenate([np.zeros(round(before * RATE)), np.full(round(seconds * RATE), 8000),
                           np.zeros(round(after * RATE))]).astype(np.int16)


class Pi:
    """
    The satellite's end of the connection: keeps everything the brain sends, and plays
    as a satellite would - each of its own sounds finished at once, and each stream
    once it has all been sent, unless the test holds it playing.
    """

    def __init__(self, sock):
        self.socket = sock
        self.stream = sock.makefile("rwb")
        self.received = []
        self.lock = threading.Lock()
        # Which of the brain's streams, counted from 1, play on until they're stopped.
        self.held = set()
        self._held_ids = set()
        self._streams = 0
        # What the user says each time the brain asks to hear their answer.
        self.answers = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                message, payload = protocol.receive(self.stream)
                self.received.append((message, payload))
                self._play(message)
        except (OSError, protocol.ProtocolError):
            pass

    def _play(self, message):
        kind = message["type"]
        if kind == "play_sound":
            self.say("played", id=message["id"])
        elif kind == "play_stream":
            self._streams += 1
            if self._streams in self.held:
                self._held_ids.add(message["id"])
        elif kind == "stream_end" and message["id"] not in self._held_ids:
            self.say("played", id=message["id"])
        elif kind == "listen" and self.answers:
            self.send_audio(self.answers.pop(0))

    def say(self, kind, payload=b"", **fields):
        with self.lock:
            protocol.send(self.stream, kind, payload, **fields)

    def wake(self, audio, talked_over=False):
        """Hears the wake word, and sends what's said after it - the moment before it first."""
        self.say("wake", talked_over=talked_over)
        self.send_audio(audio)

    def send_audio(self, audio):
        for begin in range(0, len(audio), 1600):
            self.say("audio", audio[begin:begin + 1600].astype("<i2").tobytes())

    def messages(self, kind):
        return [message for message, _ in self.received if message["type"] == kind]

    def go(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class FakeTranscription:
    """Stands in for speech-to-text listening to one recording, noting what it was given."""

    def __init__(self, text=""):
        self.text = text
        self.added = []
        self.finished_with = None
        self.cancelled = False

    def add(self, audio):
        self.added.append(audio)

    def finish(self, audio):
        self.finished_with = audio
        return {"text": self.text}

    def cancel(self):
        self.cancelled = True


class FakeSpeechToText:
    """Stands in for the brain's Nemotron, hearing in each recording the next of the texts the test gives."""

    loaded = True

    def __init__(self):
        self.texts = []
        self.made = []

    def listen(self):
        self.made.append(FakeTranscription(self.texts.pop(0) if self.texts else ""))
        return self.made[-1]


class FakePiper:
    """Stands in for Piper, keeping what it was asked to say."""

    sample_rate = 22050

    def __init__(self):
        self.said = []

    def stream(self, message, on_start=None):
        self.said.append(message)

        def pieces():
            if on_start:
                on_start()
            yield np.zeros(2205, np.float32)
        return pieces()


class FakeTimeline:
    def __init__(self):
        self.notes = {}

    def mark(self, stage, at=None):
        pass

    def note(self, **notes):
        self.notes.update(notes)


class ScriptedConversation:
    """Stands in for the conversation, answering each thing heard with the next of the replies the test gives."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.started = []
        self.heard = []

    def start(self, ringing=None, talked_over=False):
        self.started.append(talked_over)

    def handle(self, text, follow_up=False, timeline=None):
        self.heard.append((text, follow_up))
        return self.replies.pop(0)


class Device:
    """Stands in for the satellite's device in Home Assistant, noting when it's started and stopped."""

    identifier = "nova_bedroom_pi"

    def __init__(self, happened):
        self.happened = happened

    def start(self):
        self.happened.append("started")

    def stop(self):
        self.happened.append("stopped")


@pytest.fixture
def brain(tmp_path, monkeypatch, make_config):
    """What every session shares, with stand-ins for Piper and Nemotron, keeping everything in a temporary folder."""
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(tmp_path / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(tmp_path / "alarms.json"))
    # Speech told apart by how loud it is, which the made-up recordings here are made for.
    monkeypatch.setattr(session_module, "make_detector", lambda config: LoudnessDetector())
    # No music, unless a test gives some.
    monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume: None)
    config = make_config(assistant={"name": "Nova", "wake_word": "Hey Nova"})
    return SimpleNamespace(config=config, clock=Clock(config=config, start=False), memory=None,
                           routines=Routines(path=str(tmp_path / "routines.json")), ntfy=None, weather=None,
                           tts=FakePiper(), stt=FakeSpeechToText(), sounds_folder=Path(paths.SOUNDS_DIR))


@pytest.fixture
def connect():
    """Connects fake Pis, each a satellite the brain has just let in, and lets them all go after the test."""
    made = []

    def connect(name="bedroom", hostname="bedroom-pi.local"):
        ours, theirs = socket.socketpair()
        # Short, so the brain's reading wakes often to check on the satellite.
        ours.settimeout(0.05)
        hello = {"name": name, "hostname": hostname, "sample_rate": 48000, "unclean": False}
        satellite = RemoteSatellite(satellite_link.Stream(ours), hello)
        pi = Pi(theirs)
        threading.Thread(target=satellite.serve, daemon=True).start()
        made.append((satellite, pi))
        return satellite, pi

    yield connect
    for satellite, pi in made:
        satellite.close()
        pi.go()


@pytest.fixture
def make_session(brain, connect):
    """Makes sessions for the brain - for a new satellite unless one is given - closing them after the test."""
    made = []

    def make(satellite=None):
        made.append(session_module.Session(brain, satellite or connect()[0]))
        return made[-1]

    yield make
    for session in made:
        session.close()


def running(session):
    """Runs a session on a thread of its own, as the brain does."""
    thread = threading.Thread(target=session.run, daemon=True)
    thread.start()
    return thread


# On the brain

def test_the_session_needs_none_of_the_satellites_audio_libraries(monkeypatch):
    # The brain has no microphone or speaker, so no PortAudio for sounddevice to load -
    # and the session used to need it, through the wake word's detector. Imported
    # afresh, with every module of Nova's put back as it was afterwards.
    before = set(sys.modules)
    for name in [name for name in sys.modules if name.partition(".")[0] == "voice_assistant"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    try:
        importlib.import_module("voice_assistant.session")
    finally:
        for name in set(sys.modules) - before:
            if name.partition(".")[0] == "voice_assistant":
                del sys.modules[name]


# Answering the satellite

def test_a_wake_is_recorded_heard_and_answered_through_the_satellite(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    brain.stt.texts = ["What time is it?"]
    thread = running(session)

    pi.wake(said())
    assert wait_for(lambda: pi.messages("allow"))

    # Heard from what the satellite sent, given to speech-to-text as it was recorded.
    transcription = brain.stt.made[0]
    assert len(transcription.added) > 1
    assert np.array_equal(np.concatenate(transcription.added), transcription.finished_with)
    # Answered by the everyday commands, and said through the satellite.
    assert len(brain.tts.said) == 1 and brain.tts.said[0].startswith("It's ")
    kinds = [message["type"] for message, _ in pi.received if message["type"] != "stream_audio"]
    assert kinds == ["silence", "play_sound", "stop_listening", "play_stream", "stream_end", "allow"]
    assert pi.messages("silence")[0]["kind"] == pi.messages("allow")[0]["kind"] == "announcement"
    assert pi.messages("play_sound")[0]["name"] == "listening.wav"
    assert pi.messages("play_stream")[0]["kind"] == "reply" and pi.messages("stream_audio")

    pi.go()
    thread.join(2)
    assert not thread.is_alive()


def test_a_timer_set_through_a_satellite_rings_on_it(brain, connect, make_session):
    satellite, pi = connect()
    brain.stt.texts = ["Set a timer for ten minutes."]
    running(make_session(satellite))

    pi.wake(said())
    assert wait_for(lambda: pi.messages("allow"))
    assert len(brain.clock.overview(satellite="bedroom")["timers"]) == 1
    assert brain.clock.overview(satellite="kitchen")["timers"] == []


def test_talking_over_a_follow_up_starts_the_next_turn(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Which room?", listen_again=True),
                                                Reply(say="The kitchen lights are on, and the hall's..."),
                                                Reply(say="Okay."))
    brain.stt.texts = ["Turn the lights on.", "The kitchen.", "Stop."]
    # The answer to the question, said once the brain asks to hear it - and the reply
    # to that plays on until it's talked over.
    pi.answers = [said()]
    pi.held = {2}
    thread = running(session)

    pi.wake(said())
    assert wait_for(lambda: len(pi.messages("play_stream")) == 2)
    pi.wake(said(), talked_over=True)
    assert wait_for(lambda: len(pi.messages("play_stream")) == 3 and pi.messages("allow"))

    assert session.conversation.heard == [("Turn the lights on.", False), ("The kitchen.", True), ("Stop.", False)]
    # The next request knows the last reply was talked over.
    assert session.conversation.started == [False, True]
    # Which was stopped there and then, by its id.
    talked_over = pi.messages("play_stream")[1]["id"]
    assert any(message.get("id") == talked_over for message in pi.messages("stop"))
    assert len(pi.messages("listen")) == 1 and len(pi.messages("allow")) == 1
    pi.go()
    thread.join(2)


def test_a_turn_whose_satellite_goes_ends_quietly(brain, connect, make_session, caplog):
    satellite, pi = connect()
    session = make_session(satellite)
    session.conversation = ScriptedConversation(Reply(say="Okay."))
    brain.stt.texts = ["Turn the"]
    happened = []
    session.device = Device(happened)
    thread = running(session)

    # Gone part way through the request.
    pi.wake(said(after=0))
    assert wait_for(lambda: brain.stt.made and brain.stt.made[0].added)
    pi.go()
    thread.join(3)

    assert not thread.is_alive()
    # Half a request isn't acted on, and nobody's told sorry - there's nobody there.
    assert brain.stt.made[0].cancelled
    assert session.conversation.heard == [] and brain.tts.said == []
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
    # And Home Assistant shows its device as unavailable.
    assert happened == ["started", "stopped"]


def test_giving_up_without_hearing_anything_is_audible(brain, connect, make_session):
    # Giving up used to be silent, which was easy to take for the assistant still
    # listening, or still thinking.
    satellite, pi = connect()
    session = make_session(satellite)
    feed = AudioFeed()
    feed.feed(np.zeros(4 * RATE, np.int16))

    assert session._handle_interaction(feed, timeline=FakeTimeline()) is None
    assert [message["name"] for message in pi.messages("play_sound")] == ["listening.wav", "not_heard.wav"]
    # Speech-to-text, which was ready to listen, is told nothing's coming - and the
    # satellite stops sending.
    assert brain.stt.made[0].cancelled
    assert wait_for(lambda: pi.messages("stop_listening"))


def test_speech_to_text_is_given_the_recording_as_its_made(brain, make_session):
    # It used to be given the whole recording once it was over, and on the Pi
    # Moonshine then took 0.83s to work out the words.
    session = make_session()
    session.conversation = ScriptedConversation(Reply())
    brain.stt.texts = ["What time is it?"]
    feed = AudioFeed()
    feed.feed(said())
    feed.end()

    assert session._handle_interaction(feed, timeline=FakeTimeline()) is None
    transcription = brain.stt.made[0]
    assert len(transcription.added) > 1
    # The whole recording too, for it to fall back on.
    assert np.array_equal(np.concatenate(transcription.added), transcription.finished_with)
    assert session.conversation.heard == [("What time is it?", False)]


def test_the_end_of_the_wake_word_sent_before_it_isnt_taken_for_the_user_talking(brain, make_session):
    # The satellite sends the moment before it heard the wake word first, which often
    # holds the end of "Nova". Listened to, that started the recording - and the pause
    # for the listening sound then ended it, with nothing in it but "Nova".
    session = make_session()
    end_of_nova = np.concatenate([np.zeros(round(0.1 * RATE)), np.full(round(0.2 * RATE), 8000)])
    feed = AudioFeed()
    feed.feed(np.concatenate([end_of_nova, said(before=0.8)]).astype(np.int16))
    feed.end()

    session._handle_interaction(feed, timeline=FakeTimeline())
    # All of what the user said, and none of "Nova".
    assert np.count_nonzero(brain.stt.made[0].finished_with) == round(0.5 * RATE)


def test_a_request_run_on_from_the_wake_word_is_heard_whole(brain, make_session):
    # "Hey Nova what time is it" said in one breath has often started on "what" by the
    # time the wake word is heard.
    session = make_session()
    feed = AudioFeed()
    feed.feed(said(seconds=0.8, before=0))
    feed.end()

    session._handle_interaction(feed, timeline=FakeTimeline())
    assert np.count_nonzero(brain.stt.made[0].finished_with) == round(0.8 * RATE)


def test_an_answer_begun_over_the_listening_sound_is_heard(brain, make_session):
    # The satellite sends an answer from the moment the brain asks for it, with nothing
    # from before - so there's nothing to skip.
    session = make_session()
    session.conversation = ScriptedConversation(Reply())
    brain.stt.texts = ["Yes."]
    feed = AudioFeed()
    feed.feed(said(seconds=0.2, before=0))
    feed.end()

    session._handle_interaction(feed, follow_up=True, timeline=FakeTimeline())
    assert session.conversation.heard == [("Yes.", True)]


def test_speech_to_text_is_let_go_if_the_recording_fails(brain, connect, make_session, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("feed gone")

    satellite, pi = connect()
    session = make_session(satellite)
    monkeypatch.setattr(session_module, "record_until_silence", broken)

    with pytest.raises(RuntimeError):
        session._handle_interaction(AudioFeed(), timeline=FakeTimeline())
    assert brain.stt.made[0].cancelled
    assert wait_for(lambda: pi.messages("stop_listening"))


def test_saying_sorry_can_never_fail_in_turn(make_session):
    def broken_speech(message, timeline=None):
        raise RuntimeError("satellite gone")

    session = make_session()
    session._say = broken_speech
    session._apologise()


def test_saying_the_wake_word_over_a_reply_stops_it(connect, make_session):
    # A reply couldn't be talked over, so a fortnight of the calendar - three and a
    # half minutes of it - had to be sat through.
    satellite, pi = connect()
    session = make_session(satellite)
    pi.held = {1}
    timeline, heard_over = FakeTimeline(), []
    saying = threading.Thread(target=lambda: heard_over.append(
        session._say("Today, you've got...", timeline=timeline, listen=True)))
    saying.start()
    assert wait_for(lambda: pi.messages("play_stream"))

    pi.wake(said(), talked_over=True)
    saying.join(2)
    # The next request starts from what was said over it.
    assert heard_over[0]["talked_over"] and isinstance(heard_over[0]["feed"], AudioFeed)
    reply = pi.messages("play_stream")[0]["id"]
    assert wait_for(lambda: any(message.get("id") == reply for message in pi.messages("stop")))
    assert timeline.notes["talked_over"] is True


def test_a_wake_word_heard_just_as_the_reply_ends_starts_the_next_request(connect, make_session):
    # Missed as a question finished, the recording of the new request was taken for
    # the answer.
    satellite, pi = connect()
    session = make_session(satellite)
    satellite.wakes.put({"talked_over": False, "feed": AudioFeed()})
    finished = SimpleNamespace(done=True, duration=0.0)

    heard_over = session._heard_over(finished)
    assert heard_over is not None and isinstance(heard_over["feed"], AudioFeed)
    assert satellite.wakes.empty()


def test_a_reply_nobody_talks_over_plays_to_the_end(connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    assert session._say("It's sunny.", listen=True) is None
    assert pi.messages("stream_end") and pi.messages("stop") == []


def test_a_wake_word_heard_while_the_reply_was_worked_out_is_answered_instead(brain, connect, make_session):
    # The satellite is already sending what the user says next, so the reply mustn't
    # be said over them - and it counts as talking over it, so "Hey Nova, stop" means
    # don't say it, rather than pausing the music for good.
    satellite, pi = connect()
    session = make_session(satellite)
    pi.wake(said(), talked_over=False)
    assert wait_for(lambda: not satellite.wakes.empty())

    heard_over = session._say("It's sunny.", listen=True)
    assert heard_over["talked_over"] is True and isinstance(heard_over["feed"], AudioFeed)
    assert brain.tts.said == [] and pi.messages("play_stream") == []


def test_talking_over_a_reply_starts_the_next_request_with_the_music_still_held(make_session):
    session = make_session()
    held = []
    session.music = SimpleNamespace(hold=lambda: held.append("hold"), release=lambda: held.append("release"))
    session.conversation = ScriptedConversation()
    heard_over = [{"talked_over": True, "feed": AudioFeed()}, None]
    session._handle_interaction = lambda feed, timeline: heard_over.pop(0)

    # Woken over a chime, which isn't talking over a reply.
    session._converse({"talked_over": True, "feed": AudioFeed()})
    # The second request knows the first reply was talked over.
    assert session.conversation.started == [False, True]
    # The music stays paused between the two, rather than coming back for a moment.
    assert held == ["hold", "release"]
    assert session._idle.is_set() and session.activity == "idle"


def test_closing_lets_go_of_the_device_the_music_and_the_satellites_sounds_once_in_order(make_session):
    class Recorder:
        def __init__(self, name, closed):
            self.name, self.closed = name, closed

        def stop(self):
            self.closed.append(self.name)

        def close(self):
            self.closed.append(self.name)

    session = make_session()
    closed = []
    session.device = Recorder("device", closed)
    session.music_player = Recorder("music", closed)
    session.speaker = Recorder("speaker", closed)

    session.close()
    session.close()
    assert closed == ["device", "music", "speaker"]


# Each satellite's own

def test_mute_is_kept_per_satellite_and_sent_to_it(brain, connect, make_session):
    bedroom, bedroom_pi = connect("bedroom", "bedroom-pi")
    kitchen, kitchen_pi = connect("kitchen", "kitchen-pi")
    in_bedroom, in_kitchen = make_session(bedroom), make_session(kitchen)

    in_bedroom.set_muted(True)
    assert in_bedroom.muted and not in_kitchen.muted
    # Sent with the volumes, for the satellite to mute its wake word by.
    assert wait_for(lambda: bedroom_pi.messages("state"))
    assert bedroom_pi.messages("state")[-1]["muted"] is True
    assert bedroom_pi.messages("state")[-1]["volumes"] == {"general": 0.7, "alarm": 1.0, "music": 0.5}
    assert kitchen_pi.messages("state") == []

    # Home Assistant's switch is the satellite's own too.
    in_kitchen._device_controls().set_muted(True)
    assert in_kitchen.muted
    assert wait_for(lambda: kitchen_pi.messages("state") and kitchen_pi.messages("state")[-1]["muted"] is True)
    in_kitchen.set_muted(False)

    # Kept, so the bedroom is still muted when it connects again.
    again, _ = connect("bedroom", "bedroom-pi")
    assert make_session(again).muted
    assert not make_session(connect("kitchen", "kitchen-pi")[0]).muted


def test_volumes_are_kept_per_satellite_and_sent_to_it(brain, connect, make_session):
    bedroom, bedroom_pi = connect("bedroom", "bedroom-pi")
    kitchen, kitchen_pi = connect("kitchen", "kitchen-pi")
    in_bedroom, in_kitchen = make_session(bedroom), make_session(kitchen)

    # Changed by voice, and from Home Assistant.
    in_kitchen.actions.execute(Action("set_volume", {"category": "alarm", "level": 30}))
    in_kitchen._device_controls().set_volume("general", 40)

    assert (in_kitchen.volume.percent("general"), in_kitchen.volume.percent("alarm")) == (40, 30)
    assert (in_bedroom.volume.percent("general"), in_bedroom.volume.percent("alarm")) == (70, 100)
    assert wait_for(lambda: len(kitchen_pi.messages("state")) == 2)
    assert kitchen_pi.messages("state")[-1]["volumes"] == {"general": 0.4, "alarm": 0.3, "music": 0.5}
    assert bedroom_pi.messages("state") == []
    assert make_session(connect("kitchen", "kitchen-pi")[0]).volume.percent("alarm") == 30
    assert os.path.exists(os.path.join(paths.DATA_DIR, "volume-kitchen.json"))


def test_the_device_is_named_from_the_satellites_hostname(brain, connect, make_session, monkeypatch):
    # The brain runs it now, and the Pi's device - and with it the room Home Assistant
    # has it in - has to stay the one it always was.
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "nova")
    brain.config.data["mqtt"] = {"host": "192.168.1.20", "port": 1883, "username": "nova", "name": "Nova"}
    satellite, _ = connect("bedroom", "home-assistant.local")
    assert make_session(satellite).device.identifier == "nova_home_assistant"


# Things which go off by themselves

def test_an_alert_is_said_like_a_timer_and_can_then_be_sent_on(make_session):
    from voice_assistant.ntfy import Alert

    session = make_session()
    announced = []
    session.announce = lambda message, **kwargs: announced.append(message)
    session.alert(Alert("homelab", "Backup failed", "Error 5."))

    assert announced == ["Homelab: Backup failed"]
    assert session.actions.last_answer == ("Backup failed", "Backup failed. Error 5.")


def test_a_routine_run_by_itself_waits_its_turn_and_is_said_as_an_announcement(brain, make_session):
    # So the wake word stops it, as it does a timer's.
    session = make_session()
    played, sounds, happened = [], [], []
    session.music = SimpleNamespace(hold=lambda: happened.append("held"),
                                    release=lambda after=0: happened.append("let go"))
    session.conversation = SimpleNamespace(
        start=lambda: happened.append("started"),
        run_routine=lambda routine: Reply(say="It's sunny.", sounds=[("success_chime.wav", "general")]))
    session._play_sound = lambda sound, volume=None: sounds.append(sound)
    session._play_announcement = lambda playback, still_wanted=None: None
    session.speaker = SimpleNamespace(play_stream=lambda stream, rate, volume, kind: played.append(kind),
                                      close=lambda: None)
    brain.tts = SimpleNamespace(stream=lambda message: played.append(message), sample_rate=22050)

    session.run_routine({"name": "Good morning", "steps": ["what's the weather"]})

    assert sounds == ["success_chime.wav"] and played == ["It's sunny.", "announcement"]
    assert happened == ["held", "started", "let go"] and session.activity == "idle"


# For the settings page

def test_a_volume_beep_from_the_settings_page_stops_the_last_one(make_session):
    # Dragging a slider plays one at every step, which mustn't pile up.
    class Beep:
        def __init__(self, level):
            self.level, self.stopped = level, False

        def stop(self):
            self.stopped = True

    session = make_session()
    played = []
    session.speaker = SimpleNamespace(play_file=lambda path, volume: played.append(Beep(volume)) or played[-1],
                                      close=lambda: None)

    session.boop(0.3)
    session.boop(0.6)

    assert [beep.level for beep in played] == [0.3, 0.6]
    assert played[0].stopped and not played[1].stopped


def test_what_nova_is_doing_is_kept_for_the_settings_page(brain, make_session, monkeypatch):
    session = make_session()
    seen = {}

    class Watching(FakeTranscription):
        def finish(self, audio):
            seen["working out the words"] = session.activity
            return super().finish(audio)

    def record_until_silence(source, detector, heard=None, **kwargs):
        seen["recording"] = session.activity
        return np.zeros(480, np.float32)

    monkeypatch.setattr(session_module, "record_until_silence", record_until_silence)
    brain.stt = SimpleNamespace(listen=lambda: Watching("What time is it?"))
    session.conversation = ScriptedConversation(Reply())
    session._handle_interaction(AudioFeed(), timeline=FakeTimeline())

    assert seen == {"recording": "listening", "working out the words": "thinking"}


# The satellite's device in Home Assistant

def test_home_assistant_sees_and_changes_nova_as_the_settings_page_does(brain, connect, make_session):
    satellite, pi = connect()
    session = make_session(satellite)
    changed, played_at = [], []

    class Spotify:
        # Read every second, so Spotify's servers are never asked - Nova's own note of
        # the music's volume is used instead, as on the settings page.
        needs_internet = True

        def get_volume(self):
            raise AssertionError("Spotify was asked for its volume")

        def set_volume(self, percent):
            played_at.append(percent)
            return {"success": True, "message": "Done."}

        def close(self):
            pass

    levels = {"general": 60, "alarm": 80, "music": 40}
    session.volume = SimpleNamespace(
        CATEGORIES=("general", "alarm", "music"), percent=levels.get, get=lambda category: levels[category] / 100,
        set=lambda category, percent: changed.append((category, percent)) or {"success": True})
    session.music_player = Spotify()
    controls = session._device_controls()

    session.activity = "thinking"
    assert controls.status() == "thinking"
    assert controls.volumes() == {"general": 60, "alarm": 80, "music": 40}
    # The music's volume through the player, as the settings page changes it - and
    # Nova's note of Spotify's follows, so Home Assistant's slider doesn't spring back.
    controls.set_volume("music", 30)
    controls.set_volume("alarm", 90)
    assert played_at == [30] and changed == [("music", 30), ("alarm", 90)]

    # Whether the brain's speech-to-text is loaded.
    assert controls.backend() is True
    brain.stt = SimpleNamespace(loaded=False)
    assert controls.backend() is False

    controls.set_muted(True)
    assert session.muted and controls.muted()
    # Restart restarts the satellite.
    controls.restart()
    assert wait_for(lambda: pi.messages("restart"))


def test_speak_is_said_in_turn_even_when_muted(make_session):
    # Mute only stops Nova hearing "Hey Nova", so an automation's "the washing's done"
    # is still said. It's handed off at once, so a Stop pressed in Home Assistant
    # meanwhile isn't kept waiting behind it.
    let_go, said_out_loud = threading.Event(), []

    def announce(message):
        let_go.wait(2)
        said_out_loud.append(message)

    session = make_session()
    session.set_muted(True)
    session.announce = announce
    session._device_controls().speak("The washing's done")
    assert said_out_loud == []

    let_go.set()
    assert wait_for(lambda: said_out_loud, seconds=2)
    assert said_out_loud == ["The washing's done"]


def test_stop_silences_what_rings_and_what_nova_is_saying(brain, make_session):
    session = make_session()
    happened, ringing = [], [True]
    brain.clock = SimpleNamespace(is_ringing=lambda: ringing[0],
                                  dismiss=lambda: happened.append("dismissed") or {"success": True, "message": "Okay."})
    session.speaker = SimpleNamespace(stop=lambda kinds: happened.append(("stopped", list(kinds))), close=lambda: None)
    controls = session._device_controls()

    controls.stop()
    assert happened == ["dismissed", ("stopped", ["reply", "announcement"])]

    # With nothing ringing, there's nothing to dismiss.
    ringing[0] = False
    happened.clear()
    controls.stop()
    assert happened == [("stopped", ["reply", "announcement"])]


def test_the_device_is_looked_up_until_found_then_now_and_then(make_session):
    # Often until Home Assistant has taken in the device's announcement, then now and
    # then, in case the user has moved it to another room.
    found, asked, waited = [False, False, True, True], [], []

    class Stopping:
        """Stands in for the session closing, which comes after the fourth wait."""

        def wait(self, timeout):
            waited.append(timeout)
            return len(waited) == 4

        def set(self):
            pass

    session = make_session()
    session.home = SimpleNamespace(find_device=lambda identifier: asked.append(identifier) or found.pop(0))
    session.device = SimpleNamespace(identifier="nova_home_assistant", stop=lambda: None)
    session._stop_looking_up = Stopping()
    session._look_up_device()

    assert asked == ["nova_home_assistant"] * 4
    assert waited == [30, 30, 300, 300]


def test_a_session_closed_before_it_runs_never_starts_its_device(connect, make_session):
    # Replaced by its satellite connecting again before it ran - the brain closes it
    # first. Its device started regardless, and stayed connected for good, with the
    # same MQTT client id as the new session's.
    satellite, pi = connect()
    session = make_session(satellite)
    happened = []
    session.device = Device(happened)
    session.home = SimpleNamespace(find_device=lambda identifier: True)
    session.close()
    session.run()

    assert happened == ["stopped"]
    assert not any(thread.name.startswith("ha-device-lookup") for thread in threading.enumerate())


def test_the_satellite_is_in_home_assistant_only_while_its_session_runs(connect, make_session):
    # And the device is looked up from the start, until the satellite goes.
    satellite, pi = connect()
    session = make_session(satellite)
    happened, looked_up = [], threading.Event()

    def find_device(identifier):
        looked_up.set()
        return True

    session.device = Device(happened)
    session.home = SimpleNamespace(find_device=find_device)
    thread = running(session)
    assert looked_up.wait(2)
    pi.go()
    thread.join(2)

    assert not thread.is_alive() and happened == ["started", "stopped"]
    # The lookup ends straight away, rather than at its next check minutes later.
    for lookup in threading.enumerate():
        if lookup.name.startswith("ha-device-lookup"):
            lookup.join(1)
            assert not lookup.is_alive()
