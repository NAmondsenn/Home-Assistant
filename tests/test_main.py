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
    assert assistant.closed == ["clock", "speaker", "microphone"]


def test_a_second_stop_signal_doesnt_cut_the_cleanup_short(assistant):
    def stop_twice():
        try:
            assistant._shutdown()
        finally:
            assert assistant._shutdown() is None

    assistant._listen_forever = stop_twice
    with pytest.raises(SystemExit):
        assistant.run()
    assert assistant.closed == ["clock", "speaker", "microphone"]


def test_importing_main_writes_nothing(tmp_path):
    # Logging used to be set up on import, creating logs/ and writing to it.
    assert not main.logging.getLogger().handlers or all(
        "smart_assistant.log" not in getattr(handler, "baseFilename", "")
        for handler in main.logging.getLogger().handlers)
