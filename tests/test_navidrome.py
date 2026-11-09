"""
Navidrome Test
Checks how music is found on Navidrome and how the queue plays, using a fake in
place of Navidrome's API and of the speaker, so nothing is sent or played. The one
test which decodes real audio needs ffmpeg, and is skipped anywhere it isn't.
"""

import copy
import hashlib
import io
import logging
import shutil
import threading
import time
import wave

import pytest
import requests

np = pytest.importorskip("numpy")

from voice_assistant import network
from voice_assistant import volume as volume_module
from voice_assistant.navidrome import (CLEAN_VERSIONS, VERSIONS, NavidromeLibrary, NavidromePlayer, best, is_clean,
                                       likeness, marked_clean, split_by, version_of)
from voice_assistant.volume import VolumeControl


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeNavidrome:
    """Stands in for Navidrome's Subsonic API, answering each endpoint from a dict."""

    def __init__(self, answers):
        self.answers = answers
        self.headers = {}
        self.requests = []

    def get(self, url, params=None, timeout=None, stream=False):
        endpoint = url.rsplit("/rest/", 1)[1]
        self.requests.append((endpoint, dict(params or {})))
        answer = self.answers.get(endpoint, {})
        if isinstance(answer, Exception):
            raise answer
        # A copy, as from a real server, since the player shuffles what it's given.
        body = copy.deepcopy(answer(params) if callable(answer) else answer)
        return FakeResponse({"subsonic-response": {"status": "ok", **body}})


class FakePlayback:
    """One song 'playing' - finished when the test says, or when stopped."""

    def __init__(self, song, volume, paused):
        self.song = song
        self.volume = volume
        self.paused = paused
        self.position = 0
        self.duration = 200.0
        self.stopped = False
        self._done = threading.Event()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def stop(self):
        self.stopped = True
        self._done.set()

    def finish(self):
        self._done.set()


class FakeSpeaker:
    sample_rate = 48000

    def __init__(self):
        self.started = []

    def play_stream(self, song, volume=1.0, kind="reply", paused=False, most_ahead=None):
        self.started.append(FakePlayback(song, volume, paused))
        return self.started[-1]


def _until(check, timeout=2.0):
    """Waits for the player's own thread to catch up."""
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


SONGS = [{"id": str(n), "title": f"Song {n}", "artist": "Band"} for n in range(1, 5)]


@pytest.fixture
def make_library():
    def factory(answers=None):
        library = NavidromeLibrary("http://navidrome.local:4533/", "nova", "secret")
        library._session = FakeNavidrome(answers or {})
        return library

    return factory


@pytest.fixture
def make_player(tmp_path, monkeypatch):
    monkeypatch.setattr(volume_module, "VOLUMES_FILE", str(tmp_path / "volumes.json"))

    def factory(answers=None):
        speaker = FakeSpeaker()
        player = NavidromePlayer("http://navidrome.local:4533/", "nova", "secret", speaker, VolumeControl())
        player._session = FakeNavidrome(answers or {})
        # The song itself stands in for its audio, so the fake speaker knows which it is.
        player._decode = lambda song: song
        player.speaker_started = speaker.started
        return player

    return factory


def _playing(player):
    _until(lambda: player.speaker_started and not player.speaker_started[-1].stopped)
    return player.speaker_started[-1]


# Finding music

def test_names_are_matched_however_theyre_written():
    assert likeness("Beyoncé - Halo", "beyonce halo") == 3
    assert likeness("The Chronic (Remastered)", "the chronic") == 2
    assert likeness("Something Else", "the chronic") == 0
    assert split_by("Gooba by 6ix9ine") == ("Gooba", "6ix9ine")
    assert split_by("Stand by Me") == ("Stand", "Me")
    assert split_by("Drake") == ("Drake", None)


def test_the_best_match_wins_and_the_artist_breaks_a_tie():
    found = [{"title": "Gooba (Remix)", "artist": "Someone"}, {"title": "Gooba", "artist": "Other"},
             {"title": "Gooba", "artist": "6ix9ine"}]
    assert best(found, "title", "Gooba", "6ix9ine") == found[2]
    assert best(found, "title", "Nothing like it", None) is None


def test_the_password_is_never_sent_only_a_salted_hash(make_player):
    player = make_player()
    first, second = player._sign_in(), player._sign_in()
    assert "secret" not in first.values()
    assert first["t"] == hashlib.md5(("secret" + first["s"]).encode()).hexdigest()
    assert first["s"] != second["s"]


def test_a_song_is_played_then_more_by_the_artist(make_player):
    player = make_player({"search3": lambda params: {"searchResult3": {"song": [
        {"id": "9", "title": "Gooba (Remix)", "artist": "Someone"},
        {"id": "1", "title": "Gooba", "artist": "6ix9ine"},
        {"id": "2", "title": "Fefe", "artist": "6ix9ine"}]}}})
    result = player.play("Gooba by 6ix9ine", search_type="track")

    assert result == {"success": True, "message": "Playing Gooba by 6ix9ine"}
    assert _playing(player).song["id"] == "1"
    assert player._session.requests[0][1]["query"] == "Gooba 6ix9ine"


def test_an_album_plays_in_order(make_player):
    player = make_player({
        "search3": {"searchResult3": {"album": [{"id": "a1", "name": "The Chronic", "artist": "Dr. Dre"}]}},
        "getAlbum": {"album": {"song": SONGS}},
    })
    assert player.play("The Chronic by Dr. Dre", search_type="album")["message"] == "Playing The Chronic by Dr. Dre"
    assert [song["id"] for song in player._queue] == ["1", "2", "3", "4"]


def test_a_playlist_is_found_by_part_of_its_name(make_player):
    player = make_player({
        "getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "Chill"}, {"id": "p2", "name": "Workout Mix"}]}},
        "getPlaylist": lambda params: {"playlist": {"entry": SONGS if params["id"] == "p2" else []}},
    })
    assert player.play_playlist("workout") == {"success": True, "message": "Playing Workout Mix"}
    assert sorted(song["id"] for song in player._queue) == ["1", "2", "3", "4"]


def test_liked_songs_are_the_starred_ones(make_player):
    player = make_player({"getStarred2": {"starred2": {"song": SONGS[:2]}}})
    assert player.play_playlist("liked songs")["message"] == "Playing your liked songs"
    assert player.play()["message"] in ("Resumed playback", "Playing your liked songs")


def test_something_not_in_the_library_says_so(make_player):
    player = make_player({"search3": {"searchResult3": {}}})
    assert player.play("Gooba by 6ix9ine") == {"success": False, "missing": True,
                                               "message": "I couldn't find Gooba by 6ix9ine on Navidrome."}


def test_a_wrong_password_says_which_setting_to_check(make_player):
    player = make_player()
    player._session.answers["search3"] = {}
    player._session.get = lambda *args, **kwargs: FakeResponse({"subsonic-response": {
        "status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})
    assert "NAVIDROME_PASSWORD" in player.play("Drake")["message"]


def test_a_failed_request_never_logs_the_token(make_player, caplog):
    # A requests error names the address it was asking, which holds the token.
    player = make_player({"search3": requests.ConnectionError("http://nd/rest/search3?t=abc123token")})
    assert player.play("Drake")["message"] == "I can't reach Navidrome right now."
    assert "abc123token" not in caplog.text


def test_with_no_network_it_isnt_asked_at_all(make_player, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    player = make_player()
    assert player.play("Drake")["message"] == "I can't reach Navidrome right now."
    assert player._session.requests == []


def test_something_missing_is_marked_so_a_download_can_be_offered(make_player):
    assert make_player().play("Nothing like it", search_type="track") == {
        "success": False, "message": "I couldn't find Nothing like it on Navidrome.", "missing": True}


def test_with_nothing_asked_for_nothing_is_missing(make_player):
    # Nothing was asked for, so there's nothing to download.
    result = make_player({"getStarred2": {}, "getRandomSongs": {}}).play()
    assert result == {"success": False, "message": "I couldn't find anything to play on Navidrome."}


# Finding what's there, for downloads

# What a search for "Gooba" finds: the song as explicit and as clean, the same title by
# someone else, and a near miss.
GOOBA = [
    {"id": "1", "title": "Gooba", "artist": "6ix9ine", "album": "Day69", "explicitStatus": "explicit"},
    {"id": "2", "title": "Gooba (Clean)", "artist": "6ix9ine", "album": "Day69", "explicitStatus": "clean"},
    {"id": "3", "title": "Gooba", "artist": "Other", "album": "Covers"},
    {"id": "4", "title": "Goo", "artist": "6ix9ine", "album": "Day69"},
]


def _ids(items):
    return [item["id"] for item in items]


def _songs_found(songs):
    return {"search3": {"searchResult3": {"song": songs}}}


def test_a_version_is_named_in_brackets_or_after_a_dash():
    assert version_of("Gooba (Clean)") == "clean"
    assert version_of("Gooba [Radio Edit]") == "radio edit"
    assert version_of("Gooba (feat. Someone) [Slowed]") == "slowed"
    assert version_of("Gooba - Live at Wembley") == "live"
    assert version_of("Gooba - Sped Up") == "sped up"
    assert version_of("Day69 [Edited Version]") == "edited"
    assert version_of("Gooba") is None and version_of("") is None


def test_a_version_is_only_where_a_version_goes_and_a_whole_word():
    # A title is its title: only a bracketed part or one after " - " names a version.
    assert version_of("Clean") is None and version_of("Edited Highlights") is None
    assert version_of("Live and Let Die") is None and version_of("Gooba-Live") is None
    # Whole words: "Alive" isn't "live", nor "Unedited" "edited".
    assert version_of("Gooba (Alive)") is None and version_of("Gooba (Unedited)") is None
    # "Radio" and "Edit" in two brackets are no radio edit.
    assert version_of("Gooba (Radio) (Edit)") is None


def test_a_clean_version_says_so_and_nothing_more():
    # "Version" or "edit" after the word is still the clean version.
    for name in ("Gooba (Clean)", "Gooba (Clean Version)", "Gooba (Clean Edit)", "Gooba [Edited Version]",
                 "Gooba (Radio Edit)", "Gooba - Clean", "Gooba - Radio Edit", "Gooba (Live - Radio Edit)"):
        assert marked_clean(name), name
    assert version_of("Gooba (Live - Radio Edit)") == "radio edit"
    assert version_of("Gooba (Clean Radio Edit)") == "radio edit"


def test_a_credit_to_an_artist_called_clean_isnt_a_clean_version():
    # Clean Bandit are credited on other people's records; the song is not a clean one for that.
    assert version_of("Gooba (feat. Clean Bandit)") is None
    assert version_of("Gooba - feat. Clean Bandit") is None
    assert version_of("Gooba (Edited by Clean Bandit)") is None
    assert version_of("Gooba (Clean Bandit Remix)") == "remix"
    assert version_of("Gooba [Clean Bandit Remix]") == "remix"
    assert version_of("Gooba - Clean Bandit Remix") == "remix"
    # Whichever of its words comes last still counts: this one is a clean version, by Clean Bandit.
    assert version_of("Gooba (Clean Bandit - Clean)") == "clean"


def test_a_credit_to_an_artist_called_clean_is_no_clean_track():
    assert not is_clean({"title": "Gooba (feat. Clean Bandit)"})
    assert not is_clean({"title": "Gooba (Clean Bandit Remix)"})
    assert not is_clean({"title": "Gooba - Clean Bandit Remix"})
    assert not is_clean({"title": "Gooba", "album": "Hits [feat. Clean Bandit]"})
    assert is_clean({"title": "Gooba (feat. Clean Bandit)", "explicitStatus": "clean"})
    assert not is_clean({"title": "Gooba (Clean)", "album": "Hits", "explicitStatus": "explicit"})


def test_only_some_versions_are_clean_ones():
    assert CLEAN_VERSIONS == ("clean", "edited", "radio edit")
    assert all(version in VERSIONS for version in CLEAN_VERSIONS)
    assert marked_clean("Gooba (Clean)") and marked_clean("Gooba - Radio Edit")
    assert not marked_clean("Gooba (Live)") and not marked_clean("Gooba - Remix") and not marked_clean("Gooba")


def test_clean_is_navidromes_mark_or_a_title_that_says_so():
    assert is_clean({"title": "Gooba", "explicitStatus": "clean"})
    assert not is_clean({"title": "Gooba (Clean)", "explicitStatus": "explicit"})
    assert is_clean({"title": "Gooba (Clean)"}) and is_clean({"title": "Gooba - Radio Edit", "explicitStatus": ""})
    assert is_clean({"title": "Gooba", "album": "Day69 [Edited Version]"})
    assert not is_clean({"title": "Gooba", "album": "Day69"})
    # A song called Clean is only its title.
    assert not is_clean({"title": "Clean", "album": "1989"}) and not is_clean({"title": "Edited Highlights"})


def test_a_live_or_remixed_track_isnt_a_clean_one_and_no_mark_is_no_mark():
    assert not is_clean({"title": "Gooba (Live)"}) and not is_clean({"title": "Gooba", "album": "Day69 (Remixes)"})
    # Missing fields and a mark Navidrome might one day add are no mark.
    assert not is_clean({}) and not is_clean({"title": None, "album": None, "explicitStatus": None})
    assert is_clean({"title": "Gooba (Clean)", "explicitStatus": "unheard-of"})


def test_songs_are_found_by_every_word_of_the_title_and_the_artist(make_library):
    library = make_library(_songs_found(GOOBA))
    assert _ids(library.find_songs("Gooba", "6ix9ine")) == ["1", "2"]
    assert _ids(library.find_songs("Gooba", None)) == ["1", "2", "3"]
    assert library.find_songs("Nothing like it", None) == []


def test_songs_are_searched_for_as_they_are_when_played(make_library):
    library = make_library(_songs_found(GOOBA))
    library.find_songs("Gooba", "6ix9ine")
    library.find_songs("Gooba", None)
    assert [params["query"] for _, params in library._session.requests] == ["Gooba 6ix9ine", "Gooba"]
    assert all(endpoint == "search3" and params["songCount"] == 20 for endpoint, params in library._session.requests)


def test_the_title_alone_is_searched_when_title_and_artist_find_nothing(make_library):
    # The library spells the artist differently from how it was said, so the two together find nothing.
    library = make_library({"search3": lambda params: {"searchResult3": {
        "song": GOOBA[:1] if params["query"] == "Gooba" else []}}})
    assert _ids(library.find_songs("Gooba", "6ix9ine")) == ["1"]
    assert [params["query"] for _, params in library._session.requests] == ["Gooba 6ix9ine", "Gooba"]


def test_the_title_alone_is_searched_when_only_others_come_of_title_and_artist(make_library):
    library = make_library({"search3": lambda params: {"searchResult3": {
        "song": GOOBA[2:3] if params["query"] != "Gooba" else GOOBA[:1]}}})
    assert _ids(library.find_songs("Gooba", "6ix9ine")) == ["1"]


def test_a_title_alone_is_searched_once(make_library):
    library = make_library(_songs_found([]))
    assert library.find_songs("Gooba", None) == []
    assert len(library._session.requests) == 1


def test_a_live_version_isnt_the_song_asked_for(make_library):
    library = make_library(_songs_found([
        {"id": "5", "title": "Dreams (Live)", "artist": "Fleetwood Mac", "album": "The Dance"},
        {"id": "6", "title": "Dreams - Remix", "artist": "Fleetwood Mac", "album": "Remixes"},
    ]))
    assert library.find_songs("Dreams", "Fleetwood Mac") == []
    assert _ids(library.find_songs("Dreams Live", "Fleetwood Mac")) == ["5"]
    assert _ids(library.find_songs("Dreams Remix", "Fleetwood Mac")) == ["6"]


def test_a_clean_version_is_always_one_of_the_songs_found(make_library):
    library = make_library(_songs_found([
        {"id": "7", "title": "Dreams - Radio Edit", "artist": "Fleetwood Mac"},
        {"id": "8", "title": "Dreams [Edited]", "artist": "Fleetwood Mac"},
        {"id": "9", "title": "Dreams (Acoustic)", "artist": "Fleetwood Mac"},
    ]))
    assert _ids(library.find_songs("Dreams", "Fleetwood Mac")) == ["7", "8"]


def test_a_remix_by_an_artist_called_clean_isnt_the_song_asked_for(make_library):
    library = make_library(_songs_found([
        {"id": "12", "title": "Dreams (feat. Clean Bandit)", "artist": "Fleetwood Mac"},
        {"id": "13", "title": "Dreams (Clean Bandit Remix)", "artist": "Fleetwood Mac"},
        {"id": "14", "title": "Dreams - Clean Bandit Remix", "artist": "Fleetwood Mac"},
    ]))
    # The credit is on the song itself, but isn't a clean version of it; the remixes aren't the song.
    found = library.find_songs("Dreams", "Fleetwood Mac")
    assert _ids(found) == ["12"] and not is_clean(found[0])
    assert _ids(library.find_songs("Dreams Remix", "Fleetwood Mac")) == ["13", "14"]
    assert not any(is_clean(song) for song in library.find_songs("Dreams Remix", "Fleetwood Mac"))


def test_a_real_title_with_clean_or_edited_in_it_is_its_title(make_library):
    library = make_library(_songs_found([
        {"id": "10", "title": "Clean", "artist": "Taylor Swift", "album": "1989"},
        {"id": "11", "title": "Edited Highlights", "artist": "Taylor Swift", "album": "Highlights"},
    ]))
    found = library.find_songs("Clean", "Taylor Swift")
    assert _ids(found) == ["10"] and not is_clean(found[0])
    found = library.find_songs("Edited Highlights", None)
    assert _ids(found) == ["11"] and not is_clean(found[0])


def test_a_song_isnt_found_when_navidrome_cant_be_asked(make_library):
    library = make_library({"search3": requests.ConnectionError("http://nd/rest/search3?t=abc123token")})
    with pytest.raises(requests.RequestException):
        library.find_songs("Gooba", "6ix9ine")


# Rumours: the album, the live one, one by someone else, and an edited one.
ALBUMS = {
    "a1": ("Rumours", "Fleetwood Mac", [{"id": "t1", "title": "Second Hand News"}, {"id": "t2", "title": "Dreams"},
                                         {"id": "t3", "title": "Never Going Back Again"}]),
    "a2": ("Rumours (Live)", "Fleetwood Mac", [{"id": "t4", "title": "Dreams"}]),
    "a3": ("Rumours", "Other", [{"id": "t5", "title": "Dreams"}]),
    "a4": ("Rumours [Edited Version]", "Fleetwood Mac", [{"id": "t6", "title": "Dreams"}]),
}


def _albums_found(*ids):
    return {
        "search3": {"searchResult3": {"album": [{"id": id, "name": ALBUMS[id][0], "artist": ALBUMS[id][1]}
                                                for id in ids]}},
        "getAlbum": lambda params: {"album": {"song": ALBUMS[params["id"]][2]}},
    }


def test_an_albums_tracks_are_every_track_of_each_matching_album(make_library):
    library = make_library(_albums_found("a1", "a2", "a3"))
    tracks = library.find_album_tracks("Rumours", "Fleetwood Mac")
    assert _ids(tracks) == ["t1", "t2", "t3"] and {track["album"] for track in tracks} == {"Rumours"}
    assert _ids(library.find_album_tracks("Rumours", None)) == ["t1", "t2", "t3", "t5"]


def test_each_albums_tracks_carry_its_id_and_artist(make_library):
    # So two albums called Rumours are told apart, and named by the album's artist, not a track's.
    library = make_library(_albums_found("a1", "a3"))
    tracks = library.find_album_tracks("Rumours", None)
    assert [(track["albumId"], track["albumArtist"]) for track in tracks] == (
        [("a1", "Fleetwood Mac")] * 3 + [("a3", "Other")])


def test_a_live_album_isnt_the_album_asked_for(make_library):
    library = make_library(_albums_found("a1", "a2"))
    assert _ids(library.find_album_tracks("Rumours", "Fleetwood Mac")) == ["t1", "t2", "t3"]
    tracks = library.find_album_tracks("Rumours Live", "Fleetwood Mac")
    assert _ids(tracks) == ["t4"] and {track["album"] for track in tracks} == {"Rumours (Live)"}


def test_an_edited_album_is_one_of_the_albums_found_and_its_tracks_say_so(make_library):
    library = make_library(_albums_found("a1", "a4"))
    tracks = library.find_album_tracks("Rumours", "Fleetwood Mac")
    assert _ids(tracks) == ["t1", "t2", "t3", "t6"]
    assert [is_clean(track) for track in tracks] == [False, False, False, True]


def test_albums_are_searched_for_as_they_are_when_played(make_library):
    library = make_library(_albums_found("a1"))
    library.find_album_tracks("Rumours", "Fleetwood Mac")
    endpoint, params = library._session.requests[0]
    assert (endpoint, params["query"], params["albumCount"], params["songCount"]) == ("search3", "Rumours Fleetwood Mac", 10, 0)


def test_the_title_alone_is_searched_when_title_and_artist_find_no_album(make_library):
    library = make_library({**_albums_found("a1"), "search3": lambda params: {"searchResult3": {
        "album": [{"id": "a1", "name": "Rumours", "artist": "Fleetwood Mac"}] if params["query"] == "Rumours" else []}}})
    assert _ids(library.find_album_tracks("Rumours", "Fleetwood Mac")) == ["t1", "t2", "t3"]
    assert [params["query"] for endpoint, params in library._session.requests if endpoint == "search3"] == [
        "Rumours Fleetwood Mac", "Rumours"]


def test_no_album_is_no_tracks(make_library):
    library = make_library({"search3": {"searchResult3": {}}})
    assert library.find_album_tracks("Rumours", "Fleetwood Mac") == []
    assert [endpoint for endpoint, _ in library._session.requests] == ["search3", "search3"]


def test_a_scan_is_asked_for_and_a_refusal_is_logged(make_library, caplog):
    assert make_library({"startScan": {"scanStatus": {"scanning": True}}}).start_scan() is True
    refused = make_library({"startScan": {"status": "failed", "error": {"code": 50, "message": "not authorized"}}})
    assert refused.start_scan() is False and "admin" in caplog.text


def test_a_refused_scan_is_logged_once_as_a_warning_about_the_admin(make_library, caplog):
    library = make_library({"startScan": {"status": "failed", "error": {"code": 50, "message": "not authorized"}}})
    with caplog.at_level(logging.DEBUG):
        assert library.start_scan() is False
    assert [(record.levelno, "admin" in record.getMessage()) for record in caplog.records] == [(logging.WARNING, True)]


def test_a_scan_which_cant_be_asked_for_is_false_and_never_logs_the_token(make_library, caplog):
    library = make_library({"startScan": requests.ConnectionError("http://nd/rest/startScan?t=abc123token")})
    assert library.start_scan() is False
    assert "abc123token" not in caplog.text and "admin" not in caplog.text
    assert "ConnectionError" in caplog.text
    library = make_library({"startScan": {"status": "failed", "error": {"code": 40, "message": "Wrong password"}}})
    assert library.start_scan() is False and "admin" not in caplog.text


def test_a_scan_which_wasnt_answered_with_json_is_false(make_library):
    class NotJson(FakeResponse):
        def json(self):
            raise ValueError("no json")

    library = make_library()
    library._session.get = lambda *args, **kwargs: NotJson(None)
    assert library.start_scan() is False


def test_a_scan_asked_for_with_no_network_isnt_sent(make_library, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    library = make_library()
    assert library.start_scan() is False and library._session.requests == []


def test_the_scan_is_the_subsonic_one(make_library):
    library = make_library({"startScan": {"scanStatus": {"scanning": True}}})
    library.start_scan()
    assert [endpoint for endpoint, _ in library._session.requests] == ["startScan"]


def test_the_player_is_a_library_which_signs_in_as_it_did(make_player):
    player = make_player()
    assert isinstance(player, NavidromeLibrary)
    assert player.url == "http://navidrome.local:4533" and player.username == "nova"
    signed = player._sign_in()
    assert signed["t"] == hashlib.md5(("secret" + signed["s"]).encode()).hexdigest()


# Playing the queue

def test_the_next_song_plays_when_one_finishes(make_player):
    player = make_player()
    player._start(SONGS)
    _playing(player).finish()
    _until(lambda: len(player.speaker_started) == 2)
    assert player.speaker_started[1].song["id"] == "2"


def test_skipping_moves_on_once_not_twice(make_player):
    player = make_player()
    player._start(SONGS)
    first = _playing(player)
    assert player.skip()["silent"]
    _until(lambda: len(player.speaker_started) == 2)
    time.sleep(0.05)
    assert first.stopped and [p.song["id"] for p in player.speaker_started] == ["1", "2"]


def test_going_back_soon_after_a_song_starts_goes_to_the_one_before(make_player):
    player = make_player()
    player._start(SONGS)
    _playing(player)
    player.skip()
    _until(lambda: len(player.speaker_started) == 2)
    assert player.previous()["message"] == "Previous track"
    _until(lambda: len(player.speaker_started) == 3)
    assert player.speaker_started[2].song["id"] == "1"

    # Further in, it starts the song again instead.
    player.speaker_started[2].position = 10 * FakeSpeaker.sample_rate
    assert player.previous()["message"] == "Starting it again"


def test_repeating_a_track_plays_it_again(make_player):
    player = make_player()
    player._start(SONGS)
    player.repeat("track")
    _playing(player).finish()
    _until(lambda: len(player.speaker_started) == 2)
    assert player.speaker_started[1].song["id"] == "1"


def test_the_end_of_the_queue_is_the_end(make_player):
    player = make_player()
    player._start(SONGS[:1])
    _playing(player).finish()
    time.sleep(0.1)
    assert len(player.speaker_started) == 1
    assert player.current_track() == {"success": False, "message": "Nothing playing"}


def test_songs_which_wont_play_stop_the_queue(make_player):
    player = make_player()
    player.repeat("all")
    player._start(SONGS)
    for _ in range(3):
        playback = _playing(player)
        playback.duration = 0
        playback.finish()
        _until(lambda: playback is not player.speaker_started[-1] or not player._queue)
    _until(lambda: not player._queue)


# Making way for the assistant

def test_the_music_pauses_while_the_assistant_talks_and_comes_back(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)

    player.pause_for_speech()
    assert playing.paused and player.was_playing()
    player.resume_after_speech()
    assert not playing.paused


def test_a_song_asked_for_waits_until_the_reply_is_over(make_player):
    # Spotify starts the new song straight away, under the reply saying what it is.
    player = make_player({"getStarred2": {"starred2": {"song": SONGS}}})
    player.pause_for_speech()
    assert not player.was_playing()
    player.play_playlist("liked songs")
    assert _playing(player).paused
    player.resume_after_speech()
    assert not player.speaker_started[-1].paused


def test_paused_by_request_it_stays_paused(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    player.pause_for_speech()
    player.pause()
    player.cancel_resume()
    player.resume_after_speech()
    assert playing.paused


def test_the_volume_changes_the_song_playing(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    assert player.set_volume(20)["success"]
    assert playing.volume == pytest.approx(0.2) and player.get_volume() == 20


# Real audio

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_song_is_decoded_to_stereo_at_the_speakers_rate(make_player, monkeypatch):
    sound = io.BytesIO()
    with wave.open(sound, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(22050)
        file.writeframes((np.sin(np.arange(22050) / 10) * 10000).astype("<i2").tobytes())

    class Download:
        def iter_content(self, size):
            data = sound.getvalue()
            for start in range(0, len(data), size):
                yield data[start:start + size]

        def close(self):
            pass

    player = make_player()
    monkeypatch.setattr(player, "_open_song", lambda song_id: Download())
    pieces = list(NavidromePlayer._decode(player, {"id": "1", "title": "Tone"}))
    audio = np.concatenate(pieces)
    assert audio.shape[1] == 2 and abs(len(audio) - 48000) < 2000
    assert np.allclose(audio[:, 0], audio[:, 1])


def test_music_stopped_before_talking_stays_stopped(make_player):
    # Stopped, then the next thing said to the assistant started it again, since
    # every conversation ended by un-pausing it.
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    player.pause()
    player.pause_for_speech()
    player.resume_after_speech()
    assert playing.paused and not player.was_playing()


def test_resume_asked_for_while_talking_carries_on_after(make_player):
    player = make_player()
    player._start(SONGS)
    playing = _playing(player)
    player.pause()
    player.pause_for_speech()
    player.play()
    assert playing.paused
    player.resume_after_speech()
    assert not playing.paused


def test_sharing_only_songs_isnt_a_playlist_match(make_player):
    # "Light songs" - Whisper's "liked songs" - played "Car Songs".
    player = make_player({"getPlaylists": {"playlists": {"playlist": [{"id": "p1", "name": "Car Songs"}]}},
                          "getPlaylist": {"playlist": {"entry": SONGS}},
                          "getSongsByGenre": {"songsByGenre": {"song": []}}})
    assert player.play("rainy day songs", search_type="playlist")["success"] is False
    assert player.play_playlist("car songs")["message"] == "Playing Car Songs"


# For the settings page

RECORD = [{"id": "1", "title": "Song 1", "artist": "Band", "album": "Record", "duration": 200, "coverArt": "al-1"},
          {"id": "2", "title": "Song 2", "artist": "Band", "album": "Record", "duration": 180, "coverArt": "al-1"}]


class FakeImage:
    headers = {"Content-Type": "image/jpeg"}
    content = b"a cover"

    def raise_for_status(self):
        pass


def test_the_page_is_told_whats_playing_and_how_far_through(make_player):
    player = make_player()
    assert player.now_playing() is None
    player._start(RECORD)
    _playing(player).position = 48000 * 30
    assert player.now_playing() == {"track": "Song 1", "artist": "Band", "album": "Record", "playing": True,
                                    "position": 30.0, "duration": 200, "cover": "al-1"}

    # Paused while the assistant talks, it's still playing, as it carries on after -
    # but not once it's been asked to pause.
    player.pause_for_speech()
    assert player.now_playing()["playing"]
    player.pause()
    player.cancel_resume()
    player.resume_after_speech()
    assert not player.now_playing()["playing"]


def test_a_cover_is_fetched_once_for_a_whole_album(make_player):
    player = make_player()
    player._start(RECORD)
    _playing(player)
    asked = []
    player._session.get = lambda url, params=None, timeout=None: asked.append(params) or FakeImage()

    assert player.cover() == (b"a cover", "image/jpeg")
    player.skip()
    assert player.cover() == (b"a cover", "image/jpeg")
    assert len(asked) == 1 and asked[0]["id"] == "al-1" and "p" not in asked[0]


def test_the_page_is_offered_the_playlists_by_name(make_player):
    player = make_player({"getPlaylists": {"playlists": {"playlist": [{"id": "p2", "name": "workout"},
                                                                      {"id": "p1", "name": "Chill"}]}}})
    assert player.playlists() == ["Chill", "workout"]
    player._session.answers["getPlaylists"] = requests.ConnectionError("down")
    assert player.playlists() == []
