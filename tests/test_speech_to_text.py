"""
Speech-to-Text Test
Checks the Whisper warm-up actually runs the model, using a fake in place of
faster-whisper. Needs numpy and faster-whisper installed (as they are on the Pi),
and is skipped anywhere they aren't.
"""

import pytest

pytest.importorskip("numpy")
pytest.importorskip("faster_whisper")

from voice_assistant import speech_to_text


class FakeWhisper:
    """
    Stands in for WhisperModel. Like the real one, its segments are a generator,
    so the "model" only runs once something reads them.
    """

    def __init__(self, *args, **kwargs):
        self.decoded = False

    def transcribe(self, audio, **kwargs):
        def segments():
            self.decoded = True
            yield from ()
        return segments(), None


def test_warm_up_actually_runs_the_model(monkeypatch):
    monkeypatch.setattr(speech_to_text, "WhisperModel", FakeWhisper)
    stt = speech_to_text.SpeechToText(model_size="base.en")
    assert stt.model.decoded
