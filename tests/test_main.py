"""
Main Loop Test
Checks how the assistant shuts down, and that a reply can be talked over, using fakes
in place of the clock, speaker and microphone, so nothing is loaded or opened. Needs
numpy and soxr installed (as they are on the Pi), and is skipped anywhere they aren't.
"""

import threading
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("soxr")

import main
from voice_assistant.routines import Routines


class Recorder:
    """Stands in for the clock, speaker and microphone, noting the order they're closed in."""

    def __init__(self, name, closed):
        self.name = name
        self.closed = closed

    def stop(self):
        self.closed.append(self.name)

    def close(self):
        self.closed.append(self.name)


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    # Never the real data folder: running the assistant leaves a mark there.
    monkeypatch.setattr(main, "RUNNING_MARK", str(tmp_path / "running"))
    made = main.SmartAssistant.__new__(main.SmartAssistant)
    made.closed = []
    made.clock = Recorder("clock", made.closed)
    made.speaker = Recorder("speaker", made.closed)
    made.microphone = Recorder("microphone", made.closed)
    made.tts = Recorder("tts", made.closed)
    made.music_player = Recorder("music", made.closed)
    made.ntfy = None
    made.settings_page = None
    made.home, made.ha_device = None, None
    made._stop_looking_up = threading.Event()
    made.name, made.wake_word = "Nova", "Hey Nova"
    made.running = False
    made._stopping = False
    made._conversation_lock = threading.Lock()
    made.routines = Routines(path=str(tmp_path / "routines.json"))
    return made


def test_a_stop_signal_ends_the_loop_and_everything_is_closed_once_in_order(assistant):
    # The signal handler used to close everything itself, in the middle of whatever
    # the main loop was doing - including a recording still using the microphone.
    assistant._listen_forever = lambda: assistant._shutdown()

    with pytest.raises(SystemExit) as stopped:
        assistant.run()

    assert stopped.value.code == 0
    assert assistant.closed == ["clock", "music", "speaker", "microphone", "tts"]


def test_a_second_stop_signal_doesnt_cut_the_cleanup_short(assistant):
    def stop_twice():
        try:
            assistant._shutdown()
        finally:
            assert assistant._shutdown() is None

    assistant._listen_forever = stop_twice
    with pytest.raises(SystemExit):
        assistant.run()
    assert assistant.closed == ["clock", "music", "speaker", "microphone", "tts"]


def test_importing_main_writes_nothing(tmp_path):
    # Logging used to be set up on import, creating logs/ and writing to it.
    assert not main.logging.getLogger().handlers or all(
        "smart_assistant.log" not in getattr(handler, "baseFilename", "")
        for handler in main.logging.getLogger().handlers)


class FakeSpeaker:
    """Stands in for the speaker, noting which sounds were played."""

    def __init__(self):
        self.played = []

    def play_file(self, path, **kwargs):
        self.played.append(path.rsplit("/", 1)[-1])
        return type("Playback", (), {"wait": lambda self, timeout=None: True})()


class FakeTimeline:
    def mark(self, stage, at=None):
        pass

    def note(self, **notes):
        self.notes = notes


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


def _listening(assistant, record_until_silence, transcription):
    """Sets the assistant up to record with record_until_silence, and hear it with transcription."""
    from pathlib import Path
    from types import SimpleNamespace

    assistant.speaker = FakeSpeaker()
    assistant.microphone = SimpleNamespace(position=0, record_until_silence=record_until_silence)
    assistant.stt = SimpleNamespace(listen=lambda: transcription)
    assistant.wake = SimpleNamespace(heard_at=0)
    assistant.thresholds, assistant.pre_roll, assistant.speech_detector = {}, 0.3, None
    assistant.listening_sound_enabled = True
    assistant.sounds_folder = Path("sounds")
    assistant.volume = SimpleNamespace(get=lambda category: 0.5)


def test_giving_up_without_hearing_anything_is_audible(assistant):
    # Giving up used to be silent, which was easy to take for the assistant still
    # listening, or still thinking.
    import numpy as np

    transcription = FakeTranscription()
    _listening(assistant, lambda *a, **k: np.array([]), transcription)

    assistant._handle_interaction(timeline=FakeTimeline())
    assert assistant.speaker.played == ["listening.wav", "not_heard.wav"]
    # Speech-to-text, which was ready to listen, is told nothing's coming.
    assert transcription.cancelled


def test_speech_to_text_is_given_the_recording_as_its_made(assistant):
    # It used to be given the whole recording once it was over, and on the Pi
    # Moonshine then took 0.83s to work out the words.
    import numpy as np
    from types import SimpleNamespace
    from voice_assistant.conversation import Reply

    first, second = np.full(480, 0.1, np.float32), np.full(480, 0.2, np.float32)

    def record_until_silence(detector, heard=None, **kwargs):
        heard(first)
        heard(second)
        return np.concatenate([first, second])

    transcription = FakeTranscription("What time is it?")
    _listening(assistant, record_until_silence, transcription)
    handled = []
    assistant.conversation = SimpleNamespace(handle=lambda text, **kwargs: handled.append(text) or Reply())

    assert assistant._handle_interaction(timeline=FakeTimeline()) is False
    assert len(transcription.added) == 2
    assert transcription.added[0] is first and transcription.added[1] is second
    # The whole recording too, for it to fall back on.
    assert np.array_equal(transcription.finished_with, np.concatenate([first, second]))
    assert handled == ["What time is it?"]


def test_speech_to_text_is_let_go_if_the_recording_fails(assistant):
    def broken_microphone(*args, **kwargs):
        raise RuntimeError("microphone gone")

    transcription = FakeTranscription()
    _listening(assistant, broken_microphone, transcription)

    with pytest.raises(RuntimeError):
        assistant._handle_interaction(timeline=FakeTimeline())
    assert transcription.cancelled


def test_saying_sorry_can_never_fail_in_turn(assistant):
    def broken_speech(message, timeline=None):
        raise RuntimeError("speaker gone")

    assistant._say = broken_speech
    assistant._apologise()


class FakeReply:
    """Stands in for a reply being played, which lasts until it's stopped or let finish."""

    def __init__(self):
        self.ended = threading.Event()
        self.stopped = False

    def wait(self, timeout=None):
        return self.ended.wait(timeout)

    def stop(self):
        self.stopped = True
        self.ended.set()


def _speaking(assistant, wake_word_heard):
    """Sets the assistant up to say a reply, with the wake word heard over it or not."""
    from types import SimpleNamespace

    reply = FakeReply()
    assistant.speaker = SimpleNamespace(play_stream=lambda *args, **kwargs: reply)
    assistant.tts = SimpleNamespace(stream=lambda message, on_start=None: iter(()), sample_rate=22050)
    assistant.volume = SimpleNamespace(get=lambda category: 0.5)

    def listen_once(until=None):
        if wake_word_heard:
            return 0.8
        threading.Timer(0.05, reply.ended.set).start()
        while not until():
            time.sleep(0.01)
        return None

    assistant.wake = SimpleNamespace(listen_once=listen_once)
    return reply


def test_saying_the_wake_word_over_a_reply_stops_it(assistant):
    # A reply couldn't be talked over, so a fortnight of the calendar - three and a
    # half minutes of it - had to be sat through.
    reply = _speaking(assistant, wake_word_heard=True)
    timeline = FakeTimeline()
    assert assistant._say("Today, you've got...", timeline=timeline, listen=True) is True
    assert reply.stopped
    assert timeline.notes == {"talked_over": True}


def test_a_reply_nobody_talks_over_plays_to_the_end(assistant):
    reply = _speaking(assistant, wake_word_heard=False)
    assert assistant._say("It's sunny.", listen=True) is False
    assert reply.ended.is_set() and not reply.stopped


def test_talking_over_a_reply_starts_the_next_request_with_the_music_still_held(assistant, tmp_path):
    from types import SimpleNamespace

    held = []
    assistant._idle = threading.Event()
    assistant.music = SimpleNamespace(hold=lambda: held.append("hold"), release=lambda: held.append("release"))
    assistant.speaker = SimpleNamespace(silence=lambda kind: None, allow=lambda kind: None)
    assistant.clock = SimpleNamespace(ringing_state=lambda: None)
    started = []
    assistant.conversation = SimpleNamespace(start=lambda ringing, talked_over: started.append(talked_over))
    assistant.latency_file = str(tmp_path / "latency.jsonl")
    talked_over = [True, False]
    assistant._handle_interaction = lambda timeline: talked_over.pop(0)

    assistant._converse()
    # The second request knows the first reply was talked over.
    assert started == [False, True]
    # The music stays paused between the two, rather than coming back for a moment.
    assert held == ["hold", "release"]
    assert assistant._idle.is_set()


# Telling the user's phone

class FakeNtfy:
    """Stands in for ntfy, keeping what was sent."""

    def __init__(self):
        self.reminders, self.restarts = "reminders", "homelab"
        self.sent = []

    def send_soon(self, topic, message, title="", priority=0):
        self.sent.append((topic, title, message))

    def stop(self):
        pass


def test_a_start_after_a_run_which_didnt_stop_properly_is_told_to_the_phone(assistant):
    assistant.ntfy = FakeNtfy()
    # The first start has nothing to tell.
    assistant._note_start()
    assert assistant.ntfy.sent == []

    # Started again without having stopped properly - a crash, or the watchdog.
    assistant._note_start()
    assert assistant.ntfy.sent == [("homelab", "Nova restarted", "Nova stopped unexpectedly, and has started again.")]

    # And not again straight away, if it keeps happening.
    assistant._note_start()
    assert len(assistant.ntfy.sent) == 1


def test_a_start_after_stopping_properly_isnt_told(assistant):
    assistant.ntfy = FakeNtfy()
    assistant._note_start()
    assistant._clean_up()
    assistant._note_start()
    assert assistant.ntfy.sent == []


def test_only_a_reminder_nobody_answered_goes_to_the_phone(assistant):
    assistant.ntfy = FakeNtfy()
    assistant._unanswered({"label": "take the bins out"}, "clock")
    # A plain timer, and an alarm, have nothing more to say there.
    assistant._unanswered({"label": None}, "clock")
    assistant._unanswered({"label": "wake up"}, "alarm")
    assert assistant.ntfy.sent == [("reminders", "Reminder nobody answered", "Take the bins out")]


def test_an_alert_is_said_like_a_timer_and_can_then_be_sent_on(assistant):
    from types import SimpleNamespace
    from voice_assistant.ntfy import Alert

    announced = []
    assistant._announce = lambda message, **kwargs: announced.append(message)
    assistant.actions = SimpleNamespace(last_answer=None)
    assistant._alert(Alert("homelab", "Backup failed", "Error 5."))

    assert announced == ["Homelab: Backup failed"]
    assert assistant.actions.last_answer == ("Backup failed", "Backup failed. Error 5.")


# For the settings page

def test_a_volume_beep_from_the_settings_page_stops_the_last_one(assistant):
    # Dragging a slider plays one at every step, which mustn't pile up.
    from pathlib import Path
    from types import SimpleNamespace

    class Beep:
        def __init__(self, level):
            self.level, self.stopped = level, False

        def stop(self):
            self.stopped = True

    played = []
    assistant.sounds_folder = Path("sounds")
    assistant._last_boop, assistant._boop_lock = None, threading.Lock()
    assistant.speaker = SimpleNamespace(play_file=lambda path, volume: played.append(Beep(volume)) or played[-1])

    assistant._boop(0.3)
    assistant._boop(0.6)

    assert [beep.level for beep in played] == [0.3, 0.6]
    assert played[0].stopped and not played[1].stopped


def test_what_nova_is_doing_is_kept_for_the_settings_page(assistant):
    import numpy as np
    from types import SimpleNamespace
    from voice_assistant.conversation import Reply

    seen = {}

    class Watching(FakeTranscription):
        def finish(self, audio):
            seen["working out the words"] = assistant.activity
            return super().finish(audio)

    def record_until_silence(detector, heard=None, **kwargs):
        seen["recording"] = assistant.activity
        return np.zeros(480, np.float32)

    _listening(assistant, record_until_silence, Watching("What time is it?"))
    assistant.conversation = SimpleNamespace(handle=lambda text, **kwargs: Reply())
    assistant._handle_interaction(timeline=FakeTimeline())

    assert seen == {"recording": "listening", "working out the words": "thinking"}


# Routines

def test_a_routine_run_by_itself_waits_its_turn_and_is_said_as_an_announcement(assistant):
    # So the wake word stops it, as it does a timer's.
    from types import SimpleNamespace
    from voice_assistant.conversation import Reply

    played, sounds, happened = [], [], []
    assistant._announcing, assistant._idle = threading.Lock(), threading.Event()
    assistant._idle.set()
    assistant.activity = "idle"
    assistant.music = SimpleNamespace(hold=lambda: happened.append("held"),
                                      release=lambda after=0: happened.append("let go"))
    assistant.conversation = SimpleNamespace(
        start=lambda: happened.append("started"),
        run_routine=lambda routine: Reply(say="It's sunny.", sounds=[("success_chime.wav", "general")]))
    assistant._play_sound = lambda sound, volume=None: sounds.append(sound)
    assistant._play_announcement = lambda playback, still_wanted=None: None
    assistant.speaker = SimpleNamespace(play_stream=lambda stream, rate, volume, kind: played.append(kind))
    assistant.tts = SimpleNamespace(stream=lambda message: played.append(message), sample_rate=22050)
    assistant.volume = SimpleNamespace(get=lambda category: 0.5)

    assistant.run_routine({"name": "Good morning", "steps": ["what's the weather"]})

    assert sounds == ["success_chime.wav"] and played == ["It's sunny.", "announcement"]
    assert happened == ["held", "started", "let go"] and assistant.activity == "idle"


def test_a_routine_runs_by_itself_when_its_due(assistant, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(main, "ROUTINE_CHECK", 0.01)
    due, ran = [[{"name": "Good morning"}]], []

    def next_due():
        if not due:
            assistant.running = False
            return []
        return due.pop(0)

    assistant.running = True
    assistant.routines = SimpleNamespace(due=next_due)
    assistant.run_routine = lambda routine: ran.append(routine["name"])
    assistant._run_routines_when_due()

    deadline = time.monotonic() + 2
    while not ran and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ran == ["Good morning"]


# Nova's device in Home Assistant

class FakeWake:
    """Stands in for the wake word detector, which keeps the microphone's mute."""

    def __init__(self, muted=False):
        self.muted = muted

    def set_muted(self, muted):
        self.muted = muted


def _wired(assistant, **parts):
    """The controls Nova's device in Home Assistant is given, with stand-ins for the rest of Nova."""
    from types import SimpleNamespace

    assistant.activity = "idle"
    assistant.clock = SimpleNamespace(overview=lambda: {"timers": [], "alarms": []}, is_ringing=lambda: False)
    assistant.wake = FakeWake()
    assistant.stt = SimpleNamespace()
    assistant.music_player = None
    for name, part in parts.items():
        setattr(assistant, name, part)
    return assistant._device_controls()


def test_home_assistant_sees_and_changes_nova_as_the_settings_page_does(assistant):
    from types import SimpleNamespace

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

    levels = {"general": 60, "alarm": 80, "music": 40}
    volume = SimpleNamespace(CATEGORIES=("general", "alarm", "music"), percent=levels.get,
                             set=lambda category, percent: changed.append((category, percent)) or {"success": True})
    controls = _wired(assistant, volume=volume, music_player=Spotify(), stt=SimpleNamespace(reachable=False))

    assistant.activity = "thinking"
    assert controls.status() == "thinking"
    assert controls.volumes() == {"general": 60, "alarm": 80, "music": 40}
    # The music's volume through the player, as the settings page changes it - and
    # Nova's note of Spotify's follows, so Home Assistant's slider doesn't spring back.
    controls.set_volume("music", 30)
    controls.set_volume("alarm", 90)
    assert played_at == [30] and changed == [("music", 30), ("alarm", 90)]

    assert controls.backend() is False
    assistant.stt = SimpleNamespace()
    assert controls.backend() is None

    controls.set_muted(True)
    assert assistant.wake.muted and controls.muted()
    assert controls.restart is main.restart_service


def test_speak_is_said_in_turn_even_when_muted(assistant):
    # Mute only stops Nova hearing "Hey Nova", so an automation's "the washing's done"
    # is still said. It's handed off at once, so a Stop pressed in Home Assistant
    # meanwhile isn't kept waiting behind it.
    let_go, said = threading.Event(), []

    def announce(message):
        let_go.wait(2)
        said.append(message)

    controls = _wired(assistant, wake=FakeWake(muted=True))
    assistant._announce = announce
    controls.speak("The washing's done")
    assert said == []

    let_go.set()
    deadline = time.monotonic() + 2
    while not said and time.monotonic() < deadline:
        time.sleep(0.01)
    assert said == ["The washing's done"]


def test_stop_silences_what_rings_and_what_nova_is_saying(assistant):
    from types import SimpleNamespace

    happened, ringing = [], [True]
    controls = _wired(assistant)
    assistant.clock.is_ringing = lambda: ringing[0]
    assistant.clock.dismiss = lambda: happened.append("dismissed") or {"success": True, "message": "Okay."}
    assistant.speaker = SimpleNamespace(stop=lambda kinds: happened.append(("stopped", list(kinds))))

    controls.stop()
    assert happened == ["dismissed", ("stopped", ["reply", "announcement"])]

    # With nothing ringing, there's nothing to dismiss.
    ringing[0] = False
    happened.clear()
    controls.stop()
    assert happened == [("stopped", ["reply", "announcement"])]


def test_the_device_is_looked_up_until_found_then_now_and_then(assistant):
    # Often until Home Assistant has taken in Nova's announcement, then now and then,
    # in case the user has moved it to another room.
    from types import SimpleNamespace

    found, asked, waited = [False, False, True, True], [], []

    class Stopping:
        """Stands in for Nova stopping, which comes after the fourth wait."""

        def wait(self, timeout):
            waited.append(timeout)
            return len(waited) == 4

    assistant.home = SimpleNamespace(find_device=lambda identifier: asked.append(identifier) or found.pop(0))
    assistant.ha_device = SimpleNamespace(identifier="nova_home_assistant")
    assistant._stop_looking_up = Stopping()
    assistant._look_up_device()

    assert asked == ["nova_home_assistant"] * 4
    assert waited == [30, 30, 300, 300]


def test_nova_is_in_home_assistant_only_while_it_runs(assistant):
    # And the device is looked up from the start, until Nova stops.
    from types import SimpleNamespace

    happened, looked_up = [], threading.Event()

    class Device:
        identifier = "nova_home_assistant"

        def start(self):
            happened.append("started")

        def stop(self):
            happened.append("stopped")

    def find_device(identifier):
        looked_up.set()
        return True

    def run_until_looked_up():
        assert looked_up.wait(2)
        assistant._shutdown()

    assistant.ha_device = Device()
    assistant.home = SimpleNamespace(find_device=find_device)
    assistant._listen_forever = run_until_looked_up
    with pytest.raises(SystemExit):
        assistant.run()

    assert happened == ["started", "stopped"]
    # The lookup ends straight away, rather than at its next check minutes later.
    for thread in threading.enumerate():
        if thread.name == "ha-device-lookup":
            thread.join(1)
            assert not thread.is_alive()
