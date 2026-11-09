"""Tests for the display server - the clock display's page, and its API behind each clock's key."""

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import requests

from voice_assistant import display_server as server_module
from voice_assistant.display_feed import DisplayFeed
from voice_assistant.display_server import DisplayServer
from voice_assistant.displays import Displays
from voice_assistant.page_password import TRIES_EACH


class FakeVolume:
    CATEGORIES = ("general", "alarm", "music")

    def __init__(self):
        self.levels = {"general": 60, "alarm": 80, "music": 40}
        self.step = 10.0

    def percent(self, category="general"):
        return self.levels[category]

    def get(self, category="general"):
        return self.levels[category] / 100

    def set(self, category, percent):
        self.levels[category] = int(percent)
        return {"success": True, "message": f"{category} volume set to {percent}%"}


class FakePlayer:
    """Stands in for Navidrome on a satellite - playing Gooba, unless a test says otherwise."""

    def __init__(self, volume, playing=True, cover="al-1"):
        self.name = "Navidrome"
        self.needs_internet = False
        self.volume = volume
        self.song = {"track": "Gooba", "artist": "6ix9ine", "album": "Gooba", "playing": playing,
                     "position": 12.0, "duration": 125, "cover": cover}
        self.covers = 0

    def now_playing(self):
        return self.song

    def cover(self):
        self.covers += 1
        return b"a cover", "image/jpeg"

    def get_volume(self):
        return self.volume.percent("music")

    def set_volume(self, percent):
        return self.volume.set("music", percent)


class FakeSession:
    """Stands in for a connected satellite's session - what a hold and the volume buttons reach."""

    def __init__(self, playing=True):
        self.volume = FakeVolume()
        self.music_player = FakePlayer(self.volume, playing=playing)
        self.listens = 0
        self.stops = 0
        self.will_listen = True
        self.boops = []

    def listen_now(self):
        self.listens += 1
        return self.will_listen

    def stop_held(self):
        self.stops += 1

    def boop(self, level):
        self.boops.append(level)


class FakeHome:
    """Home Assistant with the Office TV playing in Jellyfin."""

    def __init__(self):
        self.players = [{"entity_id": "media_player.office_tv", "state": "playing",
                         "picture": "/api/media_player_proxy/media_player.office_tv?token=abc"},
                        {"entity_id": "media_player.kitchen", "state": "playing", "picture": None}]
        self.fetched = []
        self.fails = None
        self.kind = "image/png"

    def jellyfin_media(self):
        return list(self.players)

    def picture(self, path):
        self.fetched.append(path)
        if self.fails is not None:
            raise self.fails
        return b"a picture", self.kind


class FakeBrain:
    def __init__(self, **sessions):
        self.sessions = sessions
        self.home = FakeHome()

    def default_satellite(self):
        return "pi"

    def session_for(self, satellite):
        return self.sessions.get(satellite or "pi")


class FakeFeed:
    """
    Stands in for the display feed: each satellite's state is at version 7, and a wait
    for version 7 lasts its whole timeout - unless a test changes the version.
    """

    def __init__(self):
        self.version = 7
        self.waits = []
        self.states = []
        self.pokes = []
        self.fails = None
        self.changed = threading.Event()

    def _state(self, satellite):
        if self.fails is not None:
            raise self.fails
        return {"version": self.version, "connected": True, "nova": "idle", "satellite_was": satellite}

    def state(self, satellite):
        self.states.append(satellite)
        return self._state(satellite)

    def wait(self, satellite, since, timeout):
        self.waits.append((satellite, since, timeout))
        if self._state(satellite)["version"] == since:
            self.changed.wait(timeout)
        return self._state(satellite)

    def poke(self, satellite):
        self.pokes.append(satellite)


@pytest.fixture
def clocks(tmp_path):
    return Displays(str(tmp_path / "displays.json"))


@pytest.fixture
def parts(clocks):
    return FakeBrain(pi=FakeSession()), clocks, FakeFeed()


@pytest.fixture
def served(parts, monkeypatch):
    monkeypatch.setattr(server_module, "STOP_CHECK", 0.02)
    monkeypatch.setattr(server_module, "RECHECK", 0.05)
    monkeypatch.setattr(server_module, "LONGEST_WAIT", 2)
    brain, clocks, feed = parts
    server = DisplayServer(brain, clocks, feed, port=0, address="127.0.0.1")
    server.start()
    yield server
    feed.changed.set()
    server.stop()


def _ask(server, path, key=None, body=None, raw=None, **headers):
    """Asks the display server for something as the clock's page would, with its key if given."""
    data = raw if raw is not None else None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data)
    if key is not None:
        request.add_header("Authorization", f"Bearer {key}")
    for name, value in headers.items():
        request.add_header(name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _json(answer):
    return json.loads(answer[2])


def _until(happened, seconds=5):
    """Waits for something another thread does, failing rather than hanging if it never does."""
    deadline = time.monotonic() + seconds
    while not happened():
        assert time.monotonic() < deadline, "it never happened"
        time.sleep(0.01)


def test_its_only_there_when_switched_on(make_config, parts):
    brain, clocks, feed = parts
    assert DisplayServer.from_config(make_config(display={"port": 0}), brain, clocks, feed) is None
    assert DisplayServer.from_config(make_config(), brain, clocks, feed) is None
    server = DisplayServer.from_config(make_config(display={"port": 8766}), brain, clocks, feed)
    assert (server.port, server.address) == (8766, "0.0.0.0")


def test_static_page_needs_no_key(served):
    status, headers, body = _ask(served, "/display")
    assert status == 200 and b"<html" in body
    assert headers["Content-Type"].startswith("text/html")
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
    # Covers are fetched with the clock's key and shown from blob: addresses - nothing else is let in.
    assert "img-src 'self' blob:" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    _, headers, _ = _ask(served, "/display/api/state")
    assert "img-src 'self' blob:" in headers["Content-Security-Policy"]


def test_unknown_paths_are_not_found(served, clocks):
    _, key = clocks.add("Desk", "pi")
    assert _ask(served, "/")[0] == 404
    assert _ask(served, "/settings.js")[0] == 404
    assert _ask(served, "/display/../settings.js")[0] == 404
    assert _ask(served, "/display/api/nothing", key)[0] == 404
    assert _ask(served, "/display/api/nothing", key, body={})[0] == 404
    # Nothing but the API takes a change.
    assert _ask(served, "/display", body={})[0] == 404


@pytest.mark.parametrize("path, body", [("/display/api/state?since=7", None),
                                        ("/display/api/hold", {"action": "start"}),
                                        ("/display/api/volume", {"direction": "up"}),
                                        ("/display/api/cover?kind=nova&id=al-1", None)])
def test_api_needs_the_key(served, parts, clocks, path, body):
    brain, _, feed = parts
    feed.version = 8
    _, key = clocks.add("Desk", "pi")
    session = brain.sessions["pi"]
    # One short of the wrong tries an address is allowed, with the one below.
    for wrong in (None, "nope", key[:-1]):
        status, headers, answer = _ask(served, path, wrong, body)
        assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
        assert key.encode() not in answer
    # The key only counts as a bearer token, not as anything else the header could say.
    assert _ask(served, path, body=body, Authorization=f"Basic {key}")[0] == 401
    assert (session.listens, session.volume.levels, feed.waits) == (0, {"general": 60, "alarm": 80, "music": 40}, [])
    assert _ask(served, path, key, body)[0] == 200


def test_removed_clock_is_refused_at_once(served, parts, clocks):
    _, _, feed = parts
    entry, key = clocks.add("Desk", "pi")
    assert _ask(served, "/display/api/state?since=6", key)[0] == 200
    answers = []
    # A long poll on the version the clock already has, which would wait LONGEST_WAIT.
    polling = threading.Thread(target=lambda: answers.append(_ask(served, "/display/api/state?since=7", key)))
    polling.start()
    _until(lambda: feed.waits[1:])
    started = time.monotonic()
    clocks.remove(entry["id"])
    polling.join(5)
    assert answers[0][0] == 401 and time.monotonic() - started < 1
    assert _ask(served, "/display/api/state?since=6", key)[0] == 401
    assert _ask(served, "/display/api/hold", key, {"action": "start"})[0] == 401


def test_renewed_key_is_refused_and_the_new_one_taken(served, clocks):
    entry, old = clocks.add("Desk", "pi")
    new = clocks.renew(entry["id"])
    assert _ask(served, "/display/api/state?since=6", old)[0] == 401
    assert _ask(served, "/display/api/state?since=6", new)[0] == 200


def test_wrong_keys_are_slowed(served, parts, clocks):
    _, key = clocks.add("Desk", "pi")
    for _ in range(TRIES_EACH):
        assert _ask(served, "/display/api/state?since=6", "a guess")[0] == 401
    status, headers, _ = _ask(served, "/display/api/state?since=6", "another guess")
    assert status == 429 and headers["Retry-After"] == "60"
    # Waiting is for the address, so even the right key is turned away until it's over -
    # unchecked, as a guess would be.
    assert _ask(served, "/display/api/state?since=6", key)[0] == 429
    assert _ask(served, "/display/api/hold", key, {"action": "start"})[0] == 429
    assert parts[0].sessions["pi"].listens == 0


def test_right_keys_never_add_up_to_a_wait(served, clocks):
    _, key = clocks.add("Desk", "pi")
    for _ in range(TRIES_EACH + 2):
        assert _ask(served, "/display/api/state?since=6", key)[0] == 200


def test_wrong_keys_are_logged_a_line_a_minute(served, caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            _ask(served, "/display/api/state?since=6", "a guess")
    lines = [record.getMessage() for record in caplog.records if "wrong key" in record.getMessage()]
    assert len(lines) == 1 and "127.0.0.1" in lines[0] and "a guess" not in lines[0]


def test_state_carries_brightness_and_home_after(served, parts, clocks):
    _, _, feed = parts
    entry, key = clocks.add("Desk", "pi")
    clocks.change(entry["id"], {"brightness": 40, "home_after": 120})
    state = _json(_ask(served, "/display/api/state?since=6", key))
    assert (state["version"], state["brightness"], state["home_after"]) == (7, 40, 120)
    assert feed.waits[0][:2] == ("pi", 6)


def test_state_without_a_version_is_answered_at_once(served, parts, clocks):
    _, _, feed = parts
    _, key = clocks.add("Desk", "pi")
    started = time.monotonic()
    state = _json(_ask(served, "/display/api/state", key))
    assert state["version"] == 7 and time.monotonic() - started < 1
    assert feed.states == ["pi"]


def test_state_waits_for_a_change(served, parts, clocks):
    _, _, feed = parts
    _, key = clocks.add("Desk", "pi")
    answers = []
    polling = threading.Thread(target=lambda: answers.append(_ask(served, "/display/api/state?since=7", key)))
    polling.start()
    _until(lambda: feed.waits)
    started = time.monotonic()
    feed.version = 8
    polling.join(5)
    assert _json(answers[0])["version"] == 8 and time.monotonic() - started < 1


def test_state_is_answered_once_the_wait_is_over(served, parts, clocks):
    _, key = clocks.add("Desk", "pi")
    started = time.monotonic()
    state = _json(_ask(served, "/display/api/state?since=7", key))
    assert state["version"] == 7 and 1.9 < time.monotonic() - started < 4


def test_a_change_on_the_settings_page_is_answered_at_once(served, parts, clocks):
    _, _, feed = parts
    entry, key = clocks.add("Desk", "pi")
    answers = []
    polling = threading.Thread(target=lambda: answers.append(_ask(served, "/display/api/state?since=7", key)))
    polling.start()
    _until(lambda: feed.waits)
    started = time.monotonic()
    clocks.change(entry["id"], {"brightness": 20, "satellite": "bedroom"})
    polling.join(5)
    state = _json(answers[0])
    assert state["brightness"] == 20 and time.monotonic() - started < 1
    assert state["satellite_was"] == "bedroom"


def test_clock_with_no_satellite_follows_the_default(served, parts, clocks):
    _, _, feed = parts
    _, key = clocks.add("Desk", None)
    assert _ask(served, "/display/api/state?since=6", key)[0] == 200
    assert feed.waits[0][0] == "pi"


@pytest.mark.parametrize("since", ["seven", "7.5", "", "1" * 30])
def test_since_must_be_a_whole_number(served, parts, clocks, since):
    _, key = clocks.add("Desk", "pi")
    assert _ask(served, f"/display/api/state?since={since}", key)[0] == 400
    assert parts[2].waits == []


def test_a_feed_which_fails_answers_500_and_logs_once(served, parts, clocks, caplog):
    _, _, feed = parts
    _, key = clocks.add("Desk", "pi")
    feed.fails = RuntimeError("Home Assistant went away")
    with caplog.at_level(logging.ERROR):
        assert _ask(served, "/display/api/state?since=6", key)[0] == 500
        assert _ask(served, "/display/api/state", key)[0] == 500
    lines = [record for record in caplog.records if "Home Assistant went away" in record.getMessage()]
    assert len(lines) == 1 and lines[0].exc_info is None
    feed.fails = None
    assert _ask(served, "/display/api/state?since=6", key)[0] == 200


def test_hold_start_and_end(served, parts, clocks):
    brain, _, _ = parts
    session = brain.sessions["pi"]
    _, key = clocks.add("Desk", "pi")
    assert _json(_ask(served, "/display/api/hold", key, {"action": "start"})) == {"listening": True}
    assert (session.listens, session.stops) == (1, 0)
    assert _json(_ask(served, "/display/api/hold", key, {"action": "end"})) == {"listening": False}
    assert (session.listens, session.stops) == (1, 1)
    # Nova already in a conversation.
    session.will_listen = False
    assert _json(_ask(served, "/display/api/hold", key, {"action": "start"})) == {"listening": False}


def test_hold_on_a_satellite_not_connected(served, clocks):
    _, key = clocks.add("Desk", "bedroom")
    assert _json(_ask(served, "/display/api/hold", key, {"action": "start"})) == {"listening": False}
    assert _json(_ask(served, "/display/api/hold", key, {"action": "end"})) == {"listening": False}


def test_volume_steps_music_while_playing_else_voice(served, parts, clocks):
    brain, _, feed = parts
    session = brain.sessions["pi"]
    _, key = clocks.add("Desk", "pi")
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "up"})) == {"category": "music",
                                                                                     "percent": 50}
    assert session.boops == [0.5] and feed.pokes == ["pi"]
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "down"})) == {"category": "music",
                                                                                       "percent": 40}
    session.music_player.song["playing"] = False
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "down"})) == {"category": "general",
                                                                                       "percent": 50}
    assert session.boops[-1] == 0.5
    session.music_player = None
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "up"})) == {"category": "general",
                                                                                     "percent": 60}
    assert session.volume.levels["alarm"] == 80


def test_volume_stays_between_0_and_100(served, parts, clocks):
    session = parts[0].sessions["pi"]
    session.music_player.song["playing"] = False
    _, key = clocks.add("Desk", "pi")
    session.volume.levels["general"] = 95
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "up"}))["percent"] == 100
    session.volume.levels["general"] = 4
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "down"}))["percent"] == 0


def test_volume_on_a_satellite_not_connected(served, parts, clocks):
    _, key = clocks.add("Desk", "bedroom")
    assert _ask(served, "/display/api/volume", key, {"direction": "up"})[0] == 409
    assert parts[2].pokes == []


def test_volume_is_answered_when_the_feed_fails(served, parts, clocks):
    _, _, feed = parts
    _, key = clocks.add("Desk", "pi")

    def fails(satellite):
        raise RuntimeError("Home Assistant went away")

    feed.poke = fails
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "up"})) == {"category": "music",
                                                                                     "percent": 50}


def test_volume_while_the_player_cant_be_asked(served, parts, clocks):
    session = parts[0].sessions["pi"]
    _, key = clocks.add("Desk", "pi")

    def fails():
        raise requests.ConnectionError("Spotify went away")

    session.music_player.now_playing = fails
    assert _json(_ask(served, "/display/api/volume", key, {"direction": "up"})) == {"category": "general",
                                                                                     "percent": 70}


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'"start"', b"", b"{" * 900, b"[" * 1000,
                                 json.dumps({"action": "start", "padding": "x" * 1100}).encode(),
                                 b'{"action": "start", "direction": "up"' + b" " * 1024 + b"}"])
def test_bad_bodies_are_refused(served, parts, clocks, raw):
    brain, _, _ = parts
    session = brain.sessions["pi"]
    _, key = clocks.add("Desk", "pi")
    for path in ("/display/api/hold", "/display/api/volume"):
        assert _ask(served, path, key, raw=raw)[0] == 400
    assert (session.listens, session.boops) == (0, [])


@pytest.mark.parametrize("path, body", [("/display/api/hold", {"action": "listen"}),
                                        ("/display/api/hold", {}),
                                        ("/display/api/hold", {"action": ["start"]}),
                                        ("/display/api/volume", {"direction": "sideways"}),
                                        ("/display/api/volume", {}),
                                        ("/display/api/volume", {"direction": "up", "category": "alarm"})])
def test_unknown_actions_and_directions_are_refused(served, parts, clocks, path, body):
    session = parts[0].sessions["pi"]
    _, key = clocks.add("Desk", "pi")
    status = _ask(served, path, key, body)[0]
    if body.get("category") == "alarm":
        # Whatever else is sent, the buttons never reach the alarm's volume.
        assert status == 200 and session.volume.levels["alarm"] == 80
    else:
        assert status == 400 and (session.listens, session.boops) == (0, [])


def test_a_body_shorter_than_it_says_is_given_up_on(served, parts, clocks, monkeypatch):
    monkeypatch.setattr(server_module._Handler, "timeout", 0.2)
    _, key = clocks.add("Desk", "pi")
    with socket.create_connection(("127.0.0.1", served.port), timeout=5) as connection:
        connection.sendall(f"POST /display/api/hold HTTP/1.1\r\nAuthorization: Bearer {key}\r\n"
                           f"Content-Length: 100\r\n\r\n{{}}".encode())
        # The server closes the connection, without an answer, once it's waited long enough.
        assert connection.recv(1024) == b""
    assert parts[0].sessions["pi"].listens == 0
    assert _ask(served, "/display/api/hold", key, {"action": "start"})[0] == 200


def test_novas_cover_only_for_whats_playing_now(served, parts, clocks):
    brain, _, _ = parts
    player = brain.sessions["pi"].music_player
    _, key = clocks.add("Desk", "pi")
    status, headers, body = _ask(served, "/display/api/cover?kind=nova&id=al-1", key)
    assert (status, headers["Content-Type"], body) == (200, "image/jpeg", b"a cover")
    assert _ask(served, "/display/api/cover?kind=nova&id=al-2", key)[0] == 404
    assert _ask(served, "/display/api/cover?kind=nova", key)[0] == 404
    player.song["cover"] = None
    assert _ask(served, "/display/api/cover?kind=nova&id=None", key)[0] == 404
    assert player.covers == 1


def test_novas_cover_on_a_satellite_not_connected(served, clocks):
    _, key = clocks.add("Desk", "bedroom")
    assert _ask(served, "/display/api/cover?kind=nova&id=al-1", key)[0] == 404


def test_jellyfins_cover_is_the_players_own_picture(served, parts, clocks):
    home = parts[0].home
    _, key = clocks.add("Desk", "pi")
    status, headers, body = _ask(served, "/display/api/cover?kind=home&id=media_player.office_tv", key)
    assert (status, headers["Content-Type"], body) == (200, "image/png", b"a picture")
    assert home.fetched == ["/api/media_player_proxy/media_player.office_tv?token=abc"]
    # Only ever a player's own picture, never a path the page names.
    for asked in ("media_player.kitchen", "media_player.bedroom", "%2Fapi%2Fstates",
                  "%2Fapi%2Fmedia_player_proxy%2Fmedia_player.office_tv%3Ftoken%3Dabc"):
        assert _ask(served, f"/display/api/cover?kind=home&id={asked}", key)[0] == 404
    assert _ask(served, "/display/api/cover?kind=elsewhere&id=media_player.office_tv", key)[0] == 404
    assert len(home.fetched) == 1


@pytest.mark.parametrize("fails", [ValueError("That picture is too big"), requests.ConnectionError("gone")])
def test_jellyfins_cover_when_home_assistant_fails(served, parts, clocks, fails):
    parts[0].home.fails = fails
    _, key = clocks.add("Desk", "pi")
    assert _ask(served, "/display/api/cover?kind=home&id=media_player.office_tv", key)[0] == 404


@pytest.mark.parametrize("kind", ["text/html", "image/svg+xml", "application/octet-stream"])
def test_jellyfins_cover_must_be_a_picture(served, parts, clocks, kind):
    # Home Assistant's answer is served from the clock's own address, so anything but a
    # plain picture - a page, or an SVG, which can hold a script - could run as the
    # clock's page does.
    parts[0].home.kind = kind
    _, key = clocks.add("Desk", "pi")
    assert _ask(served, "/display/api/cover?kind=home&id=media_player.office_tv", key)[0] == 404


def test_jellyfins_cover_without_home_assistant(served, parts, clocks):
    parts[0].home = None
    _, key = clocks.add("Desk", "pi")
    assert _ask(served, "/display/api/cover?kind=home&id=media_player.office_tv", key)[0] == 404


def test_novas_cover_must_be_a_picture(served, parts, clocks):
    player = parts[0].sessions["pi"].music_player
    player.cover = lambda: (b"<svg/>", "image/svg+xml")
    _, key = clocks.add("Desk", "pi")
    assert _ask(served, "/display/api/cover?kind=nova&id=al-1", key)[0] == 404


def test_no_satellite_at_all_isnt_connected(served, parts, clocks):
    # The real feed, on a brain with no satellites it can name - its satellites' file
    # unreadable - whose session_for(None) raises, as the brain's does then.
    brain, _, _ = parts
    brain.home = brain.weather = brain.calendar = None

    def no_default():
        raise OSError("The satellites' file can't be read")

    def session_for(satellite):
        return brain.sessions.get(satellite or no_default())

    brain.default_satellite, brain.session_for = no_default, session_for
    served.feed = DisplayFeed(brain)
    _, key = clocks.add("Desk", None)
    status, _, body = _ask(served, "/display/api/state", key)
    assert status == 200 and json.loads(body)["connected"] is False
    assert _json(_ask(served, "/display/api/hold", key, {"action": "start"})) == {"listening": False}
    assert _ask(served, "/display/api/volume", key, {"direction": "up"})[0] == 409
