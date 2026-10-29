"""
Speech-to-Text Test
Checks that what speech models imagine in noise is thrown away, and that the brain's
Nemotron speech-to-text hears each recording as it's made. Needs numpy installed (as
it is on the Pi), and is skipped anywhere it isn't.
"""

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import speech_to_text


@pytest.mark.parametrize("heard", ["Thank you.", "Thanks for watching!", "you", "Bye."])
def test_known_hallucinations_are_thrown_away_when_theyre_all_there_is(heard):
    # A false wake word followed by a cough used to come back as "Thank you." and
    # be answered.
    assert speech_to_text._heard(heard, 0.1)["text"] == ""


@pytest.mark.parametrize("heard", ["Stop.", "Set a timer, thank you.", "Yes."])
def test_real_requests_are_kept(heard):
    assert speech_to_text._heard(heard, 0.1)["text"] == heard


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
        # What it hears in each recording - or, as a list, in each of them in turn,
        # where an exception is raised rather than heard.
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
        heard = self.heard.pop(0) if isinstance(self.heard, list) else self.heard
        if isinstance(heard, Exception):
            raise heard
        return heard


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


# A lone short word - "Pause.", "Wait.", "Play." - which Nemotron so often hears as nothing.

def _heard_after_nothing(*heard, please=None, timeline=None, quiet_after=0.0, first="", wake_word=None):
    """
    Nemotron hearing nothing in a recording of the user talking - or first, such as
    only the wake word - then each of heard in turn as it listens again.

    Returns:
        What finish gave, and the Nemotron, whose streams are what it was given.
    """
    nemotron = FakeNemotron(heard=[first, *heard])
    transcription = speech_to_text.NemotronSpeechToText(nemotron, please=please).listen()
    pieces = _pieces(0.1, 0.2)
    for piece in pieces:
        transcription.add(piece)
    return transcription.finish(np.concatenate(pieces), timeline=timeline, quiet_after=quiet_after,
                                wake_word=wake_word), nemotron


def test_a_lone_word_heard_as_nothing_is_heard_again_said_twice(caplog):
    caplog.set_level("INFO")
    speech = np.concatenate(_pieces(0.1, 0.2))
    result, nemotron = _heard_after_nothing("pause pause", quiet_after=0.03)

    assert result["text"] == "pause"
    # The words without the quiet the recording stopped after, a moment of quiet,
    # then the words again - through a stream of its own.
    again = np.concatenate(nemotron.streams[1])
    words = speech[:480]
    assert np.array_equal(again, np.concatenate([words, np.zeros(round(0.3 * 16000)), words]))
    assert "Heard nothing, so listening again with it said twice: 'pause'" in caplog.text


@pytest.mark.parametrize("heard, meant", [
    ("Pause. Pause.", "Pause."),
    ("Turn it up, turn it up.", "Turn it up"),
    ("louder", ""),
    ("cause pause", ""),
    ("play play play", ""),
    # Run together, as one word.
    ("Pause-pause.", "pause"),
])
def test_only_the_same_words_twice_are_kept(heard, meant):
    assert speech_to_text._said_twice(heard) == meant


def test_halves_which_differ_are_heard_again_after_please():
    please = np.full(800, 0.5, dtype=np.float32)
    result, nemotron = _heard_after_nothing("cause pause", "please pause", please=please)

    assert result["text"] == "pause"
    speech = np.concatenate(_pieces(0.1, 0.2))
    assert np.array_equal(np.concatenate(nemotron.streams[2]),
                          np.concatenate([please, np.zeros(round(0.3 * 16000)), speech]))


@pytest.mark.parametrize("heard", ["", "pause", "Please.", "Thank you, please.", "Please, please."])
def test_please_must_come_first_and_be_followed_by_something(heard):
    please = np.full(800, 0.5, dtype=np.float32)
    assert _heard_after_nothing("x y", heard, please=please)[0]["text"] == ""


def test_nothing_heard_three_times_is_nothing():
    please = np.full(800, 0.5, dtype=np.float32)
    result, nemotron = _heard_after_nothing("x y", "", please=please)

    assert result["text"] == ""
    assert len(nemotron.streams) == 3


def test_without_please_its_heard_again_only_said_twice():
    # Piper couldn't say "Please" as the brain started.
    result, nemotron = _heard_after_nothing("x y")
    assert result["text"] == "" and len(nemotron.streams) == 2


def test_hearing_again_is_timed_on_its_own():
    from voice_assistant.timeline import Timeline

    timeline = Timeline()
    _heard_after_nothing("pause pause", timeline=timeline)
    assert {"stt_retry_started", "stt_retry_done"} <= set(timeline.marks)
    assert timeline.notes["heard_again"] == "twice"


def test_words_heard_the_first_time_arent_heard_again():
    # Listening again takes a second or so, which a request heard first time never waits for.
    from voice_assistant.timeline import Timeline

    nemotron = FakeNemotron(heard=["Pause the music.", "never asked for"])
    timeline = Timeline()
    transcription = speech_to_text.NemotronSpeechToText(nemotron, please=np.ones(800, np.float32)).listen()
    transcription.add(_pieces(0.1)[0])

    assert transcription.finish(_pieces(0.1)[0], timeline=timeline)["text"] == "Pause the music."
    assert len(nemotron.streams) == 1 and timeline.marks == {}


def test_a_recording_with_no_speech_found_in_it_isnt_heard_again():
    # Nothing is handed over until the recording finds the user talking, so a
    # transcription given nothing had none in it.
    nemotron = FakeNemotron(heard=["", "pause pause"])
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()

    assert transcription.finish(np.zeros(0, np.float32))["text"] == ""
    assert len(nemotron.streams) == 1


@pytest.mark.parametrize("seconds, heard_again", [(3.0, True), (3.1, False), (14.5, False)])
def test_only_a_short_recording_is_heard_again(seconds, heard_again):
    # Hearing again decodes the recording about three times over - for a long one, a tap
    # running or a hoover that sounded like talking, a long wait before the sorry, and
    # Nemotron still busy after FINISH_LIMIT. Timed without the quiet it stopped after.
    nemotron = FakeNemotron(heard=["", "pause pause"])
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()
    recording = np.full(round((seconds + 0.6) * 16000), 0.1, dtype=np.float32)
    transcription.add(recording)

    result = transcription.finish(recording, quiet_after=0.6)
    assert result["text"] == ("pause" if heard_again else "")
    assert len(nemotron.streams) == (2 if heard_again else 1)


def test_a_nemotron_which_fails_listening_again_hears_nothing(caplog):
    nemotron = FakeNemotron(heard=["", RuntimeError("Failed to decode")])
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()
    transcription.add(_pieces(0.1)[0])

    assert transcription.finish(_pieces(0.1)[0])["text"] == ""
    assert "Failed to decode" in caplog.text


class FakePiper:
    """Stands in for Piper, saying each thing as the test sets, at 22050Hz."""

    sample_rate = 22050

    def __init__(self, pieces):
        self.pieces = pieces
        self.said = []

    def stream(self, text, remember=True):
        self.said.append((text, remember))
        if isinstance(self.pieces, Exception):
            raise self.pieces
        yield from self.pieces


def test_please_is_said_once_by_piper_at_16khz_without_its_quiet():
    pytest.importorskip("soxr")
    word = np.sin(np.arange(11025) / 5).astype(np.float32) * 0.5
    piper = FakePiper([np.zeros(2205, np.float32), word, np.zeros(4410, np.float32)])

    please = speech_to_text.make_please(piper)
    assert piper.said == [("Please", False)]
    assert please.dtype == np.float32
    # Half a second of it, give or take the edges of the quiet trimmed.
    assert abs(len(please) - 8000) < 400


@pytest.mark.parametrize("pieces", [[], [np.zeros(2205, np.float32)], OSError("No Piper")])
def test_please_is_done_without_if_piper_cant_say_it(pieces):
    pytest.importorskip("soxr")
    assert speech_to_text.make_please(FakePiper(pieces)) is None


def test_words_imagined_in_noise_arent_heard_again():
    # Most likely a false wake word - a cough heard as "Thank you." - so it isn't
    # worth a second or two more, nor an apology.
    nemotron = FakeNemotron(heard=["Thank you.", "pause pause"])
    transcription = speech_to_text.NemotronSpeechToText(nemotron, please=np.ones(800, np.float32)).listen()
    transcription.add(_pieces(0.1)[0])

    result = transcription.finish(_pieces(0.1)[0])
    assert result["text"] == "" and result["imagined"]
    assert len(nemotron.streams) == 1


def test_nothing_heard_at_all_is_heard_again_and_not_taken_for_noise():
    # Even if hearing it again only imagines words, the user did say something.
    result, nemotron = _heard_after_nothing("Thank you. Thank you.")
    assert result["text"] == "" and not result["imagined"]
    assert len(nemotron.streams) == 2

    result, nemotron = _heard_after_nothing("pause pause")
    assert result["text"] == "pause" and not result["imagined"]


# Only the wake word - all Nemotron sometimes hears of "Nova, pause."

def test_only_the_wake_word_is_heard_again_and_the_wake_word_left_out():
    result, nemotron = _heard_after_nothing("Nova, pause. Nova, pause.", first="Nova.", wake_word="Hey Nova")
    assert result["text"] == "Pause." and not result["imagined"]
    assert len(nemotron.streams) == 2


def test_only_the_wake_word_heard_again_and_again_is_nothing():
    please = np.full(800, 0.5, dtype=np.float32)
    timeline = __import__("voice_assistant.timeline", fromlist=["Timeline"]).Timeline()
    result, nemotron = _heard_after_nothing("Nova. Nova.", "Please, Nova.", please=please, first="Hey, Nova.",
                                            wake_word="Hey Nova", timeline=timeline)
    assert result["text"] == "" and not result["imagined"]
    assert len(nemotron.streams) == 3 and timeline.notes["heard_again"] == "nothing"


def test_the_wake_word_with_a_request_isnt_heard_again():
    nemotron = FakeNemotron(heard=["Nova, pause.", "never asked for"])
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()
    transcription.add(_pieces(0.1)[0])
    assert transcription.finish(_pieces(0.1)[0], wake_word="Hey Nova")["text"] == "Nova, pause."
    assert len(nemotron.streams) == 1


def test_a_nemotron_which_hangs_listening_again_is_given_up_on(monkeypatch):
    import threading

    monkeypatch.setattr(speech_to_text, "FINISH_LIMIT", 0.1)
    stuck = threading.Event()

    class Hanging(FakeNemotron):
        def finish(self, stream):
            if self.finished:
                stuck.wait(2)
            return super().finish(stream)

    nemotron = Hanging(heard=["", "pause pause"])
    transcription = speech_to_text.NemotronSpeechToText(nemotron).listen()
    transcription.add(_pieces(0.1)[0])
    try:
        with pytest.raises(TimeoutError):
            transcription.finish(_pieces(0.1)[0])
    finally:
        stuck.set()
