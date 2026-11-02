"""
Wake Word Test
Checks the detector listens through the shared microphone stream, that a muted
microphone isn't woken, and that with a voice check only the user's voice wakes it,
using fakes in place of the openWakeWord model and the input stream, and made-up
features.
Needs numpy and soxr installed (as they are on the Pi), and is skipped anywhere they
aren't.
"""

import json
import logging
import math
import threading

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import microphone as microphone_module
from voice_assistant import voice_check as voice_check_module
from voice_assistant import wake_word as wake_word_module
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.wake_word import CHUNK_SAMPLES, WakeWordDetector


class FakeInputStream:
    """Stands in for sounddevice.InputStream, delivering only what the test feeds it."""

    def __init__(self, callback, **kwargs):
        self.callback = callback

    def start(self):
        pass

    def close(self, ignore_errors=False):
        pass

    def feed(self, samples):
        self.callback(np.zeros((samples, 1), dtype=np.float32), samples, None, None)


class FakePreprocessor:
    """Gives the features of the moment just predicted - every one of them the number of chunks so far."""

    def __init__(self, model):
        self.model = model

    def get_features(self, frames):
        return np.full((1, frames, voice_check_module.FEATURES), float(len(self.model.chunks)))


class FakeModel:
    """Stands in for openWakeWord's Model, giving each chunk the next of a list of scores."""

    scores = []
    # How many frames of features the model judges each moment on.
    frames = voice_check_module.DEFAULT_FRAMES

    def __init__(self, wakeword_models, inference_framework):
        self.loaded = wakeword_models
        self.chunks = []
        key = "hey_jarvis" if wakeword_models == ["hey_jarvis"] else "hey_nova"
        self.model_inputs = {key: FakeModel.frames}
        self.preprocessor = FakePreprocessor(self)

    def reset(self):
        pass

    def predict(self, chunk):
        self.chunks.append(len(chunk))
        return {"hey_nova": FakeModel.scores[len(self.chunks) - 1]}


class FakeCheck:
    """Stands in for a trained voice check, giving each moment it's asked about the next of a list of scores."""

    def __init__(self, scores):
        self.scores = list(scores)
        self.asked = []

    def score(self, features):
        self.asked.append(features)
        return self.scores[len(self.asked) - 1]


@pytest.fixture
def models(tmp_path, monkeypatch):
    """
    A project folder of its own, with a hey_nova.onnx model in it - and data files of
    its own for the mute and the voice check.
    """
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "hey_nova.onnx").write_bytes(b"")
    monkeypatch.setattr(wake_word_module.paths, "MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(wake_word_module, "MICROPHONE_FILE", str(tmp_path / "microphone.json"))
    monkeypatch.setattr(voice_check_module, "FILE", str(tmp_path / "voice_check.json"))
    monkeypatch.setattr(wake_word_module, "Model", FakeModel)
    return tmp_path / "models"


@pytest.fixture
def stream(monkeypatch):
    """The shared microphone, and a way to feed it chunks a moment after listening starts."""
    streams = []
    monkeypatch.setattr(microphone_module.sd, "InputStream",
                        lambda **kwargs: streams.append(FakeInputStream(**kwargs)) or streams[-1])
    microphone = Microphone(sample_rate=RATE)

    def feed(chunks):
        threading.Timer(0.05, streams[0].feed, args=(chunks * CHUNK_SAMPLES,)).start()

    return microphone, feed


def save_check(models, score, model="hey_nova"):
    """
    Saves a check which gives every moment the same score, as a real one would be saved.

    Args:
        models: The models folder, from the models fixture - the check goes beside it.
        score: The score it gives, from 0 to 1, but neither.
        model: The wake word model it's for.
    """
    length = voice_check_module.DEFAULT_FRAMES * voice_check_module.FEATURES
    voice_check_module.save({
        "version": 1, "model": model, "frames": voice_check_module.DEFAULT_FRAMES,
        "features": voice_check_module.FEATURES,
        "mean": [0.0] * length, "scale": [1.0] * length, "weights": [0.0] * length,
        # With no weights, the score is the bias alone, put through the sigmoid.
        "bias": math.log(score / (1 - score)), "trained": "2026-11-02", "counts": {"pos": 1, "neg": 1},
    }, str(models.parent / "voice_check.json"))


def test_the_wake_word_is_heard_through_the_shared_microphone(monkeypatch, make_config, models):
    streams = []
    monkeypatch.setattr(microphone_module.sd, "InputStream",
                        lambda **kwargs: streams.append(FakeInputStream(**kwargs)) or streams[-1])
    FakeModel.scores = [0.1, 0.2, 0.9]

    microphone = Microphone(sample_rate=RATE)
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}),
                                microphone=microphone)
    started_at = microphone.position
    threading.Timer(0.05, streams[0].feed, args=(3 * CHUNK_SAMPLES,)).start()

    assert detector.listen_once() == 0.9
    # Read straight off the stream in 80ms chunks, with no resampling of its own.
    assert detector.model.chunks == [CHUNK_SAMPLES] * 3
    assert detector.heard_at == started_at + 3 * CHUNK_SAMPLES
    # The shared stream is used, rather than one of its own.
    assert len(streams) == 1


def test_the_last_couple_of_seconds_are_caught_up_on_but_cant_wake_it(monkeypatch, make_config, models):
    # Started from nothing, the model couldn't hear "Hey Nova" for about a second
    # after each reply.
    streams = []
    monkeypatch.setattr(microphone_module.sd, "InputStream",
                        lambda **kwargs: streams.append(FakeInputStream(**kwargs)) or streams[-1])
    microphone = Microphone(sample_rate=RATE)
    streams[0].feed(5 * RATE)
    caught_up = int(wake_word_module.CATCH_UP_SECONDS * RATE) // CHUNK_SAMPLES
    # High scores while catching up are the past - Nova's own reply, say - and are ignored.
    FakeModel.scores = [0.9] * caught_up + [0.1, 0.95]

    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    threading.Timer(0.05, streams[0].feed, args=(3 * CHUNK_SAMPLES,)).start()
    assert detector.listen_once() == 0.95
    assert len(detector.model.chunks) == caught_up + 2


def test_the_last_wake_word_is_never_heard_again(monkeypatch, make_config, models):
    streams = []
    monkeypatch.setattr(microphone_module.sd, "InputStream",
                        lambda **kwargs: streams.append(FakeInputStream(**kwargs)) or streams[-1])
    microphone = Microphone(sample_rate=RATE)
    streams[0].feed(5 * RATE)
    FakeModel.scores = [0.9, 0.95]

    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.heard_at = microphone.position - CHUNK_SAMPLES
    threading.Timer(0.05, streams[0].feed, args=(CHUNK_SAMPLES,)).start()
    # Only the one chunk since the last wake word is caught up on, then the new audio.
    assert detector.listen_once() == 0.95
    assert len(detector.model.chunks) == 2


def test_listening_over_a_reply_stops_once_the_reply_has_finished(monkeypatch, make_config, models):
    # Listened for over a reply, so it can be talked over - but only until it ends.
    streams = []
    monkeypatch.setattr(microphone_module.sd, "InputStream",
                        lambda **kwargs: streams.append(FakeInputStream(**kwargs)) or streams[-1])
    microphone = Microphone(sample_rate=RATE)
    FakeModel.scores = [0.1, 0.2, 0.9]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.heard_at = microphone.position
    streams[0].feed(2 * CHUNK_SAMPLES)

    checks = []
    assert detector.listen_once(until=lambda: checks.append(1) or len(checks) > 2) is None
    assert len(detector.model.chunks) == 2
    # Where the last wake word was heard is left alone, for the recording.
    assert detector.heard_at == microphone.position - 2 * CHUNK_SAMPLES


def test_the_wake_word_said_over_a_reply_is_heard(monkeypatch, make_config, models):
    streams = []
    monkeypatch.setattr(microphone_module.sd, "InputStream",
                        lambda **kwargs: streams.append(FakeInputStream(**kwargs)) or streams[-1])
    microphone = Microphone(sample_rate=RATE)
    FakeModel.scores = [0.1, 0.9]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    started_at = microphone.position
    threading.Timer(0.05, streams[0].feed, args=(2 * CHUNK_SAMPLES,)).start()

    assert detector.listen_once(until=lambda: False) == 0.9
    assert detector.heard_at == started_at + 2 * CHUNK_SAMPLES


def test_a_muted_microphone_isnt_woken(monkeypatch, make_config, models):
    # The microphone is still read while muted, so where it's got to - which catching
    # up, and the recording after the wake word, go by - stays right.
    streams = []
    monkeypatch.setattr(microphone_module.sd, "InputStream",
                        lambda **kwargs: streams.append(FakeInputStream(**kwargs)) or streams[-1])
    microphone = Microphone(sample_rate=RATE)
    FakeModel.scores = [0.9, 0.95] + [0.9] * 3
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.set_muted(True)
    assert detector.muted

    checks = []
    threading.Timer(0.05, streams[0].feed, args=(2 * CHUNK_SAMPLES,)).start()
    assert detector.listen_once(until=lambda: checks.append(1) or len(checks) > 2) is None
    assert len(detector.model.chunks) == 2 and detector.heard_at is None

    detector.set_muted(False)
    assert not detector.muted
    threading.Timer(0.05, streams[0].feed, args=(CHUNK_SAMPLES,)).start()
    assert detector.listen_once() == 0.9
    assert detector.heard_at == microphone.position


def test_mute_lasts_through_a_restart(make_config, models):
    config = make_config(assistant={"wake_word": "Hey Nova"})
    WakeWordDetector(config=config).set_muted(True)
    assert json.loads((models.parent / "microphone.json").read_text()) == {"muted": True}
    restarted = WakeWordDetector(config=config)
    assert restarted.muted

    restarted.set_muted(False)
    assert not WakeWordDetector(config=config).muted


def test_without_its_model_a_stock_wake_word_stands_in(make_config, models):
    # A fresh install has no custom model yet, and used to fail to start at all.
    (models / "hey_nova.onnx").unlink()
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}))
    assert detector.wake_word_phrase == "Hey Jarvis"
    assert detector.model.loaded == ["hey_jarvis"] and detector.wake_word_key == "hey_jarvis"


def wake_word_log(caplog):
    return [record.getMessage() for record in caplog.records if record.name == wake_word_module.__name__]


def test_with_no_voice_check_it_wakes_as_before(make_config, models, stream, caplog):
    microphone, feed = stream
    FakeModel.scores = [0.1, 0.9]
    with caplog.at_level(logging.INFO, logger=wake_word_module.__name__):
        detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
        assert detector.voice_check is None and detector.voice_check_threshold == 0.2
        feed(2)
        assert detector.listen_once() == 0.9
    assert wake_word_log(caplog)[-1] == "Wake word detected! (confidence: 0.90)"
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_a_wake_not_in_the_users_voice_doesnt_wake_and_says_both_scores(make_config, models, stream, caplog):
    microphone, feed = stream
    save_check(models, 0.04)
    FakeModel.scores = [0.1, 0.81, 0.3]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    assert detector.voice_check is not None
    # Only what listening says, whatever level an earlier test left the logger at.
    caplog.clear()

    feed(3)
    with caplog.at_level(logging.INFO, logger=wake_word_module.__name__):
        # Listening goes on past it, as if nothing was heard.
        assert detector.listen_once(until=lambda: len(detector.model.chunks) == 3) is None
    assert detector.heard_at is None
    assert wake_word_log(caplog) == ["Heard 'Hey Nova' (0.81) but not in your voice (0.04), so not waking"]


def test_a_wake_in_the_users_voice_wakes_and_says_both_scores(make_config, models, stream, caplog):
    microphone, feed = stream
    save_check(models, 0.6)
    FakeModel.scores = [0.1, 0.9]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    started_at = microphone.position

    feed(2)
    with caplog.at_level(logging.INFO, logger=wake_word_module.__name__):
        assert detector.listen_once() == 0.9
    assert detector.heard_at == started_at + 2 * CHUNK_SAMPLES
    assert wake_word_log(caplog)[-1] == "Wake word detected! (confidence: 0.90, voice: 0.60)"


@pytest.mark.parametrize("voice, woken", [(0.3, True), (0.29, False)])
def test_the_check_must_score_at_or_above_its_setting(make_config, models, stream, voice, woken):
    microphone, feed = stream
    FakeModel.scores = [0.9, 0.1]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.voice_check = FakeCheck([voice])
    detector.voice_check_threshold = 0.3

    feed(2)
    heard = detector.listen_once(until=lambda: len(detector.model.chunks) == 2)
    assert heard == (0.9 if woken else None)


def test_a_setting_of_0_wakes_without_asking_the_check(make_config, models, stream):
    microphone, feed = stream
    FakeModel.scores = [0.9]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.voice_check = check = FakeCheck([0.0])
    detector.voice_check_threshold = 0

    feed(1)
    assert detector.listen_once() == 0.9
    assert check.asked == []


def test_muted_nothing_wakes_it_and_the_check_isnt_asked(make_config, models, stream):
    microphone, feed = stream
    FakeModel.scores = [0.9, 0.95]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.voice_check = check = FakeCheck([1.0, 1.0])
    detector.set_muted(True)

    feed(2)
    assert detector.listen_once(until=lambda: len(detector.model.chunks) == 2) is None
    assert check.asked == [] and detector.heard_at is None


def test_the_check_is_asked_about_the_moment_the_wake_word_scored(make_config, models, stream):
    microphone, feed = stream
    # The wake word scores over two moments in a row, and only the second is the user's voice.
    FakeModel.scores = [0.1, 0.9, 0.95]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.voice_check = check = FakeCheck([0.05, 0.7])
    started_at = microphone.position

    feed(3)
    assert detector.listen_once() == 0.95
    assert detector.heard_at == started_at + 3 * CHUNK_SAMPLES
    # The features of the second and third chunks, a window of the wake word model's own size.
    assert [features.shape for features in check.asked] == [(1, 16, 96)] * 2
    assert [features[0, 0, 0] for features in check.asked] == [2.0, 3.0]


def test_a_check_which_goes_wrong_lets_the_wake_through(make_config, models, stream, caplog):
    # The user shut out by a fault is worse than a film waking Nova.
    class Broken:
        def score(self, features):
            raise ValueError("1536 features, but the voice check was trained on 1152")

    microphone, feed = stream
    FakeModel.scores = [0.9]
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    detector.voice_check = Broken()

    feed(1)
    with caplog.at_level(logging.INFO, logger=wake_word_module.__name__):
        assert detector.listen_once() == 0.9
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "1536 features" in warnings[0]
    assert wake_word_log(caplog)[-1] == "Wake word detected! (confidence: 0.90)"


@pytest.mark.parametrize("model, frames", [("hey_jarvis", 16), ("hey_nova", 12)], ids=["model", "frames"])
def test_a_check_for_another_wake_word_model_isnt_used(make_config, models, stream, caplog, monkeypatch,
                                                       model, frames):
    # Its features would mean nothing to the check, so any voice wakes Nova, as with no check.
    microphone, feed = stream
    save_check(models, 0.04, model=model)
    monkeypatch.setattr(FakeModel, "frames", frames)
    FakeModel.scores = [0.9]
    with caplog.at_level(logging.WARNING, logger=voice_check_module.__name__):
        detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}), microphone=microphone)
    assert detector.voice_check is None
    assert len(caplog.records) == 1

    feed(1)
    assert detector.listen_once() == 0.9
