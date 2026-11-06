"""Tests for the media watcher - what's playing, and which of it counts in each satellite's room."""

import logging
import threading
import time

import pytest
import requests

from voice_assistant.media_watch import MediaWatcher


def _player(entity="media_player.bedroom_tv", name="Bedroom TV", area="Bedroom", device="devtv", source=None):
    """One player as HomeAssistant.playing_media() gives it - the Bedroom TV's, unless a test says otherwise."""
    return {"entity_id": entity, "name": name, "area": area, "device": device, "source": source}


class FakeHome:
    """Stands in for Home Assistant, with what's playing - or raising."""

    def __init__(self, *players):
        self.players = list(players)
        self.fails = None
        self.reads = 0

    def playing_media(self):
        self.reads += 1
        if self.fails is not None:
            raise self.fails
        return list(self.players)


class Rig:
    """A watcher on a FakeHome, counting the changes it tells of."""

    def __init__(self, *players):
        self.home = FakeHome(*players)
        self.changes = 0
        self.watcher = MediaWatcher(self.home, on_change=self._changed, every=0.01)

    def _changed(self):
        self.changes += 1


def _counted(*players, area="Bedroom", devices=("devnova",), speakers=()):
    """What counts for a satellite in the area given, after one check."""
    rig = Rig(*players)
    rig.watcher.check()
    return rig.watcher.playing_in(area, devices, speakers)


# What counts

def test_a_player_playing_in_the_satellites_room_counts():
    assert _counted(_player()) == ["Bedroom TV"]


def test_whats_playing_is_named_in_order():
    assert _counted(_player("media_player.firefox", "Firefox"), _player(),
                    _player("media_player.kodi", "Kodi")) == ["Bedroom TV", "Firefox", "Kodi"]


def test_nothing_playing_counts_as_nothing():
    # Paused, idle, off and unavailable players never reach the watcher: the template renders only those
    # playing - see test_home_assistant.py's test_only_players_playing_are_rendered_with_what_the_watcher_needs.
    assert _counted() == []


def test_a_player_in_another_room_doesnt_count():
    assert _counted(_player(area="Kitchen")) == []


def test_a_player_with_no_room_doesnt_count():
    # Like Jellyfin's per-session players, e.g. media_player.firefox_20, which have no area and no device.
    assert _counted(_player("media_player.firefox_20", "Firefox", area=None, device=None)) == []


@pytest.mark.parametrize("area", [None, ""])
def test_nothing_counts_for_a_satellite_whose_room_isnt_known(area):
    assert _counted(_player(area=None), _player(), area=area) == []


def test_a_player_on_any_connected_satellites_device_doesnt_count():
    players = (_player("media_player.bedroom_pi", "Bedroom Pi", device="devbedroom"),
               _player("media_player.kitchen_pi", "Kitchen Pi", device="devkitchen"), _player())
    assert _counted(*players, devices=["devbedroom", "devkitchen"]) == ["Bedroom TV"]


def test_a_player_with_no_device_counts_when_a_satellites_device_isnt_known():
    # A player with no device isn't a satellite's own just because a satellite's device isn't known either.
    assert _counted(_player(device=None), devices=[None]) == ["Bedroom TV"]


def test_two_satellites_in_two_rooms_each_see_only_their_own():
    rig = Rig(_player(), _player("media_player.kitchen_radio", "Kitchen Radio", area="Kitchen", device="devradio"))
    rig.watcher.check()
    devices = ["devbedroom", "devkitchen"]
    assert rig.watcher.playing_in("Bedroom", devices, []) == ["Bedroom TV"]
    assert rig.watcher.playing_in("Kitchen", devices, []) == ["Kitchen Radio"]
    assert rig.watcher.playing_in("Hall", devices, []) == []


def test_spotify_playing_on_a_satellites_own_speaker_doesnt_count():
    spotify = _player("media_player.spotify_nathan", "Spotify Nathan", device=None, source="Voice-Assistant")
    assert _counted(spotify, speakers=["Kitchen Pi", "voice-assistant "]) == []


def test_spotify_playing_on_something_else_counts():
    spotify = _player("media_player.spotify_nathan", "Spotify Nathan", device=None, source="Living Room Speaker")
    assert _counted(spotify, speakers=["Voice-Assistant", None]) == ["Spotify Nathan"]


def test_a_player_with_no_source_counts_whatever_the_speakers():
    assert _counted(_player(source=None), speakers=["Voice-Assistant"]) == ["Bedroom TV"]


def test_nothing_plays_before_the_first_check():
    rig = Rig(_player())
    assert rig.watcher.playing() == [] and rig.watcher.playing_in("Bedroom", [], []) == []


def test_every_player_playing_is_kept_as_read():
    players = [_player(), _player("media_player.firefox_20", "Firefox", area=None, device=None)]
    rig = Rig(*players)
    rig.watcher.check()
    assert rig.watcher.playing() == players


# Telling of a change

def test_a_change_is_told_once_and_nothing_while_unchanged():
    rig = Rig()
    rig.watcher.check()
    assert rig.changes == 0
    rig.home.players = [_player()]
    rig.watcher.check()
    rig.watcher.check()
    assert rig.changes == 1
    rig.home.players = [_player(), _player("media_player.firefox", "Firefox")]
    rig.watcher.check()
    assert rig.changes == 2
    rig.home.players = []
    rig.watcher.check()
    rig.watcher.check()
    assert rig.changes == 3


@pytest.mark.parametrize("moved", [{"area": "Kitchen"}, {"device": "devother"}, {"source": "Voice-Assistant"}])
def test_a_player_moving_room_device_or_source_is_a_change(moved):
    rig = Rig(_player())
    rig.watcher.check()
    rig.home.players = [_player(**moved)]
    rig.watcher.check()
    assert rig.changes == 2


def test_a_name_changing_alone_is_no_change():
    rig = Rig(_player())
    rig.watcher.check()
    rig.home.players = [_player(name="Bedroom TV - The Film")]
    rig.watcher.check()
    assert rig.changes == 1
    # The new name is still what's said.
    assert rig.watcher.playing_in("Bedroom", [], []) == ["Bedroom TV - The Film"]


def test_whats_playing_is_up_to_date_when_on_change_is_called():
    # The satellites' states are sent from on_change, and say whether anything plays.
    rig = Rig(_player())
    seen = []
    rig.watcher._on_change = lambda: seen.append(rig.watcher.playing_in("Bedroom", [], []))
    rig.watcher.check()
    assert seen == [["Bedroom TV"]]


# Home Assistant unreadable

@pytest.mark.parametrize("error", [requests.ConnectionError("down"), ValueError("not JSON"), KeyError("area"),
                                   TypeError("not a list")])
def test_nothing_counts_while_home_assistant_cant_be_read_twice_in_a_row(error):
    rig = Rig(_player())
    rig.watcher.check()
    rig.home.fails = error
    rig.watcher.check()
    rig.watcher.check()
    assert rig.watcher.playing() == [] and rig.watcher.playing_in("Bedroom", [], []) == []
    assert rig.changes == 2


def test_one_read_which_fails_leaves_what_was_playing_counting():
    # One over its two seconds, say, mustn't drop the stronger wake word for a cycle.
    rig = Rig(_player())
    rig.watcher.check()
    rig.home.fails = requests.ConnectionError("slow")
    rig.watcher.check()
    assert rig.watcher.playing_in("Bedroom", [], []) == ["Bedroom TV"]
    rig.home.fails = None
    rig.watcher.check()
    assert rig.watcher.playing_in("Bedroom", [], []) == ["Bedroom TV"]
    # Told once, as it began.
    assert rig.changes == 1


def test_failed_reads_with_one_that_works_between_are_never_in_a_row(caplog):
    rig = Rig(_player())
    rig.watcher.check()
    with caplog.at_level(logging.INFO, logger="voice_assistant.media_watch"):
        for _ in range(3):
            rig.home.fails = requests.ConnectionError("slow")
            rig.watcher.check()
            rig.home.fails = None
            rig.watcher.check()
    assert rig.watcher.playing_in("Bedroom", [], []) == ["Bedroom TV"] and rig.changes == 1
    assert caplog.records == []


def test_home_assistant_down_is_warned_of_once_and_its_return_said(caplog):
    rig = Rig(_player())
    with caplog.at_level(logging.INFO, logger="voice_assistant.media_watch"):
        rig.home.fails = requests.ConnectionError("down")
        for _ in range(3):
            rig.watcher.check()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        assert "Couldn't read what's playing from Home Assistant, so nothing counts as playing: down" in caplog.text
        assert not [r for r in caplog.records if r.levelno == logging.INFO]
        rig.home.fails = None
        rig.watcher.check()
        rig.watcher.check()
        assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1
        assert rig.watcher.playing_in("Bedroom", [], []) == ["Bedroom TV"]
        rig.home.fails = requests.ConnectionError("down again")
        rig.watcher.check()
        rig.watcher.check()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_an_unexpected_failure_in_a_read_leaves_nothing_counting(caplog):
    rig = Rig(_player())
    rig.watcher.check()
    rig.home.fails = RuntimeError("a bug")
    with caplog.at_level(logging.INFO, logger="voice_assistant.media_watch"):
        rig.watcher.check()
        rig.watcher.check()
    assert rig.watcher.playing() == [] and rig.changes == 2
    # With its traceback, as it's a bug rather than Home Assistant - and once while it keeps happening.
    assert [r.exc_info[0] for r in caplog.records if r.exc_info] == [RuntimeError]


def test_a_player_of_the_wrong_shape_leaves_nothing_counting():
    # Compared by entity, so one with none can't be put in order with the rest.
    rig = Rig(_player())
    rig.watcher.check()
    rig.home.players = [_player(), _player(entity=None)]
    rig.watcher.check()
    rig.watcher.check()
    assert rig.watcher.playing() == [] and rig.changes == 2


def test_a_first_good_read_says_nothing(caplog):
    with caplog.at_level(logging.INFO, logger="voice_assistant.media_watch"):
        Rig(_player()).watcher.check()
    assert caplog.records == []


# The thread

def _wait_for(condition):
    deadline = time.time() + 5
    while not condition() and time.time() < deadline:
        time.sleep(0.01)


def test_the_thread_checks_until_stopped():
    rig = Rig(_player())
    rig.watcher.start()
    _wait_for(lambda: rig.home.reads >= 3)
    rig.watcher.stop()
    assert rig.home.reads >= 3 and rig.changes == 1
    assert not [t for t in threading.enumerate() if t.name == "media"]


def test_an_on_change_which_raises_doesnt_end_the_thread(caplog):
    rig = Rig(_player())
    calls = []

    def broken():
        calls.append(rig.watcher.playing_in("Bedroom", [], []))
        raise RuntimeError("boom")

    rig.watcher._on_change = broken
    with caplog.at_level(logging.WARNING, logger="voice_assistant.media_watch"):
        rig.watcher.start()
        _wait_for(lambda: len(calls) == 1)
        rig.home.players = []
        _wait_for(lambda: len(calls) == 2)
        rig.home.players = [_player()]
        _wait_for(lambda: len(calls) == 3)
        rig.watcher.stop()
    assert calls == [["Bedroom TV"], [], ["Bedroom TV"]]
    assert [r for r in caplog.records if r.exc_info and r.exc_info[0] is RuntimeError]


def test_a_failure_is_logged_once_in_a_row(caplog):
    rig = Rig(_player())

    def broken():
        raise RuntimeError("boom")

    rig.watcher._on_change = broken
    with caplog.at_level(logging.WARNING, logger="voice_assistant.media_watch"):
        for players in ([], [_player()], []):
            rig.home.players = players
            rig.watcher._pass()
        assert len([r for r in caplog.records if r.exc_info]) == 1
        # A clean pass, then the same failure again, is logged again.
        rig.watcher._pass()
        rig.home.players = [_player()]
        rig.watcher._pass()
        assert len([r for r in caplog.records if r.exc_info]) == 2


def test_a_different_failure_in_a_row_is_logged_too(caplog):
    rig = Rig()
    failures = iter([RuntimeError("one"), KeyError("two")])

    def broken():
        raise next(failures)

    rig.watcher._on_change = broken
    with caplog.at_level(logging.WARNING, logger="voice_assistant.media_watch"):
        rig.home.players = [_player()]
        rig.watcher._pass()
        rig.home.players = []
        rig.watcher._pass()
    assert [r.exc_info[0] for r in caplog.records if r.exc_info] == [RuntimeError, KeyError]


def test_stopping_is_prompt_and_leaves_no_thread():
    rig = Rig()
    rig.watcher._every = 3600
    rig.watcher.start()
    started = time.time()
    rig.watcher.stop()
    assert time.time() - started < 2
    assert not [t for t in threading.enumerate() if t.name == "media"]
