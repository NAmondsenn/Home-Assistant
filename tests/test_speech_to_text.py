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


class FakeMoonshine:
    """Stands in for the moonshine_voice package, giving back whatever text the test sets."""

    heard = "Set a timer for ten minutes."

    class ModelArch:
        SMALL_STREAMING = "small"

    @staticmethod
    def string_to_model_arch(name):
        return name

    @staticmethod
    def get_model_for_language(language, arch, cache_root=None, on_progress=None):
        FakeMoonshine.asked = (language, arch, cache_root)
        return "/models/small", arch

    class Transcriber:
        def __init__(self, model_path, model_arch, options=None):
            self.keyterms = None
            self.given = []
            self.options = options
            self.streams = []
            # What the streams it makes do - a test can make them fail, or keep them busy.
            self.fail = None
            self.busy = None

        def set_keyterms(self, keyterms):
            self.keyterms = keyterms

        def transcribe_without_streaming(self, audio, sample_rate):
            self.given.append((len(audio), sample_rate))
            return SimpleNamespace(lines=[SimpleNamespace(text=FakeMoonshine.heard)])

        def create_stream(self, update_interval=None):
            self.streams.append(FakeStream(fail=self.fail, busy=self.busy))
            return self.streams[-1]


class FakeStream:
    """Stands in for one of Moonshine's streams, keeping the audio it's given and hearing FakeMoonshine.heard in it."""

    def __init__(self, fail=None, busy=None):
        self.audio = []
        self.starts = 0
        self.stops = 0
        self.closed = False
        self.fail = fail
        self.busy = busy

    def start(self):
        self.starts += 1

    def add_audio(self, audio, sample_rate):
        if self.busy:
            self.busy.wait(5)
        if self.fail:
            raise self.fail
        self.audio.extend(audio)

    def stop(self):
        self.stops += 1
        return SimpleNamespace(lines=[SimpleNamespace(text=FakeMoonshine.heard)] if self.audio else [])

    def close(self):
        self.closed = True


@pytest.fixture
def moonshine(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "moonshine_voice", FakeMoonshine)
    return FakeMoonshine


def test_moonshine_is_used_when_chosen_and_hears_the_whole_recording(moonshine, make_config):
    stt = speech_to_text.make_speech_to_text(make_config(
        speech_to_text={"engine": "moonshine"},
        moonshine={"model": "small_streaming", "download_root": "models/moonshine", "keyterms": ["Nova", "a,b"]}))
    # Asked for by Moonshine's own, hyphenated name, however it's written in the settings.
    assert moonshine.asked[1] == "small-streaming"
    assert isinstance(stt, speech_to_text.MoonshineSpeechToText)
    # Kept in the project's models folder, warmed up, and told what to listen out for.
    assert moonshine.asked[2].endswith("models/moonshine")
    assert stt.transcriber.given and stt.transcriber.keyterms == ["Nova"]
    result = stt.transcribe(np.zeros(32000, dtype=np.float32))
    assert result["text"] == "Set a timer for ten minutes." and stt.transcriber.given[-1] == (32000, 16000)


def test_moonshine_imagining_thank_you_is_thrown_away(moonshine, make_config):
    stt = speech_to_text.make_speech_to_text(make_config(speech_to_text={"engine": "moonshine"}))
    moonshine.heard = "Thank you."
    assert stt.transcribe(np.zeros(16000, dtype=np.float32))["text"] == ""
    moonshine.heard = "Set a timer for ten minutes."


# Listening as it's heard: Moonshine works out the words while the user is still
# saying them, so there's little left to do once they stop. Transcribing the whole
# recording only once it was over took 0.83s on the Pi.

def _pieces(*values):
    """Pieces of a recording, each 30ms long and all one value, as the microphone hands them over."""
    return [np.full(480, value, dtype=np.float32) for value in values]


def test_moonshine_hears_the_recording_as_its_made(moonshine, make_config):
    stt = speech_to_text.make_speech_to_text(make_config(speech_to_text={"engine": "moonshine"}))
    transcription = stt.listen()
    pieces = _pieces(0.1, 0.2, 0.3)
    for piece in pieces:
        transcription.add(piece)
    result = transcription.finish(np.concatenate(pieces))

    assert result["text"] == "Set a timer for ten minutes."
    stream = stt.transcriber.streams[0]
    assert stream.audio == pytest.approx(np.concatenate(pieces).tolist())
    # The whole recording isn't transcribed all over again - only the warm-up was.
    assert len(stt.transcriber.given) == 1
    # And Moonshine doesn't hand its copy of each piece's audio back every time it
    # looks again, which Nova has no use for.
    assert stt.transcriber.options == {"return_audio_data": "false"}


def test_the_recording_never_waits_for_moonshine(moonshine, make_config):
    import threading

    stt = speech_to_text.make_speech_to_text(make_config(speech_to_text={"engine": "moonshine"}))
    stt.transcriber.busy = threading.Event()
    transcription = stt.listen()
    transcription.add(_pieces(0.1)[0])

    # Moonshine is still busy with it, and yet the recording has carried on.
    assert all(stream.audio == [] for stream in stt.transcriber.streams)
    stt.transcriber.busy.set()
    assert transcription.finish(_pieces(0.1)[0])["text"] == "Set a timer for ten minutes."
    assert stt.transcriber.streams[0].audio == pytest.approx([0.1] * 480)


def test_if_moonshine_fails_while_listening_the_whole_recording_is_transcribed(moonshine, make_config):
    stt = speech_to_text.make_speech_to_text(make_config(speech_to_text={"engine": "moonshine"}))
    stt.transcriber.fail = RuntimeError("Failed to encode: 6")
    transcription = stt.listen()
    pieces = _pieces(0.1, 0.2)
    for piece in pieces:
        transcription.add(piece)

    assert transcription.finish(np.concatenate(pieces))["text"] == "Set a timer for ten minutes."
    assert stt.transcriber.given[-1] == (960, 16000)


def test_each_recording_is_heard_through_a_stream_of_its_own(moonshine, make_config):
    # Starting a Moonshine stream again doesn't clear what its speech detection last
    # heard. With one stream for every recording, a recording which ended mid-word
    # left the next one starting with a made-up "Yeah." - "Yeah. Turn off the
    # kitchen lights" - measured on the Mac with the real Moonshine.
    stt = speech_to_text.make_speech_to_text(make_config(speech_to_text={"engine": "moonshine"}))
    stt.listen().cancel()
    transcription = stt.listen()
    transcription.add(_pieces(0.5)[0])
    transcription.finish(_pieces(0.5)[0])

    first, second = stt.transcriber.streams
    assert first.audio == [] and second.audio == pytest.approx([0.5] * 480)
    # Each started, finished with, and let go of.
    assert all(stream.starts == stream.stops == 1 and stream.closed for stream in (first, second))


def test_moonshine_imagining_thank_you_while_listening_is_thrown_away(moonshine, make_config):
    stt = speech_to_text.make_speech_to_text(make_config(speech_to_text={"engine": "moonshine"}))
    moonshine.heard = "Thank you."
    transcription = stt.listen()
    transcription.add(_pieces(0.1)[0])
    try:
        assert transcription.finish(_pieces(0.1)[0])["text"] == ""
    finally:
        moonshine.heard = "Set a timer for ten minutes."


def test_whisper_hears_the_recording_once_its_over(monkeypatch):
    # Whisper can only start on a recording once it's finished, so it's given the
    # whole thing, as before.
    monkeypatch.setattr(speech_to_text, "WhisperModel", ScriptedWhisper)
    monkeypatch.setattr(ScriptedWhisper, "segments", [_segment(" Set a timer for ten minutes.")])
    stt = speech_to_text.SpeechToText(model_size="base.en")
    transcription = stt.listen()
    transcription.add(_pieces(0.1)[0])
    assert transcription.finish(_pieces(0.1)[0])["text"] == "Set a timer for ten minutes."


def test_without_moonshine_whisper_is_used(monkeypatch, make_config):
    def missing(name, *args, **kwargs):
        if name == "moonshine_voice":
            raise ImportError("No module named 'moonshine_voice'")
        return original(name, *args, **kwargs)

    import builtins
    original = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", missing)
    monkeypatch.setattr(speech_to_text, "WhisperModel", FakeWhisper)
    stt = speech_to_text.make_speech_to_text(make_config(speech_to_text={"engine": "moonshine"}))
    assert isinstance(stt, speech_to_text.SpeechToText)


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
