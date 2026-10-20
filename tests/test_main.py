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
    made.name, made.wake_word = "Nova", "Hey Nova"
    made.running = False
    made._stopping = False
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
