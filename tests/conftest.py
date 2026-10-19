"""
Shared test setup.

The tests only exercise the assistant's own logic, so they're meant to run anywhere -
including machines without the Pi's full set of packages installed. Where one of the
third party packages the modules import at the top isn't available, a minimal stand-in
is registered in its place. Nothing here is used when the real package is installed,
and no test ever reaches the network either way.
"""

import sys
import types

import pytest


def _module_missing(name):
    """Whether a package can't be imported on this machine."""
    try:
        __import__(name)
        return False
    except ImportError:
        return True


# python-dotenv: only load_dotenv() is used, and the tests set any variables they need themselves.
if _module_missing("dotenv"):
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv


# anthropic: the client class, the timeout helper and the error types llm.py catches.
# Tests replace the client with their own fake, so none of this ever makes a request.
if _module_missing("anthropic"):
    anthropic = types.ModuleType("anthropic")

    class AnthropicError(Exception):
        pass

    class APIError(AnthropicError):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        status_code = 500

    class AuthenticationError(APIStatusError):
        status_code = 401

    class RateLimitError(APIStatusError):
        status_code = 429

    class Anthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    anthropic.Anthropic = Anthropic
    anthropic.Timeout = lambda *args, **kwargs: ("timeout", args, kwargs)
    for error in (AnthropicError, APIError, APIConnectionError, APITimeoutError,
                  APIStatusError, AuthenticationError, RateLimitError):
        setattr(anthropic, error.__name__, error)
    sys.modules["anthropic"] = anthropic


# spotipy: the client and OAuth classes. Tests swap in their own fakes for both.
if _module_missing("spotipy"):
    spotipy = types.ModuleType("spotipy")
    oauth2 = types.ModuleType("spotipy.oauth2")

    class Spotify:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    class SpotifyOAuth:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    spotipy.Spotify = Spotify
    oauth2.SpotifyOAuth = SpotifyOAuth
    spotipy.oauth2 = oauth2
    sys.modules["spotipy"] = spotipy
    sys.modules["spotipy.oauth2"] = oauth2


# pyaudio: the PyAudio class and the one sample format audio.py uses. The tests give
# AudioManager their own fake PyAudio, so nothing ever opens a sound card.
if _module_missing("pyaudio"):
    pyaudio = types.ModuleType("pyaudio")
    pyaudio.paInt16 = 8
    pyaudio.PyAudio = lambda: None
    sys.modules["pyaudio"] = pyaudio


# librosa and soundfile: only used by audio.py to read and resample sound files,
# which none of the tests do.
for name in ("librosa", "soundfile"):
    if _module_missing(name):
        sys.modules[name] = types.ModuleType(name)


# sounddevice: only its input and output streams, which the tests replace with their own fakes.
if _module_missing("sounddevice"):
    sounddevice = types.ModuleType("sounddevice")
    sounddevice.InputStream = None
    sounddevice.OutputStream = None
    sys.modules["sounddevice"] = sounddevice


# faster_whisper: only WhisperModel, which the tests replace with their own fake.
if _module_missing("faster_whisper"):
    faster_whisper = types.ModuleType("faster_whisper")
    faster_whisper.WhisperModel = None
    sys.modules["faster_whisper"] = faster_whisper


# openwakeword: the wake word Model and Silero's VAD. Tests give the detectors their
# own fakes for both, so no model ever has to be downloaded.
if _module_missing("openwakeword"):
    openwakeword = types.ModuleType("openwakeword")
    openwakeword_model = types.ModuleType("openwakeword.model")
    openwakeword_vad = types.ModuleType("openwakeword.vad")
    openwakeword_model.Model = None
    openwakeword_vad.VAD = None
    openwakeword.model = openwakeword_model
    openwakeword.vad = openwakeword_vad
    sys.modules["openwakeword"] = openwakeword
    sys.modules["openwakeword.model"] = openwakeword_model
    sys.modules["openwakeword.vad"] = openwakeword_vad


class FakeConfig:
    """Stands in for Config, returning sections from a plain dict instead of config.yaml."""

    def __init__(self, data=None):
        self.data = data or {}

    def section(self, name):
        return self.data.get(name, {})


@pytest.fixture
def make_config():
    """Builds a FakeConfig from keyword sections, e.g. make_config(timers={...})."""
    return lambda **sections: FakeConfig(sections)
