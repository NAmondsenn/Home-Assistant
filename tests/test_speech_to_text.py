"""
Speech-to-Text Test
Checks the Whisper warm-up actually runs the model, and that what Whisper imagines
in noise is thrown away, using a fake in place of faster-whisper. Needs numpy
installed (as it is on the Pi), and is skipped anywhere it isn't.
"""

from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

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


def _segment(text, no_speech_prob=0.1, avg_logprob=-0.3):
    """One of Whisper's segments, with how sure it was there was speech, and of the words."""
    return SimpleNamespace(text=text, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob)


class ScriptedWhisper:
    """Stands in for WhisperModel, giving back whichever segments the test sets."""

    segments = []

    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, audio, **kwargs):
        return iter(ScriptedWhisper.segments), SimpleNamespace(language="en", language_probability=1.0)


@pytest.fixture
def transcribe(monkeypatch):
    monkeypatch.setattr(speech_to_text, "WhisperModel", ScriptedWhisper)
    stt = speech_to_text.SpeechToText(model_size="base.en")

    def run(*segments):
        ScriptedWhisper.segments = list(segments)
        return stt.transcribe(np.zeros(16000, dtype=np.float32))["text"]

    return run


def test_words_found_in_noise_are_thrown_away(transcribe):
    # A false wake word followed by a cough used to come back as "Thank you." and
    # be answered.
    assert transcribe(_segment(" Turn the lights off.", no_speech_prob=0.9, avg_logprob=-1.4)) == ""


@pytest.mark.parametrize("heard", [" Thank you.", " Thanks for watching!", " you", " Bye."])
def test_known_hallucinations_are_thrown_away_when_theyre_all_there_is(transcribe, heard):
    assert transcribe(_segment(heard)) == ""


@pytest.mark.parametrize("heard", [" Stop.", " Set a timer, thank you.", " Yes."])
def test_real_requests_are_kept(transcribe, heard):
    assert transcribe(_segment(heard)) == heard.strip()


def test_clear_speech_is_kept_even_if_whisper_doubted_there_was_any(transcribe):
    # Both have to point the same way before anything is thrown away.
    assert transcribe(_segment(" Pause the music.", no_speech_prob=0.8, avg_logprob=-0.2)) == "Pause the music."


def test_the_model_is_kept_where_config_says(monkeypatch):
    # It used to go to ~/.cache at first start, so a fresh install needed the internet,
    # and clearing the cache broke the next offline boot.
    made = {}

    class RememberingWhisper(ScriptedWhisper):
        def __init__(self, *args, **kwargs):
            made.update(kwargs)

    monkeypatch.setattr(speech_to_text, "WhisperModel", RememberingWhisper)
    speech_to_text.SpeechToText(model_size="base.en", download_root="/srv/nova/models/whisper")
    assert made["download_root"] == "/srv/nova/models/whisper"
