"""
Record Wake Samples Test
Checks the script which records what the voice check is trained on keeps each take
and each file where it should, numbers them on from what's there, retakes a "Hey Nova"
which wasn't heard, and keeps what's finished when stopped - with stand-ins for the
microphone, the wake word model and the blip. Needs numpy installed (as it is on the
Pi), and is skipped anywhere it isn't.
"""

import importlib.util
import math
import os
import sys
import types
import stat
import wave

import pytest

np = pytest.importorskip("numpy")

spec = importlib.util.spec_from_file_location(
    "record_wake_samples", os.path.join(os.path.dirname(__file__), "..", "scripts", "record_wake_samples.py"))
wake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wake)

RATE = 16000
CHUNK_SAMPLES = 1280


class Session:
    """Stands in for the blip, the microphone and the model, giving back what the test lines up."""

    def __init__(self, scores=(0.9,), stop_after=None):
        self.scores = list(scores)
        self.stop_after = stop_after
        self.events = []
        self.shown = []
        self.pauses = []
        self.heard = 0

    def blip(self):
        self.events.append("blip")

    def take(self):
        self.events.append("take")
        if self.stop_after is not None and self.heard == self.stop_after:
            raise KeyboardInterrupt
        # Each take a different level, so the file's the take it was meant to be.
        self.heard += 1
        return np.full(3 * RATE, self.heard / 100, dtype=np.float32), self.scores.pop(0)

    def read(self, count):
        if self.stop_after is not None and self.heard >= self.stop_after * 10 * RATE:
            raise KeyboardInterrupt
        self.heard += count
        return np.full(count, 0.5, dtype=np.float32)


def _hey_nova(folder, count, session, label="close"):
    return wake.record_hey_nova(count, label, str(folder), session.blip, session.take, session.pauses.append,
                                session.shown.append)


def _stretch(folder, seconds, session, label="film"):
    return wake.record_stretch(seconds, label, str(folder), session.read, session.shown.append)


def _files(folder, kind):
    return sorted(os.listdir(folder / kind))


def _frames(path):
    with wave.open(str(path)) as file:
        assert (file.getnchannels(), file.getsampwidth(), file.getframerate()) == (1, 2, RATE)
        return file.getnframes()


def test_n_takes_make_n_files_each_after_a_blip_and_scored(tmp_path):
    session = Session(scores=[0.91, 0.5, 0.7])
    assert _hey_nova(tmp_path, 3, session) == 3

    assert _files(tmp_path, "hey_nova") == ["close-001.wav", "close-002.wav", "close-003.wav"]
    assert session.events == ["blip", "take"] * 3
    assert session.shown[:3] == ["1 of 3: 0.91", "2 of 3: 0.50", "3 of 3: 0.70"]
    # Between takes, but not after the last.
    assert session.pauses == [wake.PAUSE_SECONDS] * 2
    assert _frames(tmp_path / "hey_nova" / "close-001.wav") == 3 * RATE


def test_numbering_carries_on_from_what_is_there_for_each_label(tmp_path):
    _hey_nova(tmp_path, 2, Session(scores=[0.9] * 2))
    _hey_nova(tmp_path, 1, Session(scores=[0.9]), label="far")
    # Gaps are left alone: the next is one on from the highest.
    os.remove(tmp_path / "hey_nova" / "close-001.wav")
    _hey_nova(tmp_path, 2, Session(scores=[0.9] * 2))

    assert _files(tmp_path, "hey_nova") == ["close-002.wav", "close-003.wav", "close-004.wav", "far-001.wav"]


def test_a_label_isnt_mistaken_for_one_which_ends_the_same(tmp_path):
    _hey_nova(tmp_path, 1, Session(scores=[0.9]), label="close")
    _hey_nova(tmp_path, 1, Session(scores=[0.9]), label="very-close")
    _hey_nova(tmp_path, 1, Session(scores=[0.9]), label="close")
    assert _files(tmp_path, "hey_nova") == ["close-001.wav", "close-002.wav", "very-close-001.wav"]


def test_one_under_what_training_keeps_is_taken_again_even_if_it_was_heard(tmp_path):
    # 0.35 is a Hey Nova which was heard, but openWakeWord's training only keeps 0.5 and over.
    session = Session(scores=[0.35, 0.8])
    assert _hey_nova(tmp_path, 1, session) == 1

    assert session.events == ["blip", "take"] * 2
    assert session.shown[:2] == ["1 of 1: 0.35 - under 0.5, taking it again", "1 of 1: 0.80 (best of 2)"]
    assert _files(tmp_path, "hey_nova") == ["close-001.wav"]


def test_the_best_of_the_goes_is_the_one_kept(tmp_path):
    session = Session(scores=[0.3, 0.45, 0.1])
    assert _hey_nova(tmp_path, 1, session) == 1

    # Three goes, and not four.
    assert session.events == ["blip", "take"] * 3
    assert _files(tmp_path, "hey_nova") == ["close-001.wav"]
    with wave.open(str(tmp_path / "hey_nova" / "close-001.wav")) as file:
        kept = np.frombuffer(file.readframes(10), dtype=np.int16)
    # The second go's recording, each being a different level.
    assert kept[0] == round(0.02 * 32768)
    assert session.shown[:3] == ["1 of 1: 0.30 - under 0.5, taking it again",
                                 "1 of 1: 0.45 - under 0.5, taking it again",
                                 "1 of 1: 0.45 (best of 3) - too low to train on"]


def test_the_best_is_kept_when_the_last_is_the_worst_and_when_the_first_is_the_best(tmp_path):
    for scores, level in (([0.1, 0.2, 0.0], 0.02), ([0.4, 0.2, 0.3], 0.01)):
        folder = tmp_path / str(level)
        _hey_nova(folder, 1, Session(scores=scores))
        with wave.open(str(folder / "hey_nova" / "close-001.wav")) as file:
            assert np.frombuffer(file.readframes(1), dtype=np.int16)[0] == round(level * 32768)


def test_one_still_too_low_after_two_retakes_is_kept_all_the_same(tmp_path):
    session = Session(scores=[0.0, 0.0, 0.0, 0.9])
    assert _hey_nova(tmp_path, 2, session) == 2

    # Three goes at the first, not four, then one for the second.
    assert session.events.count("take") == 4
    assert _files(tmp_path, "hey_nova") == ["close-001.wav", "close-002.wav"]
    assert session.shown[2] == "1 of 2: 0.00 (best of 3) - too low to train on"


def test_the_run_ends_by_saying_how_many_are_good_enough_to_train_on(tmp_path):
    session = Session(scores=[0.9, 0.1, 0.1, 0.1, 0.6])
    assert _hey_nova(tmp_path, 3, session) == 3

    assert session.shown[-1] == f"Done - 2 of 3 are good enough to train on. Kept in {tmp_path / 'hey_nova'}."


def test_a_score_of_exactly_half_is_enough(tmp_path):
    session = Session(scores=[0.5])
    _hey_nova(tmp_path, 1, session)
    assert session.events == ["blip", "take"]


def test_the_folders_are_private_to_this_user_and_so_are_the_files(tmp_path):
    root = tmp_path / "voice-samples" / "wake"
    _hey_nova(root, 1, Session(scores=[0.9]))
    _stretch(root, 4, Session())

    for folder in (root.parent, root, root / "hey_nova", root / "not_hey_nova"):
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700, folder
    for path in (root / "hey_nova" / "close-001.wav", root / "not_hey_nova" / "film-001.wav"):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_folder_which_is_already_there_is_made_private_too(tmp_path):
    root = tmp_path / "wake"
    (root / "hey_nova").mkdir(parents=True)
    os.chmod(root, 0o755)
    os.chmod(root / "hey_nova", 0o755)
    _hey_nova(root, 1, Session(scores=[0.9]))
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "hey_nova").st_mode) == 0o700


def test_a_relative_folder_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _hey_nova("data/wake", 1, Session(scores=[0.9]))
    assert os.listdir(tmp_path / "data" / "wake" / "hey_nova") == ["close-001.wav"]


def test_a_loose_umask_doesnt_open_the_folders_up(tmp_path):
    old = os.umask(0o000)
    try:
        _hey_nova(tmp_path / "a" / "wake", 1, Session(scores=[0.9]))
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "a").st_mode) == 0o700


def test_a_film_is_split_into_ten_second_files_with_the_rest_in_the_last(tmp_path):
    session = Session()
    assert _stretch(tmp_path, 25, session) == 3

    names = _files(tmp_path, "not_hey_nova")
    assert names == ["film-001.wav", "film-002.wav", "film-003.wav"]
    assert [_frames(tmp_path / "not_hey_nova" / name) for name in names] == [10 * RATE, 10 * RATE, 5 * RATE]
    assert session.shown == ["10 of 25 seconds", "20 of 25 seconds", "25 of 25 seconds",
                             f"Done - kept 3 files in {tmp_path / 'not_hey_nova'}."]
    assert not (tmp_path / "hey_nova").exists()


def test_one_file_is_said_in_the_singular(tmp_path):
    session = Session()
    _stretch(tmp_path, 5, session)
    assert session.shown[-1] == f"Done - kept 1 file in {tmp_path / 'not_hey_nova'}."


def test_a_stretch_which_is_a_whole_number_of_files_has_no_empty_one(tmp_path):
    assert _stretch(tmp_path, 20, Session(), label="talk") == 2
    assert _files(tmp_path, "not_hey_nova") == ["talk-001.wav", "talk-002.wav"]


def test_a_stretch_is_read_a_second_at_a_time_without_gaps(tmp_path):
    asked = []

    def read(count):
        asked.append(count)
        return np.zeros(count, dtype=np.float32)

    wake.record_stretch(10.5, "film", str(tmp_path), read, lambda line: None)
    assert max(asked) == RATE
    assert sum(asked) == 10.5 * RATE


def test_talking_carries_on_numbering_from_a_film_with_its_own_label(tmp_path):
    _stretch(tmp_path, 10, Session(), label="film")
    _stretch(tmp_path, 10, Session(), label="talk")
    _stretch(tmp_path, 10, Session(), label="film")
    assert _files(tmp_path, "not_hey_nova") == ["film-001.wav", "film-002.wav", "talk-001.wav"]


def test_stopped_part_way_through_the_takes_keeps_what_is_done(tmp_path):
    session = Session(scores=[0.9] * 5, stop_after=2)
    assert _hey_nova(tmp_path, 5, session) == 2

    assert _files(tmp_path, "hey_nova") == ["close-001.wav", "close-002.wav"]
    # One closing line, which says how it ended.
    assert session.shown[-1] == (f"Stopped - kept 2 of 5, and 2 of 2 are good enough to train on. "
                                 f"They're in {tmp_path / 'hey_nova'}.")
    assert sum(line.startswith(("Stopped", "Done")) for line in session.shown) == 1


def test_stopped_part_way_through_a_film_keeps_the_finished_files(tmp_path):
    # Stops during the third file, which isn't finished.
    session = Session(stop_after=2)
    assert _stretch(tmp_path, 60, session) == 2

    assert _files(tmp_path, "not_hey_nova") == ["film-001.wav", "film-002.wav"]
    assert session.shown[-1] == f"Stopped - kept 2 files in {tmp_path / 'not_hey_nova'}."
    assert sum(line.startswith(("Stopped", "Done")) for line in session.shown) == 1


class FakeReader:
    def __init__(self, samples):
        self.samples = samples
        self.reads = []

    def read(self, count):
        self.reads.append(count)
        return self.samples[:count]


class FakeMicrophone:
    def __init__(self, samples):
        self.reader = FakeReader(samples)
        self.listened = 0

    def listen(self):
        self.listened += 1
        return self.reader


class FakeModel:
    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = []
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.calls.append("reset")

    def predict(self, chunk):
        self.calls.append(len(chunk))
        return {"hey_nova": self.scores.pop(0), "other": 1.0}


def test_a_take_is_scored_chunk_by_chunk_on_a_reset_model_and_saved_as_floats():
    chunks = math.ceil(3 * RATE / CHUNK_SAMPLES)
    microphone = FakeMicrophone(np.full(CHUNK_SAMPLES, 16384, dtype=np.int16))
    # The best is in the middle, not the end.
    model = FakeModel([0.1] * 10 + [0.93] + [0.2] * (chunks - 11))

    audio, score = wake.listen_for_take(microphone, model, "hey_nova", CHUNK_SAMPLES)

    assert score == 0.93
    assert model.calls == ["reset"] + [CHUNK_SAMPLES] * chunks
    assert microphone.listened == 1
    assert audio.dtype == np.float32 and len(audio) == 3 * RATE
    assert audio[0] == 0.5


def test_a_take_with_no_score_for_the_wake_word_scores_nothing():
    microphone = FakeMicrophone(np.zeros(CHUNK_SAMPLES, dtype=np.int16))
    model = FakeModel([0.9] * 38)
    model.predict = lambda chunk: {"other": 1.0}
    assert wake.listen_for_take(microphone, model, "hey_nova", CHUNK_SAMPLES)[1] == 0.0


def test_a_label_can_only_be_one_safe_in_a_file_name():
    import argparse
    assert wake._label("across-the-room_2") == "across-the-room_2"
    for bad in ("", "../x", "a/b", "a b", "close.wav"):
        with pytest.raises(argparse.ArgumentTypeError):
            wake._label(bad)


def test_the_blip_is_waited_for_and_then_a_moment_more_for_the_microphone_to_catch_up():
    events = []

    class Playback:
        def wait(self):
            events.append("wait")

    class Speaker:
        def play_file(self, path, volume):
            events.append(("play", os.path.basename(path), volume))
            return Playback()

    wake.play_blip(Speaker(), lambda seconds: events.append(("sleep", seconds)))
    # The microphone runs behind the speaker, so a take starting the moment the blip ends
    # would have the end of it in.
    assert events == [("play", "listening.wav", 0.5), "wait", ("sleep", wake.BLIP_TAIL_SECONDS)]
    assert wake.BLIP_TAIL_SECONDS >= 0.15


def test_no_blip_file_doesnt_stop_a_run():
    class Speaker:
        def play_file(self, path, volume):
            return None

    wake.play_blip(Speaker(), lambda seconds: None)


class Closable:
    closed = 0

    def close(self):
        self.closed += 1


def _fake_voice_assistant(monkeypatch, tmp_path, samples, scores=()):
    """Stands in for the five modules main() imports late, and gives back what it opens."""
    opened = types.SimpleNamespace(played=[], microphone=None, speaker=None, model=None)

    class Config:
        def section(self, name):
            return {}

    class Reader:
        def read(self, count):
            return samples[:count]

    class Microphone(Closable):
        def __init__(self, sample_rate, echo=None):
            opened.microphone = self

        def listen(self):
            return Reader()

    class Playback:
        def wait(self):
            pass

    class Speaker(Closable):
        def __init__(self, sample_rate, echo_reference=None):
            opened.speaker = self

        def play_file(self, path, volume):
            opened.played.append(os.path.basename(path))
            return Playback()

    class Model:
        def __init__(self):
            self.scores = list(scores)

        def reset(self):
            pass

        def predict(self, chunk):
            return {"hey_nova": self.scores.pop(0) if self.scores else 0.0}

    class Detector:
        wake_word_key = "hey_nova"

        def __init__(self, config, microphone):
            self.model = opened.model = Model()

    modules = {
        "voice_assistant.config": types.SimpleNamespace(Config=Config),
        "voice_assistant.echo": types.SimpleNamespace(make_echo_canceller=lambda config, rate: None),
        "voice_assistant.microphone": types.SimpleNamespace(Microphone=Microphone),
        "voice_assistant.speaker": types.SimpleNamespace(Speaker=Speaker),
        "voice_assistant.wake_word": types.SimpleNamespace(WakeWordDetector=Detector, CHUNK_SAMPLES=CHUNK_SAMPLES)}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(wake.paths, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wake.time, "sleep", lambda seconds: None)
    return opened


def test_a_film_run_saves_what_the_microphone_gives_as_16_bit_samples(tmp_path, monkeypatch, capsys):
    # The microphone's 16-bit samples, which have to come out of the file as they went in.
    samples = np.array([0, 16384, -16384, 32767, -32768] * 3200, dtype=np.int16)
    opened = _fake_voice_assistant(monkeypatch, tmp_path, samples)
    monkeypatch.setattr(sys, "argv", ["record_wake_samples.py", "film", "--seconds", "12"])

    wake.main()

    folder = tmp_path / "voice-samples" / "wake" / "not_hey_nova"
    assert sorted(os.listdir(folder)) == ["film-001.wav", "film-002.wav"]
    with wave.open(str(folder / "film-001.wav")) as file:
        assert file.getnframes() == 10 * RATE
        saved = np.frombuffer(file.readframes(5), dtype=np.int16)
    assert list(saved) == [0, 16384, -16384, 32767, -32768]
    assert opened.microphone.closed == 1 and opened.speaker.closed == 1
    lines = capsys.readouterr().out.splitlines()
    # Said as it starts, since the first file takes ten seconds to arrive.
    assert lines[0] == "Recording 12 seconds as film - keep it quiet of anything but the film."


def test_a_talk_run_says_what_to_do(tmp_path, monkeypatch, capsys):
    _fake_voice_assistant(monkeypatch, tmp_path, np.zeros(RATE, dtype=np.int16))
    monkeypatch.setattr(sys, "argv", ["record_wake_samples.py", "talk", "--seconds", "3", "--label", "kitchen"])
    wake.main()
    assert capsys.readouterr().out.splitlines()[0] == (
        "Recording 3 seconds as kitchen - talk as you normally would, without saying Hey Nova.")
    assert os.listdir(tmp_path / "voice-samples" / "wake" / "not_hey_nova") == ["kitchen-001.wav"]


def test_a_hey_nova_run_blips_scores_and_closes_everything(tmp_path, monkeypatch, capsys):
    samples = np.full(CHUNK_SAMPLES, 16384, dtype=np.int16)
    opened = _fake_voice_assistant(monkeypatch, tmp_path, samples, scores=[0.9] + [0.0] * 200)
    monkeypatch.setattr(sys, "argv", ["record_wake_samples.py", "hey-nova", "--count", "1", "--label", "close"])

    wake.main()

    folder = tmp_path / "voice-samples" / "wake" / "hey_nova"
    with wave.open(str(folder / "close-001.wav")) as file:
        assert file.getnframes() == 3 * RATE
        assert np.frombuffer(file.readframes(1), dtype=np.int16)[0] == 16384
    assert opened.played == ["listening.wav"]
    assert opened.microphone.closed == 1 and opened.speaker.closed == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Scoring with the 'hey_nova' model")
    assert out[1] == "1 of 1: 0.90"
    assert out[-1].startswith("Done - 1 of 1 are good enough to train on.")
