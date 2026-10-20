"""
Speech-to-Text Comparison Test
Checks how the comparison scores what each model heard - the words, the everyday
commands, the waits - and that each model is given the recordings as it should be,
using stand-ins for the models. Needs numpy installed (as it is on the Pi), and is
skipped anywhere it isn't.
"""

import importlib.util
import json
import os
import sys
import time
import wave
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

spec = importlib.util.spec_from_file_location(
    "compare_speech_to_text", os.path.join(os.path.dirname(__file__), "..", "scripts", "compare_speech_to_text.py"))
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


@pytest.mark.parametrize("said, compared", [
    ("Set a timer for ten minutes.", ["set", "a", "timer", "for", "10", "minutes"]),
    ("set a timer for 10 minutes", ["set", "a", "timer", "for", "10", "minutes"]),
    ("Twenty-five minutes", ["25", "minutes"]),
    ("Wake me up at six thirty", ["wake", "me", "up", "at", "6", "30"]),
    ("Wake me up at 6:30", ["wake", "me", "up", "at", "6", "30"]),
    ("Remind me at four o'clock", ["remind", "me", "at", "4", "oclock"]),
    ("Remind me at 4pm", ["remind", "me", "at", "4", "pm"]),
    ("Volume to a hundred", ["volume", "to", "100"]),
    ("Volume to 50%", ["volume", "to", "50", "percent"]),
    ("What's the weather?", ["whats", "the", "weather"]),
])
def test_words_are_compared_without_case_punctuation_or_how_numbers_are_written(said, compared):
    assert compare.words(said) == compared


def test_each_word_swapped_missed_or_added_counts_as_one_wrong():
    said = compare.words("set a timer for ten minutes")
    assert compare.word_errors(said, said) == 0
    assert compare.word_errors(said, compare.words("set a time for ten minutes")) == 1
    assert compare.word_errors(said, compare.words("set timer for ten minutes")) == 1
    assert compare.word_errors(said, compare.words("yeah set a timer for ten minutes")) == 1
    assert compare.word_errors(said, []) == 6


def test_an_everyday_command_counts_as_heard_if_nova_would_do_just_the_same():
    assert compare.handled_the_same("Set a timer for ten minutes", "set a timer for 10 minutes.") is True
    # The word Moonshine made up in front would have sent it to Claude instead.
    assert compare.handled_the_same("Turn off the kitchen lights", "Yeah. Turn off the kitchen lights") is False
    # How Moonshine heard "stop" on its own, which Nova allows for.
    assert compare.handled_the_same("Stop", "Soap.") is True
    assert compare.handled_the_same("Cancel the timer", "Cancel the timer.") is True


def test_anything_else_isnt_counted_as_an_everyday_command():
    assert compare.handled_the_same("How long does it take to boil an egg", "How long to boil an egg") is None


def _result(said, heard, condition="", waiting=0.1, processing=0.5, seconds=2.0):
    return {"said": said, "heard": heard, "condition": condition, "waiting": waiting,
            "processing": processing, "seconds": seconds}


def test_a_models_results_are_added_up():
    summary = compare.summarise([
        _result("Set a timer for ten minutes", "Set a timer for 10 minutes.", waiting=0.1),
        _result("Turn off the kitchen lights", "Yeah. Turn off the kitchen lights", "across the room", waiting=0.3),
        _result("How long does it take to boil an egg", "How long does it take to boil an egg?", "across the room",
                waiting=0.2),
    ])
    assert (summary["wrong"], summary["total"], summary["exactly"]) == (1, 20, 2)
    assert summary["conditions"] == {"": [0, 6], "across the room": [1, 14]}
    assert (summary["handled"], summary["commands"]) == (1, 2)
    assert (summary["typical"], summary["worst"]) == (0.2, 0.3)
    assert summary["processing"] == pytest.approx(1.5 / 6.0)
    assert summary["mistakes"] == [("Turn off the kitchen lights", "Yeah. Turn off the kitchen lights")]


def test_the_report_reads_plainly():
    summary = compare.summarise([_result("Pause", "Pause.", "quiet"),
                                 _result("Stop", "Soup", "across the room", waiting=0.4)])
    lines = compare.report("Moonshine small-streaming, live", summary, recordings=2, live=True)

    assert lines[:2] == ["Moonshine small-streaming, live",
                         "  words wrong        50.0% (1 of 2) - quiet 0.0%, across the room 100.0%"]
    assert "  heard exactly      1 of 2" in lines
    assert "  everyday commands  1 of 2 still handled on the Pi" in lines
    assert "  waiting            0.25s typically after the recording stopped, 0.40s at worst" in lines
    assert lines[-1] == '    "Stop" heard as "Soup"'


def _write_wav(path, samples, rate=16000, channels=1):
    with wave.open(str(path), "wb") as file:
        file.setnchannels(channels)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def test_recordings_are_read_at_16khz_with_what_was_said(tmp_path):
    _write_wav(tmp_path / "001.wav", np.full(16000, 3277))
    # A second of another machine's 48kHz stereo, brought down to Nova's 16kHz mono.
    _write_wav(tmp_path / "002.wav", np.full(48000 * 2, 3277), rate=48000, channels=2)
    _write_wav(tmp_path / "003.wav", np.zeros(800))
    (tmp_path / "samples.json").write_text(json.dumps([
        {"number": 1, "file": "001.wav", "text": "Pause", "condition": ""},
        {"number": 2, "file": "002.wav", "text": "Stop", "condition": "across the room"},
        # Its words never typed in, so there's nothing to compare it with.
        {"number": 3, "file": "003.wav", "text": "", "condition": ""}]))

    samples = compare.load_samples(str(tmp_path))

    assert [sample["text"] for sample in samples] == ["Pause", "Stop"]
    assert len(samples[0]["audio"]) == 16000 and samples[0]["audio"].dtype == np.float32
    assert samples[0]["audio"][100] == pytest.approx(0.1, abs=0.001)
    assert len(samples[1]["audio"]) == pytest.approx(16000, abs=50)


def test_a_recording_which_isnt_16_bit_is_refused(tmp_path):
    with wave.open(str(tmp_path / "001.wav"), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(1)
        file.setframerate(16000)
        file.writeframes(bytes(160))
    with pytest.raises(ValueError):
        compare.read_wav(str(tmp_path / "001.wav"))


def test_each_recording_is_measured_once_the_model_has_warmed_up():
    given = []

    def transcribe(audio):
        given.append(len(audio))
        return "Pause", 0.05

    samples = [{"text": "Pause", "audio": np.zeros(16000, np.float32), "condition": "quiet"},
               {"text": "Stop", "audio": np.zeros(8000, np.float32)}]
    results = compare.measure(transcribe, samples)

    # The first recording a second time, not counted, as loading can make it slow.
    assert given == [16000, 16000, 8000]
    assert [result["said"] for result in results] == ["Pause", "Stop"]
    assert results[0]["waiting"] == 0.05 and results[1]["seconds"] == 0.5 and results[1]["condition"] == ""


def test_live_is_given_the_recording_at_the_microphones_pace_and_timed_from_its_end():
    given = []

    class Listening:
        def add(self, audio):
            given.append(len(audio))

        def finish(self, audio):
            given.append(("whole", len(audio)))
            return {"text": "Pause."}

    started = time.monotonic()
    text, waiting = compare._live(SimpleNamespace(listen=Listening), np.zeros(1200, dtype=np.float32))

    # In 30ms pieces, as the microphone hands them over, then the whole of it.
    assert given == [480, 480, 240, ("whole", 1200)]
    # Taking as long as the recording - 75ms of it.
    assert time.monotonic() - started >= 0.07
    assert text == "Pause." and waiting < 0.05


def test_parakeet_is_loaded_quantised_into_the_models_folder(monkeypatch):
    loaded = {}

    class Parakeet:
        def recognize(self, audio, sample_rate):
            loaded["given"] = (len(audio), sample_rate)
            return "Set a timer for ten minutes."

    def load_model(name, path, quantization=None):
        loaded.update(name=name, path=path, quantization=quantization)
        return Parakeet()

    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=load_model))
    label, transcribe = compare.make_model("parakeet")
    text, waiting = transcribe(np.zeros(16000, dtype=np.float32))

    assert label == "Parakeet nemo-parakeet-tdt-0.6b-v2"
    assert loaded["path"] == os.path.join(compare.PROJECT_DIRECTORY, "models", "nemo-parakeet-tdt-0.6b-v2")
    assert loaded["quantization"] == "int8" and loaded["given"] == (16000, 16000)
    assert text == "Set a timer for ten minutes." and waiting >= 0


def test_nemotron_works_on_the_recording_as_its_made_and_is_timed_from_its_end(monkeypatch):
    made, happened = {}, []

    class Recognizer:
        def __init__(self):
            self.waiting = 0

        def create_stream(self):
            return Stream(self)

        def is_ready(self, stream):
            return self.waiting > 0

        def decode_stream(self, stream):
            self.waiting -= 1
            happened.append("worked on")

        def get_result(self, stream):
            return "Pause."

    class Stream:
        def __init__(self, recognizer):
            self.recognizer = recognizer

        def accept_waveform(self, rate, samples):
            happened.append((rate, len(samples)))
            self.recognizer.waiting += 1

        def input_finished(self):
            happened.append("finished")

    def from_transducer(**settings):
        made.update(settings)
        return Recognizer()

    monkeypatch.setitem(sys.modules, "sherpa_onnx",
                        SimpleNamespace(OnlineRecognizer=SimpleNamespace(from_transducer=from_transducer)))
    label, transcribe = compare.make_model("nemotron")
    text, waiting = transcribe(np.zeros(1200, dtype=np.float32))

    folder = os.path.join(compare.PROJECT_DIRECTORY, "models",
                          "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25")
    assert label == "Nemotron streaming, 560ms chunks"
    assert (made["tokens"], made["encoder"]) == (os.path.join(folder, "tokens.txt"),
                                                 os.path.join(folder, "encoder.int8.onnx"))
    # Half a second of silence to start on, then a piece at a time as the microphone
    # hands them over, then more silence to finish the last words on.
    assert happened == [(16000, 8000), (16000, 480), "worked on", "worked on", (16000, 480), "worked on",
                        (16000, 240), "worked on", (16000, 19200), "finished", "worked on"]
    assert text == "Pause." and waiting < 0.05


def test_moonshine_is_set_up_as_novas_settings_say(monkeypatch, make_config):
    made = {}

    class Transcriber:
        def __init__(self, model_path, model_arch, options=None):
            made["where"] = model_path

        def set_keyterms(self, keyterms):
            made["keyterms"] = keyterms

        def transcribe_without_streaming(self, audio, sample_rate):
            return SimpleNamespace(lines=[SimpleNamespace(text="Pause.")])

    monkeypatch.setitem(sys.modules, "moonshine_voice", SimpleNamespace(
        string_to_model_arch=lambda name: name, Transcriber=Transcriber,
        get_model_for_language=lambda language, arch, cache_root=None, on_progress=None: (cache_root, arch)))
    monkeypatch.setattr(compare, "Config", lambda: make_config(
        moonshine={"model": "medium-streaming", "download_root": "models/moonshine", "keyterms": ["Navidrome"]}))

    label, transcribe = compare.make_model("moonshine")
    assert label == "Moonshine medium-streaming"
    assert made == {"where": os.path.join(compare.PROJECT_DIRECTORY, "models", "moonshine"),
                    "keyterms": ["Navidrome"]}
    assert transcribe(np.zeros(16000, np.float32))[0] == "Pause."
    # A model named after the colon is used instead.
    assert compare.make_model("moonshine-live:tiny-streaming")[0] == "Moonshine tiny-streaming, live"


def test_a_model_it_doesnt_know_is_refused():
    with pytest.raises(ValueError):
        compare.make_model("siri")


def test_each_model_is_reported_on_in_turn(tmp_path, monkeypatch, capsys):
    _write_wav(tmp_path / "001.wav", np.zeros(16000))
    (tmp_path / "samples.json").write_text(json.dumps(
        [{"number": 1, "file": "001.wav", "text": "Pause", "condition": ""}]))
    monkeypatch.setattr(compare, "make_model", lambda choice: (f"Stand-in {choice}", lambda audio: ("Pause.", 0.01)))

    compare.main([str(tmp_path), "first", "second"])

    printed = capsys.readouterr().out
    assert "1 recordings, 1s in all" in printed
    assert "Stand-in first" in printed and "Stand-in second" in printed
    assert "heard exactly      1 of 1" in printed


def test_with_no_recordings_it_says_how_to_make_some(tmp_path, capsys):
    compare.main([str(tmp_path), "moonshine"])
    assert "make some with record_samples.py" in capsys.readouterr().out
