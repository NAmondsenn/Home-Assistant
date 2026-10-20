"""
Wake Word Test
Checks the detector listens through the shared microphone stream, and that the wake
word is taken off the front of what was transcribed, using fakes in place of the
openWakeWord model and the input stream. Needs numpy and soxr installed (as they
are on the Pi), and is skipped anywhere they aren't.
"""

import threading

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import microphone as microphone_module
from voice_assistant import wake_word as wake_word_module
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.wake_word import CHUNK_SAMPLES, WakeWordDetector, strip_wake_word


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


class FakeModel:
    """Stands in for openWakeWord's Model, giving each chunk the next of a list of scores."""

    scores = []

    def __init__(self, wakeword_models, inference_framework):
        self.loaded = wakeword_models
        self.chunks = []

    def reset(self):
        pass

    def predict(self, chunk):
        self.chunks.append(len(chunk))
        return {"hey_nova": FakeModel.scores[len(self.chunks) - 1]}


@pytest.fixture
def models(tmp_path, monkeypatch):
    """A project folder of its own, with a hey_nova.onnx model in it."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "hey_nova.onnx").write_bytes(b"")
    monkeypatch.setattr(wake_word_module.paths, "MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(wake_word_module, "Model", FakeModel)
    return tmp_path / "models"


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


def test_without_its_model_a_stock_wake_word_stands_in(make_config, models):
    # A fresh install has no custom model yet, and used to fail to start at all.
    (models / "hey_nova.onnx").unlink()
    detector = WakeWordDetector(config=make_config(assistant={"wake_word": "Hey Nova"}))
    assert detector.wake_word_phrase == "Hey Jarvis"
    assert detector.model.loaded == ["hey_jarvis"] and detector.wake_word_key == "hey_jarvis"


@pytest.mark.parametrize("heard, meant", [
    ("Hey Nova, what time is it?", "What time is it?"),
    ("Nova. Stop.", "Stop."),
    ("nova stop", "Stop"),
    ("Hey, Nova.", ""),
    ("What time is it?", "What time is it?"),
    # Only taken off the front, and only as a whole word.
    ("What's a nova?", "What's a nova?"),
    ("Novak Djokovic's age", "Novak Djokovic's age"),
    # The end of the wake word, misheard - all heard on the Pi.
    ("In Nova, what's the weather?", "What's the weather?"),
    ("Turn over, set a one minute timer.", "Set a one minute timer."),
    ("Get over. Cancel that alarm I just set.", "Cancel that alarm I just set."),
    # Only with punctuation straight after, so a real "over" is left alone.
    ("Over the weekend, remind me to call mum.", "Over the weekend, remind me to call mum."),
    ("I'm over it.", "I'm over it."),
])
def test_the_wake_word_is_taken_off_the_front_of_a_transcription(heard, meant):
    assert strip_wake_word(heard, "Hey Nova") == meant
