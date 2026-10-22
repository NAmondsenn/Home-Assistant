"""
Speech-to-Text Test
Checks that what speech models imagine in noise is thrown away, and that the brain's
Nemotron speech-to-text hears each recording as it's made. Needs numpy installed (as
it is on the Pi), and is skipped anywhere it isn't.
"""

from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import speech_to_text


def _segment(text, no_speech_prob=0.1, avg_logprob=-0.3):
    """One of Whisper's segments, with how sure it was there was speech, and of the words."""
    return SimpleNamespace(text=text, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob)


def test_words_found_in_noise_are_thrown_away():
    # A false wake word followed by a cough used to come back as "Thank you." and
    # be answered.
    segment = _segment(" Turn the lights off.", no_speech_prob=0.9, avg_logprob=-1.4)
    assert speech_to_text.looks_like_hallucination("Turn the lights off.", [segment])


@pytest.mark.parametrize("heard", [" Thank you.", " Thanks for watching!", " you", " Bye."])
def test_known_hallucinations_are_thrown_away_when_theyre_all_there_is(heard):
    assert speech_to_text.looks_like_hallucination(heard, [_segment(heard)])


@pytest.mark.parametrize("heard", [" Stop.", " Set a timer, thank you.", " Yes."])
def test_real_requests_are_kept(heard):
    assert not speech_to_text.looks_like_hallucination(heard, [_segment(heard)])


def test_clear_speech_is_kept_even_if_whisper_doubted_there_was_any():
    # Both have to point the same way before anything is thrown away.
    segment = _segment(" Pause the music.", no_speech_prob=0.8, avg_logprob=-0.2)
    assert not speech_to_text.looks_like_hallucination("Pause the music.", [segment])


def _pieces(*values):
    """Pieces of a recording, each 30ms long and all one value, as the microphone hands them over."""
    return [np.full(480, value, dtype=np.float32) for value in values]


# Nemotron, on the brain - it hears every satellite's recordings, as the backend did.

class FakeNemotron:
    """
    Stands in for Nemotron, hearing whatever the test sets. Each recording's stream is
    the list of pieces it was given, and every stream is kept.
    """

    def __init__(self, heard="Set a timer for ten minutes."):
        self.heard = heard
        self.streams = []
        self.finished = []
        # Set to an Event to keep it busy with each piece until the test lets it go,
        # or to an exception for the next piece to raise.
        self.busy = None
        self.fail = None

    def start(self):
        self.streams.append([])
        return self.streams[-1]

    def add(self, stream, audio):
        if self.busy is not None:
            self.busy.wait(2)
        if self.fail is not None:
            failure, self.fail = self.fail, None
            raise failure
        stream.append(audio)

    def finish(self, stream):
        self.finished.append(stream)
        return self.heard


def test_nemotron_hears_the_recording_as_its_made():
    nemotron = FakeNemotron()
    stt = speech_to_text.NemotronSpeechToText(nemotron)
    transcription = stt.listen()
    pieces = _pieces(0.1, 0.2, 0.3)
    for piece in pieces:
        transcription.add(piece)

    assert transcription.finish(np.concatenate(pieces))["text"] == "Set a timer for ten minutes."
    # In order, through the one stream - and not all over again once it was over.
    assert len(nemotron.streams) == 1
    assert np.array_equal(np.concatenate(nemotron.streams[0]), np.concatenate(pieces))
    assert stt.loaded


def test_the_recording_never_waits_for_nemotron():
    import threading

    nemotron = FakeNemotron()
    nemotron.busy = threading.Event()
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()
    transcription.add(_pieces(0.1)[0])
    transcription.add(_pieces(0.2)[0])

    # Nemotron is still busy with the first piece, and yet the recording has carried on.
    assert all(stream == [] for stream in nemotron.streams)
    nemotron.busy.set()
    assert transcription.finish(np.concatenate(_pieces(0.1, 0.2)))["text"] == "Set a timer for ten minutes."
    assert len(nemotron.streams[0]) == 2


def test_if_nemotron_fails_while_listening_the_whole_recording_is_heard_again():
    nemotron = FakeNemotron()
    nemotron.fail = RuntimeError("Failed to decode")
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()
    pieces = _pieces(0.1, 0.2)
    for piece in pieces:
        transcription.add(piece)

    assert transcription.finish(np.concatenate(pieces))["text"] == "Set a timer for ten minutes."
    # Afresh, through a stream of its own, so nothing said is lost.
    assert np.array_equal(np.concatenate(nemotron.finished[-1]), np.concatenate(pieces))


def test_a_recording_which_came_to_nothing_isnt_finished():
    # Finishing costs Nemotron the second of quiet it's given after every recording.
    nemotron = FakeNemotron()
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()
    transcription.add(_pieces(0.1)[0])
    transcription.cancel()
    assert nemotron.finished == []


def test_nemotron_imagining_thank_you_is_thrown_away():
    transcription = speech_to_text.NemotronSpeechToText(FakeNemotron(heard="Thank you.")).listen()
    transcription.add(_pieces(0.1)[0])
    assert transcription.finish(_pieces(0.1)[0])["text"] == ""


def test_a_nemotron_which_hangs_is_given_up_on(monkeypatch):
    # The session waited for it for good - stuck, with its satellite still connected,
    # so every wake word there got silence.
    import threading
    import time

    monkeypatch.setattr(speech_to_text, "FINISH_LIMIT", 0.1)
    monkeypatch.setattr(speech_to_text, "CANCEL_LIMIT", 0.1)
    nemotron = FakeNemotron()
    nemotron.busy = threading.Event()
    stt = speech_to_text.NemotronSpeechToText(nemotron)
    try:
        finishing = stt.listen()
        finishing.add(_pieces(0.1)[0])
        with pytest.raises(TimeoutError):
            finishing.finish(_pieces(0.1)[0])

        cancelling = stt.listen()
        cancelling.add(_pieces(0.1)[0])
        started = time.monotonic()
        cancelling.cancel()
        assert time.monotonic() - started < 1
    finally:
        nemotron.busy.set()
