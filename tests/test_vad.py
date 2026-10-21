"""
Voice Activity Test
Checks the speech detectors the recorder uses, and that Silero is used when it can
be loaded, using a fake in place of the Silero model itself. Needs numpy installed
(as it is on the Pi), and is skipped anywhere it isn't.
"""

import pytest

np = pytest.importorskip("numpy")

import openwakeword.vad

from voice_assistant.vad import LoudnessDetector, SileroDetector, make_detector


class FakeVAD:
    """Stands in for openWakeWord's copy of Silero, keeping what it's given."""

    def __init__(self):
        self.frames = []
        self.resets = 0

    def reset_states(self):
        self.resets += 1

    def predict(self, frame, frame_size):
        self.frames.append((len(frame), frame_size))
        return 0.8


class MissingVAD:
    """Stands in for openWakeWord's VAD when its model was never downloaded."""

    def __init__(self):
        raise FileNotFoundError("silero_vad.onnx")


def test_silero_is_given_each_frame_whole_and_forgets_between_recordings(monkeypatch):
    monkeypatch.setattr(openwakeword.vad, "VAD", FakeVAD)
    detector = SileroDetector()

    assert detector.speech_probability(np.zeros(480, dtype=np.int16)) == pytest.approx(0.8)
    assert detector.model.frames == [(480, 480)]
    detector.reset()
    assert detector.model.resets == 1


def test_silero_is_used_with_the_configured_threshold(monkeypatch, make_config):
    monkeypatch.setattr(openwakeword.vad, "VAD", FakeVAD)
    detector = make_detector(make_config(thresholds={"vad_threshold": 0.7}))
    assert isinstance(detector, SileroDetector) and detector.threshold == 0.7


def test_loudness_is_used_if_silero_cannot_be_loaded(monkeypatch, make_config):
    monkeypatch.setattr(openwakeword.vad, "VAD", MissingVAD)
    detector = make_detector(make_config(thresholds={"silence_threshold": 0.1}))
    assert isinstance(detector, LoudnessDetector) and detector.silence_threshold == 0.1


def test_loudness_counts_anything_loud_enough_as_speech():
    detector = LoudnessDetector(silence_threshold=0.05)
    assert detector.speech_probability(np.full(480, 3000, dtype=np.int16)) == 1.0
    assert detector.speech_probability(np.full(480, 300, dtype=np.int16)) == 0.0
