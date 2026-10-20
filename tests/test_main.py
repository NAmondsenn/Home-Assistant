"""
Main Loop Test
Checks how the assistant shuts down, using fakes in place of the clock, speaker and
microphone, so nothing is loaded or opened. Needs numpy and soxr installed (as they
are on the Pi), and is skipped anywhere they aren't.
"""

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
def assistant():
    made = main.SmartAssistant.__new__(main.SmartAssistant)
    made.closed = []
    made.clock = Recorder("clock", made.closed)
    made.speaker = Recorder("speaker", made.closed)
    made.microphone = Recorder("microphone", made.closed)
    made.tts = Recorder("tts", made.closed)
    made.music_player = Recorder("music", made.closed)
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


def test_giving_up_without_hearing_anything_is_audible(assistant):
    # Giving up used to be silent, which was easy to take for the assistant still
    # listening, or still thinking.
    import numpy as np
    from pathlib import Path
    from types import SimpleNamespace

    assistant.speaker = FakeSpeaker()
    assistant.microphone = SimpleNamespace(position=0, record_until_silence=lambda *a, **k: np.array([]))
    assistant.wake = SimpleNamespace(heard_at=0)
    assistant.thresholds, assistant.pre_roll, assistant.speech_detector = {}, 0.3, None
    assistant.listening_sound_enabled = True
    assistant.sounds_folder = Path("sounds")
    assistant.volume = SimpleNamespace(get=lambda category: 0.5)

    assistant._handle_interaction(timeline=FakeTimeline())
    assert assistant.speaker.played == ["listening.wav", "not_heard.wav"]


def test_saying_sorry_can_never_fail_in_turn(assistant):
    def broken_speech(message, timeline=None):
        raise RuntimeError("speaker gone")

    assistant._say = broken_speech
    assistant._apologise()
