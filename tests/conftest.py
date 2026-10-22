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


# soundfile: only used to read sound files, which the tests that need it skip without.
if _module_missing("soundfile"):
    sys.modules["soundfile"] = types.ModuleType("soundfile")


# sounddevice: only its input and output streams, which the tests replace with their own fakes.
if _module_missing("sounddevice"):
    sounddevice = types.ModuleType("sounddevice")
    sounddevice.InputStream = None
    sounddevice.OutputStream = None
    sys.modules["sounddevice"] = sounddevice


# requests: get() and Session, which the tests replace with their own fakes, and the
# errors the modules catch.
if _module_missing("requests"):
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        def __init__(self, *args, response=None):
            super().__init__(*args)
            self.response = response

    class Session:
        def __init__(self):
            self.headers = {}

    requests.RequestException = RequestException
    requests.HTTPError = type("HTTPError", (RequestException,), {})
    requests.ConnectionError = type("ConnectionError", (RequestException,), {})
    requests.Timeout = type("Timeout", (RequestException,), {})
    requests.Session = Session
    requests.Response = type("Response", (), {})
    requests.get = None
    sys.modules["requests"] = requests


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
