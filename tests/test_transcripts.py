"""
Transcripts Test
Checks that with logging.log_transcripts off, none of the user's words reach the log
file - running a conversation through the real clock, local commands, Home Assistant
and Spotify, with stand-ins only for what they reach over the network.
"""

import json
import logging
import threading

import pytest
import requests

from voice_assistant import clock as clock_module
from voice_assistant import logging_setup, network, spotify_controller
from voice_assistant.actions import Action, ActionExecutor
from voice_assistant.clock import Clock
from voice_assistant.conversation import Conversation
from voice_assistant.ha_registry import Entry, Snapshot
from voice_assistant.home_assistant import LIGHTS_TEMPLATE, HomeAssistant
from voice_assistant.intents import LocalIntents

# What the user says, and asks for - none of which may be in the log.
PRIVATE = ("take my tablets", "gate lights", "reading lamp", "lullabies for my nephew", "dream choir",
           "the quiet hours")


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.status_code = 200
        # The template endpoint answers in plain text.
        self.text = json.dumps(body)

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeHomeSession:
    """
    Stands in for Home Assistant's API: a house with a lamp in it, which Assist turns
    on, and the gate lights, which Nova's own lights switch, as the gate's never
    named to Assist.
    """

    headers = {}

    def request(self, method, url, timeout=None, **kwargs):
        if url.endswith("/api/states"):
            return FakeResponse([{"entity_id": "light.reading_lamp", "attributes": {"friendly_name": "Reading lamp"}}])
        if url.endswith("/api/template"):
            # The lights' template alone, so another the run asks for fails, rather than
            # being given the lights.
            assert kwargs["json"]["template"] == LIGHTS_TEMPLATE, kwargs["json"]["template"]
            return FakeResponse([{"entity_id": "light.driveway", "area": "Garden", "state": "on",
                                  "attributes": {"friendly_name": "Gate lights"}}])
        return FakeResponse({"response": {"response_type": "action_done",
                                          "speech": {"plain": {"speech": "Turned on the reading lamp"}}}})


class FakeHomeSocket:
    """Stands in for Home Assistant's WebSocket: Assist's sentence debugging, which matches the lamp."""

    def call(self, messages):
        return [{"results": [{"intent": {"name": "HassTurnOn"}, "targets": {"light.reading_lamp": {"matched": True}},
                              "match": True}]}]


class FakeRegistry:
    """Stands in for the HomeRegistry: the lamp, so the guard can vouch for it to Assist."""

    def snapshot(self):
        lamp = Entry("light.reading_lamp", "hue", None, None, "lamp", "Reading lamp", (), True)
        return Snapshot(entries={lamp.entity_id: lamp}, devices={}, areas={}, read_at=0.0)


class FakeSpotify:
    """
    Stands in for spotipy's client: the kitchen speaker, and searches which find no
    artist or album worth playing, and only a song by someone else.
    """

    def __init__(self, **kwargs):
        pass

    def __getattr__(self, name):
        # Starting playback, shuffling and the like.
        return lambda *args, **kwargs: None

    def devices(self):
        return {"devices": [{"name": "Kitchen", "id": "kitchen"}]}

    def current_playback(self):
        return {"device": {"id": "kitchen"}}

    def search(self, q, limit, type):
        if type == "album":
            raise requests.ConnectionError("Spotify's search went away")
        if type == "artist":
            return {"artists": {"items": [{"name": "Two Door Cinema Club"}]}}
        return {"tracks": {"items": [{"name": "Sleepy Songs", "uri": "spotify:track:1",
                                      "artists": [{"name": "Someone Else"}]}]}}


class FakeOAuth:
    class cache_handler:
        @staticmethod
        def get_cached_token():
            return {"access_token": "token"}

    def get_cached_token(self):
        return {"access_token": "token"}


class FakeLLM:
    """Stands in for Claude, asking for each request's actions in turn."""

    def __init__(self, *actions):
        self.actions = list(actions)

    def forget_stale_history(self):
        pass

    def process_query(self, text, context=None, lights=None, read=None, playing=()):
        return {"response": "Okay.", "actions": [self.actions.pop(0)], "success": True}

    def remember(self, text, reply):
        pass

    def record_reply(self, text, spoken, tool_answers=()):
        pass


class FakeMusic:
    def wait(self):
        pass


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """Logs into a file of the test's own, with transcripts off, as config.yaml can have it."""
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    level = logging.getLogger().level
    handlers = logging_setup.setup_logging(str(tmp_path / "logs" / "backend.log"))
    logging_setup.log_transcripts(False)
    yield tmp_path / "logs" / "backend.log", handlers
    for handler in handlers:
        logging.getLogger().removeHandler(handler)
        handler.close()
    logging_setup.log_transcripts(True)
    logging.getLogger().setLevel(level)


def test_none_of_what_the_user_says_reaches_the_log_with_transcripts_off(log_file, tmp_path, monkeypatch,
                                                                         make_config, caplog):
    path, handlers = log_file
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(tmp_path / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(tmp_path / "alarms.json"))
    monkeypatch.setattr(network, "online", lambda: True)
    for name, value in (("SPOTIFY_CLIENT_ID", "id"), ("SPOTIFY_CLIENT_SECRET", "secret"),
                        ("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(spotify_controller, "SpotifyOAuth", lambda **kwargs: FakeOAuth())
    monkeypatch.setattr(spotify_controller.spotipy, "Spotify", FakeSpotify)
    monkeypatch.setattr(spotify_controller.os, "chmod", lambda path, mode: None)
    monkeypatch.setattr(spotify_controller.time, "sleep", lambda seconds: None)

    clock = Clock(config=make_config(), start=False)
    home = HomeAssistant("http://homeassistant.local:8123", "token", registry=FakeRegistry())
    home._session = FakeHomeSession()
    home.socket = FakeHomeSocket()
    music = spotify_controller.SpotifyController(config=make_config(spotify={"device_name": "Kitchen"}))
    llm = FakeLLM(Action("set_timer", {"duration_seconds": 600, "label": "take my tablets", "is_reminder": True},
                         from_model=True),
                  Action("play_music", {"query": "lullabies for my nephew"}, from_model=True),
                  Action("play_music", {"query": "Sleepy Songs by the Dream Choir"}, from_model=True),
                  Action("play_music", {"query": "the quiet hours", "search_type": "album"}, from_model=True))
    conversation = Conversation(clock, LocalIntents(clock=clock), llm,
                                ActionExecutor(music=music, clock=clock, home=home), FakeMusic())
    for said in ("remind me in ten minutes to take my tablets", "turn off the gate lights",
                 "turn on the reading lamp", "play lullabies for my nephew",
                 "play sleepy songs by the dream choir", "play the quiet hours album"):
        conversation.start()
        conversation.handle(said)
    reminder = next(iter(clock._timers))
    clock.remove("timer", reminder)
    for handler in handlers:
        handler.flush()

    # Each was logged - caplog has every line, transcripts or not - but none reached the file.
    logged = caplog.text.lower()
    assert [words for words in PRIVATE if words.lower() not in logged] == []
    written = path.read_text().lower()
    assert [words for words in PRIVATE if words.lower() in written] == []
