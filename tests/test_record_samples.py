"""
Record Samples Test
Checks the recording session keeps each recording with what was actually said, and
carries on where it left off, using a stand-in for the microphone and for the person
typing. Needs numpy installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

import importlib.util
import json
import os
import stat
import wave

import pytest

np = pytest.importorskip("numpy")

spec = importlib.util.spec_from_file_location(
    "record_samples", os.path.join(os.path.dirname(__file__), "..", "scripts", "record_samples.py"))
record_samples = importlib.util.module_from_spec(spec)
spec.loader.exec_module(record_samples)


def test_prompts_are_read_with_how_theyre_said():
    prompts = record_samples.parse_prompts(
        "# Said without Hey Nova.\n\nSet a timer for ten minutes\n[from across the room]\nStop\n\n"
        "[anything you'd ask Nova]\n*\n")
    assert prompts == [{"text": "Set a timer for ten minutes", "condition": ""},
                       {"text": "Stop", "condition": "from across the room"},
                       {"text": "", "condition": "anything you'd ask Nova"}]


def test_the_list_to_start_from_reads_cleanly():
    with open(os.path.join(os.path.dirname(__file__), "..", "scripts", "sample_prompts.txt")) as file:
        prompts = record_samples.parse_prompts(file.read())
    assert {prompt["condition"] for prompt in prompts} == {
        "where you usually talk to Nova", "from across the room", "with the TV or radio on",
        "anything you'd ask Nova"}
    assert not any(prompt["text"].startswith(("#", "[")) for prompt in prompts)
    assert sum(not prompt["text"] for prompt in prompts) == 5


def test_a_recording_is_saved_exactly_and_kept_private(tmp_path):
    # As the microphone records it: 16-bit samples, as floats.
    audio = np.array([0, 1, -1, 32767, -32768, 1234], dtype=np.int16).astype(np.float32) / 32768
    path = str(tmp_path / "001.wav")
    record_samples.save_wav(path, audio)

    with wave.open(path) as file:
        assert (file.getnchannels(), file.getsampwidth(), file.getframerate()) == (1, 2, 16000)
        saved = np.frombuffer(file.readframes(file.getnframes()), dtype=np.int16)
    assert np.array_equal(saved, [0, 1, -1, 32767, -32768, 1234])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


class Session:
    """Stands in for the microphone and the person typing, giving back what the test lines up."""

    def __init__(self, answers, recordings=None):
        self.answers = list(answers)
        self.recordings = list(recordings or [])
        self.shown = []

    def ask(self, question):
        return self.answers.pop(0)

    def record(self):
        return self.recordings.pop(0) if self.recordings else np.full(16000, 0.1, dtype=np.float32)


def _run(folder, prompts, session):
    return record_samples.record_samples(record_samples.parse_prompts(prompts), str(folder), session.record,
                                         session.ask, show=session.shown.append)


def _listing(folder):
    with open(folder / "samples.json") as file:
        return json.load(file)


def test_each_recording_is_kept_with_what_was_actually_said(tmp_path):
    # Read out "Set a timer for ten minutes", said "set a ten minute timer".
    session = Session(["", "set a ten minute timer", "", ""])
    assert _run(tmp_path, "Set a timer for ten minutes\n[from across the room]\nStop\n", session) == 2

    assert _listing(tmp_path) == [
        {"number": 1, "file": "001.wav", "text": "set a ten minute timer",
         "prompt": "Set a timer for ten minutes", "condition": ""},
        {"number": 2, "file": "002.wav", "text": "Stop", "prompt": "Stop", "condition": "from across the room"}]
    assert (tmp_path / "001.wav").exists() and (tmp_path / "002.wav").exists()


def test_one_made_up_on_the_spot_isnt_kept_without_its_words(tmp_path):
    # Enter on its own doesn't count as having typed what was said.
    session = Session(["", "", "what's on at the cinema tonight"])
    _run(tmp_path, "*\n", session)
    assert _listing(tmp_path)[0]["text"] == "what's on at the cinema tonight"


def test_one_can_be_recorded_again_or_skipped(tmp_path):
    first, second = np.full(800, 0.1, np.float32), np.full(1600, 0.2, np.float32)
    session = Session(["", "r", "", "", "s"], recordings=[first, second])
    assert _run(tmp_path, "Pause\nSkip\n", session) == 1

    with wave.open(str(tmp_path / "001.wav")) as file:
        assert file.getnframes() == 1600
    assert [sample["number"] for sample in _listing(tmp_path)] == [1]


def test_nothing_heard_is_tried_again(tmp_path):
    session = Session(["", "", ""], recordings=[np.array([], dtype=np.float32)])
    assert _run(tmp_path, "Pause\n", session) == 1
    assert "Nothing heard - try again." in session.shown


def test_a_session_can_be_stopped_and_carried_on_later(tmp_path):
    prompts = "Pause\nSkip\nStop\n"
    assert _run(tmp_path, prompts, Session(["", "", "q"])) == 1

    later = Session(["", "", "", ""])
    assert _run(tmp_path, prompts, later) == 2
    # Starting from the first not yet recorded.
    assert later.shown[0].startswith("\n2 of 3")
    assert [sample["text"] for sample in _listing(tmp_path)] == ["Pause", "Skip", "Stop"]
