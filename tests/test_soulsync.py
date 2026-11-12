"""
SoulSync Test
Checks how Nova asks SoulSync for a song or an album, with a fake in place of
SoulSync's API and its MCP server - answering in the shapes the live checks of 9
and 12 November recorded, with made-up ids - so nothing is ever sent to the real one.
"""

import copy
import logging

import pytest
import requests

from voice_assistant import network
from voice_assistant.soulsync import (MCP_TIMEOUT, REQUEST_TIMEOUT, SEARCH_TIMEOUT, AlbumRequest, Progress,
                                      SongRequest, SoulSync, SoulSyncError, pick_album, pick_track)

URL = "http://soulsync.local:8008"
RECORDED_TRACK_COUNT = 11

SEARCH = f"{URL}/api/v1/search/tracks"
WISHLIST = f"{URL}/api/v1/wishlist"
PROCESS = f"{URL}/api/v1/wishlist/process"


def _envelope(data, status=200):
    """An answer in SoulSync's REST envelope, as its API sends every one."""
    return status, {"success": True, "data": data, "error": None, "pagination": None}


def _refusal(status, code, message):
    return status, {"success": False, "data": None, "error": {"code": code, "message": message}, "pagination": None}


def _track(name, artist, album="Made Up Album", **more):
    """A track as POST /api/v1/search/tracks lists it from Deezer, with its album a plain string."""
    return {"id": f"dz-{name}-{artist}".lower().replace(" ", "-"), "name": name, "artists": [artist], "album": album,
            "duration_ms": 213000, "release_date": "1979-09-07", "image_url": None, "popularity": 0,
            "preview_url": None, **more}


def _found(*tracks):
    return _envelope({"source": "deezer", "tracks": list(tracks)})


GOOBA_FOUND = _track("GOOBA", "6ix9ine", "GOOBA")
SONG_FOUND = _found(GOOBA_FOUND)
WISHLIST_ADDED = _envelope({"created": True}, 201)
# What POST /api/v1/wishlist answers for a track already on it, which writes nothing.
ALREADY_WISHED_FOR = _refusal(409, "CONFLICT", "Track is already in the wishlist")
WISHLIST_PROCESSING = (200, {"message": "Wishlist processing started."})
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
    answers from the recorded shapes, by address, and its MCP server each tool from
    a dict. Everything Nova asks of SoulSync is a POST, so it has no GET to answer.
    """

    def __init__(self):
        self.headers = {}
        # Each REST POST as (address, body, headers).
        self.posted = []
        # Each JSON-RPC call posted to /mcp, and every request's timeout.
        self.mcp_calls = []
        self.timeouts = []
        self.answers = {SEARCH: SONG_FOUND, WISHLIST: WISHLIST_ADDED, PROCESS: WISHLIST_PROCESSING}
        self.tools = {"find_album": ALBUM_FOUND, "request_album": ALBUM_REQUESTED,
                      "process_wishlist": WISHLIST_STARTED}
        # An answer to every REST request, or every MCP call, instead of its own - such as NO_KEY.
        self.rest_answer = None
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
        self.posted.append((url, copy.deepcopy(json), dict(self.headers)))
        return FakeResponse(*(self.rest_answer or self.answers[url]))


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


# A song

def test_a_song_is_searched_for_as_artist_dash_title_with_the_key(soulsync, http):
    song = soulsync.find_song("Gooba", "6ix9ine")
    # Named as SoulSync has it, which is how the watch looks for it and says it.
    assert song == SongRequest("GOOBA", "6ix9ine", GOOBA_FOUND)
    assert http.posted == [(SEARCH, {"query": "6ix9ine - Gooba", "limit": 10}, {"Authorization": "Bearer the-key"})]
    # It searches Deezer, beyond the local network, so it's given longer.
    assert http.timeouts == [SEARCH_TIMEOUT]


def test_a_title_alone_is_searched_for_as_it_is(soulsync, http):
    soulsync.find_song("Gooba", None)
    assert http.posted[0][1] == {"query": "Gooba", "limit": 10}


def test_the_song_found_goes_on_the_wishlist_which_is_started(soulsync, http, caplog):
    studio = _track("M.E.", "Gary Numan", "The Pleasure Principle")
    http.answers[SEARCH] = _found(studio)

    soulsync.request_song(soulsync.find_song("M.E.", "Gary Numan"))

    # The wishlist, as SoulSync's own requests go, files what it gets into the library:
    # its /api/v1/request only downloads, live on 11 November, and nothing reached Navidrome.
    assert [url for url, _, _ in http.posted] == [SEARCH, WISHLIST, PROCESS]
    # Its album as a name: given Deezer's plain string, SoulSync tagged the album as
    # the song's own title, "M.E.", live on 12 November.
    assert http.posted[1][1] == {"track_data": {**studio, "album": {"name": "The Pleasure Principle"}},
                                 "source_type": "api", "failure_reason": "Asked for by voice through Nova"}
    assert all(headers == {"Authorization": "Bearer the-key"} for _, _, headers in http.posted)
    assert http.timeouts == [SEARCH_TIMEOUT, REQUEST_TIMEOUT, REQUEST_TIMEOUT]
    assert "couldn't be started" not in caplog.text


def test_an_album_given_as_more_than_a_name_goes_on_the_wishlist_as_it_is(soulsync, http):
    album = {"name": "GOOBA", "id": "made-up-album"}
    http.answers[SEARCH] = _found(_track("GOOBA", "6ix9ine", album))
    soulsync.request_song(soulsync.find_song("Gooba", "6ix9ine"))
    assert http.posted[1][1]["track_data"]["album"] == album


def test_a_song_already_on_the_wishlist_is_still_requested(soulsync, http, caplog):
    caplog.set_level(logging.INFO)
    http.answers[WISHLIST] = ALREADY_WISHED_FOR

    soulsync.request_song(soulsync.find_song("Gooba", "6ix9ine"))

    # Started all the same, so it comes down now rather than at the wishlist's next run.
    assert [url for url, _, _ in http.posted] == [SEARCH, WISHLIST, PROCESS]
    assert "already on SoulSync's wishlist" in caplog.text


def test_a_song_still_comes_down_when_the_wishlist_couldnt_be_started(soulsync, http, caplog):
    # It's on SoulSync's wishlist either way, so it comes down at the wishlist's next run.
    http.answers[PROCESS] = _refusal(500, "INTERNAL_ERROR", "The wishlist is already being processed")
    soulsync.request_song(soulsync.find_song("Gooba", "6ix9ine"))
    assert "SoulSync's wishlist couldn't be started, so the song waits for its next run" in caplog.text


def test_a_song_is_only_waited_for_as_navidrome_decides(soulsync, http):
    song = soulsync.find_song("Gooba", "6ix9ine")
    soulsync.request_song(song)
    posted = len(http.posted)
    assert soulsync.song_progress(song) == Progress("waiting")
    assert len(http.posted) == posted


@pytest.mark.parametrize("found", [
    _found(), _envelope({"source": "deezer"}),
    # Only a clean version, and only another artist's.
    _found(_track("Gooba (Clean)", "6ix9ine"), _track("Gooba", "Someone Else"))])
def test_no_song_found_that_will_do_says_so(soulsync, http, found):
    http.answers[SEARCH] = found
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason == "SoulSync couldn't find it."
    assert [url for url, _, _ in http.posted] == [SEARCH]


def test_a_misheard_artist_spelt_like_the_one_found_is_taken_for_exactly_the_title():
    # "Gunner - Endless", live on 12 November - speech to text's mishearing of Gunna -
    # found Gunna's "endless" first.
    endless = _track("endless", "Gunna")
    assert pick_track([endless], "Endless", "Gunner") is endless
    # Not another title, though, nor an artist only a little like it.
    assert pick_track([_track("Endless Love", "Gunna")], "Endless", "Gunner") is None
    assert pick_track([_track("Endless", "Guns N' Roses")], "Endless", "Gunner") is None
    assert pick_track([_track("Endless Love", "Gunna"), endless], "Endless", "Gunner") is endless


def test_the_named_artists_own_song_comes_before_a_close_spelling_and_the_exact_title_before_more_words():
    # A spelling close to the artist asked for is only a guess at a mishearing, so the
    # artist's own song is taken first, wherever SoulSync lists it.
    bac, mac = _track("Dreams", "Fleetwood Bac"), _track("Dreams", "Fleetwood Mac")
    assert pick_track([bac, mac], "Dreams", "Fleetwood Mac") is mac
    assert pick_track([bac], "Dreams", "Fleetwood Mac") is bac
    # And the title asked for exactly before one with more words to it.
    love, endless = _track("Endless Love", "Gunna"), _track("endless", "Gunna")
    assert pick_track([love, endless], "Endless", "Gunna") is endless
    assert pick_track([love], "Endless", "Gunna") is love


def test_the_studio_song_is_picked_over_live_and_demo_ones():
    # "Gary Numan - M.E.", live on 12 November, found it, then live and demo versions.
    live, demo = _track("M.E. (Live 79)", "Gary Numan"), _track("M.E. (Demo Version)", "Gary Numan")
    studio = _track("M.E.", "Gary Numan", "The Pleasure Principle")
    assert pick_track([live, demo, studio], "M.E.", "Gary Numan") is studio
    assert pick_track([live, demo], "M.E.", "Gary Numan") is None
    # Unless it's the version asked for.
    assert pick_track([demo, studio, live], "M.E. live", "Gary Numan") is live


def test_a_song_from_a_live_album_is_a_live_version():
    # Called plain "M.E." on an album whose name says it's live.
    live = _track("M.E.", "Gary Numan", "Living Ornaments '79 (Live)")
    unplugged = _track("M.E.", "Gary Numan", {"name": "M.E. - Unplugged"})
    studio = _track("M.E.", "Gary Numan", "The Pleasure Principle")
    assert pick_track([live, unplugged, studio], "M.E.", "Gary Numan") is studio
    assert pick_track([live, unplugged], "M.E.", "Gary Numan") is None


def test_a_mix_isnt_picked_unless_its_the_one_asked_for():
    extended, club = _track("Blue Monday (Extended Mix)", "New Order"), _track("Blue Monday - Club Mix", "New Order")
    single = _track("Blue Monday", "New Order")
    assert pick_track([extended, club, single], "Blue Monday", "New Order") is single
    assert pick_track([club, extended, single], "Blue Monday extended mix", "New Order") is extended
    # A remix by name is still a remix, which "remix" asks for.
    remix = _track("Gooba (Made Up Remix)", "6ix9ine")
    assert pick_track([remix], "Gooba remix", "6ix9ine") is remix


def test_a_credit_is_never_read_as_a_version():
    # Little Mix ends in "mix", and is no mix of the song.
    with_them = _track("Kiss My (Uh Oh) (with Little Mix)", "Anne-Marie")
    assert pick_track([with_them], "Kiss My (Uh Oh)", "Anne-Marie") is with_them
    for credit in ("Gooba (feat. Live Wire)", "Gooba [ft. The Remix Crew]", "Gooba - featuring Acoustic Ladyland"):
        candidate = _track(credit, "6ix9ine")
        assert pick_track([candidate], "Gooba", "6ix9ine") is candidate


def test_an_original_mix_is_the_song_itself():
    # On an electronic release, the original mix is the original.
    original = _track("Strobe (Original Mix)", "deadmau5")
    assert pick_track([_track("Strobe (Club Mix)", "deadmau5"), original], "Strobe", "deadmau5") is original
    version = _track("Strobe - Original Version", "deadmau5")
    assert pick_track([version], "Strobe", "deadmau5") is version


@pytest.mark.parametrize("name", ["Gooba (Remix)", "Gooba - Acoustic", "Gooba [Instrumental]", "Gooba (Sped Up)",
                                  "Gooba (Slowed)", "Gooba (Karaoke Version)", "Gooba - Live at Wembley",
                                  "Gooba (Extended Mix)", "Gooba - Club Mix", "Gooba (Megamix)", "Gooba (Unplugged)"])
def test_another_version_of_a_song_isnt_picked(name):
    assert pick_track([_track(name, "6ix9ine")], "Gooba", "6ix9ine") is None


@pytest.mark.parametrize("name, album", [
    ("Gooba (Clean)", "GOOBA"), ("Gooba - Radio Edit", "GOOBA"), ("Gooba [Edited]", "GOOBA"),
    ("Gooba (Censored Version)", "GOOBA"), ("Gooba", "GOOBA (Clean)"), ("Gooba", {"name": "GOOBA - Clean Version"})])
def test_a_clean_version_of_a_song_is_never_picked(name, album):
    assert pick_track([_track(name, "6ix9ine", album)], "Gooba", "6ix9ine") is None


def test_a_song_called_clean_is_its_title_not_a_version():
    # Only a bracketed or dashed part saying so makes it one, as navidrome.is_clean has it.
    clean = _track("Clean", "Taylor Swift", "1989")
    assert pick_track([clean], "Clean", "Taylor Swift") is clean
    credit = _track("Gooba (feat. Clean Bandit)", "6ix9ine")
    assert pick_track([credit], "Gooba", "6ix9ine") is credit


def test_a_song_is_by_the_artist_only_with_every_word_of_their_name():
    # Mac Miller isn't Fleetwood Mac, nor spelt enough like it to be a mishearing.
    assert pick_track([_track("Dreams", "Mac Miller")], "Dreams", "Fleetwood Mac") is None
    featuring = _track("Dreams", "Someone Else", artists=["Someone Else", "Fleetwood Mac"])
    assert pick_track([featuring], "Dreams", "Fleetwood Mac") is featuring
    together = _track("No Church in the Wild", "Jay-Z", artists=["Jay-Z", "Kanye West"])
    assert pick_track([together], "No Church in the Wild", "Jay-Z and Kanye West") is together


def test_the_first_song_which_will_do_is_picked_with_every_word_of_the_title():
    # SoulSync lists its best guesses first.
    first, second = _track("Gooba", "6ix9ine"), _track("Gooba", "Someone Else")
    assert pick_track([first, second], "Gooba", None) is first
    assert pick_track([second, first], "Gooba", None) is second
    longer = _track("Gooba Gooba", "6ix9ine")
    assert pick_track([longer], "Gooba", "6ix9ine") is longer
    # With an artist asked for, their song with exactly the title asked for comes first.
    assert pick_track([longer, first], "Gooba", "6ix9ine") is first
    # With only a title, SoulSync's order decides: an exact title further down may be
    # a cover, or another act's song.
    assert pick_track([longer, second], "Gooba", None) is longer
    assert pick_track([_track("Goo", "6ix9ine")], "Gooba", "6ix9ine") is None


# An album

def test_an_album_is_found_then_requested_through_mcp(soulsync, http):
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    # process_wishlist starts the download now, not at the wishlist's next run.
    assert [call["params"]["name"] for call in http.mcp_calls] == ["find_album", "request_album",
                                                                    "process_wishlist"]
    assert [call["method"] for call in http.mcp_calls] == ["tools/call"] * 3
    # As many editions as SoulSync gives, for pick_album to choose past the clean and deluxe ones.
    assert http.mcp_calls[0]["params"]["arguments"] == {"query": "Fleetwood Mac - Rumours", "limit": 25}
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
    assert http.mcp_calls[0]["params"]["arguments"] == {"query": "Rumours", "limit": 25}


def test_an_album_is_only_waited_for_as_soulsync_gives_nothing_to_follow(soulsync, http):
    request = soulsync.request_album("Rumours", "Fleetwood Mac")
    calls = len(http.mcp_calls)
    assert soulsync.album_progress(request) == Progress("waiting")
    assert len(http.mcp_calls) == calls and http.posted == []


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
    ("Rumours - Clean Version", ""), ("Rumours \u2013 Clean Edition", ""), ("Rumours \u2014 Censored", "")])
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


def test_an_artist_named_with_and_is_the_same_as_with_an_ampersand_or_as_two():
    # Claude writes "and" where the release has "&" or "+", or lists each artist on their own.
    together = _candidate("Watch the Throne", "Jay-Z", artists=["Jay-Z", "Kanye West"])
    assert pick_album([together], "Watch the Throne", "Jay-Z and Kanye West") is together
    assert pick_album([together], "Watch the Throne", "Kanye West and Jay-Z") is together
    ampersand = _candidate("Bookends", "Simon & Garfunkel")
    assert pick_album([ampersand], "Bookends", "Simon and Garfunkel") is ampersand
    plus = _candidate("Lungs", "Florence + the Machine")
    assert pick_album([plus], "Lungs", "Florence and the Machine") is plus
    # Still every word: Mac Miller isn't Fleetwood Mac, nor Jay-Z alone Jay-Z and Kanye West.
    assert pick_album([_candidate("Rumours", "Mac Miller")], "Rumours", "Fleetwood Mac") is None
    assert pick_album([_candidate("Watch the Throne", "Jay-Z")], "Watch the Throne", "Jay-Z and Kanye West") is None


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


# When it goes wrong

def test_an_error_soulsync_answers_with_gives_its_own_words(soulsync, http):
    song = soulsync.find_song("Gooba", "6ix9ine")
    http.answers[SEARCH] = _refusal(400, "BAD_REQUEST", "query is required")
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", None)
    assert error.value.reason == "SoulSync said: query is required."

    http.answers[WISHLIST] = _refusal(400, "BAD_REQUEST", "track_data is required")
    with pytest.raises(SoulSyncError) as error:
        soulsync.request_song(song)
    assert error.value.reason == "SoulSync said: track_data is required."


@pytest.mark.parametrize("refusal", [
    NO_KEY,
    # A key it knows but won't let request anything - one which can only read - is the same key to check.
    _refusal(403, "FORBIDDEN", "This key can't do that")])
def test_a_refused_key_says_which_key_to_check(soulsync, http, refusal):
    song = soulsync.find_song("Gooba", "6ix9ine")
    http.rest_answer = http.mcp_answer = refusal
    for ask in (lambda: soulsync.find_song("Gooba", "6ix9ine"), lambda: soulsync.request_song(song),
                lambda: soulsync.request_album("Rumours", "Fleetwood Mac")):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == "SoulSync didn't accept its key, so SOULSYNC_API_KEY under Keys needs checking."


def test_soulsync_out_of_reach_is_said_plainly_and_the_key_never_logged(soulsync, http, caplog):
    # The fake raises requests.ConnectionError - with the key in it, to show its words aren't logged.
    http.fail = requests.ConnectionError("Authorization: Bearer the-key")
    song = SongRequest("GOOBA", "6ix9ine", GOOBA_FOUND)
    for ask in (lambda: soulsync.find_song("Gooba", "6ix9ine"), lambda: soulsync.request_song(song),
                lambda: soulsync.request_album("Rumours", "Fleetwood Mac")):
        with pytest.raises(SoulSyncError) as error:
            ask()
        assert error.value.reason == "SoulSync isn't answering." and "the-key" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_soulsync_too_slow_to_answer_isnt_answering(soulsync, http):
    http.fail = requests.Timeout("Read timed out")
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason == "SoulSync isn't answering."


def test_an_answer_which_isnt_json_is_an_error_with_no_reason(soulsync, http, caplog):
    http.answers[SEARCH] = (502, None)
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason is None and "502" in caplog.text


def test_with_no_network_soulsync_isnt_asked_at_all(soulsync, http, monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)
    with pytest.raises(SoulSyncError) as error:
        soulsync.find_song("Gooba", "6ix9ine")
    assert error.value.reason == "SoulSync isn't answering." and http.timeouts == []


# Set up or not

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
