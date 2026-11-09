"""Tests for the display feed - each satellite's state for its clock displays, and waking the clocks waiting on it."""

import logging
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace

import requests

from voice_assistant.display_feed import DisplayFeed

# 10:00 on Monday 9 November 2026, the brain's local time.
TEN = datetime(2026, 11, 9, 10, 0).astimezone().timestamp()

KEYS = {"version", "connected", "nova", "heard", "muted", "now", "utc_offset", "weather", "events", "music", "volume"}
MUSIC_KEYS = {"source", "room", "title", "artist", "album", "playing", "position", "duration", "at", "cover"}


def _at(hour, minute=0, day=9):
    """A local time on a day of November 2026, as the calendar gives a timed event's start or end."""
    return datetime(2026, 11, day, hour, minute).astimezone()


def _song(track="Gooba", playing=True, position=12.0, cover="al-1"):
    """What Nova's player says is playing, as now_playing gives it - Gooba, unless a test says otherwise."""
    return {"track": track, "artist": "6ix9ine", "album": "Gooba", "playing": playing, "position": position,
            "duration": 125, "cover": cover}


def _jellyfin(entity="media_player.office_tv", area="Office", state="playing", title="Arrival"):
    """One Jellyfin player as HomeAssistant.jellyfin_media() gives it - the Office TV playing, unless told otherwise."""
    return {"entity_id": entity, "name": "Office TV", "area": area, "state": state, "title": title,
            "artist": None, "album": None, "picture": f"/api/media_player_proxy/{entity}?token=abc",
            "position": 600.0, "duration": 6960.0, "updated": TEN - 3}


class FakeVolume:
    CATEGORIES = ("general", "alarm", "music")

    def __init__(self):
        self.levels = {"general": 60, "alarm": 80, "music": 40}

    def percent(self, category):
        return self.levels[category]


class FakePlayer:
    """Stands in for Nova's player on a satellite - Navidrome, or Spotify, which is asked over the internet."""

    def __init__(self, name="Navidrome", song=None):
        self.name = name
        self.needs_internet = name == "Spotify"
        self.song = song
        self.fails = None
        self.asked = 0
        self.level = 40

    def now_playing(self):
        self.asked += 1
        if self.fails is not None:
            raise self.fails
        return self.song

    def get_volume(self):
        return self.level


class FakeSession:
    """Stands in for a connected satellite's session - what Nova is doing there, its mute, volumes and music."""

    def __init__(self, area="Office", player=None):
        self.home = SimpleNamespace(area=area)
        self.music_player = player if player is not None else FakePlayer()
        self.volume = FakeVolume()
        self.activity = "idle"
        self.heard = ""
        self.muted = False
        self.watchers = []

    def watch(self, watcher):
        self.watchers.append(watcher)

    def unwatch(self, watcher):
        if watcher in self.watchers:
            self.watchers.remove(watcher)

    def change(self, activity=None, heard=None):
        """Changes what Nova is doing and tells the watchers, as the session does - on the thread changing it."""
        if activity is not None:
            self.activity = activity
        if heard is not None:
            self.heard = heard
        for watcher in list(self.watchers):
            watcher()


class FakeWeather:
    def __init__(self, today=None):
        self.today_is = today if today is not None else {"temperature": 9.4, "condition": "partly cloudy",
                                                         "high": 11.0, "low": 4.0}
        self.fails = None
        self.asked = 0

    def today(self, now=None):
        self.asked += 1
        if self.fails is not None:
            raise self.fails
        return self.today_is


class FakeCalendar:
    def __init__(self, *events):
        self.found = list(events)
        self.fails = None
        self.asked = []

    def left_out(self):
        return {"work@group.calendar.google.com"}

    def events(self, first, days, calendar_ids=None, leave_out=frozenset()):
        self.asked.append((first, leave_out))
        if self.fails is not None:
            raise self.fails
        return list(self.found)


class FakeHome:
    def __init__(self, *players):
        self.players = list(players)
        self.fails = None

    def jellyfin_media(self):
        if self.fails is not None:
            raise self.fails
        return list(self.players)


class FakeBrain:
    def __init__(self, **sessions):
        self.sessions = dict(sessions)
        self.weather = FakeWeather()
        self.calendar = FakeCalendar()
        self.home = FakeHome()

    def session_for(self, satellite):
        return self.sessions.get(satellite)


class Rig:
    """A feed on a FakeBrain with one satellite, "pi", in the Office - on a clock which only moves when told."""

    def __init__(self, session=None):
        self.session = session if session is not None else FakeSession()
        self.brain = FakeBrain(pi=self.session)
        self.time = TEN
        self.feed = DisplayFeed(self.brain, now=lambda: self.time, every=0.01)

    def later(self, seconds):
        """Moves the clock on, then has the feed read whatever is due and build its states."""
        self.time += seconds
        self.feed.check()
        return self.feed.state("pi")


# The state's shape

def test_state_has_the_contract_keys():
    state = Rig().feed.state("pi")
    assert set(state) == KEYS
    assert state["connected"] is True and state["nova"] == "idle" and state["now"] == TEN
    assert isinstance(state["version"], int) and isinstance(state["utc_offset"], int)
    assert set(state["weather"]) == {"temperature", "condition", "high", "low"}
    assert set(state["volume"]) == {"category", "percent"}


def test_an_unknown_satellite_isnt_connected():
    state = Rig().feed.state("kitchen")
    assert set(state) == KEYS
    assert state["connected"] is False and state["nova"] == "idle" and state["muted"] is False
    assert state["volume"] == {"category": "general", "percent": 0}


def test_no_satellite_isnt_the_default():
    # A clock asks for no satellite only when there's none to name - the brain's
    # session_for(None) would give the default's, or raise, so it's never asked.
    rig = Rig()
    rig.brain.session_for = lambda satellite: rig.session if satellite is None else rig.brain.sessions.get(satellite)
    state = rig.feed.state(None)
    assert set(state) == KEYS and state["connected"] is False and state["music"] is None
    assert rig.session.watchers == [] and rig.session.music_player.asked == 0
    rig.feed.check()
    assert rig.feed.state(None)["connected"] is False


def test_without_home_assistant_or_a_calendar_theres_simply_less():
    rig = Rig()
    rig.brain.weather = rig.brain.calendar = rig.brain.home = None
    rig.session.music_player.song = None
    state = rig.feed.state("pi")
    assert state["weather"] is None and state["events"] == [] and state["music"] is None


# What Nova is doing, as it happens

def test_activity_change_wakes_a_waiter():
    rig = Rig()
    before = rig.feed.state("pi")["version"]
    answers = []
    waiter = threading.Thread(target=lambda: answers.append(rig.feed.wait("pi", before, 5)), daemon=True)
    waiter.start()
    rig.session.change(activity="listening")
    waiter.join(1)
    # No poll thread is running: the watcher alone wakes the waiter.
    assert not waiter.is_alive()
    assert answers[0]["nova"] == "listening" and answers[0]["version"] != before


def test_what_was_heard_is_carried_at_once():
    rig = Rig()
    before = rig.feed.state("pi")["version"]
    rig.session.change(activity="thinking", heard="What time is it?")
    state = rig.feed.wait("pi", before, 5)
    assert state["nova"] == "thinking" and state["heard"] == "What time is it?"


def test_a_wait_with_nothing_changing_gives_the_state_after_its_timeout():
    rig = Rig()
    before = rig.feed.state("pi")["version"]
    started = time.monotonic()
    state = rig.feed.wait("pi", before, 0.05)
    assert state["version"] == before and 0.04 <= time.monotonic() - started < 1


def test_a_wait_for_an_old_version_answers_at_once():
    rig = Rig()
    version = rig.feed.state("pi")["version"]
    started = time.monotonic()
    assert rig.feed.wait("pi", version - 1, 5)["version"] == version
    assert time.monotonic() - started < 1


def test_muted_is_carried():
    rig = Rig()
    assert rig.feed.state("pi")["muted"] is False
    rig.session.muted = True
    assert rig.later(1)["muted"] is True


def test_a_satellite_gone_isnt_connected_or_watched():
    rig = Rig()
    assert rig.feed.state("pi")["connected"] is True and len(rig.session.watchers) == 1
    rig.brain.sessions.clear()
    assert rig.later(1)["connected"] is False
    assert rig.session.watchers == []


def test_each_session_is_watched_once():
    rig = Rig()
    for _ in range(3):
        rig.later(1)
    assert len(rig.session.watchers) == 1


def test_stop_unwatches_the_sessions():
    rig = Rig()
    rig.feed.state("pi")
    rig.feed.start()
    rig.feed.stop()
    assert rig.session.watchers == []
    # Nor is one watched again by a build after it - a pass still running as it stopped.
    rig.feed.poke("pi")
    assert rig.session.watchers == []


def test_stop_wakes_the_waiting_clocks():
    rig = Rig()
    before = rig.feed.state("pi")["version"]
    answers = []
    waiter = threading.Thread(target=lambda: answers.append(rig.feed.wait("pi", before, 5)), daemon=True)
    waiter.start()
    rig.feed.stop()
    waiter.join(1)
    assert not waiter.is_alive() and answers[0]["version"] == before


# What's playing

def test_novas_music_wins_over_jellyfin():
    rig = Rig()
    rig.session.music_player.song = _song()
    rig.brain.home.players = [_jellyfin()]
    music = rig.feed.state("pi")["music"]
    assert set(music) == MUSIC_KEYS
    assert music["source"] == "Navidrome" and music["room"] == "Office" and music["title"] == "Gooba"
    assert music["playing"] is True and music["position"] == 12.0 and music["at"] == TEN
    assert music["cover"] == "/display/api/cover?kind=nova&id=al-1"
    rig.session.music_player.song = None
    music = rig.later(2)["music"]
    assert music["source"] == "Jellyfin" and music["title"] == "Arrival"
    assert music["position"] == 600.0 and music["at"] == TEN - 3
    assert music["cover"] == "/display/api/cover?kind=home&id=media_player.office_tv"


def test_novas_music_paused_still_wins():
    rig = Rig()
    rig.session.music_player.song = _song(playing=False)
    rig.brain.home.players = [_jellyfin()]
    music = rig.feed.state("pi")["music"]
    assert music["source"] == "Navidrome" and music["playing"] is False


def test_jellyfin_in_the_room_first():
    rig = Rig()
    rig.session.music_player.song = None
    rig.brain.home.players = [_jellyfin("media_player.kitchen", "Kitchen", title="Dune"),
                              _jellyfin("media_player.office_firefox", "Office", state="paused", title="Heat"),
                              _jellyfin("media_player.office_tv", "Office", title="Arrival")]
    music = rig.feed.state("pi")["music"]
    assert music["title"] == "Arrival" and music["room"] == "Office"
    rig.session.home.area = "Bedroom"
    # With none in the satellite's room, the first by entity id - never the paused one.
    assert rig.later(5)["music"]["title"] == "Dune"


def test_a_paused_jellyfin_player_is_never_shown():
    rig = Rig()
    rig.session.music_player.song = None
    rig.brain.home.players = [_jellyfin(state="paused")]
    assert rig.feed.state("pi")["music"] is None


def test_position_alone_doesnt_change_the_version():
    rig = Rig()
    rig.session.music_player.song = _song(position=12.0)
    before = rig.feed.state("pi")["version"]
    rig.session.music_player.song = _song(position=14.0)
    state = rig.later(2)
    assert state["version"] == before
    assert state["music"]["position"] == 14.0 and state["music"]["at"] == TEN + 2 and state["now"] == TEN + 2
    rig.session.music_player.song = _song(track="Fefe", position=0.0)
    assert rig.later(2)["version"] != before


def test_spotify_is_asked_less_often_than_navidrome():
    navidrome, spotify = FakePlayer(song=_song()), FakePlayer("Spotify", song=_song())
    rig = Rig(FakeSession(player=navidrome))
    rig.brain.sessions["den"] = FakeSession(player=spotify)
    rig.feed.state("pi")
    rig.feed.state("den")
    for _ in range(4):
        rig.later(1)
    # Four seconds on: Navidrome at 0, 2 and 4, Spotify only at 0.
    assert navidrome.asked == 3 and spotify.asked == 1
    rig.later(1)
    assert spotify.asked == 2


def test_the_volume_is_the_musics_while_nova_plays_else_the_voices():
    rig = Rig()
    rig.session.music_player.song = _song()
    assert rig.feed.state("pi")["volume"] == {"category": "music", "percent": 40}
    rig.session.music_player.song = _song(playing=False)
    assert rig.later(2)["volume"] == {"category": "general", "percent": 60}


def test_poke_shows_a_volume_change_at_once():
    rig = Rig()
    before = rig.feed.state("pi")["version"]
    rig.session.volume.levels["general"] = 70
    rig.feed.poke("pi")
    state = rig.feed.wait("pi", before, 0)
    assert state["volume"]["percent"] == 70 and state["version"] != before


# Events

def test_events_are_the_next_five_not_over():
    rig = Rig()
    calendar = "Work"
    found = [{"summary": "Gym", "start": _at(8), "end": _at(9), "all_day": False},
             {"summary": "Bin day", "start": date(2026, 11, 9), "end": None, "all_day": True},
             {"summary": "Lecture", "start": _at(9, 30), "end": _at(10, 30), "all_day": False},
             {"summary": "Lunch", "start": _at(13), "end": _at(14), "all_day": False},
             {"summary": "Dentist", "start": _at(9, day=10), "end": _at(10, day=10), "all_day": False},
             {"summary": "Coursework due", "start": date(2026, 11, 12), "end": None, "all_day": True},
             {"summary": "Party", "start": _at(20, day=14), "end": _at(23, day=14), "all_day": False}]
    rig.brain.calendar = FakeCalendar(*[dict(event, calendar=calendar, calendar_id="someone@example.com") for event in found])
    events = rig.feed.state("pi")["events"]
    assert [event["title"] for event in events] == ["Bin day", "Lecture", "Lunch", "Dentist", "Coursework due"]
    assert all(set(event) == {"title", "start", "all_day"} for event in events)
    assert events[0] == {"title": "Bin day", "start": "2026-11-09", "all_day": True}
    assert events[1] == {"title": "Lecture", "start": "2026-11-09T09:30", "all_day": False}
    # From midnight today, without the calendars chosen out of what's on.
    assert rig.brain.calendar.asked[0] == (date(2026, 11, 9), {"work@group.calendar.google.com"})


def test_an_event_ending_drops_off_before_the_next_read():
    rig = Rig()
    rig.brain.calendar = FakeCalendar({"summary": "Lecture", "start": _at(9, 30), "end": _at(10, 1), "all_day": False})
    assert len(rig.feed.state("pi")["events"]) == 1
    assert rig.later(120)["events"] == []
    assert len(rig.brain.calendar.asked) == 1


def test_events_and_weather_are_read_on_their_own_schedules():
    rig = Rig()
    rig.feed.state("pi")
    rig.later(299)
    assert len(rig.brain.calendar.asked) == 1 and rig.brain.weather.asked == 1
    rig.later(1)
    assert len(rig.brain.calendar.asked) == 2 and rig.brain.weather.asked == 1
    rig.later(300)
    assert len(rig.brain.calendar.asked) == 3 and rig.brain.weather.asked == 2


# Failures

def test_failures_keep_the_last_good(caplog):
    rig = Rig(FakeSession(player=FakePlayer("Spotify", song=_song())))
    rig.brain.calendar = FakeCalendar({"summary": "Lunch", "start": _at(13), "end": _at(14), "all_day": False})
    state = rig.feed.state("pi")
    assert state["weather"]["temperature"] == 9.4 and state["music"]["source"] == "Spotify"
    rig.brain.weather.fails = requests.ConnectionError("Home Assistant is down")
    rig.brain.calendar.fails = requests.ConnectionError("Google is down")
    rig.brain.home.fails = requests.ConnectionError("Home Assistant is down")
    rig.session.music_player.fails = RuntimeError("Spotify said no")
    rig.feed.start()
    try:
        with caplog.at_level(logging.WARNING):
            rig.time += 600
            # Two whole rounds of failing reads, on the thread.
            for asked in (2, 3):
                for _ in range(200):
                    if rig.brain.weather.asked >= asked:
                        break
                    time.sleep(0.01)
                rig.time += 600
            assert rig.brain.weather.asked >= 3
            state = rig.feed.state("pi")
            assert rig.feed._thread.is_alive()
    finally:
        rig.feed.stop()
    assert state["weather"]["temperature"] == 9.4
    assert [event["title"] for event in state["events"]] == ["Lunch"]
    assert state["music"] is None
    # Once for each kind while it keeps failing, not once a read.
    assert len([record for record in caplog.records if "weather" in record.getMessage()]) == 1


def test_a_satellite_without_home_assistant_has_no_room():
    rig = Rig()
    rig.session.home = None
    rig.session.music_player.song = _song()
    state = rig.feed.state("pi")
    assert state["connected"] is True and state["music"]["room"] is None


def test_a_failing_build_doesnt_end_the_thread():
    rig = Rig()
    rig.feed.state("pi")
    broken = FakeSession()
    broken.volume = None
    rig.brain.sessions["pi"] = broken
    rig.feed.start()
    try:
        for _ in range(20):
            time.sleep(0.01)
        assert rig.feed._thread.is_alive()
    finally:
        rig.feed.stop()


def test_one_satellite_failing_to_build_doesnt_stop_the_others(caplog):
    rig = Rig()
    den = FakeSession()
    rig.brain.sessions["den"] = den
    rig.feed.state("pi")
    rig.feed.state("den")
    broken = FakeSession()
    broken.volume = None
    rig.brain.sessions["pi"] = broken
    den.muted = True
    with caplog.at_level(logging.WARNING):
        rig.feed.check()
        rig.feed.check()
    assert rig.feed.state("den")["muted"] is True
    # Logged once while it keeps failing, not once a pass.
    assert len([record for record in caplog.records if "pi satellite" in record.getMessage()]) == 1


def test_a_calendars_id_is_never_logged(caplog):
    rig = Rig()
    rig.brain.calendar.fails = requests.HTTPError(
        "404 Client Error: Not Found for url: https://www.googleapis.com/calendar/v3/calendars/someone%40example.com/events")
    with caplog.at_level(logging.DEBUG):
        rig.feed.state("pi")
    assert any("events" in record.getMessage() for record in caplog.records)
    assert not any("someone" in record.getMessage() for record in caplog.records)
