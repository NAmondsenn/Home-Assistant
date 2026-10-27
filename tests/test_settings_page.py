"""
Settings Page Test
Checks the settings page shows what it should - and never a key - saves only what
Nova would accept when it starts, and only lets the user in, and only changes made
from the page itself: over a real server on this machine, with stand-ins for the
rest of Nova, and its files in a folder of the test's own.
"""

import http.client
import json
import logging
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest
import requests
import yaml

from voice_assistant import logging_setup, paths, storage
from voice_assistant.batteries import BatteryChoices, BatteryWatcher
from voice_assistant import clock as clock_module
from voice_assistant import settings_page as page_module
from voice_assistant import google_calendar, spotify_controller
from voice_assistant.brain import Brain
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import Clock
from voice_assistant.config import Config
from voice_assistant.ha_registry import Entry, Snapshot
from voice_assistant.lists import Lists
from voice_assistant.ntfy import Ntfy
from voice_assistant.page_password import Logins, PagePassword
from voice_assistant.pc import PCPower
from voice_assistant.routines import Routines
from voice_assistant.settings_page import LATER, NOW, SettingsPage
from voice_assistant.weather import Weather

LOGIN = "you@example.com"


class FakeVolume:
    CATEGORIES = ("general", "alarm", "music")

    def __init__(self):
        self.levels = {"general": 60, "alarm": 80, "music": 40}

    def percent(self, category):
        return self.levels[category]

    def get(self, category):
        return self.levels[category] / 100

    def set(self, category, percent):
        self.levels[category] = percent
        return {"success": True, "message": f"Volume {percent}%."}


class FakeClock:
    def __init__(self):
        self.removed = []
        self.alarms = []
        # The satellites something is ringing on.
        self.ringing = set()

    def is_ringing(self, satellite=None):
        return bool(self.ringing) if satellite is None else satellite in self.ringing

    def overview(self, satellite=None):
        self.overviewed = satellite
        return {"timers": [{"id": 1, "name": "pizza reminder", "due_at": 1_800_000_000}], "alarms": []}

    def remove(self, kind, item_id):
        self.removed.append((kind, item_id))
        return {"success": True, "message": "Removed."}

    def set_alarm(self, hour, minute, repeat=None, satellite=None):
        self.alarms.append((hour, minute, repeat, satellite))
        return {"success": True, "message": "Alarm set."}


class FakeMemory:
    def __init__(self):
        self.known = ["Your sister is called Amy."]

    def facts(self):
        return list(self.known)

    def forget(self, what):
        at = self.known.index(what)
        self.known.remove(what)
        return {"success": True, "message": "Forgotten.", "forgotten": what, "at": at}

    def put_back(self, fact, at):
        self.known.insert(at, fact)
        return {"success": True, "message": "Put it back."}


@pytest.fixture
def files(tmp_path, monkeypatch):
    """Nova's settings, keys and logs, in a folder of the test's own."""
    monkeypatch.setattr(paths, "CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(paths, "SECRETS_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(paths, "LOGS_DIR", str(tmp_path / "logs"))
    return tmp_path


class FakePlayer:
    """Stands in for Navidrome, which keeps the music's volume itself."""

    needs_internet = False
    name = "Navidrome"

    def __init__(self):
        self.level = 40
        self.asked_for_playlists = 0

    def get_volume(self):
        return self.level

    def set_volume(self, percent):
        self.level = int(percent)
        return {"success": True, "message": f"Music volume {self.level} percent."}

    def now_playing(self):
        return {"track": "Gooba", "artist": "6ix9ine", "album": "Gooba", "playing": True, "position": 12.0,
                "duration": 125, "cover": "al-1"}

    def playlists(self):
        self.asked_for_playlists += 1
        return ["Chill", "Workout"]

    def cover(self):
        return b"a cover", "image/jpeg"


class FakeActions:
    """Stands in for the actions asking out loud goes through, noting what was asked."""

    def __init__(self):
        self.asked = []

    def play_music(self, query=None):
        self.asked.append(("play", query))
        return {"success": True, "message": f"Playing {query}" if query else "Resumed playback", "silent": True}

    def play_playlist(self, name=""):
        self.asked.append(("playlist", name))
        return {"success": True, "message": f"Playing {name}"}

    def control_playback(self, action=None):
        self.asked.append((action, None))
        return {"success": True, "message": "Done."}


class FakeHome:
    """Stands in for Home Assistant."""

    def __init__(self):
        self.changed = []
        self.area = "Bedroom"

    def overview(self):
        return {"success": True, "lights": [{"id": "light.kitchen", "name": "Kitchen", "on": True}],
                "routines": [{"id": "scene.relax", "name": "Relax", "kind": "scene"}]}

    def set_light(self, entity_id, on=None, brightness=None, hue=None, saturation=None, kelvin=None):
        self.changed.append((entity_id, on, brightness) if hue is saturation is kelvin is None
                            else (entity_id, hue, saturation, kelvin))
        return {"success": True, "message": "Done."}

    def run(self, entity_id):
        self.changed.append((entity_id, "run", None))
        return {"success": True, "message": "Set Relax."}


class FakeSession:
    """Stands in for one connected satellite's session: its volumes, music, lights and mute."""

    def __init__(self, music=None, actions=None, home=None, activity="idle", speaker=None):
        self.volume = FakeVolume()
        # The Spotify speaker its satellite said as it connected - None for one which didn't.
        self.satellite = SimpleNamespace(speaker_name=speaker)
        self.music_player, self.actions, self.home = music, actions, home
        self.activity = activity
        self.muted = False
        self.booped = []
        # The speeds "Hear it" was said at, and whether it's in a conversation, so can't be.
        self.heard = []
        self.busy = False
        # How many times Stop was pressed for it.
        self.stopped = 0
        # The routines it was told to run.
        self.routines_run = []

    def run_routine(self, routine):
        self.routines_run.append(routine["name"])

    def set_muted(self, muted):
        self.muted = muted

    def hear_voice(self, speed):
        if self.busy:
            return False
        self.heard.append(speed)
        return True

    def boop(self, level):
        self.booped.append(level)

    def stop_what_rings_and_speaks(self):
        self.stopped += 1


class FakeBrain:
    """Stands in for the brain: what every satellite shares, and the sessions of those connected."""

    def __init__(self, sessions, known=None, **parts):
        self.sessions = sessions
        self.known = list(sessions) if known is None else known
        self.clock, self.memory = FakeClock(), FakeMemory()
        self.ntfy = self.stt = self.routines = self.batteries = self.config = self.calendar = None
        self.home = self.lists = self.weather = self.pc = None
        self._night = Ntfy("")
        self.ran = []
        for name, part in parts.items():
            setattr(self, name, part)

    def satellites(self):
        return list(self.known)

    def default_satellite(self):
        return self.known[0] if self.known else None

    def session_for(self, satellite):
        return self.sessions.get(satellite or self.default_satellite())

    def run_routine(self, routine):
        self.ran.append(routine)
        # The brain's own, so it finds the routine's satellite as it does on the brain.
        return Brain.run_routine(self, routine)

    def remove_satellite(self, name):
        if name not in self.known:
            return None
        self.known.remove(name)
        # Whether its copy was emptied - it was connected.
        return name in self.sessions

    def make_default(self, name):
        if name not in self.known:
            raise ValueError(f"There is no satellite called {name}")
        self.known.remove(name)
        self.known.insert(0, name)

    # The brain's own, so Stop finds what to stop as it does on the brain.
    def _stoppable_sessions(self):
        return Brain._stoppable_sessions(self)

    def stoppable(self):
        return Brain.stoppable(self)

    def stop_everywhere(self):
        return Brain.stop_everywhere(self)

    def apply_settings(self, changes):
        # The brain's own, so a change reaches ntfy and the logs as it does on the brain.
        return Brain.apply_settings(self, changes)


def _page(running=None, sessions=None, known=None, wake_word=None, **parts):
    """A page over a brain with one satellite connected, the bedroom - or the sessions given."""
    if sessions is None:
        sessions = {"bedroom": FakeSession()}
    brain = FakeBrain(sessions, known, **parts)
    # The login as Tailscale might capitalise it, which mustn't matter.
    page = SettingsPage([LOGIN.upper()], port=0, running=running, brain=brain, wake_word=wake_word,
                        restart=lambda: "Restarting.", started=1_800_000_000)
    return page


def _bedroom(page):
    return page.brain.sessions["bedroom"]


# What it shows

def test_the_page_shows_whats_set_and_never_a_key(files):
    (files / ".env").write_text('HA_TOKEN="the-secret-token"\nClaude_API_Key=sk-ant-secret\nNTFY_TOKEN=\n')
    state = _page().state()

    keys = {key["name"]: key["set"] for key in state["keys"]}
    # Claude's key counts under its older name too.
    assert keys["HA_TOKEN"] and keys["ANTHROPIC_API_KEY"] and not keys["NTFY_TOKEN"] and not keys["NAVIDROME_PASSWORD"]
    shown = json.dumps(state)
    assert "the-secret-token" not in shown and "sk-ant-secret" not in shown

    assert state["volumes"] == {"general": 60, "alarm": 80, "music": 40}
    assert state["memories"] == ["Your sister is called Amy."]
    assert state["clock"]["timers"][0]["name"] == "pizza reminder"
    assert {"navidrome.url", "ntfy.alerts"} <= {setting["key"] for setting in state["settings"]}
    # What belongs to a satellite - the brain's address, and the satellite's own password - isn't on the brain's page.
    assert "backend.address" not in {setting["key"] for setting in state["settings"]}
    assert "NOVA_BACKEND_TOKEN" not in keys
    # What can only be one of a few words comes with them, to pick from.
    player = next(setting for setting in state["settings"] if setting["key"] == "music.player")
    assert player["choices"] == ["navidrome", "spotify"]


@pytest.mark.parametrize("loaded, said", [(True, "ready"), (False, "missing")])
def test_speech_to_text_is_nemotron_on_the_brain(files, loaded, said):
    # It asked for a backend's reachable, which Nemotron hasn't got - so the tile said
    # "This Pi - No backend set up" since the brain took over.
    status = _page(stt=SimpleNamespace(loaded=loaded)).state()["status"]
    assert status["speech_to_text"] == said and "backend" not in status


def test_the_latest_problems_in_the_log_are_shown_newest_first(files):
    (files / "logs").mkdir()
    (files / "logs" / "backend.log").write_text(
        "2026-10-20 16:50:01,100 - main - INFO - Transcription complete: 'what's on today'\n"
        "2026-10-20 16:51:58,131 - voice_assistant.dropouts - WARNING - The speaker ran dry\n"
        "2026-10-20 17:02:00,001 - voice_assistant.ntfy - ERROR - Couldn't say the alert\n")
    problems = _page().state()["status"]["problems"]

    assert [(problem["time"], problem["level"]) for problem in problems] == [("2026-10-20 17:02", "error"),
                                                                              ("2026-10-20 16:51", "warning")]
    # What's said is only ever logged as information, so it's never shown here.
    assert "what's on today" not in json.dumps(problems)


def test_how_quickly_nova_replies_is_from_the_last_replies_alone(files, monkeypatch):
    # Read from the end of latency.jsonl, rather than the whole of it every 20 seconds.
    (files / "logs").mkdir()
    lines = [{"marks_ms": {"speech_ended": 0, "tts_done": 9000}}] * 50 + [
        {"marks_ms": {"speech_ended": 0, "tts_done": 1000}}] * page_module.REPLIES_COUNTED
    (files / "logs" / "latency.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    asked = []
    loaded = page_module.timeline.load
    monkeypatch.setattr(page_module.timeline, "load", lambda path, most=None: asked.append(most) or loaded(path, most))

    status = _page().state()["status"]
    assert (status["reply_ms"], status["replies"]) == (1000, page_module.REPLIES_COUNTED)
    assert asked == [page_module.REPLIES_COUNTED]


def test_a_restart_is_asked_for_once_whats_saved_differs_from_whats_running(files):
    running = Config()
    page = _page(running=running)
    assert not page.state()["restart_needed"]
    page.save_settings({"navidrome.url": "http://music.local:4533"}, LOGIN)
    assert page.state()["restart_needed"]


def test_looking_at_the_settings_isnt_logged(files, caplog):
    # The page asks every 20 seconds while it's open, which was a "Settings loaded"
    # line each time - thousands a day, burying what the log is for.
    (files / "config.yaml").write_text("navidrome:\n  url: http://music.local:4533\n")
    page = _page(running=Config())
    with caplog.at_level(logging.INFO):
        caplog.clear()
        page.state()
    assert "Settings loaded" not in caplog.text


# What it changes

def test_a_volume_change_is_heard_at_its_new_level(files):
    # So the user hears how loud it now is, on Nova's own speaker, as they drag.
    page = _page()
    page.set_volume("alarm", 30, LOGIN)
    assert _bedroom(page).volume.levels["alarm"] == 30 and _bedroom(page).booped == [0.3]


def test_the_music_volume_is_the_players_own_so_a_playing_song_changes_too(files):
    player = FakePlayer()
    page = _page(sessions={"bedroom": FakeSession(music=player)})
    page.set_volume("music", 25, LOGIN)
    assert player.level == 25 and _bedroom(page).booped == [0.25]
    assert page.state()["volumes"]["music"] == 25


def test_spotifys_volume_is_shown_as_last_set_without_asking_spotify(files):
    # Asking Spotify's servers every time the page - or Home Assistant - looks would be
    # slow, so Nova keeps its own note of the level, which follows each change.
    class Spotify(FakePlayer):
        needs_internet = True

        def get_volume(self):
            raise AssertionError("Spotify was asked for its volume")

    player = Spotify()
    page = _page(sessions={"bedroom": FakeSession(music=player)})
    page.set_volume("music", 25, LOGIN)
    assert player.level == 25 and page.state()["volumes"]["music"] == 25


def test_the_page_can_tell_what_nova_is_doing(files):
    assert _page(sessions={"bedroom": FakeSession(activity="listening")}).live() == {"activity": "listening", "stoppable": False}
    assert _page().live() == {"activity": "idle", "stoppable": False}
    # A satellite which isn't connected isn't doing anything.
    assert _page(sessions={}, known=["bedroom"]).live() == {"activity": "idle", "stoppable": False}


def test_the_page_is_told_whats_playing_and_the_playlists_are_kept_a_while(files):
    player = FakePlayer()
    page = _page(sessions={"bedroom": FakeSession(music=player)})
    state = page.music_state()
    assert state["player"] == "Navidrome" and state["now"]["track"] == "Gooba" and state["playlists"] == ["Chill", "Workout"]
    page.music_state()
    assert player.asked_for_playlists == 1


class FakeSpotifyOAuth:
    """Stands in for spotipy's SpotifyOAuth through a sign-in, writing the cache as it does."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.cache_handler = SimpleNamespace(cache_path=kwargs["cache_path"], get_cached_token=self._cached)

    def _cached(self):
        try:
            with open(self.cache_handler.cache_path) as file:
                return json.load(file)
        except FileNotFoundError:
            return None

    def get_authorize_url(self, state=None):
        return f"https://accounts.spotify.com/authorize?client_id=client-id&state={state}"

    @staticmethod
    def parse_auth_response_url(url):
        form = dict(parse_qsl(urlsplit(url).query))
        if "error" in form:
            raise spotify_controller.SpotifyOauthError("Received error from auth server", error=form["error"])
        return form.get("state"), form.get("code")

    def get_access_token(self, code=None, as_dict=True, check_cache=True):
        with open(self.cache_handler.cache_path, "w") as file:
            json.dump({"access_token": "the-access-token", "refresh_token": "the-refresh-token"}, file)
        return "the-access-token"


@pytest.fixture
def spotify(files, monkeypatch, make_config):
    """Spotify as the player, with its keys set as Nova starts - and a fake sign-in to it."""
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    monkeypatch.setattr(spotify_controller.paths, "SPOTIFY_TOKEN_FILE", str(files / "spotify_token.json"))
    monkeypatch.setattr(spotify_controller, "SpotifyOAuth", FakeSpotifyOAuth)
    return make_config(music={"player": "spotify"})


def test_the_page_says_why_theres_no_music(spotify, make_config, monkeypatch):
    keys = "Spotify's client ID and secret aren't set. Add them under Keys, then restart Nova."
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET")
    page = _page(running=spotify)
    assert page.music_state()["why"] == keys
    # Playing says the same, in place of there being no player.
    assert page.play("pause", LOGIN) == {"success": False, "message": keys}

    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "client-secret")
    couldnt = "Nova couldn't set up Spotify - Recent problems, in Status, says why."
    assert page.music_state()["why"] == couldnt
    assert page.play("pause", LOGIN)["message"] == couldnt
    navidrome = make_config(music={"player": "navidrome"}, navidrome={"url": "", "username": "nova"})
    assert _page(running=navidrome).music_state() == {
        "available": False, "why": "Navidrome's address isn't set. Add it in Settings, under Music."}

    # Not being signed in to Spotify isn't the player missing: the sign-in is shown, and
    # the player is, as it is whether or not it's signed in.
    shown = _page(running=spotify, sessions={"bedroom": FakeSession(music=FakePlayer())}).music_state()
    assert shown["available"] and "why" not in shown and shown["spotify"] == {"signed_in": False}


def test_without_novas_config_the_page_says_theres_no_player_the_same_way_everywhere():
    # Nothing's known of what should have played, so neither player is blamed - and the
    # page's own words, the music section's and a button's, are the same sentence.
    page = _page()
    said = "Nova has no music player set up. Choose one in Settings, under Music."
    assert page.music_state() == {"available": False, "why": said}
    assert page.play("pause", LOGIN) == {"success": False, "message": said}


def test_the_page_tells_a_satellite_which_is_away_from_one_which_doesnt_exist(spotify):
    page = _page(running=spotify, sessions={"bedroom": FakeSession()}, known=["bedroom", "garage"])
    # Away, with no sign of a player to blame.
    away = {"available": False, "why": "No satellite is connected just now.", "spotify": {"signed_in": False}}
    assert page.music_state("garage") == away
    assert page.music_state("ghost") == {"available": False, "why": "There's no satellite called ghost.",
                                         "spotify": {"signed_in": False}}


def test_the_spotify_sign_in_is_offered_with_no_satellite_connected(spotify, make_config):
    page = _page(running=spotify, sessions={})
    assert page.music_state() == {"available": False, "why": "No satellite is connected just now.",
                                  "spotify": {"signed_in": False}}
    # Connected, with Spotify not signed in to.
    assert _page(running=spotify).music_state()["spotify"] == {"signed_in": False}
    # Navidrome has no sign-in.
    assert "spotify" not in _page(running=make_config(music={"player": "navidrome"}), sessions={}).music_state()


def _finish_spotify(page, address="http://127.0.0.1:8888/callback?code=the-code"):
    state = parse_qs(urlsplit(page.start_spotify_sign_in(LOGIN)["url"]).query)["state"][0]
    return page.finish_spotify_sign_in(f"{address}&state={state}", LOGIN)


def _raises(error):
    def get_access_token(self, code=None, as_dict=True, check_cache=True):
        raise error
    return get_access_token


@pytest.mark.parametrize("error, said", [
    (requests.ConnectionError("api.spotify.com unreachable"), "Couldn't reach Spotify - try again."),
    (requests.Timeout("too slow"), "Couldn't reach Spotify - try again."),
    # Anything else's own words never reach the page.
    (ValueError("Invalid IPv6 URL"), "Spotify didn't accept that sign-in - start again."),
])
def test_a_spotify_sign_in_that_goes_wrong_is_answered_plainly(spotify, monkeypatch, error, said):
    monkeypatch.setattr(FakeSpotifyOAuth, "get_access_token", _raises(error))
    page = _page(running=spotify, sessions={})
    assert _finish_spotify(page) == {"success": False, "message": said}


def test_a_spotify_sign_in_that_couldnt_be_saved_says_so(spotify, monkeypatch):
    # spotipy only warns when it can't write the cache.
    monkeypatch.setattr(FakeSpotifyOAuth, "get_access_token", lambda self, code=None, **kwargs: "the-access-token")
    page = _page(running=spotify, sessions={})
    assert _finish_spotify(page) == {"success": False,
                                     "message": "Couldn't save the sign-in - check Nova's data folder."}
    assert page.music_state()["spotify"] == {"signed_in": False}


def test_the_page_lets_another_spotify_account_be_chosen_and_forgets_the_old_ones_playlists(spotify):
    page = _page(running=spotify, sessions={"bedroom": FakeSession(music=FakePlayer())})
    assert page._spotify.oauth.kwargs["show_dialog"] is True
    page.music_state()
    assert page._playlists
    assert _finish_spotify(page)["success"]
    assert page._playlists == {}


def test_the_page_signs_in_to_spotify(spotify, monkeypatch, caplog):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _page(running=spotify, sessions={})
    page.start()
    try:
        def change(path, body):
            return json.loads(_ask(page, path, body, X_Nova="page")[2])

        caplog.set_level(logging.DEBUG)
        # Only from the page itself, like every change.
        assert _ask(page, "/api/spotify/start", {})[0] == 403
        started = change("/api/spotify/start", {})
        assert started["success"] and started["url"].startswith("https://accounts.spotify.com/authorize?")
        state = parse_qs(urlsplit(started["url"]).query)["state"][0]
        address = f"http://127.0.0.1:8888/callback?code=the-code&state={state}"
        assert change("/api/spotify/finish", {"address": address}) == {"success": True,
                                                                       "message": "Signed in to Spotify."}
        assert page.music_state()["spotify"] == {"signed_in": True}

        # Used once, and a cancelled one answered.
        assert change("/api/spotify/finish", {"address": address}) == {
            "success": False, "message": "That address isn't from the sign-in this page started - start again."}
        state = parse_qs(urlsplit(change("/api/spotify/start", {})["url"]).query)["state"][0]
        assert change("/api/spotify/finish", {"address": f"http://127.0.0.1:8888/callback?error=access_denied"
                                                         f"&state={state}"}) == {
            "success": False, "message": "Spotify didn't accept that sign-in - start again."}

        shown = json.dumps(page.music_state())
        for secret in ("the-code", "the-access-token", "the-refresh-token", "client-secret"):
            assert secret not in caplog.text and secret not in shown
        assert "signed in to Spotify" in caplog.text
    finally:
        page.stop()


class FakeTokenAnswer:
    """An answer from Google's token address, as its OAuth reference gives it."""

    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeGoogle:
    """
    Stands in for Google's token address, and the list of calendars a sign-in reads at
    once, so nothing reaches Google.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []
        # Google's list of calendars, as the new sign-in reads it.
        self.listed = {"items": [{"id": "calendar-gym", "summary": "Gym", "accessRole": "owner"}]}

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        assert url == google_calendar.CALENDAR_LIST
        if isinstance(self.listed, Exception):
            raise self.listed
        return FakeTokenAnswer(200, self.listed)

    def post(self, url, data=None, timeout=None, **kwargs):
        assert url == google_calendar.TOKEN
        self.asked.append(dict(data or {}))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


GOOGLE_TOKENS = {"access_token": "the-google-access-token", "refresh_token": "the-google-refresh-token",
                 "expires_in": 3600, "token_type": "Bearer", "scope": " ".join(google_calendar.SCOPES)}


@pytest.fixture
def calendar(files, monkeypatch):
    """Nova's calendar, its keys set as Nova started - with Google a fake, and always online."""
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)
    google = FakeGoogle(FakeTokenAnswer(200, GOOGLE_TOKENS))
    sign_in = google_calendar.GoogleSignIn("the-google-client-id", "the-google-client-secret",
                                           str(files / "google_token.json"), session=google)
    return google_calendar.GoogleCalendar(
        sign_in, ChoiceStore(str(files / "calendars.json"), google_calendar.CALENDAR_CHOICES), session=google)


# Google names the user's own calendar by their address, and a person's own shared with
# them by theirs - both made up.
OWN_ADDRESS = "me@example.com"
THEIR_ADDRESS = "someone@example.com"


@pytest.fixture
def signed_in(calendar, files):
    """
    The calendar signed in, with Google listing the user's own, Work, Uni - shared with
    them, so Nova can't add to it - and a person's own, named by their address.
    """
    storage.save_json(str(files / "google_token.json"), {"refresh_token": "the-google-refresh-token",
                                                         "scope": " ".join(google_calendar.SCOPES)})
    calendar.sign_in._session.listed = {"items": [
        {"id": "calendar-personal", "summary": OWN_ADDRESS, "accessRole": "owner", "primary": True},
        {"id": "calendar-work", "summary": "Work", "accessRole": "owner"},
        {"id": "calendar-uni", "summary": "Uni", "accessRole": "reader"},
        {"id": "calendar-theirs", "summary": THEIR_ADDRESS, "accessRole": "reader"}]}
    return calendar


def _google_address(started, code="the-google-code"):
    """The address Google sends the browser to, for the sign-in the page started."""
    state = parse_qs(urlsplit(started["url"]).query)["state"][0]
    return f"{google_calendar.REDIRECT}?state={state}&code={code}&scope=" + "+".join(google_calendar.SCOPES)


def test_the_calendars_section_says_what_google_needs_first(files):
    page = _page()
    needs = "Google's client ID and secret go under Keys, then restart Nova."
    assert page.calendars_state() == {"available": False, "message": needs}
    assert page.start_google_sign_in(LOGIN) == {"success": False, "message": needs}
    assert page.finish_google_sign_in("http://127.0.0.1:8085/?code=the-google-code", LOGIN) == {
        "success": False, "message": needs}


def test_the_page_signs_in_to_google(calendar, monkeypatch, caplog):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _page(sessions={}, calendar=calendar)
    page.start()
    try:
        def change(path, body):
            return json.loads(_ask(page, path, body, X_Nova="page")[2])

        def shown():
            return json.loads(_ask(page, "/api/calendars")[2])

        caplog.set_level(logging.DEBUG)
        assert shown() == {"available": True, "signed_in": False}
        # Only from the page itself, like every change.
        assert _ask(page, "/api/google/start", {})[0] == 403
        started = change("/api/google/start", {})
        assert started["success"] and started["url"].startswith(google_calendar.AUTHORISE + "?")
        address = _google_address(started)
        finished = change("/api/google/finish", {"address": address})
        assert finished == {"success": True, "message": "Signed in to Google."}
        assert shown() == {"available": True, "signed_in": True, "new_events": None, "calendars": [
            {"id": "calendar-gym", "name": "Gym", "primary": False, "writable": True, "whats_on": True,
             "activity": True}]}
        # Google's list of calendars - another account's, maybe - is read again at once.
        waited = time.monotonic() + 5
        while not calendar.known() and time.monotonic() < waited:
            time.sleep(0.01)
        assert [each["id"] for each in calendar.known()] == ["calendar-gym"]
        [asked] = calendar.sign_in._session.asked
        assert asked["code"] == "the-google-code"

        # Used once.
        assert change("/api/google/finish", {"address": address}) == {
            "success": False, "message": "That address isn't from this sign-in - start again."}

        state = parse_qs(urlsplit(started["url"]).query)["state"][0]
        said = json.dumps([finished, shown()])
        for secret in (address, state, "the-google-code", asked["code_verifier"], "the-google-access-token",
                       "the-google-refresh-token", "the-google-client-secret"):
            assert secret not in caplog.text and secret not in said
        assert "signed in to Google" in caplog.text
    finally:
        page.stop()


@pytest.mark.parametrize("started", [True, False])
def test_a_google_address_from_another_sign_in_is_refused(calendar, started):
    page = _page(calendar=calendar)
    if started:
        page.start_google_sign_in(LOGIN)
    address = f"{google_calendar.REDIRECT}?state=another-sign-ins-state&code=the-google-code"
    assert page.finish_google_sign_in(address, LOGIN) == {
        "success": False, "message": "That address isn't from this sign-in - start again."}
    assert calendar.sign_in._session.asked == []
    assert page.calendars_state() == {"available": True, "signed_in": False}


def test_a_second_google_start_refuses_the_first_starts_address(calendar):
    # The button tapped twice: only the newest sign-in's address is taken.
    page = _page(calendar=calendar)
    first = page.start_google_sign_in(LOGIN)
    page.start_google_sign_in(LOGIN)
    assert page.finish_google_sign_in(_google_address(first), LOGIN) == {
        "success": False, "message": "That address isn't from this sign-in - start again."}
    assert calendar.sign_in._session.asked == []


@pytest.mark.parametrize("answer, said", [
    (FakeTokenAnswer(401, {"error": "invalid_client", "error_description": "Unauthorized"}),
     "Google didn't accept the client ID or secret - check both under Keys."),
    (FakeTokenAnswer(400, {"error": "invalid_grant", "error_description": "Bad Request"}),
     "Google didn't accept that sign-in - start again."),
    (requests.ConnectionError("oauth2.googleapis.com unreachable"), "Couldn't reach Google - try again."),
    # Set up wrongly in Google Cloud - only its error code is said.
    (FakeTokenAnswer(400, {"error": "redirect_uri_mismatch", "error_description": "unreachable"}),
     "Google refused the sign-in (redirect_uri_mismatch) - check the client in Google Cloud."),
])
def test_each_google_failure_is_said_plainly(calendar, caplog, answer, said):
    calendar.sign_in._session.answers = [answer]
    page = _page(calendar=calendar)
    started = page.start_google_sign_in(LOGIN)
    caplog.set_level(logging.DEBUG)
    assert page.finish_google_sign_in(_google_address(started), LOGIN) == {"success": False, "message": said}
    assert page.calendars_state() == {"available": True, "signed_in": False}
    assert "the-google-code" not in caplog.text and "unreachable" not in caplog.text


def test_a_google_sign_in_that_goes_wrong_otherwise_is_answered_plainly(calendar, caplog):
    # A pasted "http://[x" gives urllib's "Invalid IPv6 URL" - not words for the page.
    page = _page(calendar=calendar)
    page.start_google_sign_in(LOGIN)
    assert page.finish_google_sign_in("http://[x?code=the-google-code", LOGIN) == {
        "success": False, "message": "Google didn't accept that sign-in - start again."}
    assert "the-google-code" not in caplog.text and calendar.sign_in._session.asked == []


def test_a_google_sign_in_that_couldnt_be_saved_says_so(calendar, monkeypatch):
    def full(path, data, indent=None):
        raise OSError(28, "No space left on device", path)

    monkeypatch.setattr(google_calendar.storage, "save_json", full)
    page = _page(calendar=calendar)
    assert page.finish_google_sign_in(_google_address(page.start_google_sign_in(LOGIN)), LOGIN) == {
        "success": False, "message": "Couldn't save the sign-in - check Nova's data folder."}
    assert page.calendars_state() == {"available": True, "signed_in": False}


def _calendar_row(calendar_id, name, primary=False, writable=False, whats_on=True, activity=False):
    return {"id": calendar_id, "name": name, "primary": primary, "writable": writable, "whats_on": whats_on,
            "activity": activity}


def test_the_page_shows_each_calendar_with_its_choices(signed_in, monkeypatch, caplog):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _page(sessions={}, calendar=signed_in)
    page.start()
    try:
        def change(path, body):
            return json.loads(_ask(page, path, body, X_Nova="page")[2])

        def shown():
            status, headers, body = _ask(page, "/api/calendars")
            assert status == 200 and headers["Cache-Control"] == "no-store"
            return json.loads(body)

        caplog.set_level(logging.DEBUG)
        # By Google's own names, an address too - it's the user's own page, and they need
        # to tell them apart. New events go to the primary until another is chosen.
        assert shown() == {"available": True, "signed_in": True, "new_events": "calendar-personal", "calendars": [
            _calendar_row("calendar-personal", OWN_ADDRESS, primary=True, writable=True),
            _calendar_row("calendar-work", "Work", writable=True, activity=True),
            _calendar_row("calendar-uni", "Uni", activity=True),
            _calendar_row("calendar-theirs", THEIR_ADDRESS)]}

        saved = {"success": True, "message": "Saved. It's used straight away."}
        assert change("/api/calendars", {"id": "calendar-uni", "whats_on": False}) == saved
        assert change("/api/calendars", {"id": "calendar-theirs", "activity": True}) == saved
        assert change("/api/calendars", {"id": "calendar-work", "whats_on": False, "activity": False}) == saved
        assert change("/api/calendars/new-events", {"id": "calendar-work"}) == saved
        assert shown()["new_events"] == "calendar-work"
        assert shown()["calendars"] == [
            _calendar_row("calendar-personal", OWN_ADDRESS, primary=True, writable=True),
            _calendar_row("calendar-work", "Work", writable=True, whats_on=False),
            _calendar_row("calendar-uni", "Uni", whats_on=False, activity=True),
            _calendar_row("calendar-theirs", THEIR_ADDRESS, activity=True)]
        # Only as a change from the page itself.
        assert _ask(page, "/api/calendars", {"id": "calendar-uni", "whats_on": True})[0] == 403

        nonsense = {"success": False, "message": "That didn't make sense."}
        # One with neither switch would be "Saved." for nothing.
        for body in ({"id": "calendar-uni", "whats_on": "true"}, {"id": "calendar-uni", "activity": 1},
                     {"id": ["calendar-uni"], "whats_on": True}, {"whats_on": True}, {"id": "calendar-uni"}):
            assert change("/api/calendars", body) == nonsense, body
        for body in ({"id": 7}, {}):
            assert change("/api/calendars/new-events", body) == nonsense, body
        assert change("/api/calendars", {"id": "calendar-made-up", "activity": True}) == {
            "success": False, "message": "Google isn't showing that calendar just now."}
        assert change("/api/calendars/new-events", {"id": "calendar-uni"}) == {
            "success": False, "message": "Nova can only add to your own calendars."}
        assert signed_in.left_out() == {"calendar-uni", "calendar-work"}

        # Google out of reach, once the list is five minutes old.
        signed_in._known_at -= 301
        signed_in.sign_in._session.listed = requests.ConnectionError("www.googleapis.com/calendar-personal")
        assert shown() == {"available": True, "signed_in": True, "message": "Nova can't reach Google just now."}

        # A calendar's name or id - the primary's is the user's address - is never logged
        # where the journal keeps it.
        kept = " ".join(record.getMessage() for record in caplog.records if record.levelno >= logging.INFO)
        assert "changed" in kept
        for private in (OWN_ADDRESS, THEIR_ADDRESS, "calendar-", "Work", "Uni"):
            assert private not in kept, private
    finally:
        page.stop()


def test_a_calendar_google_no_longer_lists_keeps_its_choice(signed_in):
    page = _page(calendar=signed_in)
    page.calendars_state()
    page.choose_calendar("calendar-work", LOGIN, whats_on=False)
    page.set_new_events("calendar-work", LOGIN)
    listed = signed_in.sign_in._session.listed["items"]

    # Unshared, say - it drops off the page, and new events go to the primary.
    signed_in.sign_in._session.listed = {"items": [each for each in listed if each["id"] != "calendar-work"]}
    signed_in._known_at -= 301
    state = page.calendars_state()
    assert [each["id"] for each in state["calendars"]] == ["calendar-personal", "calendar-uni", "calendar-theirs"]
    assert state["new_events"] == "calendar-personal"

    # Back, it's as it was chosen.
    signed_in.sign_in._session.listed = {"items": listed}
    signed_in._known_at -= 301
    state = page.calendars_state()
    assert state["calendars"][1] == _calendar_row("calendar-work", "Work", writable=True, whats_on=False,
                                                  activity=True)
    assert state["new_events"] == "calendar-work"


def test_the_first_choice_after_a_restart_reads_googles_list_first(signed_in):
    # Nothing's known of Google's list until it's read - the page open across a restart
    # said "Google isn't showing that calendar just now." at the first tap.
    page = _page(calendar=signed_in)
    read = signed_in.calendars
    held = []

    def calendars(*args, **kwargs):
        # Never while every other save waits on it.
        held.append(page._saving.locked())
        return read(*args, **kwargs)

    signed_in.calendars = calendars
    saved = {"success": True, "message": "Saved. It's used straight away."}
    assert signed_in.known() == []
    assert page.choose_calendar("calendar-work", LOGIN, whats_on=False) == saved
    signed_in._known, signed_in._known_at = [], None
    assert page.set_new_events("calendar-work", LOGIN) == saved
    assert held and not any(held)
    assert signed_in.left_out() == {"calendar-work"} and signed_in.choices.get("new_events") == "calendar-work"
    # Only one Google really hasn't got is turned away.
    assert page.choose_calendar("calendar-made-up", LOGIN, activity=True) == {
        "success": False, "message": "Google isn't showing that calendar just now."}


@pytest.mark.parametrize("answer, said", [
    # Removed in the Google account, or a week on while the client's in Testing.
    (FakeTokenAnswer(400, {"error": "invalid_grant", "error_description": "Bad Request"}),
     "Nova needs signing in to Google again."),
    (FakeTokenAnswer(401, {"error": "invalid_client", "error_description": "Unauthorized"}),
     "Google didn't accept the client ID or secret - check both under Keys."),
])
def test_a_sign_in_google_no_longer_takes_is_never_shown_as_signed_in(signed_in, answer, said):
    # Shown as signed in, beside "Nova can't reach Google just now.", it sent the user to the network.
    signed_in.sign_in._session.answers = [answer, answer, answer]
    page = _page(calendar=signed_in)
    assert page.calendars_state() == {"available": True, "signed_in": False, "message": said}
    assert page.choose_calendar("calendar-work", LOGIN, whats_on=False) == {"success": False, "message": said}
    assert page.set_new_events("calendar-work", LOGIN) == {"success": False, "message": said}
    assert signed_in.left_out() == set()


def test_the_music_buttons_go_through_the_same_actions_as_asking_out_loud(files):
    actions = FakeActions()
    page = _page(sessions={"bedroom": FakeSession(music=FakePlayer(), actions=actions)})
    page.play("play", LOGIN, query=" Gooba by 6ix9ine ")
    page.play("play", LOGIN)
    page.play("playlist", LOGIN, playlist="Workout")
    page.play("pause", LOGIN)
    assert actions.asked == [("play", "Gooba by 6ix9ine"), ("play", None), ("playlist", "Workout"), ("pause", None)]
    assert page.play("eject", LOGIN) == {"success": False, "message": "That isn't something the music does."}
    assert not _page().play("pause", LOGIN)["success"]


def test_the_lights_and_scenes_come_from_home_assistant_and_go_back_to_it(files):
    home = FakeHome()
    page = _page(sessions={"bedroom": FakeSession(home=home)})
    assert page.home_state()["lights"][0]["id"] == "light.kitchen"
    page.set_light("light.kitchen", LOGIN, on=False)
    page.set_light("light.kitchen", LOGIN, brightness="40")
    page.run("scene.relax", LOGIN)
    assert home.changed == [("light.kitchen", False, None), ("light.kitchen", None, 40), ("scene.relax", "run", None)]
    # Only a yes or no switches a light.
    assert not page.set_light("light.kitchen", LOGIN, on="off")["success"]
    assert page.home_state()["area"] == "Bedroom"
    assert _page().home_state() == {"available": False}
    assert not _page().run("scene.relax", LOGIN)["success"]


def test_a_routine_is_made_run_and_deleted_from_the_page(files):
    page = _page(routines=Routines(path=str(files / "routines.json")))
    ran = page.brain.ran
    made = page.save_routine({"name": "Good morning", "phrases": "good morning", "steps": "what's the weather"}, LOGIN)
    assert made == {"success": True, "message": "Saved Good morning."}
    routine = page.state()["routines"][0]

    assert page.start_routine(routine["id"], LOGIN) == {"success": True, "message": "Running Good morning."}
    deadline = time.monotonic() + 2
    while not ran and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [running["name"] for running in ran] == ["Good morning"]

    assert page.remove_routine(routine["id"], LOGIN)["success"] and page.state()["routines"] == []
    assert not page.start_routine(routine["id"], LOGIN)["success"]
    assert not page.save_routine("Good morning", LOGIN)["success"]


def test_the_page_says_what_nova_is_waiting_to_hear(files):
    # openWakeWord's stand-in, when Nova's own model is missing.
    assert _page(wake_word=lambda: "Hey Jarvis").state()["wake_word"] == "Hey Jarvis"
    assert _page().state()["wake_word"] == "Hey Nova"


def test_a_key_replaces_the_old_one_and_stays_private(files, caplog):
    (files / ".env").write_text("HA_TOKEN=old-token\nNAVIDROME_PASSWORD=keep-me\n")
    page = _page()

    assert page.set_key("HA_TOKEN", 'new"token', LOGIN)["success"]

    saved = (files / ".env").read_text()
    assert 'HA_TOKEN="new\\"token"' in saved and "old-token" not in saved and "NAVIDROME_PASSWORD=keep-me" in saved
    assert stat.S_IMODE(os.stat(files / ".env").st_mode) == 0o600
    assert 'new"token' not in caplog.text
    assert page.state()["restart_needed"]


@pytest.mark.parametrize("name, value", [("PATH", "/tmp"), ("HA_TOKEN", "two\nlines"), ("HA_TOKEN", "   "),
                                         # Which python-dotenv would read as a variable, even in quotes.
                                         ("HA_TOKEN", "with ${HOME} in it")])
def test_only_novas_own_keys_are_saved_and_nothing_odd(files, name, value):
    assert not _page().set_key(name, value, LOGIN)["success"]
    assert not (files / ".env").exists()


def test_the_page_takes_googles_keys(files, caplog):
    page = _page()
    labels = {key["name"]: key["label"] for key in page.state()["keys"]}
    assert labels["GOOGLE_CLIENT_ID"] == "Google client ID"
    assert labels["GOOGLE_CLIENT_SECRET"] == "Google client secret"

    assert page.set_key("GOOGLE_CLIENT_SECRET", "made-up-client-secret", LOGIN)["success"]

    assert 'GOOGLE_CLIENT_SECRET="made-up-client-secret"' in (files / ".env").read_text()
    assert stat.S_IMODE(os.stat(files / ".env").st_mode) == 0o600
    assert "made-up-client-secret" not in caplog.text and "made-up-client-secret" not in json.dumps(page.state())


def test_settings_are_saved_as_only_whats_different_with_a_copy_of_the_old_file(files):
    (files / "config.yaml").write_text("# My own settings\nnavidrome:\n  url: http://old.local:4533\n")
    result = _page().save_settings({"navidrome.url": "http://music.local:4533", "ntfy.url": "http://ntfy.local",
                                    "ntfy.alerts": "homelab, servers",
                                    "thresholds.silence_duration": "0.7", "logging.log_transcripts": False,
                                    # The same as config.yaml's, so left out of the file.
                                    "music.player": "spotify"}, LOGIN)

    assert result["success"]
    assert yaml.safe_load((files / "config.yaml").read_text()) == {
        "navidrome": {"url": "http://music.local:4533"}, "ntfy": {"url": "http://ntfy.local",
                                                                  "alerts": ["homelab", "servers"]},
        "thresholds": {"silence_duration": 0.7}, "logging": {"log_transcripts": False}}
    assert (files / "config.yaml.bak").read_text().startswith("# My own settings")


def test_the_page_can_save_with_a_removed_setting_still_in_the_file(files):
    # The brain's own file still has weather.location from before the weather was Home Assistant's.
    (files / "config.yaml").write_text("weather:\n  location: Leeds\n")
    page = _page()
    assert "weather.location" not in {setting["key"] for setting in page.state()["settings"]}
    assert page.save_settings({"navidrome.url": "http://music.local:4533"}, LOGIN)["success"]
    assert yaml.safe_load((files / "config.yaml").read_text())["navidrome"] == {"url": "http://music.local:4533"}
    assert not page.save_settings({"weather.location": "Leeds"}, LOGIN)["success"]


def test_saving_settings_keeps_the_header_setup_wrote(files):
    # yaml drops comments, so the first save from the page lost it.
    _page().save_settings({"navidrome.url": "http://music.local:4533"}, LOGIN)
    assert (files / "config.yaml").read_text().startswith(page_module.SETTINGS_HEADER)


@pytest.mark.parametrize("save, first, second, kept", [
    ("save_settings", {"navidrome.url": "http://music.local:4533"}, {"ntfy.quiet_from": "22:00"},
     lambda files: yaml.safe_load((files / "config.yaml").read_text())),
    ("set_key", ("HA_TOKEN", "first-token"), ("NTFY_TOKEN", "second-token"),
     lambda files: (files / ".env").read_text()),
])
def test_two_quick_saves_dont_lose_either(files, monkeypatch, save, first, second, kept):
    # From two tabs, or two keys pasted quickly: each read the file, changed it and
    # wrote it back, so the second could write over the first.
    page = _page()
    written = page_module.storage.save_text
    others = []

    def save_slowly(path, text):
        if not others:
            # The second save starts while the first is still writing.
            others.append(threading.Thread(target=_save, args=(page, save, second)))
            others[0].start()
            others[0].join(0.3)
        written(path, text)

    monkeypatch.setattr(page_module.storage, "save_text", save_slowly)
    _save(page, save, first)
    others[0].join(5)

    saved = str(kept(files))
    for change in (first, second):
        assert str(list(change.values())[0] if isinstance(change, dict) else change[1]) in saved


def _save(page, save, change):
    if save == "save_settings":
        assert page.save_settings(change, LOGIN)["success"]
    else:
        assert page.set_key(*change, LOGIN)["success"]


@pytest.mark.parametrize("changes, said", [({"music.player": "winamp"}, "navidrome, spotify"),
                                           ({"thresholds.silence_duration": "soon"}, "number"),
                                           ({"llm.model": "claude-opus-5-5"}, "can't change")])
def test_a_setting_nova_wouldnt_start_with_is_never_saved(files, changes, said):
    result = _page().save_settings(changes, LOGIN)
    assert not result["success"] and said in result["message"]
    assert not (files / "config.yaml").exists()


def _running(files, text="", **parts):
    """
    A page over a brain running on the settings in the file, as Nova's does, with ntfy -
    the brain's own apply_settings telling its parts of each change.
    """
    (files / "config.yaml").write_text("ntfy:\n  url: http://ntfy.test\n" + text)
    running = Config()
    parts.setdefault("ntfy", Ntfy("http://ntfy.test"))
    return _page(running=running, config=running, **parts), running


def test_a_straight_away_change_is_used_with_no_restart(files):
    page, _ = _running(files)
    result = page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)

    assert result == {"success": True, "message": "Saved. It's used straight away.", "restart_needed": False}
    assert page.brain.ntfy.quiet_from == "22:00"
    assert not page.state()["restart_needed"]


def test_a_mixed_save_uses_some_now_and_says_the_rest_waits(files):
    page, running = _running(files, "navidrome:\n  url: http://music.local:4533\n")
    result = page.save_settings({"ntfy.quiet_from": "22:00", "music.player": "navidrome"}, LOGIN)

    assert result["message"] == "Saved. What plays the music is used once Nova restarts, and the rest straight away."
    assert running.section("ntfy")["quiet_from"] == "22:00" and page.brain.ntfy.quiet_from == "22:00"
    # A player is a connection, swapped only at a restart.
    assert running.section("music")["player"] == "spotify"
    assert page.state()["restart_needed"]


def _unreachable(*args):
    raise RuntimeError("ntfy has gone")


def test_a_change_that_cant_be_used_now_is_kept_for_the_restart(files):
    page, _ = _running(files)
    page.brain.ntfy.retopic = _unreachable
    result = page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)

    assert yaml.safe_load((files / "config.yaml").read_text())["ntfy"]["quiet_from"] == "22:00"
    # Never "used straight away" for what wasn't.
    assert result["message"] == "Saved. It's used once Nova restarts, as it couldn't be used straight away."
    assert result["restart_needed"] and page.state()["restart_needed"]


def test_a_mixed_save_whose_straight_away_part_fails_says_it_waits(files):
    page, _ = _running(files)
    page.brain.ntfy.retopic = _unreachable
    result = page.save_settings({"ntfy.quiet_from": "22:00", "thresholds.silence_duration": 1.2}, LOGIN)

    assert result["message"] == ("Saved. Night starts at is used once Nova restarts, and the rest "
                                 "straight away. Some of it couldn't be used straight away.")
    assert result["restart_needed"]


def test_the_brain_failing_to_use_a_save_at_all_keeps_it_for_the_restart(files):
    page, _ = _running(files)

    def broken(changes):
        raise RuntimeError("the brain is in trouble")

    page.brain.apply_settings = broken
    result = page.save_settings({"thresholds.silence_duration": 1.2}, LOGIN)

    assert yaml.safe_load((files / "config.yaml").read_text())["thresholds"]["silence_duration"] == 1.2
    assert result == {"success": True, "restart_needed": True,
                      "message": "Saved. It's used once Nova restarts, as it couldn't be used straight away."}
    assert page.state()["restart_needed"]


def test_a_value_which_failed_and_is_passed_on_again_is_used(files):
    # ntfy is always told all five of its settings, so a later save of another passes
    # the one which failed on again - and once it's used, no restart is owed for it.
    page, _ = _running(files)
    retopic = page.brain.ntfy.retopic
    page.brain.ntfy.retopic = _unreachable
    page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)
    page.brain.ntfy.retopic = retopic
    result = page.save_settings({"ntfy.quiet_until": "06:30"}, LOGIN)

    assert (page.brain.ntfy.quiet_from, page.brain.ntfy.quiet_until) == ("22:00", "06:30")
    assert not result["restart_needed"] and not page.state()["restart_needed"]


def test_a_topic_saved_before_ntfy_is_set_up_waits_for_the_restart(files):
    # With no ntfy server when Nova started, there's nothing to send through until it
    # restarts with one - so the topic isn't "used straight away", though the night is.
    (files / "config.yaml").write_text("")
    running = Config()
    page = _page(running=running, config=running)
    result = page.save_settings({"ntfy.url": "http://ntfy.test", "ntfy.phone": "my-phone",
                                 "ntfy.quiet_from": "22:00"}, LOGIN)

    assert result["message"] == ("Saved. Your ntfy server and Topic for sending things to your phone are used "
                                 "once Nova restarts, and the rest straight away. Some of it couldn't be used "
                                 "straight away.")
    assert page.brain._night.quiet_from == "22:00"


def test_a_restart_still_owed_from_an_earlier_save_is_still_said(files):
    page, _ = _running(files, "navidrome:\n  url: http://music.local:4533\n")
    page.save_settings({"music.player": "navidrome"}, LOGIN)
    result = page.save_settings({"ntfy.quiet_from": "22:00"}, LOGIN)

    # The page's save bar shows it beside Restart now, so it says why there's still one.
    assert result["message"] == "Saved. It's used straight away. An earlier change is used once Nova restarts."
    assert result["restart_needed"]


@pytest.mark.parametrize("given", [5, 10 ** 400], ids=["five", "too big for a float"])
def test_an_error_names_the_setting_as_the_page_does(files, given):
    # 10 ** 400 is a whole number too big for a float, which the page's JSON can send.
    result = _page().save_settings({"thresholds.silence_duration": given}, LOGIN)
    assert result == {"success": False,
                      "message": "Seconds of quiet which end what you're saying should be from 0.2 to 3."}
    assert not (files / "config.yaml").exists()


@pytest.mark.parametrize("key, given, said", [
    # A number box sends "" for anything it can't read, so this is the one most often seen.
    ("thresholds.silence_duration", "", "Seconds of quiet which end what you're saying should be a number."),
    ("sounds.chime_enabled", "yes", "The chime for done should be yes or no."),
], ids=["number", "switch"])
def test_a_value_of_the_wrong_kind_names_the_setting_as_the_page_does(files, key, given, said):
    assert _page().save_settings({key: given}, LOGIN) == {"success": False, "message": said}
    assert not (files / "config.yaml").exists()


def test_settings_that_need_each_other_are_refused_on_saving(files):
    result = _page().save_settings({"music.player": "navidrome"}, LOGIN)
    assert result == {"success": False, "message": "Navidrome as the player needs Navidrome's address."}
    assert not (files / "config.yaml").exists()


def test_each_setting_says_how_its_used_and_its_limits(files):
    settings = {setting["key"]: setting for setting in _page().state()["settings"]}
    silence, night = settings["thresholds.silence_duration"], settings["ntfy.quiet_from"]

    assert silence["used"] == "Used straight away" and (silence["min"], silence["max"]) == (0.2, 3)
    assert night["used"] == "Used straight away" and night["form"] == "time"
    # A time picker, so no "as 23:00" for typing it - which a save's message would say too.
    assert (night["label"], settings["ntfy.quiet_until"]["label"]) == ("Night starts at", "Night ends at")
    assert settings["music.player"]["used"] == "Used once Nova restarts"


def test_the_safe_settings_are_on_the_page(files):
    settings = {setting["key"]: setting for setting in _page().state()["settings"]}
    expected = {
        "sounds.listening_enabled": ("The blip when Nova's listening", NOW),
        "sounds.chime_enabled": ("The chime for done", NOW),
        "sounds.reminder_enabled": ("The sound before alerts and warnings", NOW),
        "web_search.enabled": ("Search the web when asked to", NOW),
        "conversation.history_max_age": ("Seconds a conversation's thread is kept", NOW),
        "spotify.pause_while_speaking": ("Pause Spotify while Nova talks", NOW),
        "mqtt.host": ("Home Assistant's MQTT host", LATER),
        "mqtt.port": ("Its port", LATER),
        "mqtt.username": ("Nova's MQTT user", LATER),
    }
    for key, (label, used) in expected.items():
        assert (settings[key]["label"], settings[key]["used"]) == (label, used)


def test_mqtt_waits_for_a_restart(files):
    page, running = _running(files)
    result = page.save_settings({"mqtt.host": "192.168.1.20"}, LOGIN)

    assert result == {"success": True, "message": "Saved. It's used once Nova restarts.", "restart_needed": True}
    assert running.section("mqtt")["host"] == ""


def test_the_transcripts_switch_is_used_straight_away(files):
    page, _ = _running(files)
    try:
        assert page.save_settings({"logging.log_transcripts": False}, LOGIN)["success"]
        assert not logging_setup.transcripts_logged()
        assert page.save_settings({"logging.log_transcripts": True}, LOGIN)["success"]
        assert logging_setup.transcripts_logged()
    finally:
        logging_setup.log_transcripts(True)


def test_two_quick_saves_both_reach_the_running_settings(files, monkeypatch):
    page, running = _running(files)
    written = page_module.storage.save_text
    others = []

    def save_slowly(path, text):
        if not others:
            # The second save starts while the first is still writing.
            others.append(threading.Thread(target=_save, args=(page, "save_settings",
                                                               {"thresholds.silence_duration": 1.2})))
            others[0].start()
            others[0].join(0.3)
        written(path, text)

    monkeypatch.setattr(page_module.storage, "save_text", save_slowly)
    _save(page, "save_settings", {"ntfy.quiet_from": "22:00"})
    others[0].join(5)

    saved = yaml.safe_load((files / "config.yaml").read_text())
    assert (saved["ntfy"]["quiet_from"], saved["thresholds"]["silence_duration"]) == ("22:00", 1.2)
    assert (running.section("ntfy")["quiet_from"], running.section("thresholds")["silence_duration"]) == (
        "22:00", 1.2)


def test_its_only_there_when_switched_on(make_config):
    brain = FakeBrain({})
    assert SettingsPage.from_config(make_config(settings_page={"port": 0}), brain) is None
    page = SettingsPage.from_config(make_config(settings_page={"port": 8765, "allow": [LOGIN]}), brain)
    assert (page.port, page.allow, page.brain) == (8765, [LOGIN], brain)


# Serving it

@pytest.fixture
def served(files, monkeypatch):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _page()
    page.start()
    yield page
    page.stop()


def _ask(page, path, body=None, login=LOGIN, **headers):
    """Asks the page for something as a browser on the tailnet would, through Tailscale."""
    request = urllib.request.Request(f"http://127.0.0.1:{page.port}{path}",
                                     data=None if body is None else json.dumps(body).encode())
    if login:
        request.add_header("Tailscale-User-Login", login)
    for name, value in headers.items():
        request.add_header(name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_only_the_users_own_login_gets_in(served):
    status, headers, body = _ask(served, "/")
    assert status == 200 and b"<title>Nova</title>" in body
    assert _ask(served, "/", login="someone@else.com")[0] == 403
    # Not through Tailscale at all.
    assert _ask(served, "/api/state", login=None)[0] == 403


def test_the_page_comes_with_its_safety_headers(served):
    _, headers, _ = _ask(served, "/")
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["/settings.js", "/settings.css"])
def test_the_pages_script_and_style_are_served_with_its_safety_headers(served, path):
    # There's no JavaScript engine to check the script with, so this is what stands in.
    status, headers, body = _ask(served, path)
    assert status == 200 and headers["Content-Type"] == page_module.FILES[path][1]
    for name, value in page_module.SAFETY_HEADERS.items():
        assert headers[name] == value
    if path == "/settings.js":
        assert b'"/api/voice/hear"' in body and b'"restart-now"' in body and b'"/api/stop"' in body
        assert b'"/api/undo"' in body and b'getElementById("undos")' in body
        assert b'"/api/satellites/add"' in body and b'"/api/satellites/password"' in body
        assert b'"/api/satellites/remove"' in body and b'"/api/satellites/default"' in body
        # The speaker a satellite says it plays Spotify on is put on the page only as text.
        assert b'element("span", `Spotify plays on ${satellite.spotify_speaker}`, "detail")' in body
        # No longer than the name rule lets a name be.
        assert b'id="satellite-name"' in _ask(served, "/")[2] and b'maxlength="63"' in _ask(served, "/")[2]
        # The password is only ever put on the page as text.
        assert b'getElementById("password-text").textContent = password' in body and b"innerHTML" not in body
        # Where each removal's Undo is stacked.
        assert b'id="undos"' in _ask(served, "/")[2]
        # Spotify's and Google's tabs are opened in the click itself, before anything's waited for.
        assert b"send(`/api/${service}/start`, {})" in body and b"`/api/${service}/finish`" in body
        assert b'startSignIn("spotify")' in body and b'startSignIn("google")' in body
        # The calendars' choices, and their names put on the page only as text.
        assert b'changeCalendar("/api/calendars", { id: calendar.id, [key]: box.checked })' in body
        assert b'changeCalendar("/api/calendars/new-events", { id: event.target.value })' in body
        assert b'id="calendars"' in _ask(served, "/")[2] and b'id="new-events-calendar"' in _ask(served, "/")[2]
        assert b'const tab = window.open("", "_blank");\n  // So' in body
        html = _ask(served, "/")[2]
        assert b'id="spotify-address"' in html and b'id="google-address"' in html
        assert b'fetch("/api/calendars")' in body
        # A tab closed while Nova was asked gets the link instead.
        assert b"const opened = Boolean(tab && !tab.closed);" in body


def test_a_change_has_to_come_from_the_page_itself(served):
    change = {"category": "music", "percent": 20}
    # As another website open in the user's browser would send it.
    assert _ask(served, "/api/volume", change)[0] == 403
    assert _ask(served, "/api/volume", change, X_Nova="page", Sec_Fetch_Site="cross-site")[0] == 403
    assert _bedroom(served).volume.levels["music"] == 40

    status, _, body = _ask(served, "/api/volume", change, X_Nova="page", Sec_Fetch_Site="same-origin")
    assert status == 200 and json.loads(body)["success"] and _bedroom(served).volume.levels["music"] == 20


def test_each_change_reaches_the_right_part_of_nova(served):
    def change(path, body):
        return json.loads(_ask(served, path, body, X_Nova="page")[2])

    change("/api/alarms", {"time": "07:30", "repeat": "weekdays"})
    change("/api/remove", {"kind": "timer", "id": 1})
    change("/api/forget", {"fact": "Your sister is called Amy."})

    # The alarm is the default satellite's, as none was named.
    assert served.brain.clock.alarms == [(7, 30, "weekdays", "bedroom")]
    assert served.brain.clock.removed == [("timer", 1)]
    assert served.brain.memory.known == []
    assert change("/api/restart", {})["message"] == "Restarting."


def test_removing_on_the_page_offers_an_undo(files, monkeypatch, caplog):
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(files / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(files / "alarms.json"))
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    clock = Clock(start=False)
    clock.set_alarm(7, 0, repeat="weekdays", satellite="bedroom")
    before = dict(clock._alarms[1])
    page = _page(clock=clock)
    page.start()
    try:
        def change(path, body):
            return json.loads(_ask(page, path, body, X_Nova="page")[2])

        with caplog.at_level(logging.INFO):
            removed = change("/api/remove", {"kind": "alarm", "id": 1})
        # Only what's said and the token - never the alarm itself, nor in the log.
        assert set(removed) == {"success", "message", "undo"} and len(removed["undo"]) >= 20
        assert removed["undo"] not in caplog.text and clock._alarms == {}

        # As every change, only from the page itself.
        assert _ask(page, "/api/undo", {"token": removed["undo"]})[0] == 403
        assert change("/api/undo", {"token": removed["undo"]}) == {
            "success": True, "message": "Put back the 7:00 AM alarm, every weekday."}
        assert clock._alarms[1] == before
        cant = {"success": False, "message": "That can't be undone now."}
        assert change("/api/undo", {"token": removed["undo"]}) == cant
        assert change("/api/undo", {"token": "made-up"}) == cant
        assert change("/api/undo", {"token": 12}) == cant
        # What's already gone has nothing to undo.
        assert change("/api/remove", {"kind": "timer", "id": 9}) == {"success": False, "message": "That's already gone."}
    finally:
        page.stop()


def test_forgetting_on_the_page_offers_an_undo(caplog):
    page = _page()
    page.brain.memory.known = ["You work at Tesco.", "Your sister is called Amy.", "Your bin day is Thursday."]
    with caplog.at_level(logging.INFO):
        forgotten = page.forget("Your sister is called Amy.", LOGIN)
    # Only what's said and the token - never the fact or its place, nor in the log.
    assert set(forgotten) == {"success", "message", "undo"}
    assert "Amy" not in caplog.text and forgotten["undo"] not in caplog.text
    assert page.brain.memory.known == ["You work at Tesco.", "Your bin day is Thursday."]

    with caplog.at_level(logging.INFO):
        assert page.undo(forgotten["undo"], LOGIN) == {"success": True, "message": "Put it back."}
    assert "undid forgetting something" in caplog.text and "Amy" not in caplog.text
    assert page.brain.memory.known == ["You work at Tesco.", "Your sister is called Amy.", "Your bin day is Thursday."]
    assert page.undo(forgotten["undo"], LOGIN) == {"success": False, "message": "That can't be undone now."}


def _undoable(files, monkeypatch):
    """A page over a real clock, with a weekday 7am alarm on the bedroom."""
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(files / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(files / "alarms.json"))
    clock = Clock(start=False)
    clock.set_alarm(7, 0, repeat="weekdays", satellite="bedroom")
    return _page(clock=clock), clock


def test_an_undo_for_a_satellite_since_gone_isnt_put_back(files, monkeypatch):
    page, clock = _undoable(files, monkeypatch)
    token = page.remove("alarm", 1, LOGIN)["undo"]
    page.brain.known = ["kitchen"]
    assert page.undo(token, LOGIN) == {"success": False, "message": "That satellite's gone, so it can't ring there."}
    assert clock._alarms == {}
    # Back again within the minute, it can be.
    page.brain.known = ["bedroom", "kitchen"]
    assert page.undo(token, LOGIN)["success"] and clock._alarms[1]["satellite"] == "bedroom"


def test_an_undo_refused_for_a_clash_works_once_the_clash_is_gone(files, monkeypatch):
    page, clock = _undoable(files, monkeypatch)
    token = page.remove("alarm", 1, LOGIN)["undo"]
    clock.set_alarm(7, 0, repeat="daily", satellite="bedroom")
    assert page.undo(token, LOGIN) == {"success": False, "message": "You already have a 7:00 AM alarm, every day."}
    page.remove("alarm", 2, LOGIN)
    assert page.undo(token, LOGIN) == {"success": True, "message": "Put back the 7:00 AM alarm, every weekday."}
    assert page.undo(token, LOGIN) == {"success": False, "message": "That can't be undone now."}


def test_two_undo_taps_at_once_put_back_one_alarm(files, monkeypatch):
    page, clock = _undoable(files, monkeypatch)
    token = page.remove("alarm", 1, LOGIN)["undo"]
    both = threading.Barrier(2)
    put_back = clock.put_back

    def slowly(kind, item, now=None):
        # Long enough for the other tap to come in meanwhile.
        time.sleep(0.2)
        return put_back(kind, item, now)

    clock.put_back = slowly
    answers = []

    def tap():
        both.wait()
        answers.append(page.undo(token, LOGIN)["message"])

    taps = [threading.Thread(target=tap) for _ in range(2)]
    for each in taps:
        each.start()
    for each in taps:
        each.join()
    assert sorted(answers) == ["Put back the 7:00 AM alarm, every weekday.", "That can't be undone now."]
    assert list(clock._alarms) == [1]


def test_its_typeface_icon_and_what_nova_is_doing_are_served(served):
    status, headers, body = _ask(served, "/Outfit.woff2")
    assert status == 200 and headers["Content-Type"] == "font/woff2" and body[:4] == b"wOF2"
    status, headers, body = _ask(served, "/icon.svg")
    assert status == 200 and headers["Content-Type"] == "image/svg+xml" and body.startswith(b"<svg")
    assert json.loads(_ask(served, "/api/live")[2]) == {"activity": "idle", "stoppable": False}


def test_the_quick_check_says_when_theres_something_to_stop(served, caplog):
    def live():
        return json.loads(_ask(served, "/api/live")[2])["stoppable"]

    def stop(**headers):
        status, _, body = _ask(served, "/api/stop", {}, **headers)
        return status, json.loads(body) if status == 200 else body

    bedroom = _bedroom(served)
    # Pressed after it stopped by itself, there's nothing to do.
    assert stop(X_Nova="page") == (200, {"success": True, "message": "Nothing's ringing or speaking."})
    assert not live()

    served.brain.clock.ringing.add("bedroom")
    bedroom.stop_what_rings_and_speaks = lambda: served.brain.clock.ringing.discard("bedroom")
    assert live()
    # As every change, only from the page itself.
    assert stop()[0] == 403 and live()
    with caplog.at_level(logging.INFO, logger="voice_assistant.settings_page"):
        assert stop(X_Nova="page") == (200, {"success": True, "message": "Stopped."})
    assert f"Settings page: {LOGIN} pressed Stop" in caplog.text
    assert not live()

    bedroom.activity = "speaking"
    assert live()


def test_the_cover_of_whats_playing_is_served(served):
    # Nothing's playing without a music player.
    assert _ask(served, "/api/cover")[0] == 404
    _bedroom(served).music_player = FakePlayer()
    status, headers, body = _ask(served, "/api/cover?for=al-1")
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and body == b"a cover"


def test_music_and_home_assistant_changes_reach_them(served):
    bedroom = _bedroom(served)
    bedroom.music_player, bedroom.actions, bedroom.home = FakePlayer(), FakeActions(), FakeHome()
    assert json.loads(_ask(served, "/api/music")[2])["now"]["artist"] == "6ix9ine"
    assert json.loads(_ask(served, "/api/home")[2])["routines"][0]["name"] == "Relax"

    _ask(served, "/api/music", {"action": "skip"}, X_Nova="page")
    _ask(served, "/api/light", {"id": "light.kitchen", "brightness": 70}, X_Nova="page")
    _ask(served, "/api/run", {"id": "scene.relax"}, X_Nova="page")
    assert bedroom.actions.asked == [("skip", None)]
    assert bedroom.home.changed == [("light.kitchen", None, 70), ("scene.relax", "run", None)]


def test_a_script_runs_only_with_a_yes(served, caplog):
    bedroom = _bedroom(served)
    bedroom.home = FakeHome()
    refused = {"success": False,
               "message": "A script only runs once you've said yes. Reload the page to be asked."}

    def run(body):
        return json.loads(_ask(served, "/api/run", body, X_Nova="page")[2])

    # A stray POST, an old tab or a yes that's only truthy never reaches Home Assistant.
    assert run({"id": "script.movie_night"}) == refused
    assert run({"id": "script.movie_night", "confirmed": "true"}) == refused
    assert run({"id": "script.movie_night", "confirmed": 1}) == refused
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="voice_assistant.settings_page"):
        assert served.run("script.movie_night", LOGIN) == refused
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f"Settings page: refused running script.movie_night for {LOGIN} without a yes")]
    assert bedroom.home.changed == []
    assert run({"id": "script.movie_night", "confirmed": True})["success"]
    assert bedroom.home.changed == [("script.movie_night", "run", None)]


def test_a_scene_still_runs_with_one_tap(served):
    bedroom = _bedroom(served)
    bedroom.home = FakeHome()
    assert json.loads(_ask(served, "/api/run", {"id": "scene.relax"}, X_Nova="page")[2])["success"]
    assert bedroom.home.changed == [("scene.relax", "run", None)]


def test_the_settings_page_shows_and_changes_mute(served):
    def state():
        return json.loads(_ask(served, "/api/state")[2])

    assert state()["muted"] is False
    status, _, body = _ask(served, "/api/mute", {"muted": True}, X_Nova="page")
    assert status == 200 and json.loads(body)["success"]
    assert _bedroom(served).muted and state()["muted"] is True

    # Only a yes or no mutes it.
    assert not json.loads(_ask(served, "/api/mute", {"muted": "no"}, X_Nova="page")[2])["success"]
    assert _bedroom(served).muted


def test_nonsense_is_answered_rather_than_crashing_on(served):
    assert json.loads(_ask(served, "/api/volume", {}, X_Nova="page")[2]) == {
        "success": False, "message": "That didn't make sense."}
    assert _ask(served, "/api/nothing", {}, X_Nova="page")[0] == 400
    assert _ask(served, "/nothing")[0] == 404
    # JSON's Infinity, which int() can't take.
    for path, body in (("/api/volume", {"category": "general", "percent": float("inf")}),
                       ("/api/remove", {"kind": "timer", "id": float("inf")})):
        assert json.loads(_ask(served, path, body, X_Nova="page")[2]) == {
            "success": False, "message": "That didn't make sense."}


@pytest.mark.parametrize("length", ["-1", "lots"])
def test_a_length_which_isnt_one_is_answered(served, length):
    # A negative one read to the end of the connection, which never came; one which
    # wasn't a number dropped the connection.
    connection = http.client.HTTPConnection("127.0.0.1", served.port, timeout=2)
    connection.request("POST", "/api/volume", body=b'{"category": "general", "percent": 30}',
                       headers={"Tailscale-User-Login": LOGIN, "X-Nova": "page", "Content-Length": length})
    response = connection.getresponse()
    assert (response.status, json.loads(response.read())) == (400, {"success": False,
                                                                    "message": "That didn't make sense."})
    connection.close()


def test_a_change_which_cant_be_saved_is_answered(served, monkeypatch):
    def full(path, text):
        raise OSError("No space left on device")

    monkeypatch.setattr(page_module.storage, "save_text", full)
    for path, body in (("/api/keys", {"name": "HA_TOKEN", "value": "a-token"}),
                       ("/api/settings", {"changes": {"navidrome.url": "http://music.local:4533"}})):
        assert json.loads(_ask(served, path, body, X_Nova="page")[2]) == {
            "success": False, "message": "Couldn't save that."}


def test_a_lights_colour_and_warmth_reach_home_assistant(files):
    home = FakeHome()
    page = _page(sessions={"bedroom": FakeSession(home=home)})
    assert page.set_light("light.kitchen", LOGIN, hue=240, saturation="100")["success"]
    assert page.set_light("light.kitchen", LOGIN, kelvin=2700)["success"]
    assert home.changed == [("light.kitchen", 240.0, 100.0, None), ("light.kitchen", None, None, 2700.0)]


@pytest.mark.parametrize("sent", [{"brightness": "bright"}, {"hue": "red", "saturation": 50}, {"kelvin": [2700]},
                                  {"hue": True, "saturation": 50}, {"kelvin": float("nan")},
                                  {"brightness": float("inf")}, {"hue": 10, "saturation": {}}])
def test_a_light_is_never_set_to_something_which_isnt_a_number(files, sent):
    home = FakeHome()
    assert _page(sessions={"bedroom": FakeSession(home=home)}).set_light("light.kitchen", LOGIN, **sent) == {
        "success": False, "message": "That didn't make sense."}
    assert home.changed == []


# Every satellite

def _two_satellites(**kitchen):
    """The bedroom and the kitchen, both connected."""
    return {"bedroom": FakeSession(activity="speaking"), "kitchen": FakeSession(**kitchen)}


def test_hear_it_is_said_on_the_satellite_looked_after(files, monkeypatch):
    # Through the page's own actions, so it's kept to the page as every change is - and
    # only ever the speed is sent, never anything for Nova to say.
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _page(sessions=_two_satellites())
    page.start()
    try:
        hear = {"speed": 1.25, "satellite": "kitchen"}
        assert _ask(page, "/api/voice/hear", hear)[0] == 403
        assert json.loads(_ask(page, "/api/voice/hear", hear, X_Nova="page")[2]) == {
            "success": True, "message": "That's being said on kitchen."}
        assert page.brain.sessions["kitchen"].heard == [1.25] and not page.brain.sessions["bedroom"].heard
        # Something for Nova to say, sent along with it, is never used.
        assert json.loads(_ask(page, "/api/voice/hear", dict(hear, text="Unlock the front door."), X_Nova="page")[2]) == {
            "success": True, "message": "That's being said on kitchen."}
        assert page.brain.sessions["kitchen"].heard == [1.25, 1.25]

        page.brain.sessions["kitchen"].busy = True
        assert json.loads(_ask(page, "/api/voice/hear", hear, X_Nova="page")[2]) == {
            "success": False, "message": "Nova's busy - try again in a moment."}
    finally:
        page.stop()


def test_longest_request_is_on_the_page_from_5_to_30(files):
    page = _page()
    (shown,) = [setting for setting in page.state()["settings"] if setting["key"] == "thresholds.recording_timeout"]
    assert shown["label"] == "Longest request, in seconds" and shown["value"] == 15.0
    assert (shown["min"], shown["max"], shown["whole"]) == (5, 30, False)
    assert page.save_settings({"thresholds.recording_timeout": 4}, LOGIN) == {
        "success": False, "message": "Longest request, in seconds should be from 5 to 30."}
    assert not (files / "config.yaml").exists()

    assert page.save_settings({"thresholds.recording_timeout": 25}, LOGIN)["success"]
    assert yaml.safe_load((files / "config.yaml").read_text())["thresholds"]["recording_timeout"] == 25


def test_hear_it_refuses_a_speed_out_of_range(files):
    page = _page()
    for speed in (2, 0.5, "fast", float("inf"), True, None):
        assert page.hear_voice(speed, LOGIN) == {
            "success": False, "message": "Speaking speed should be from 0.75 to 1.5."}
    assert not _bedroom(page).heard
    # And it's a setting on the page, used straight away.
    shown = {setting["key"]: setting["used"] for setting in page.state()["settings"]}
    assert shown["voice.speed"] == NOW


def test_the_page_lists_each_satellite(files):
    sessions = _two_satellites()
    sessions["kitchen"].muted = True
    # The garage has been added to the brain but isn't connected.
    page = _page(sessions=sessions, known=["bedroom", "kitchen", "garage"])
    state = page.state()

    assert state["satellites"] == [
        {"name": "bedroom", "connected": True, "activity": "speaking", "muted": False, "default": True,
         "spotify_speaker": "Voice-Assistant"},
        {"name": "kitchen", "connected": True, "activity": "idle", "muted": True, "default": False,
         "spotify_speaker": "Voice-Assistant"},
        {"name": "garage", "connected": False, "activity": "idle", "muted": False, "default": False,
         "spotify_speaker": None}]
    # What's shown below is the default's, unless another is asked for.
    assert state["muted"] is False and page.state("kitchen")["muted"] is True


def test_the_satellites_list_shows_each_speaker(files, make_config):
    # The kitchen's is its own; the bedroom's an older satellite's, which doesn't say,
    # so plays on the brain's; and the garage isn't connected, so plays on none.
    sessions = {"bedroom": FakeSession(), "kitchen": FakeSession(speaker="Kitchen Pi")}
    page = _page(sessions=sessions, known=["bedroom", "kitchen", "garage"],
                 config=make_config(spotify={"device_name": "Living Room"}))
    assert [(each["name"], each["spotify_speaker"]) for each in page.state()["satellites"]] == [
        ("bedroom", "Living Room"), ("kitchen", "Kitchen Pi"), ("garage", None)]


def test_no_spotify_speaker_is_shown_when_navidrome_plays(files, make_config):
    # Spotify isn't what plays the music then, so saying where it plays would be wrong.
    sessions = {"bedroom": FakeSession(), "kitchen": FakeSession(speaker="Kitchen Pi")}
    page = _page(sessions=sessions, known=["bedroom", "kitchen"],
                 config=make_config(music={"player": "navidrome"}))
    assert [(each["name"], each["spotify_speaker"]) for each in page.state()["satellites"]] == [
        ("bedroom", None), ("kitchen", None)]


def test_the_default_satellite_is_marked(files):
    # The first added, whether it's connected or not - what belongs to no satellite goes there.
    page = _page(sessions={"kitchen": FakeSession()}, known=["garage", "kitchen"])
    assert [(each["name"], each["default"]) for each in page.state()["satellites"]] == [
        ("garage", True), ("kitchen", False)]


def test_a_volume_change_goes_to_the_satellite_named(files):
    page = _page(sessions=_two_satellites())
    assert page.set_volume("general", 20, LOGIN, "kitchen")["success"]
    assert page.brain.sessions["kitchen"].volume.levels["general"] == 20
    assert page.brain.sessions["kitchen"].booped == [0.2]
    assert page.brain.sessions["bedroom"].volume.levels["general"] == 60 and not page.brain.sessions["bedroom"].booped
    # None named, so the default's.
    page.set_volume("general", 10, LOGIN)
    assert page.brain.sessions["bedroom"].volume.levels["general"] == 10

    page.set_muted(True, LOGIN, "kitchen")
    assert page.brain.sessions["kitchen"].muted and not page.brain.sessions["bedroom"].muted


def test_the_music_and_lights_are_the_satellite_named(files):
    actions, home = FakeActions(), FakeHome()
    page = _page(sessions=_two_satellites(music=FakePlayer(), actions=actions, home=home))
    assert page.music_state("kitchen")["available"] and page.home_state("kitchen")["available"]
    assert not page.music_state()["available"] and not page.home_state()["available"]
    page.play("skip", LOGIN, satellite="kitchen")
    page.set_light("light.kitchen", LOGIN, on=False, satellite="kitchen")
    assert actions.asked == [("skip", None)] and home.changed == [("light.kitchen", False, None)]
    assert not page.play("skip", LOGIN)["success"]


def test_a_satellite_which_isnt_connected_is_said_so(files):
    page = _page(sessions={"bedroom": FakeSession()}, known=["bedroom", "garage"])
    said = {"success": False, "message": "garage isn't connected."}
    assert page.set_volume("general", 20, LOGIN, "garage") == said
    assert page.set_muted(True, LOGIN, "garage") == said
    assert page.play("pause", LOGIN, satellite="garage") == said
    assert page.set_light("light.kitchen", LOGIN, on=True, satellite="garage") == said
    assert page.run("scene.relax", LOGIN, satellite="garage") == said
    # The default is named in the same way when it's the one which isn't connected.
    away = _page(sessions={}, known=["bedroom"])
    assert away.set_volume("general", 20, LOGIN) == {"success": False, "message": "bedroom isn't connected."}
    assert away.music_state() == {"available": False, "why": "No satellite is connected just now."}
    assert away.home_state() == {"available": False}
    assert away.cover() is None and away.state()["volumes"] == {}


def test_alarms_and_routines_work_with_no_satellite(files):
    page = _page(sessions={}, known=["bedroom"], routines=Routines(path=str(files / "routines.json")))

    assert page.add_alarm("07:30", None, LOGIN)["success"]
    # Set for the default satellite, which rings it once it's back.
    assert page.brain.clock.alarms == [(7, 30, None, "bedroom")]
    assert page.add_alarm("08:00", None, LOGIN, "bedroom")["success"] and page.brain.clock.alarms[1][3] == "bedroom"
    assert page.remove("timer", 1, LOGIN)["success"]
    assert page.forget("Your sister is called Amy.", LOGIN)["success"]
    assert page.save_routine({"name": "Good morning", "phrases": "good morning", "steps": "what's the weather"},
                             LOGIN)["success"]
    assert page.set_key("HA_TOKEN", "a-token", LOGIN)["success"]

    state = page.state()
    assert state["clock"]["timers"] and state["memories"] == [] and state["routines"][0]["name"] == "Good morning"
    assert state["volumes"] == {} and state["muted"] is False
    assert state["satellites"] == [
        {"name": "bedroom", "connected": False, "activity": "idle", "muted": False, "default": True,
         "spotify_speaker": None}]
    # Without any satellite added at all, too.
    assert _page(sessions={}, known=[]).state()["satellites"] == []


def test_the_clock_is_shown_for_the_satellite_asked_about(files):
    page = _page(sessions=_two_satellites())
    page.state()
    assert page.brain.clock.overviewed == "bedroom"
    page.state("kitchen")
    assert page.brain.clock.overviewed == "kitchen"


def test_the_page_asks_after_the_satellite_it_names(served):
    served.brain.sessions["kitchen"] = FakeSession(activity="thinking")
    served.brain.known.append("kitchen")
    assert json.loads(_ask(served, "/api/live?satellite=kitchen")[2]) == {"activity": "thinking", "stoppable": False}
    assert json.loads(_ask(served, "/api/live")[2]) == {"activity": "idle", "stoppable": False}
    assert json.loads(_ask(served, "/api/state?satellite=kitchen")[2])["satellite"] == "kitchen"

    _ask(served, "/api/volume", {"category": "general", "percent": 5, "satellite": "kitchen"}, X_Nova="page")
    assert served.brain.sessions["kitchen"].volume.levels["general"] == 5
    said = json.loads(_ask(served, "/api/mute", {"muted": True, "satellite": "porch"}, X_Nova="page")[2])
    assert said == {"success": False, "message": "porch isn't connected."}


def test_an_alarm_is_never_set_for_a_satellite_the_brain_doesnt_know(files):
    page = _page(sessions={"bedroom": FakeSession()}, known=["bedroom", "garage"])
    for ghost in ("ghosst", 5):
        assert page.add_alarm("07:30", None, LOGIN, ghost) == {
            "success": False, "message": f"There's no satellite called {ghost}."}
    assert page.brain.clock.alarms == []
    # One which is merely away is the brain's to set alarms for.
    assert page.add_alarm("07:30", None, LOGIN, "garage")["success"]
    assert page.brain.clock.alarms == [(7, 30, None, "garage")]


def test_a_routine_keeps_its_satellite_when_saved_on_the_page(files):
    page = _page(sessions=_two_satellites(), routines=Routines(path=str(files / "routines.json")))
    routine = {"name": "Lights out", "phrases": "lights out", "steps": "turn off the lights", "satellite": "kitchen"}
    assert page.save_routine(routine, LOGIN)["success"]
    saved = page.state()["routines"][0]
    assert saved["satellite"] == "kitchen"
    # Saved again as the editor sends it - with the satellite it started at.
    assert page.save_routine(dict(saved, name="Lights off"), LOGIN)["success"]
    assert [(r["name"], r["satellite"]) for r in page.state()["routines"]] == [("Lights off", "kitchen")]
    # An empty one is the default's, as Routines keeps it.
    assert page.save_routine(dict(saved, satellite=""), LOGIN)["success"]
    assert page.state()["routines"][0]["satellite"] is None


def test_a_routine_for_a_satellite_not_let_in_is_refused(files):
    page = _page(sessions={"bedroom": FakeSession()}, known=["bedroom", "garage"],
                 routines=Routines(path=str(files / "routines.json")))
    for ghost in ("ghosst", 5):
        assert page.save_routine({"name": "Lights out", "steps": "turn off the lights", "satellite": ghost}, LOGIN) == {
            "success": False, "message": f"There's no satellite called {ghost}."}
    assert page.state()["routines"] == []
    # One which is merely away is the brain's to keep routines for.
    assert page.save_routine({"name": "Lights out", "steps": "turn off the lights", "satellite": "garage"}, LOGIN)["success"]


def test_run_on_the_page_runs_a_routine_on_its_own_satellite(files):
    page = _page(sessions=_two_satellites(), routines=Routines(path=str(files / "routines.json")))
    page.save_routine({"name": "Lights out", "steps": "turn off the lights", "satellite": "kitchen"}, LOGIN)
    routine = page.state()["routines"][0]
    assert page.start_routine(routine["id"], LOGIN)["success"]
    sessions = page.brain.sessions
    deadline = time.monotonic() + 2
    while not sessions["kitchen"].routines_run and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sessions["kitchen"].routines_run == ["Lights out"] and sessions["bedroom"].routines_run == []


def test_a_satellite_which_doesnt_exist_is_handled_everywhere(served):
    ghost = "ghost"
    said = {"success": False, "message": "ghost isn't connected."}

    def post(path, body):
        return json.loads(_ask(served, path, {**body, "satellite": ghost}, X_Nova="page")[2])

    assert post("/api/volume", {"category": "general", "percent": 5}) == said
    assert post("/api/mute", {"muted": True}) == said
    assert post("/api/music", {"action": "pause"}) == said
    assert post("/api/light", {"id": "light.kitchen", "on": True}) == said
    assert post("/api/run", {"id": "scene.relax"}) == said
    assert post("/api/alarms", {"time": "07:30"}) == {"success": False, "message": "There's no satellite called ghost."}
    assert _ask(served, "/api/state?satellite=ghost")[0] == 200
    assert json.loads(_ask(served, "/api/state?satellite=ghost")[2])["volumes"] == {}
    assert _ask(served, "/api/cover?satellite=ghost")[0] == 404
    assert json.loads(_ask(served, "/api/music?satellite=ghost")[2]) == {"available": False,
                                                                       "why": "There's no satellite called ghost."}
    assert json.loads(_ask(served, "/api/home?satellite=ghost")[2]) == {"available": False}
    assert json.loads(_ask(served, "/api/live?satellite=ghost")[2]) == {"activity": "idle", "stoppable": False}
    assert served.brain.clock.alarms == []


def test_a_satellite_is_removed_or_made_the_default_on_the_page(served):
    served.brain.known = ["bedroom", "kitchen", "hall"]

    def post(path, name, **headers):
        return _ask(served, path, {"name": name}, **headers)

    # Only from the page itself, as every change.
    assert post("/api/satellites/remove", "kitchen")[0] == 403
    assert post("/api/satellites/default", "hall")[0] == 403
    assert served.brain.known == ["bedroom", "kitchen", "hall"]

    said = [json.loads(post(path, name, X_Nova="page")[2])
            for path, name in [("/api/satellites/remove", "kitchen"), ("/api/satellites/default", "hall"),
                               ("/api/satellites/remove", "ghost"), ("/api/satellites/default", "ghost")]]
    # The kitchen wasn't connected, so nothing could empty its copy.
    assert said == [{"success": True, "message": "Removed kitchen. It wasn't connected, so it still has a copy of its "
                                                 "alarms for the next week - wipe it or connect it before switching it on."},
                    {"success": True, "message": "hall is the default now."},
                    {"success": False, "message": "There's no satellite called ghost."},
                    {"success": False, "message": "There's no satellite called ghost."}]
    assert served.brain.known == ["hall", "bedroom"]


def test_a_broken_satellites_file_is_said_so_not_taken_for_no_satellite(files, caplog):
    page = _page()

    def broken():
        raise json.JSONDecodeError("Expecting value", "", 0)

    page.brain.satellites = broken
    page.brain.routines = Routines(path=str(files / "routines.json"))
    said = {"success": False, "message": "The satellites' file can't be read, so nothing was changed."}
    assert page.save_routine({"name": "Lights out", "steps": "turn off the lights", "satellite": "bedroom"},
                             LOGIN) == said
    assert page.brain.routines.all() == []
    # The default's needs no satellites file.
    assert page.save_routine({"name": "Lights out", "steps": "turn off the lights"}, LOGIN)["success"]
    assert page.add_satellite("study", LOGIN) == said
    assert page.new_password("bedroom", LOGIN) == said
    assert page.remove_satellite("bedroom", LOGIN) == said
    assert page.make_default("bedroom", LOGIN) == said
    assert "Settings page: couldn't read the satellites' file" in caplog.text


def test_the_page_loads_with_a_broken_satellites_file(served, caplog):
    def broken(*args):
        raise json.JSONDecodeError("Expecting value", "", 0)

    served.brain.satellites = served.brain.default_satellite = broken
    status, _, body = _ask(served, "/api/state")
    state = json.loads(body)
    # Said as broken, not as there being none - and everything else still shown.
    assert status == 200 and state["satellites"] == [] and state["satellites_unreadable"] is True
    assert state["memories"] == ["Your sister is called Amy."]
    assert json.loads(_ask(served, "/api/live")[2]) == {"activity": "idle", "stoppable": False}
    assert _page().state()["satellites_unreadable"] is False


def test_a_broken_satellites_file_is_logged_once_until_its_read_again(files, caplog):
    # The page asks for its state every twenty seconds.
    page = _page()
    known = page.brain.satellites

    def broken():
        raise json.JSONDecodeError("Expecting value", "", 0)

    page.brain.satellites = broken
    for _ in range(3):
        page.state()
    assert caplog.text.count("couldn't read the satellites' file") == 1

    # Readable again, then broken again, it's said again.
    page.brain.satellites = known
    page.state()
    page.brain.satellites = broken
    page.state()
    assert caplog.text.count("couldn't read the satellites' file") == 2


def test_the_page_works_with_no_satellite_added_at_all(files):
    page = _page(sessions={}, known=[])
    state = page.state()
    assert state["satellites"] == [] and state["satellite"] is None and state["volumes"] == {}
    assert page.add_alarm("07:30", None, LOGIN)["success"]
    assert page.brain.clock.alarms == [(7, 30, None, None)]
    assert page.set_volume("general", 5, LOGIN) == {"success": False, "message": "There's no satellite to do that on."}


def test_restarting_asks_the_brain_to_stop_for_a_restart(files):
    asked = []
    page = _page()
    page.brain.restart = lambda: asked.append("restart") or "Restarting."
    page._restart = None
    assert page.restart(LOGIN) == {"success": True, "message": "Restarting."} and asked == ["restart"]


# The batteries

BATTERIES_NOW = 1_800_000_000.0


class FakeBatteryHome:
    """Answers batteries() as Home Assistant does, from what a test sets."""

    def __init__(self):
        self.fails = None
        self.people_now = []
        self.found = [
            # Not in name order, so the page's sorting is what puts the MacBook first.
            {"id": "sensor.s25_battery_level", "name": "s25", "level": 80, "changed": BATTERIES_NOW - 7200,
             "device": "d2", "maker": "samsung", "model": "SM-S938B", "charging": "Charging", "app": True},
            {"id": "sensor.macbook_battery_level", "name": "macbook", "level": 40, "changed": BATTERIES_NOW,
             "device": "d1", "maker": "Apple", "model": "Mac16,8", "charging": "Not Charging", "app": True},
        ]

    def batteries(self):
        if self.fails:
            raise self.fails
        return list(self.found)

    def people(self):
        return self.people_now


def _watched(tmp_path):
    """A page over a real battery watcher, which a test can find fault in Home Assistant with."""
    home = FakeBatteryHome()
    choices = BatteryChoices(str(tmp_path / "batteries.json"), mobile_marks=[5], other_marks=[15, 5])
    watcher = BatteryWatcher(home, choices, lambda message: True, lambda message, title: False, lambda: False,
                             check_every=60, stale_after=3600)
    return _page(batteries=watcher), home, choices


def _device(page, battery_id):
    return next(d for d in page.batteries_state()["devices"] if d["id"] == battery_id)


MAC = "sensor.macbook_battery_level"
PHONE = "sensor.s25_battery_level"


def test_the_batteries_are_listed_with_their_choices(files, tmp_path, monkeypatch):
    page, home, choices = _watched(tmp_path)
    monkeypatch.setattr(page_module.time, "time", lambda: BATTERIES_NOW)
    state = page.batteries_state()
    assert state["available"] is True and state["also_send_when_said"] is True
    # In name order, each with its choices and the age of a reading which has gone stale.
    assert [d["id"] for d in state["devices"]] == [MAC, PHONE]
    mac, phone = state["devices"]
    assert mac == {"id": MAC, "name": "MacBook", "level": 40, "charging": False, "stale": None, "kind": "other",
                   "enabled": True, "marks": [15, 5], "default_marks": [15, 5], "send_to_phone": True,
                   "changed": False}
    assert phone["kind"] == "mobile" and phone["charging"] is True and phone["stale"] == "2 hours"
    assert phone["marks"] == [5] and phone["send_to_phone"] is False
    choices.set(page.brain.batteries.read()[1], marks=[30, 10])
    assert _device(page, MAC)["marks"] == [30, 10] and _device(page, MAC)["default_marks"] == [15, 5]
    assert _device(page, MAC)["changed"] is True


def test_each_device_shows_who_decides_home_only_with_more_than_one_person(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    # One person (or none) decides everything, so there is nothing to show.
    assert all("person" not in d for d in page.batteries_state()["devices"])
    home.people_now = [{"id": "person.a", "name": "Amy", "state": "home", "devices": ["d2"]}]
    assert all("person" not in d for d in page.batteries_state()["devices"])
    # With two, the one whose trackers include the device, else the first - as the watcher chooses.
    home.people_now = [{"id": "person.a", "name": "Amy", "state": "home", "devices": ["d2"]},
                       {"id": "person.b", "name": "Ben", "state": "not_home", "devices": ["d1"]}]
    assert _device(page, MAC)["person"] == "Ben" and _device(page, PHONE)["person"] == "Amy"
    home.found[0]["device"] = None
    assert _device(page, PHONE)["person"] == "Amy"


def test_a_batterys_warnings_can_be_switched_off_and_it_works_straight_away(files, tmp_path, caplog):
    page, home, choices = _watched(tmp_path)
    batteries = page.brain.batteries.read()
    home.found[1]["level"] = 10
    with caplog.at_level(logging.INFO):
        result = page.set_battery(MAC, LOGIN, enabled=False)
    assert result["success"] and "restart" not in result["message"].lower()
    assert LOGIN in caplog.text and MAC in caplog.text
    assert not _device(page, MAC)["enabled"]
    assert choices.due(page.brain.batteries.read()) == []
    page.set_battery(MAC, LOGIN, enabled=True)
    assert [w.mark for w in choices.due(page.brain.batteries.read())] == [15]
    # Marks and the phone switch work the same way, and the defaults come back.
    assert page.set_battery(PHONE, LOGIN, marks=[50], send_to_phone=True)["success"]
    assert _device(page, PHONE)["marks"] == [50] and _device(page, PHONE)["send_to_phone"] is True
    assert page.set_battery(PHONE, LOGIN, reset=True)["success"]
    assert _device(page, PHONE)["marks"] == [5] and _device(page, PHONE)["send_to_phone"] is False
    assert len(batteries) == 2


@pytest.mark.parametrize("marks, said", [
    ([150, 5], "whole number"), ([5, 5], "must be different"), ([5, 10, 15], "needs two marks"),
    ([1.5, 10], "whole number"), ([0, 10], "whole number")])
def test_marks_which_dont_make_sense_are_refused(files, tmp_path, marks, said):
    page, home, choices = _watched(tmp_path)
    result = page.set_battery(MAC, LOGIN, marks=marks)
    assert not result["success"] and said in result["message"]
    assert _device(page, MAC)["marks"] == [15, 5]
    # One mark is too few for the Mac, and two too many for the phone.
    assert "needs two marks" in page.set_battery(MAC, LOGIN, marks=[5])["message"]
    assert "needs one mark" in page.set_battery(PHONE, LOGIN, marks=[5, 10])["message"]


def test_a_battery_change_which_cant_be_understood_or_saved_is_refused(files, tmp_path, monkeypatch):
    page, home, choices = _watched(tmp_path)
    # In Python bool("false") is True, so nothing is coerced.
    for bad in ({"enabled": "false"}, {"send_to_phone": 0}, {"marks": "15"}):
        assert page.set_battery(MAC, LOGIN, **bad) == {"success": False, "message": "That didn't make sense."}
    # A True in the marks is refused by the choices, with their own sentence.
    assert not page.set_battery(MAC, LOGIN, marks=[True, 5])["success"]
    assert page.set_battery_also_send("no", LOGIN)["success"] is False
    assert choices.also_send_when_said is True

    def full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(choices._store, "change", full)
    assert page.set_battery(MAC, LOGIN, enabled=False) == {"success": False, "message": "Couldn't save that."}
    assert page.set_battery(MAC, LOGIN, reset=True) == {"success": False, "message": "Couldn't save that."}
    assert page.set_battery_also_send(False, LOGIN) == {"success": False, "message": "Couldn't save that."}


def test_a_battery_home_assistant_doesnt_show_just_now_is_refused(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    result = page.set_battery("sensor.gone_battery_level", LOGIN, enabled=False)
    assert not result["success"] and "sensor.gone_battery_level" not in choices._state["devices"]
    import requests
    home.fails = requests.ConnectionError("down")
    assert not page.set_battery(MAC, LOGIN, enabled=False)["success"]
    assert MAC not in choices._state["devices"]
    assert page.brain.batteries is not None and _page().batteries_state()["available"] is False


def test_also_send_when_said_can_be_switched_off(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    assert page.set_battery_also_send(False, LOGIN)["success"]
    assert choices.also_send_when_said is False and page.batteries_state()["also_send_when_said"] is False
    assert page.set_battery_also_send(True, LOGIN)["success"] and choices.also_send_when_said


def test_the_section_says_when_home_assistant_cant_be_reached(files, tmp_path):
    import requests
    page, home, choices = _watched(tmp_path)
    home.fails = requests.ConnectionError("down")
    state = page.batteries_state()
    assert state["available"] is False and "Home Assistant" in state["message"]
    # Nor does it with nothing to reach it with.
    assert _page().batteries_state()["available"] is False
    home.fails = ValueError("not JSON")
    assert page.batteries_state()["available"] is False


def test_a_battery_change_needs_the_pages_own_header(files, tmp_path):
    page, home, choices = _watched(tmp_path)
    page.start()
    try:
        body = {"id": MAC, "enabled": False}
        assert _ask(page, "/api/batteries", body)[0] == 403
        assert _ask(page, "/api/batteries/also", {"on": False})[0] == 403
        assert _ask(page, "/api/batteries", body, X_Nova="page", Sec_Fetch_Site="cross-site")[0] == 403
        assert choices.enabled(MAC) and choices.also_send_when_said
        assert json.loads(_ask(page, "/api/batteries")[2])["devices"][0]["id"] == MAC
        assert json.loads(_ask(page, "/api/batteries", body, X_Nova="page")[2])["success"]
        assert json.loads(_ask(page, "/api/batteries/also", {"on": False}, X_Nova="page")[2])["success"]
        assert not choices.enabled(MAC) and not choices.also_send_when_said
        # No server error for nonsense.
        assert json.loads(_ask(page, "/api/batteries", {"enabled": False}, X_Nova="page")[2])["success"] is False
    finally:
        page.stop()


# The lists, sending, the weather and the PCs

class FakeChoicesHome:
    """Stands in for Home Assistant's lists, weather, devices, PCs and Wake-on-LAN buttons. The ids are made up."""

    def __init__(self):
        self.fails = None
        self.pressed = []
        self.lists = [
            {"id": "todo.shopping_list", "name": "Shopping list", "count": 3, "aliases": ["groceries"]},
            {"id": "todo.todo", "name": "todo", "count": 0, "aliases": []}]
        self.weather = [{"id": "weather.forecast_home", "name": "Forecast Home", "state": "sunny", "attributes": {},
                         "reported": None},
                        {"id": "weather.cabin", "name": "Cabin", "state": "sunny", "attributes": {}, "reported": None}]
        self.devices = [
            {"device": "d1", "name": "s25-ultra", "maker": "samsung", "model": "SM-S938B", "notify": "mobile_app_s25",
             "tracker": None, "place": None},
            {"device": "d2", "name": "macbook", "maker": "Apple", "model": "Mac16,8", "notify": "mobile_app_macbook",
             "tracker": None, "place": None},
            {"device": "d3", "name": "old-tablet", "maker": "Amazon", "model": "Fire", "notify": None,
             "tracker": None, "place": None}]
        entries = [Entry("button.desk_off", "mqtt", "desk", None, "desk_shutdown", "Shutdown", (), False),
                   Entry("sensor.desk_cpu", "mqtt", "desk", None, "desk_cpu", "CPU Load", (), False),
                   Entry("button.wake_desk_pc", "wake_on_lan", None, None, "wake_desk_pc", None, (), False),
                   Entry("button.wake_nas", "wake_on_lan", None, None, "wake_nas", None, (), False)]
        self.snapshot = Snapshot(entries={entry.entity_id: entry for entry in entries},
                                 devices={"desk": {"name": "Desk PC", "maker": "HASS.Agent Team"}}, areas={},
                                 read_at=0.0)
        self.names = {"button.wake_desk_pc": "Wake Desk PC", "button.wake_nas": "Wake NAS"}

    def _fail(self):
        if self.fails:
            raise self.fails

    def todo_lists(self):
        self._fail()
        return [dict(found) for found in self.lists]

    def weather_entities(self):
        self._fail()
        return [dict(found) for found in self.weather]

    def app_devices(self):
        self._fail()
        return [dict(found) for found in self.devices]

    def _snapshot(self):
        return self.snapshot

    def _states(self):
        self._fail()
        # The PC is off, so every one of HASS.Agent's entities on it is unavailable.
        return [{"entity_id": entity, "state": "unavailable" if entry.platform == "mqtt" else "unknown",
                 "attributes": {"friendly_name": self.names[entity]} if entity in self.names else {}}
                for entity, entry in self.snapshot.entries.items()]

    def press(self, entity_id):
        self.pressed.append(entity_id)


def _home_page(files, phone="", **home_changes):
    """A page over Nova with Home Assistant set up, and its real lists, weather and PC choices."""
    home = FakeChoicesHome()
    for name, value in home_changes.items():
        setattr(home, name, value)

    def store(name, defaults):
        return ChoiceStore(str(files / name), defaults)

    page = _page(home=home, ntfy=Ntfy("http://ntfy.local", phone=phone),
                 lists=Lists(home, store("lists.json", {"main": None})),
                 weather=Weather(home, store("weather.json", {"entity": None})),
                 pc=PCPower(home, store("pcs.json", {"wake": {}})))
    return page, home


def test_sending_says_how_each_device_is_reached(files):
    page, home = _home_page(files, phone="my-phone")
    # The phone through ntfy, a Mac through Home Assistant - and a tablet with no notify service
    # isn't one Nova can send to.
    assert page.home_choices_state()["sending"] == [
        {"name": "MacBook", "how": "Through Home Assistant"},
        {"name": "S25 Ultra", "how": "Through ntfy, to the topic under Notifications"}]
    page, home = _home_page(files)
    assert page.home_choices_state()["sending"][1] == {
        "name": "S25 Ultra", "how": "Through ntfy, once a topic for your phone is set under Notifications"}


def test_the_lists_weather_and_pcs_are_shown_and_chosen_from_the_page(files, monkeypatch, caplog):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page, home = _home_page(files)
    page.start()
    try:
        def change(path, body):
            return json.loads(_ask(page, path, body, X_Nova="page")[2])

        def shown():
            status, headers, body = _ask(page, "/api/home-choices")
            assert status == 200 and headers["Cache-Control"] == "no-store"
            return json.loads(body)

        caplog.set_level(logging.DEBUG)
        state = shown()
        assert state["lists"] == [
            {"id": "todo.shopping_list", "name": "Shopping list", "count": 3, "aliases": ["groceries"], "main": False},
            {"id": "todo.todo", "name": "todo", "count": 0, "aliases": [], "main": True}]
        # The first by entity id is the one used until another is chosen.
        assert [(entity["id"], entity["chosen"]) for entity in state["weather"]] == [
            ("weather.forecast_home", False), ("weather.cabin", True)]
        assert state["pcs"] == {"pcs": [{"device": "desk", "name": "Desk PC", "wake": None}],
                                "buttons": [{"id": "button.wake_desk_pc", "name": "Wake Desk PC"},
                                            {"id": "button.wake_nas", "name": "Wake NAS"}]}

        saved = {"success": True, "message": "Saved. It's used straight away."}
        assert change("/api/lists/main", {"id": "todo.shopping_list"}) == saved
        assert change("/api/weather", {"id": "weather.forecast_home"}) == saved
        assert change("/api/pcs/wake", {"device": "desk", "button": "button.wake_desk_pc"}) == saved
        state = shown()
        assert [found["main"] for found in state["lists"]] == [True, False]
        assert [entity["chosen"] for entity in state["weather"]] == [True, False]
        assert state["pcs"]["pcs"][0]["wake"] == "button.wake_desk_pc"
        # Used straight away.
        assert page.brain.pc.act("on")["message"] == "Turning your PC on."
        assert home.pressed == ["button.wake_desk_pc"]
        assert _ask(page, "/api/lists/main", {"id": "todo.todo"})[0] == 403

        # Only what Home Assistant has just now, never what the page says.
        gone = "Home Assistant isn't showing that"
        assert change("/api/lists/main", {"id": "todo.made_up"})["message"].startswith(gone)
        assert change("/api/weather", {"id": "light.kitchen"})["message"].startswith(gone)
        assert change("/api/pcs/wake", {"device": "desk", "button": "button.desk_off"})["message"].startswith(gone)
        assert change("/api/pcs/wake", {"device": "nobody", "button": "button.wake_nas"})["message"].startswith(gone)
        for path, body in (("/api/lists/main", {"id": 7}), ("/api/weather", {"id": ["weather.cabin"]}),
                           ("/api/pcs/wake", {"device": "desk", "button": None})):
            assert change(path, body) == {"success": False, "message": "That didn't make sense."}
        assert page.brain.lists.choices.get("main") == "todo.shopping_list"

        # A hardware address never reaches the journal: nothing is logged of an entity id.
        kept = " ".join(record.getMessage() for record in caplog.records if record.levelno >= logging.INFO)
        assert "chose which button wakes a PC" in kept
        for private in ("button.", "wake_desk_pc", "todo.", "weather."):
            assert private not in kept, private
    finally:
        page.stop()


def test_a_wake_button_chosen_for_another_pc_is_moved_and_the_page_says_so(files):
    page, home = _home_page(files)
    study = [Entry("button.study_off", "mqtt", "study", None, "study_shutdown", "Shutdown", (), False),
             Entry("sensor.study_cpu", "mqtt", "study", None, "study_cpu", "CPU Load", (), False)]
    home.snapshot = Snapshot(entries={**home.snapshot.entries, **{entry.entity_id: entry for entry in study}},
                             devices={**home.snapshot.devices, "study": {"name": "Study PC", "maker": "HASS.Agent Team"}},
                             areas={}, read_at=0.0)
    # Chosen the wrong way round, each can still be put right.
    assert page.choose_wake("desk", "button.wake_nas", LOGIN)["success"] is True
    assert page.choose_wake("study", "button.wake_desk_pc", LOGIN)["success"] is True
    assert page.choose_wake("desk", "button.wake_desk_pc", LOGIN) == {
        "success": True, "message": "Saved - Study PC no longer has a Wake-on-LAN button."}
    assert page.choose_wake("study", "button.wake_nas", LOGIN) == {
        "success": True, "message": "Saved. It's used straight away."}
    assert [pc["wake"] for pc in page.home_choices_state()["pcs"]["pcs"]] == ["button.wake_desk_pc", "button.wake_nas"]


def test_each_home_choice_needs_only_its_own_part(files):
    # Built together from Home Assistant on the brain - but each is checked by itself, so
    # one missing never raises in another's place.
    page, home = _home_page(files)
    page.brain.lists = None
    assert page.choose_main_list("todo.todo", LOGIN) == {"success": False,
                                                        "message": "Nova isn't connected to Home Assistant."}
    assert page.choose_weather("weather.cabin", LOGIN)["success"] is True
    assert page.choose_wake("desk", "button.wake_nas", LOGIN)["success"] is True
    state = page.home_choices_state()
    assert state["available"] and state["lists"] == [] and state["weather"] is not None

    page, home = _home_page(files)
    page.brain.weather = page.brain.pc = None
    assert page.choose_weather("weather.cabin", LOGIN)["success"] is False
    assert page.choose_wake("desk", "button.wake_nas", LOGIN)["success"] is False
    state = page.home_choices_state()
    assert state["available"] and state["lists"] and state["weather"] is None and state["pcs"] is None


def test_a_choice_which_cant_be_saved_or_read_says_so(files, monkeypatch):
    page, home = _home_page(files)

    def full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(page.brain.lists.choices, "change", full)
    assert page.choose_main_list("todo.todo", LOGIN) == {"success": False, "message": "Couldn't save that."}
    home.fails = requests.ConnectionError("down")
    gone = {"success": False, "message": "Nova can't reach Home Assistant just now."}
    assert page.choose_main_list("todo.todo", LOGIN) == gone
    assert page.choose_weather("weather.cabin", LOGIN) == gone
    assert page.choose_wake("desk", "button.wake_nas", LOGIN) == gone


def test_home_choices_without_home_assistant_say_so(files):
    page = _page()
    said = "Nova isn't connected to Home Assistant. Add its address in Settings, and its token in Keys."
    assert page.home_choices_state() == {"available": False, "message": said}
    assert page.choose_main_list("todo.todo", LOGIN) == {"success": False, "message": "Nova isn't connected to Home Assistant."}
    assert page.choose_weather("weather.cabin", LOGIN)["success"] is False
    assert page.choose_wake("desk", "button.wake_nas", LOGIN)["success"] is False

    # Unreachable is said as the batteries say it.
    page, home = _home_page(files)
    home.fails = requests.ConnectionError("down")
    assert page.home_choices_state() == {"available": False, "message": "Nova can't reach Home Assistant just now."}


def test_the_weather_and_pcs_are_left_out_without_a_choice(files):
    lonely = FakeChoicesHome().snapshot
    page, home = _home_page(files, weather=[], snapshot=Snapshot(
        entries={entity: entry for entity, entry in lonely.entries.items() if entity != "button.wake_nas"},
        devices=lonely.devices, areas={}, read_at=0.0))
    state = page.home_choices_state()
    # One entity, or none, is no choice - and neither is one PC with one button, or none.
    assert state["weather"] is None and state["pcs"] is None
    home.weather = [FakeChoicesHome().weather[0]]
    assert page.home_choices_state()["weather"] is None
    # Two buttons for the one PC are a choice.
    home.snapshot = FakeChoicesHome().snapshot
    assert page.home_choices_state()["pcs"] is not None


# Through Nginx Proxy Manager

PASSWORD = "correct horse battery"


def _free_port():
    """A port nothing's listening on - as 0 switches the NPM listener off."""
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()[1]


def _npm_page(files, proxy_from=("127.0.0.1",), password=None):
    """A page with its NPM listener on, the test counting as NPM, and a password if given."""
    page = SettingsPage([LOGIN], port=0, brain=FakeBrain({"bedroom": FakeSession()}),
                        restart=lambda: "Restarting.", started=1_800_000_000,
                        proxy_port=_free_port(), proxy_from=list(proxy_from),
                        password=PagePassword(str(files / "page_password")),
                        logins=Logins(str(files / "page_logins.json")))
    if password:
        assert page.password.set(password, page.logins) is None
    return page


@pytest.fixture
def npm(files, monkeypatch):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _npm_page(files, password=PASSWORD)
    page.start()
    yield page
    page.stop()


def _through_npm(page, path, body=None, cookie=None, **headers):
    """Asks the page for something as NPM would pass a browser's request on."""
    request = urllib.request.Request(f"http://127.0.0.1:{page.proxy_port}{path}",
                                     data=None if body is None else json.dumps(body).encode())
    if cookie:
        request.add_header("Cookie", f"nova_login={cookie}")
    for name, value in headers.items():
        request.add_header(name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _log_in(page, password=PASSWORD, **headers):
    """Logs in through NPM, returning what was said and the login's token, or None."""
    status, headers, body = _through_npm(page, "/api/login", {"password": password}, X_Nova="page", **headers)
    assert status == 200
    cookie = headers.get("Set-Cookie")
    token = cookie.split(";")[0].split("=", 1)[1] if cookie else None
    return json.loads(body), token


def test_only_npms_address_is_answered(files, monkeypatch, caplog):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _npm_page(files, proxy_from=["192.0.2.1"], password=PASSWORD)
    token = page.logins.make()
    page.start()
    try:
        with caplog.at_level(logging.WARNING):
            for path in ("/", "/settings.js", "/api/login-state", "/api/state"):
                status, _, body = _through_npm(page, path, cookie=token)
                assert (status, body) == (403, b"")
            for path in ("/api/login", "/api/volume", "/api/logout-everywhere"):
                assert _through_npm(page, path, {"password": PASSWORD}, cookie=token, X_Nova="page")[0] == 403
            # Whatever the method.
            connection = http.client.HTTPConnection("127.0.0.1", page.proxy_port, timeout=2)
            connection.request("HEAD", "/")
            assert connection.getresponse().status == 403
            connection.close()
        assert "isn't NPM's" in caplog.text and page.logins.check(token)
    finally:
        page.stop()


def test_npms_listener_needs_npms_address(files, caplog):
    page = _npm_page(files, proxy_from=[])
    with caplog.at_level(logging.WARNING):
        page.start()
    try:
        assert page._proxy_server is None and "settings_page.proxy_from" in caplog.text
        assert page._server.route == "tailnet"
    finally:
        page.stop()


def test_the_npm_settings_come_from_config(make_config):
    page = SettingsPage.from_config(make_config(settings_page={
        "port": 8765, "allow": [LOGIN], "proxy_port": 8766, "proxy_from": ["192.0.2.1"]}), FakeBrain({}))
    assert (page.proxy_port, page.proxy_from) == (8766, ["192.0.2.1"])
    page = SettingsPage.from_config(make_config(settings_page={"port": 8765, "allow": [LOGIN]}), FakeBrain({}))
    assert (page.proxy_port, page.proxy_from) == (0, [])


def test_a_tailscale_header_through_npm_lets_nobody_in(npm):
    assert npm._proxy_server.route == "npm"
    assert _through_npm(npm, "/api/state", Tailscale_User_Login=LOGIN)[0] == 401
    assert _through_npm(npm, "/api/volume", {"category": "music", "percent": 20},
                        Tailscale_User_Login=LOGIN, X_Nova="page")[0] == 401
    assert _bedroom(npm).volume.levels["music"] == 40


def test_with_no_password_npm_lets_nobody_in(files, monkeypatch):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _npm_page(files)
    page.start()
    try:
        status, _, body = _through_npm(page, "/api/login-state")
        assert (status, json.loads(body)) == (200, {"route": "npm", "password_set": False, "logged_in": False})
        for typed in ("", PASSWORD, "anything at all"):
            said, token = _log_in(page, typed)
            assert said == {"success": False,
                            "message": "Set a page password at the page's Tailscale address first."}
            assert token is None
        status, _, body = _through_npm(page, "/api/state")
        assert (status, json.loads(body)) == (401, {"success": False, "message": "Log in first."})
    finally:
        page.stop()


def test_the_right_password_logs_in_for_thirty_days(npm, caplog):
    caplog.set_level(logging.DEBUG)
    said, _ = _log_in(npm, "not the password")
    assert said == {"success": False, "message": "That's not the password."}

    status, headers, body = _through_npm(npm, "/api/login", {"password": PASSWORD}, X_Nova="page")
    cookie = headers["Set-Cookie"]
    token = cookie.split(";")[0].split("=", 1)[1]
    parts = [part.strip() for part in cookie.split(";")]
    assert parts[0] == f"nova_login={token}" and len(token) >= 40
    assert {"HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=2592000"} <= set(parts)
    assert json.loads(body)["success"]
    # Never the token or the password in the log.
    assert token not in caplog.text and PASSWORD not in caplog.text

    assert _through_npm(npm, "/api/state", cookie=token)[0] == 200
    status, _, body = _through_npm(npm, "/api/login-state", cookie=token)
    assert json.loads(body) == {"route": "npm", "password_set": True, "logged_in": True}
    status, _, body = _through_npm(npm, "/api/volume", {"category": "music", "percent": 20},
                                   cookie=token, X_Nova="page")
    assert status == 200 and json.loads(body)["success"] and _bedroom(npm).volume.levels["music"] == 20
    assert "Settings page: password " in caplog.text
    assert "Settings page: password logged in" in caplog.text
    assert token not in caplog.text and PASSWORD not in caplog.text


def test_the_page_files_are_served_before_a_login(npm):
    for path in ("/", "/settings.js", "/settings.css", "/icon.svg"):
        status, headers, _ = _through_npm(npm, path)
        assert status == 200 and headers["Content-Type"] == page_module.FILES[path][1]
        assert headers["Content-Security-Policy"] == page_module.SAFETY_HEADERS["Content-Security-Policy"]
    for path in ("/api/music", "/api/state", "/api/live", "/nothing"):
        assert _through_npm(npm, path)[0] == 401
    # A cookie which isn't one, or isn't a login, is no login.
    assert _through_npm(npm, "/api/state", cookie="made-up")[0] == 401
    assert _through_npm(npm, "/api/state", Cookie='nova_login="unclosed')[0] == 401


def test_a_login_post_must_come_from_the_page(npm):
    for headers in ({}, {"X_Nova": "page", "Sec_Fetch_Site": "cross-site"}):
        status, sent, _ = _through_npm(npm, "/api/login", {"password": PASSWORD}, **headers)
        assert status == 403 and sent.get("Set-Cookie") is None


def test_an_overlong_password_or_cookie_is_never_checked(npm, monkeypatch):
    checked = []
    real_password_check, real_login_check = npm.password.check, npm.logins.check
    monkeypatch.setattr(npm.password, "check", lambda typed: checked.append("password") or real_password_check(typed))
    # None, for no cookie, isn't a check.
    monkeypatch.setattr(npm.logins, "check",
                        lambda token: (token is not None and checked.append("login")) or real_login_check(token))
    said, token = _log_in(npm, "x" * 1025)
    assert not said["success"] and token is None
    assert _through_npm(npm, "/api/state", cookie="x" * 257)[0] == 401
    assert checked == []
    # A body bigger than any change is refused unread.
    status, _, body = _through_npm(npm, "/api/login", {"password": "x" * (page_module.MOST_BODY + 1)}, X_Nova="page")
    assert (status, json.loads(body)) == (400, {"success": False, "message": "That didn't make sense."})
    assert checked == []


def test_a_login_which_cant_be_saved_is_answered(npm, monkeypatch):
    def full():
        raise OSError("No space left on device")

    monkeypatch.setattr(npm.logins, "make", full)
    assert _log_in(npm) == ({"success": False, "message": "Couldn't save that."}, None)


def test_five_wrong_passwords_make_that_device_wait(npm):
    for _ in range(5):
        assert _log_in(npm, "not the password", X_Real_IP="192.0.2.7")[0]["message"] == "That's not the password."
    wait = {"success": False, "message": "Too many wrong passwords - try again in a minute."}
    assert _log_in(npm, PASSWORD, X_Real_IP="192.0.2.7") == (wait, None)
    said, token = _log_in(npm, PASSWORD, X_Real_IP="192.0.2.8")
    assert said["success"] and token


def test_log_out_ends_only_this_login(npm):
    _, this = _log_in(npm)
    _, other = _log_in(npm)
    status, headers, body = _through_npm(npm, "/api/logout", {}, cookie=this, X_Nova="page")
    assert status == 200 and json.loads(body)["success"]
    assert "nova_login=;" in headers["Set-Cookie"] and "Max-Age=0" in headers["Set-Cookie"]
    assert _through_npm(npm, "/api/state", cookie=this)[0] == 401
    assert _through_npm(npm, "/api/state", cookie=other)[0] == 200
    # One already ended is still cleared from the browser.
    status, headers, body = _through_npm(npm, "/api/logout", {}, cookie=this, X_Nova="page")
    assert status == 200 and json.loads(body)["success"] and "Max-Age=0" in headers["Set-Cookie"]
    assert _through_npm(npm, "/api/logout", {}, cookie=this)[0] == 403
    # On the tailnet there's no login to end.
    status, _, body = _ask(npm, "/api/logout", {}, X_Nova="page")
    assert status == 200 and json.loads(body)["success"]


def test_a_new_password_logs_everyone_out(npm):
    _, this = _log_in(npm)
    _, other = _log_in(npm)
    new = "a brand new page password"

    # With the wrong current password, nothing changes.
    status, headers, body = _through_npm(npm, "/api/page-password", {"password": new, "current": "wrong guess"},
                                         cookie=this, X_Nova="page")
    assert json.loads(body) == {"success": False, "message": "That's not the current password."}
    assert headers.get("Set-Cookie") is None
    status, _, body = _through_npm(npm, "/api/page-password", {"password": new}, cookie=this, X_Nova="page")
    assert not json.loads(body)["success"]
    assert npm.password.check(PASSWORD) and not npm.password.check(new)
    assert _through_npm(npm, "/api/state", cookie=other)[0] == 200

    # Too short is said, and changes nothing either.
    status, _, body = _through_npm(npm, "/api/page-password", {"password": "short", "current": PASSWORD},
                                   cookie=this, X_Nova="page")
    assert json.loads(body) == {"success": False, "message": "The page password needs at least 12 characters."}
    assert npm.password.check(PASSWORD)

    status, headers, body = _through_npm(npm, "/api/page-password", {"password": new, "current": PASSWORD},
                                         cookie=this, X_Nova="page")
    assert json.loads(body)["success"] and npm.password.check(new)
    fresh = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    assert fresh and fresh != this
    assert _through_npm(npm, "/api/state", cookie=other)[0] == 401
    assert _through_npm(npm, "/api/state", cookie=this)[0] == 401
    assert _through_npm(npm, "/api/state", cookie=fresh)[0] == 200

    # From the tailnet it's set without the current one, and nobody stays logged in.
    status, headers, body = _ask(npm, "/api/page-password", {"password": PASSWORD}, X_Nova="page")
    assert json.loads(body)["success"] and npm.password.check(PASSWORD) and headers.get("Set-Cookie") is None
    assert _through_npm(npm, "/api/state", cookie=fresh)[0] == 401


def test_log_out_everywhere_ends_every_login(npm):
    _, first = _log_in(npm)
    _, second = _log_in(npm)
    status, _, body = _ask(npm, "/api/logout-everywhere", {}, X_Nova="page")
    assert status == 200 and json.loads(body)["success"]
    assert _through_npm(npm, "/api/state", cookie=first)[0] == 401
    assert _through_npm(npm, "/api/state", cookie=second)[0] == 401


def test_the_tailnet_route_is_unchanged(npm):
    assert _ask(npm, "/api/state")[0] == 200
    status, _, body = _ask(npm, "/api/login-state")
    assert json.loads(body) == {"route": "tailnet", "password_set": True, "logged_in": True}
    # Still only for the logins allowed, cookie or not.
    _, token = _log_in(npm)
    assert _ask(npm, "/api/state", login=None, Cookie=f"nova_login={token}")[0] == 403
    # And never slowed by wrong passwords through NPM.
    for _ in range(6):
        _log_in(npm, "not the password")
    assert _ask(npm, "/api/state")[0] == 200


def test_a_cookie_which_cant_be_read_doesnt_hide_the_login(npm):
    # Another service behind NPM may set a cookie on the whole domain which isn't one
    # Python's parser reads.
    _, token = _log_in(npm)
    assert _through_npm(npm, "/api/state", Cookie=f'a="x y" z; nova_login={token}')[0] == 200
    assert _through_npm(npm, "/api/state", Cookie=f"junk; other=1;nova_login={token}; more=2")[0] == 200
    assert _through_npm(npm, "/api/state", Cookie="nova_login=" + "x" * 257)[0] == 401


def _raw(port, request):
    """Sends bytes as they are, returning the whole answer."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
    return answer


def test_anyone_but_npm_gets_a_bare_403_even_for_nonsense(files, monkeypatch, caplog):
    monkeypatch.setattr(page_module, "STOP_CHECK", 0.02)
    page = _npm_page(files, proxy_from=["192.0.2.1"])
    page.start()
    try:
        with caplog.at_level(logging.WARNING):
            for request in (b"NONSENSE\r\n\r\n", b"GET /" + b"x" * 70000 + b" HTTP/1.1\r\n\r\n",
                            b"POST /api/login HTTP/1.1\r\nExpect: 100-continue\r\nContent-Length: 5\r\n\r\n"):
                answer = _raw(page.proxy_port, request)
                head, _, body = answer.partition(b"\r\n\r\n")
                assert head.startswith(b"HTTP/1.1 403") and body == b"" and b"100 Continue" not in answer
                for name, value in page_module.SAFETY_HEADERS.items():
                    assert f"{name}: {value}".encode() in head
        assert caplog.text.count("isn't NPM's") == 3
    finally:
        page.stop()


def test_an_address_which_cant_be_split_is_answered(npm):
    # As a whole address, which the server doesn't shorten to a path as it does "//[x".
    assert _raw(npm.proxy_port, b"GET http://[x HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.0 401")
    _, token = _log_in(npm)
    answer = _raw(npm.proxy_port, f"GET http://[x HTTP/1.1\r\nCookie: nova_login={token}\r\n\r\n".encode())
    assert answer.startswith(b"HTTP/1.0 400") and b"That didn't make sense." in answer
    answer = _raw(npm.port, f"GET http://[x HTTP/1.1\r\nTailscale-User-Login: {LOGIN}\r\n\r\n".encode())
    assert answer.startswith(b"HTTP/1.0 400")
    assert _raw(npm.port, b"GET http://[x HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.0 403")


def test_a_login_with_the_old_password_never_outlives_a_change(npm, monkeypatch):
    # The login's check is held up until the change has finished - or, if the change
    # has to wait for the login, until it's plainly waiting.
    checked, go = threading.Event(), threading.Event()
    real_check = npm.password.check

    def slow_check(typed):
        right = real_check(typed)
        checked.set()
        assert go.wait(5)
        return right

    monkeypatch.setattr(npm.password, "check", slow_check)
    logged_in = []
    login = threading.Thread(target=lambda: logged_in.append(npm.log_in(PASSWORD, "192.0.2.7")))
    login.start()
    assert checked.wait(5)
    change = threading.Thread(target=npm.set_page_password, args=("a brand new page password", LOGIN))
    change.start()
    change.join(1)
    go.set()
    login.join(5)
    change.join(5)
    assert not login.is_alive() and not change.is_alive()
    said, token = logged_in[0]
    assert npm.password.check("a brand new page password")
    assert token is None or not npm.logins.check(token)
