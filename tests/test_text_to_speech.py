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
    log.write("started " + " ".join(sys.argv[1:]) + "\\n")
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
def make_tts(tmp_path, monkeypatch, make_config):
    """Makes TextToSpeech with a stand-in Piper, which logs each start with its arguments."""
    piper = tmp_path / "models" / "piper" / "piper"
    piper.parent.mkdir(parents=True)
    piper.write_text(FAKE_PIPER)
    piper.chmod(0o755)
    voice = tmp_path / "models" / "en_US-hfc_female-medium.onnx"
    voice.write_text("")
    (tmp_path / "models" / "en_US-hfc_female-medium.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 16000}}))
    log = tmp_path / "piper.log"
    monkeypatch.setenv("FAKE_PIPER_LOG", str(log))
    monkeypatch.setattr(text_to_speech.paths, "MODELS_DIR", str(tmp_path / "models"))

    made = []

    def make(**sections):
        made.append(TextToSpeech(make_config(**sections)))
        made[-1].started = lambda: log.read_text().count("started")
        # The --length_scale each Piper was started with, in the order they started.
        made[-1].scales = lambda: [line.split("--length_scale ")[1].split()[0]
                                   for line in log.read_text().splitlines()]
        return made[-1]

    yield make
    for tts in made:
        tts.close()


@pytest.fixture
def tts(make_tts):
    return make_tts()


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
    tts._take_piper = lambda speed: taken.append(take(speed)) or taken[-1]

    pieces = tts.stream("slow one two three four five six")
    next(pieces)
    pieces.close()
    assert taken[0].poll() is not None


def test_two_sessions_can_speak_at_once(tts):
    # The brain's one Piper is every satellite's, so two of them can be saying
    # something at the same moment - each with a Piper process of its own.
    import threading

    begun = threading.Barrier(2, timeout=5)
    heard = {}

    def speak(satellite, text):
        pieces = tts.stream(text, remember=False)
        first = next(pieces)
        # Neither finishes before both have begun.
        begun.wait()
        heard[satellite] = len(first) + sum(len(piece) for piece in pieces)

    speakers = [threading.Thread(target=speak, args=("bedroom", "slow one two")),
                threading.Thread(target=speak, args=("kitchen", "slow one two three four"))]
    for speaker in speakers:
        speaker.start()
    for speaker in speakers:
        speaker.join(5)
    assert heard == {"bedroom": 300, "kitchen": 500}


def test_something_said_again_soon_comes_from_memory(tts):
    # A timer says the same thing every round until it's dismissed.
    list(tts.stream("Your timer is done"))
    time.sleep(0.3)
    before = tts.started()
    assert sum(len(piece) for piece in tts.stream("Your timer is done")) == 400
    time.sleep(0.3)
    assert tts.started() == before


def test_piper_speaks_at_the_chosen_speed(make_tts):
    # Piper's flag is how long each sound lasts, so a quarter faster is 0.8 of the length.
    tts = make_tts(voice={"speed": 1.25})
    time.sleep(0.3)
    assert tts.speed == 1.25
    assert tts.scales() and set(tts.scales()) == {"0.8"}


def test_a_new_speed_starts_the_next_piper_at_once(tts):
    # So the next reply after a change on the page finds one loaded, rather than
    # loading its voice then or being said at the old speed.
    list(tts.stream("Your timer is done"))
    time.sleep(0.3)
    waiting = tts._waiting
    assert tts._recent and tts.scales()[-1] == "1.0"

    tts.set_speed(1.25)
    assert waiting.poll() is not None
    assert tts._waiting is not waiting and tts._waiting.poll() is None
    time.sleep(0.3)
    assert tts.scales()[-1] == "0.8" and not tts._recent


def test_a_reply_under_way_at_the_old_speed_isnt_kept(tts):
    # Kept, it would be said again at the old speed the next time a timer rang.
    pieces = tts.stream("slow one two three")
    next(pieces)
    tts.set_speed(1.25)
    list(pieces)
    assert not tts._recent


def test_hear_it_speaks_at_the_sliders_speed_without_changing_nova(tts):
    # The page's "Hear it" plays the slider's speed before it's saved.
    time.sleep(0.3)
    waiting = tts._waiting
    assert sum(len(piece) for piece in tts.stream("one two", speed=0.75)) == 200
    assert tts.scales()[-1] == "1.3333"
    assert tts.speed == 1.0 and tts._waiting is waiting and not tts._recent


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


@pytest.mark.parametrize("written, said", [
    # Piper read these digit by digit: "two zero zero P M", "seven zero five".
    ("from 2:00 PM until 10:00 PM", "from 2 PM until 10 PM"),
    ("Your 7:00am alarm.", "Your 7am alarm."),
    ("It's 7:05 AM.", "It's 7 oh 5 AM."),
    ("the 6:00 one", "the 6 o'clock one"),
    # Which it reads properly, so are left alone.
    ("from 3:30 PM until 11:30 PM", "from 3:30 PM until 11:30 PM"),
    ("It's 10:45 PM.", "It's 10:45 PM."),
    # Midnight and noon are said as words, as Nova's own times are.
    ("You're on from 4 PM until 12 AM.", "You're on from 4 PM until midnight."),
    ("Lunch at 12:00 PM today.", "Lunch at noon today."),
    ("It's 12 a.m. now.", "It's midnight now."), ("Until 12 a.m.", "Until midnight."),
    ("It's 12:30 AM.", "It's 12:30 AM."), ("12 amps", "12 amps"),
])
def test_times_are_written_so_piper_reads_them_properly(written, said):
    from voice_assistant.text_to_speech import speakable
    assert speakable(written) == said
