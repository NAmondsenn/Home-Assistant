"""
Text-to-Speech Test
Checks how Piper is kept loaded and its audio streamed, using a small stand-in
script in place of Piper, which reads a line and writes a burst of raw samples per
word, just as Piper writes a sentence at a time. Needs numpy installed (as it is on
the Pi), and is skipped anywhere it isn't.
"""

import json
import sys
import time

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import text_to_speech
from voice_assistant.text_to_speech import TextToSpeech

FAKE_PIPER = f"""#!{sys.executable}
import os, sys, time
with open(os.environ["FAKE_PIPER_LOG"], "a") as log:
    log.write("started\\n")
text = sys.stdin.readline().strip()
if text == "fail":
    sys.stderr.write("voice file is broken")
    sys.exit(1)
for word in text.split():
    sys.stdout.buffer.write(int(16384).to_bytes(2, "little", signed=True) * 100)
    sys.stdout.buffer.flush()
    time.sleep(0.2 if text.startswith("slow") else 0)
"""


@pytest.fixture
def tts(tmp_path, monkeypatch, make_config):
    piper = tmp_path / "models" / "piper" / "piper"
    piper.parent.mkdir(parents=True)
    piper.write_text(FAKE_PIPER)
    piper.chmod(0o755)
    voice = tmp_path / "models" / "en_GB-alan-medium.onnx"
    voice.write_text("")
    (tmp_path / "models" / "en_GB-alan-medium.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 16000}}))
    log = tmp_path / "piper.log"
    monkeypatch.setenv("FAKE_PIPER_LOG", str(log))
    monkeypatch.setattr(text_to_speech, "PROJECT_DIRECTORY", str(tmp_path))

    made = TextToSpeech(make_config())
    made.started = lambda: log.read_text().count("started")
    yield made
    made.close()


def test_the_audio_comes_back_a_piece_at_a_time_at_the_voices_rate(tts):
    pieces = list(tts.stream("one two three"))
    assert tts.sample_rate == 16000
    assert sum(len(piece) for piece in pieces) == 300
    assert np.allclose(np.concatenate(pieces), 0.5)


def test_the_next_piper_is_already_loaded_and_waiting(tts):
    # Piper used to be started afresh for every reply, loading its voice each time.
    list(tts.stream("hello"))
    time.sleep(0.3)
    assert tts._waiting is not None and tts._waiting.poll() is None
    waiting = tts._waiting

    list(tts.stream("hello there"))
    assert tts._waiting is not waiting


def test_the_first_audio_is_timed_before_the_rest_is_ready(tts):
    started = []
    pieces = tts.stream("slow one two three", on_start=lambda: started.append(time.monotonic()))
    began = time.monotonic()
    next(pieces)
    assert started and started[0] - began < 0.2
    list(pieces)


def test_stopping_part_way_stops_piper(tts):
    # The reply is silenced part way when the user says the wake word over an announcement.
    taken = []
    take = tts._take_piper
    tts._take_piper = lambda: taken.append(take()) or taken[-1]

    pieces = tts.stream("slow one two three four five six")
    next(pieces)
    pieces.close()
    assert taken[0].poll() is not None


def test_something_said_again_soon_comes_from_memory(tts):
    # A timer says the same thing every round until it's dismissed.
    list(tts.stream("Your timer is done"))
    time.sleep(0.3)
    before = tts.started()
    assert sum(len(piece) for piece in tts.stream("Your timer is done")) == 400
    time.sleep(0.3)
    assert tts.started() == before


def test_a_failed_synthesis_says_nothing_and_doesnt_raise(tts, caplog):
    assert list(tts.stream("fail")) == []
    assert "voice file is broken" in caplog.text


def test_nothing_to_say_says_nothing(tts):
    assert list(tts.stream("  \n ")) == []


def test_a_wav_can_still_be_written_for_trying_the_voice(tts, tmp_path):
    import wave
    path = str(tmp_path / "out.wav")
    assert tts.synthesise("one two", path) == path
    with wave.open(path) as file:
        assert file.getframerate() == 16000 and file.getnframes() == 200
