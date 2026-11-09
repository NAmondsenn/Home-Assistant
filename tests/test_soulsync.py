"""
SoulSync Test
Checks how Nova asks SoulSync for a song or an album, and reads how it's getting on,
with a fake in place of SoulSync's API and its MCP server - answering in the shapes
the live check of 9 November recorded, with made-up names and ids - so nothing is
ever sent to the real one.
"""

import copy
import logging

import pytest
import requests

from voice_assistant import network
from voice_assistant.soulsync import (MCP_TIMEOUT, REQUEST_TIMEOUT, AlbumRequest, Progress, SoulSync, SoulSyncError,
                                      pick_album)

URL = "http://soulsync.local:8008"
RECORDED_ID = "5b0e7c2a-91d4-4f3e-8a6b-2c7d9e1f0a43"
RECORDED_REASON = "No one sharing it would send it"
RECORDED_TRACK_COUNT = 11


def _envelope(data, status=200):
    """An answer in SoulSync's REST envelope, as its API sends every one."""
    return status, {"success": True, "data": data, "error": None, "pagination": None}


def _refusal(status, code, message):
    return status, {"success": False, "data": None, "error": {"code": code, "message": message}, "pagination": None}


def _request(status, error=None, timed_out=False):
    """A song request's progress, as GET /api/v1/request/<id> describes it."""
    return _envelope({"request_id": RECORDED_ID, "query": "6ix9ine - Gooba", "status": status,
                      "download_id": None if status in ("queued", "searching") else "made-up-download",
                      "error": error, "completed_at": "2026-11-09T10:12:00Z" if status == "completed" else None,
                      "timed_out": timed_out})


SONG_ACCEPTED = _envelope({"request_id": RECORDED_ID, "status": "queued", "query": "6ix9ine - Gooba"}, 202)
SONG_WAITING = _request("downloading")
SONG_DONE = _request("completed")
SONG_FAILED = _request("failed", RECORDED_REASON)
SONG_NOT_FOUND = _request("not_found")
SONG_EXPIRED = _refusal(404, "NOT_FOUND", "Request not found")
NO_KEY = _refusal(401, "AUTH_REQUIRED", "An API key is needed")
# What POST /mcp answers while SoulSync's MCP server is switched off in its settings.
MCP_OFF = (404, {"error": "not found"})


def _candidate(title, artist, album_id=None, **more):
    """An album as find_album lists it."""
    return {"album_id": album_id or f"mb-{title}-{artist}".lower().replace(" ", "-"), "title": title,
            "artists": [artist], "release_date": "1977-02-04", "tracks": RECORDED_TRACK_COUNT, "type": "album",
            "secondary_types": [], "disambiguation": "", **more}


def _tool_result(content, text="made-up summary"):
    return {"content": [{"type": "text", "text": text}], "isError": False, "structuredContent": content}


ALBUM_FOUND = _tool_result({"albums": [_candidate("Rumours", "Fleetwood Mac", "mb-rumours"),
                                       _candidate("Rumours (Live)", "Fleetwood Mac", "mb-rumours-live")]})
ALBUM_REQUESTED = _tool_result({"album": "Rumours", "artist": "Fleetwood Mac", "tracks": RECORDED_TRACK_COUNT,
                                "queued": RECORDED_TRACK_COUNT, "note": "Queued on the wishlist"})
NO_ALBUM_FOUND = _tool_result({"albums": []})
WISHLIST_STARTED = _tool_result({"started": True})


def _tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        # A copy, as from a real server.
        return copy.deepcopy(self.body)


class FakeSoulSync:
    """
    Stands in for the requests.Session SoulSync is asked through: its REST API
    answers from the recorded shapes, and its MCP server each tool from a dict.
    """

    def __init__(self):
        self.headers = {}
        # Each REST POST as (address, body, headers), and each GET as (address, headers).
        self.posted = []
        self.got = []
        # Each JSON-RPC call posted to /mcp, and every request's timeout.
        self.mcp_calls = []
        self.timeouts = []
        self.request_answer = SONG_ACCEPTED
        # Answered in turn, one a GET.
        self.progress = [SONG_WAITING, SONG_DONE, SONG_FAILED]
        self.tools = {"find_album": ALBUM_FOUND, "request_album": ALBUM_REQUESTED,
                      "process_wishlist": WISHLIST_STARTED}
        # An answer to every MCP call instead of the tools', such as MCP_OFF.
        self.mcp_answer = None
        # An exception every request raises instead of being answered.
        self.fail = None

    def post(self, url, json=None, timeout=None):
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        if url == f"{URL}/mcp":
            self.mcp_calls.append(json)
            if self.mcp_answer:
                return FakeResponse(*self.mcp_answer)
            return FakeResponse(200, {"jsonrpc": "2.0", "id": json["id"],
                                      "result": self.tools[json["params"]["name"]]})
        self.posted.append((url, json, dict(self.headers)))
        return FakeResponse(*self.request_answer)

    def get(self, url, timeout=None):
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        self.got.append((url, dict(self.headers)))
        return FakeResponse(*self.progress.pop(0))


@pytest.fixture
def http(monkeypatch):
    """The fake, as the session SoulSync makes - with a network to ask it over."""
    fake = FakeSoulSync()
    monkeypatch.setattr(requests, "Session", lambda: fake)
    monkeypatch.setattr(network, "online", lambda: True)
    return fake


@pytest.fixture
def soulsync(http):
    return SoulSync(URL, "the-key")


def test_a_song_is_asked_for_as_artist_dash_title_with_the_key(soulsync, http):
    assert soulsync.request_song("Gooba", "6ix9ine") == RECORDED_ID
    url, body, headers = http.posted[0]
    assert url == "http://soulsync.local:8008/api/v1/request"
    # Nothing else: SoulSync's request has no field to steer away from a clean version,
    # and itself avoids live, remixed and radio-edit versions of a song.
    assert body == {"query": "6ix9ine - Gooba", "title": "Gooba", "artist": "6ix9ine"}
    assert headers["Authorization"] == "Bearer the-key"
    assert http.timeouts == [REQUEST_TIMEOUT]


def test_a_title_alone_is_asked_for_as_it_is(soulsync, http):
    soulsync.request_song("Gooba", None)
    assert http.posted[0][1] == {"query": "Gooba", "title": "Gooba"}


def test_a_songs_progress_is_waiting_done_or_failed_with_its_reason(soulsync, http):
    assert [soulsync.song_progress(RECORDED_ID) for _ in range(3)] == [
        Progress("waiting"), Progress("done"), Progress("failed", f"SoulSync said: {RECORDED_REASON}.")]
    assert [url for url, _ in http.got] == [f"{URL}/api/v1/request/{RECORDED_ID}"] * 3
    assert all(headers["Authorization"] == "Bearer the-key" for _, headers in http.got)


def test_every_state_before_the_end_is_waiting(soulsync, http):
    http.progress = [_request("queued"), _request("searching"), _request("downloading")]
    assert {soulsync.song_progress(RECORDED_ID).state for _ in range(3)} == {"waiting"}


def test_a_song_not_found_or_failed_without_saying_why_has_the_reason_there_is(soulsync, http):
    http.progress = [SONG_NOT_FOUND, _request("failed", ""), _request("failed", "Nobody had it.")]
    assert [soulsync.song_progress(RECORDED_ID) for _ in range(3)] == [
        Progress("failed", "SoulSync couldn't find it."), Progress("failed"),
        Progress("failed", "SoulSync said: Nobody had it.")]


def test_a_download_past_soulsyncs_own_time_limit_is_still_waited_for(soulsync, http):
    # SoulSync stops following it, but the download may still be running, so the watch
    # goes on to Nova's own time limit, with Navidrome to say whether it landed.
    http.progress = [_request("downloading", timed_out=True)]
    assert soulsync.song_progress(RECORDED_ID) == Progress("waiting")


def test_a_request_soulsync_has_cleared_away_is_still_waited_for(soulsync, http):
    # SoulSync forgets a request a while after it ends, so a 404 says nothing about
    # whether it came down - Navidrome decides.
    http.progress = [SONG_EXPIRED]
    assert soulsync.song_progress(RECORDED_ID) == Progress("waiting")


def test_an_album_is_found_then_requested_through_mcp(soulsync, http):
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    # process_wishlist starts the download now, not at the wishlist's next run.
    assert [call["params"]["name"] for call in http.mcp_calls] == ["find_album", "request_album",
                                                                    "process_wishlist"]
    assert [call["method"] for call in http.mcp_calls] == ["tools/call"] * 3
    assert http.mcp_calls[0]["params"]["arguments"] == {"query": "Fleetwood Mac - Rumours"}
    assert http.mcp_calls[1]["params"]["arguments"] == {"album_id": "mb-rumours"}
    assert http.headers["Authorization"] == "Bearer the-key"
    # find_album searches beyond the local network, so it's given longer.
    assert http.timeouts == [MCP_TIMEOUT] * 3
    # SoulSync gives an album's track count but not its titles, and no id to follow it by.
    assert request == AlbumRequest(None, [], RECORDED_TRACK_COUNT, RECORDED_TRACK_COUNT)


def test_an_album_partly_had_is_followed_by_the_tracks_soulsync_queued(soulsync, http):
    # Two of its eleven tracks already in the library, so SoulSync queues the other nine.
    http.tools["request_album"] = _tool_result({"album": "Rumours", "artist": "Fleetwood Mac",
                                                "tracks": RECORDED_TRACK_COUNT, "queued": 9,
                                                "note": "Queued on the wishlist"})
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    assert (request.track_count, request.queued) == (RECORDED_TRACK_COUNT, 9)

    # None queued is none, and an answer which doesn't say is taken as the whole album.
    http.tools["request_album"] = _tool_result({"album": "Rumours", "tracks": RECORDED_TRACK_COUNT, "queued": 0})
    assert soulsync.request_album("Rumours", "Fleetwood Mac").queued == 0
    http.tools["request_album"] = _tool_result({"album": "Rumours", "tracks": RECORDED_TRACK_COUNT})
    assert soulsync.request_album("Rumours", "Fleetwood Mac").queued == RECORDED_TRACK_COUNT


def test_an_album_title_alone_is_searched_for_as_it_is(soulsync, http):
    soulsync.request_album("Rumours", None)
    assert http.mcp_calls[0]["params"]["arguments"] == {"query": "Rumours"}


def test_an_album_is_only_waited_for_as_soulsync_gives_nothing_to_follow(soulsync, http):
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    calls = len(http.mcp_calls)
    assert soulsync.album_progress(request) == Progress("waiting")
    assert len(http.mcp_calls) == calls and http.got == []


def test_an_album_still_comes_down_when_the_wishlist_couldnt_be_started(soulsync, http, caplog):
    # It's on SoulSync's wishlist either way, so it comes down at the wishlist's next run.
    http.tools["process_wishlist"] = _tool_error("The wishlist is already being processed")
    assert soulsync.request_album("Rumours", "Fleetwood Mac").track_count == RECORDED_TRACK_COUNT
    assert "wishlist" in caplog.text


def test_the_album_picked_is_the_closest_and_never_a_clean_edition():
    candidates = [_candidate("Rumours (Clean)", "Fleetwood Mac"), _candidate("Rumours (Live)", "Fleetwood Mac"),
                  _candidate("Rumours", "Someone Else"), _candidate("Rumours", "Fleetwood Mac")]
    assert pick_album(candidates, "Rumours", "Fleetwood Mac") is candidates[3]
    assert pick_album(candidates, "Rumours Live", "Fleetwood Mac") is candidates[1]
    assert pick_album(candidates[:1], "Rumours", "Fleetwood Mac") is None


def test_an_edition_musicbrainz_calls_clean_or_live_isnt_the_album_asked_for():
    # MusicBrainz says which edition a release is in its disambiguation, and a live
    # album in its secondary types, rather than in the title.
    clean = _candidate("good kid, m.A.A.d city", "Kendrick Lamar", disambiguation="clean")
    live = _candidate("good kid, m.A.A.d city", "Kendrick Lamar", secondary_types=["Live"])
    deluxe = _candidate("good kid, m.A.A.d city", "Kendrick Lamar", disambiguation="deluxe edition")
    assert pick_album([clean, live, deluxe], "good kid maad city", "Kendrick Lamar") is deluxe
    assert pick_album([clean, live], "good kid maad city", "Kendrick Lamar") is None


@pytest.mark.parametrize("title, disambiguation", [
    ("Rumours", "Clean Edition"), ("Rumours", "clean, deluxe edition"), ("Rumours", "censored"),
    ("Rumours", "clean / Target exclusive"), ("Rumours", "clean (US)"), ("Rumours", "radio edit"),
    ("Rumours (Clean Edition)", ""), ("Rumours (Censored)", ""), ("Rumours [Edited]", ""),
    ("Rumours - Clean Version", "")])
def test_a_clean_edition_however_its_noted_is_never_picked(title, disambiguation):
    # A disambiguation is a free note on the edition, so a clean word anywhere in it
    # counts - unlike in a track's title, where "(feat. Clean Bandit)" is a credit.
    candidate = _candidate(title, "Fleetwood Mac", disambiguation=disambiguation)
    assert pick_album([candidate], "Rumours", "Fleetwood Mac") is None


def test_an_album_called_clean_is_its_title_not_an_edition():
    # Only a note on the edition counts, as a track's title does in navidrome.version_of.
    album = _candidate("Clean", "Soccer Mommy")
    assert pick_album([album], "Clean", "Soccer Mommy") is album


def test_the_edition_with_the_swearing_left_in_is_picked_over_one_without():
    clean_deluxe = _candidate("Rumours", "Fleetwood Mac", disambiguation="clean, deluxe edition")
    deluxe = _candidate("Rumours", "Fleetwood Mac", disambiguation="deluxe edition")
    censored = _candidate("Rumours", "Fleetwood Mac", disambiguation="censored")
    plain = _candidate("Rumours", "Fleetwood Mac")
    assert pick_album([clean_deluxe, deluxe], "Rumours", "Fleetwood Mac") is deluxe
    assert pick_album([censored, plain], "Rumours", "Fleetwood Mac") is plain


def test_an_edition_musicbrainz_calls_explicit_wins_among_equals():
    plain = _candidate("Rumours", "Fleetwood Mac")
    explicit = _candidate("Rumours", "Fleetwood Mac", disambiguation="explicit")
    assert pick_album([plain, explicit], "Rumours", "Fleetwood Mac") is explicit
    # Only as a whole word, and never over a closer match.
    inexplicit = _candidate("Rumours", "Fleetwood Mac", disambiguation="inexplicit")
    assert pick_album([plain, inexplicit], "Rumours", "Fleetwood Mac") is plain
    closer = _candidate("Rumours", "Fleetwood Mac")
    further = _candidate("Rumours Deluxe", "Fleetwood Mac", disambiguation="explicit")
    assert pick_album([further, closer], "Rumours", "Fleetwood Mac") is closer


def test_an_album_by_someone_else_entirely_isnt_picked():
    # Downloading it would put an album nobody asked for into the library for good.
    assert pick_album([_candidate("Rumours", "Someone Else")], "Rumours", "Fleetwood Mac") is None
    assert pick_album([_candidate("Rumours", "Someone Else")], "Rumours", None) is not None


def test_an_album_is_by_the_artist_only_with_every_word_of_their_name():
    # Sharing a word isn't enough: Mac Miller isn't Fleetwood Mac.
    assert pick_album([_candidate("Rumours", "Mac Miller")], "Rumours", "Fleetwood Mac") is None
    assert pick_album([_candidate("Tha Carter", "Lil Uzi Vert")], "Tha Carter", "Lil Wayne") is None
    featuring = _candidate("Rumours", "Fleetwood Mac", artists=["Someone Else", "Fleetwood Mac"])
    assert pick_album([featuring], "Rumours", "Fleetwood Mac") is featuring


def test_the_album_is_picked_over_a_single_of_the_same_name():
    # find_album "Michael Jackson - Thriller", live on 9 November: eight releases of
    # the album and two of the single, all called Thriller.
    single = _candidate("Thriller", "Michael Jackson", album_id="mb-single", type="single", tracks=2)
    album = _candidate("Thriller", "Michael Jackson", album_id="mb-album", type="album", tracks=9)
    candidates = [single] + [dict(album, album_id=f"mb-album-{n}") for n in range(8)] + [dict(single)]
    assert pick_album(candidates, "Thriller", "Michael Jackson") is candidates[1]
    # Anything else is still picked when it's all there is - an EP asked for by name.
    ep = _candidate("Paper Walls", "Made Up Band", type="ep", tracks=4)
    assert pick_album([ep], "Paper Walls", "Made Up Band") is ep


def test_no_album_found_says_so(soulsync, http):
    http.tools["find_album"] = NO_ALBUM_FOUND
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Nothing like it", None)
    assert error.value.reason == "SoulSync couldn't find it."
    assert [call["params"]["name"] for call in http.mcp_calls] == ["find_album"]


def test_only_clean_editions_found_is_nothing_found(soulsync, http):
    http.tools["find_album"] = _tool_result({"albums": [_candidate("Rumours (Clean)", "Fleetwood Mac")]})
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Rumours", "Fleetwood Mac")
    assert error.value.reason == "SoulSync couldn't find it."


def test_mcp_switched_off_says_where_to_turn_it_on(soulsync, http):
    http.mcp_answer = MCP_OFF
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Rumours", "Fleetwood Mac")
    assert error.value.reason == ("SoulSync's MCP is off, so it can't download albums. "
                                  "Turn it on in SoulSync's settings.")


def test_a_tool_soulsync_says_failed_gives_its_own_words(soulsync, http):
    http.tools["request_album"] = _tool_error("This needs an admin key")
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_album("Rumours", "Fleetwood Mac")
    assert error.value.reason == "SoulSync said: This needs an admin key."


def test_an_error_soulsync_answers_with_gives_its_own_words(soulsync, http):
    http.request_answer = _refusal(400, "BAD_REQUEST", "query is required")
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_song("Gooba", None)
    assert error.value.reason == "SoulSync said: query is required."


def test_a_refused_key_says_which_key_to_check(soulsync, http):
    # Answered with NO_KEY, as SoulSync answers without one.
    http.request_answer = http.mcp_answer = NO_KEY
    http.progress = [NO_KEY]
    for ask in (lambda: soulsync.request_song("Gooba", "6ix9ine"), lambda: soulsync.song_progress(RECORDED_ID),
                lambda: soulsync.request_album("Rumours", "Fleetwood Mac")):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == "SoulSync didn't accept its key, so SOULSYNC_API_KEY under Keys needs checking."


def test_soulsync_out_of_reach_is_said_plainly_and_the_key_never_logged(soulsync, http, caplog):
    # The fake raises requests.ConnectionError - with the key in it, to show its words aren't logged.
    http.fail = requests.ConnectionError("Authorization: Bearer the-key")
    for ask in (lambda: soulsync.request_song("Gooba", "6ix9ine"), lambda: soulsync.song_progress(RECORDED_ID),
                lambda: soulsync.request_album("Rumours", "Fleetwood Mac")):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == "SoulSync isn't answering." and "the-key" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_soulsync_too_slow_to_answer_isnt_answering(soulsync, http):
    http.fail = requests.Timeout("Read timed out")
    with pytest.raises(SoulSyncError) as error:
        soulsync.song_progress(RECORDED_ID)
    assert error.value.reason == "SoulSync isn't answering."


def test_an_answer_which_isnt_json_is_an_error_with_no_reason(soulsync, http, caplog):
    http.progress = [(502, None)]
    with pytest.raises(SoulSyncError) as error:
        soulsync.song_progress(RECORDED_ID)
    assert error.value.reason is None and "502" in caplog.text


def test_with_no_network_soulsync_isnt_asked_at_all(soulsync, http, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_song("Gooba", "6ix9ine")
    assert error.value.reason == "SoulSync isn't answering." and http.timeouts == []


def test_it_is_made_only_with_an_address_and_a_key(make_config):
    assert SoulSync.from_config(make_config(soulsync={"url": ""}), "the-key") is None
    assert SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008"}), None) is None
    assert SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008/"}), "the-key").url \
        == "http://soulsync.local:8008"


def test_half_set_up_is_logged_without_the_key_and_not_set_up_at_all_isnt(make_config, caplog):
    with caplog.at_level(logging.WARNING):
        SoulSync.from_config(make_config(soulsync={"url": ""}), "the-key")
        SoulSync.from_config(make_config(soulsync={"url": "http://soulsync.local:8008"}), "")
        SoulSync.from_config(make_config(), None)
    assert len(caplog.records) == 2 and "the-key" not in caplog.text
