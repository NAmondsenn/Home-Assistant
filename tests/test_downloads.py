"""
Downloads Test
Checks the watch on a download - asked of SoulSync, said to be requested, then
followed until Navidrome has it or its time is up - with fakes in place of SoulSync
and Navidrome, and a fake clock which only moves on when the watch waits, so nothing
real is asked and no test waits out a poll.
"""

import functools
import logging
import threading

import pytest
import requests

from voice_assistant.actions import Action
from voice_assistant.downloads import NOT_SET_UP, POLL_EVERY, SCAN_AFTER, Downloads, Wanted, _bare_title
from voice_assistant.navidrome import NavidromeLibrary
from voice_assistant.soulsync import MCP_OFF, NOT_ANSWERING, NOT_FOUND, AlbumRequest, Progress, SoulSync, SoulSyncError

GOOBA = {"id": "1", "title": "Gooba", "artist": "6ix9ine", "album": "Gooba", "explicitStatus": "explicit"}
GOOBA_CLEAN = {"id": "2", "title": "Gooba (Clean)", "artist": "6ix9ine", "album": "Gooba", "explicitStatus": ""}
GOOBA_WANTED = Wanted("Gooba", "6ix9ine", "track")
RUMOURS_WANTED = Wanted("Rumours", "Fleetwood Mac", "album")
RUMOURS_TITLES = ["Dreams", "Go Your Own Way", "The Chain"]

WAITING, DONE = Progress("waiting"), Progress("done")

GOOBA_REQUESTED = ("kitchen", "Gooba by 6ix9ine requested.")
GOOBA_DOWNLOADED = ("kitchen", "Gooba by 6ix9ine has been downloaded.")


def _track(number, title, clean=False, album="Rumours", album_id=None):
    """
    One of Rumours' tracks as Navidrome describes it - marked clean, or unmarked as most
    are - with its album's name, id and artist, as find_album_tracks gives them.
    """
    return {"id": f"r{number}", "title": title, "artist": "Fleetwood Mac", "album": album,
            "albumId": album_id or album, "albumArtist": "Fleetwood Mac", "explicitStatus": "clean" if clean else ""}


DREAMS, GO_YOUR_OWN_WAY, THE_CHAIN = (_track(number, title) for number, title in enumerate(RUMOURS_TITLES, 1))
# A bonus track of a deluxe edition, which isn't one SoulSync named.
SILVER_SPRINGS = _track(4, "Silver Springs")
RUMOURS_ALL = ["Second Hand News", "Dreams", "Never Going Back Again", "Don't Stop", "Go Your Own Way", "Songbird",
               "The Chain", "You Make Loving Fun", "I Don't Want to Know", "Oh Daddy", "Gold Dust Woman"]


def _answer(answers):
    """
    A fake's next answer - the last one again once they run out. One which is an
    error is raised, and one which is a function is called for the answer.
    """
    answer = answers.pop(0) if len(answers) > 1 else answers[0]
    if callable(answer):
        answer = answer()
    if isinstance(answer, Exception):
        raise answer
    return answer


class FakeTime:
    """The clock the watch reads, which moves on only when the watch waits."""

    def __init__(self):
        self.time = 0.0
        # When each message was said, by this clock.
        self.said_at = []
        # Things to do once the clock reaches a time, as (time, what).
        self.later = []

    def now(self):
        return self.time

    def wait(self, seconds):
        self.time += seconds
        for when, then in [item for item in self.later if item[0] <= self.time]:
            self.later.remove((when, then))
            then()
        return False

    @property
    def given_up_at(self):
        return self.said_at[-1]


class FakeSoulSync:
    """Stands in for SoulSync: records what it's asked for, and answers how it's getting on from a list."""

    def __init__(self, progress=(WAITING,), album=None, fail=None):
        self.progress = list(progress)
        self.album = album or AlbumRequest(None, [], 3, 3)
        self.fail = fail
        self.requested = []

    def _request(self, title, artist, answer):
        self.requested.append((title, artist))
        if self.fail:
            raise self.fail
        return answer

    def request_song(self, title, artist):
        return self._request(title, artist, "made-up-request")

    def song_progress(self, request_id):
        assert request_id == "made-up-request"
        return _answer(self.progress)

    def request_album(self, title, artist):
        return self._request(title, artist, self.album)

    def album_progress(self, request):
        assert request is self.album
        return _answer(self.progress)


class FakeLibrary:
    """
    Stands in for Navidrome: each find_* call is answered by the next of its list -
    the first being what it had before the request - and each scan is noted by the
    fake clock's time.
    """

    def __init__(self, clock, songs=([],), tracks=([],), scans_allowed=True):
        self.clock = clock
        self.songs = list(songs)
        self.tracks = list(tracks)
        self.scans_allowed = scans_allowed
        self.scans = []

    def find_songs(self, title, artist):
        assert (title, artist) == ("Gooba", "6ix9ine")
        return list(_answer(self.songs))

    def find_album_tracks(self, title, artist):
        assert (title, artist) == ("Rumours", "Fleetwood Mac")
        return list(_answer(self.tracks))

    def start_scan(self):
        self.scans.append(self.clock.now())
        return self.scans_allowed


@pytest.fixture
def clock():
    return FakeTime()


@pytest.fixture
def said():
    """What the watch said, as (satellite, message)."""
    return []


@pytest.fixture
def config(make_config):
    return make_config(music={"song_download_minutes": 10, "album_download_minutes": 30})


@pytest.fixture
def set_up(make_config, monkeypatch):
    """Settings with SoulSync and Navidrome set up - made up - so a download can be asked for."""
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    return make_config(music={"song_download_minutes": 10, "album_download_minutes": 30},
                       soulsync={"url": "http://soulsync.local:8008"},
                       navidrome={"url": "http://navidrome.local:4533", "username": "nova"})


@pytest.fixture
def watching(config, clock, said):
    """Makes Downloads which asks the fakes given, on the fake clock - with the settings given, if any."""
    def make(soulsync, library, settings=None):
        def say(satellite, message):
            said.append((satellite, message))
            clock.said_at.append(clock.time)

        return Downloads(settings or config, say, soulsync=lambda: soulsync, library=lambda: library, now=clock.now,
                         wait=clock.wait)

    return make


def _watches():
    return [thread for thread in threading.enumerate() if thread.name == "download"]


def _all_ended():
    """Waits a few seconds at most for every watch's thread to end, and says whether they all did."""
    watches = _watches()
    for thread in watches:
        thread.join(timeout=5)
    return not any(thread.is_alive() for thread in watches)


@pytest.fixture
def running(config, clock, said):
    """
    Makes Downloads whose watches run on threads of their own and wait between polls
    as Nova's do, on the stop event - so each waits until stop, which ends every one
    at once. Anything a test leaves running is stopped afterwards.
    """
    made = []

    def make(soulsync, library, settings=None):
        downloads = Downloads(settings or config, lambda satellite, message: said.append((satellite, message)),
                              soulsync=lambda: soulsync, library=lambda: library, now=clock.now)
        made.append(downloads)
        return downloads

    yield make
    for downloads in made:
        downloads.stop()
    _all_ended()


# What's asked for

def test_a_query_is_a_song_unless_its_an_album():
    assert Wanted.from_query("Rumours by Fleetwood Mac", "album") == RUMOURS_WANTED
    assert Wanted.from_query("Gooba by 6ix9ine", "track") == GOOBA_WANTED
    assert Wanted.from_query("Gooba", None) == Wanted("Gooba", None, "track")


def test_its_named_as_asked_for_and_told_apart_however_its_spelt():
    assert GOOBA_WANTED.name == "Gooba by 6ix9ine"
    assert Wanted("Gooba", None, "track").name == "Gooba"
    assert Wanted("gooba", "6IX9INE", "track").key == GOOBA_WANTED.key
    assert Wanted("Gooba", "6ix9ine", "album").key != GOOBA_WANTED.key
    assert Wanted("Gooba", None, "track").key != GOOBA_WANTED.key


# A song

def test_a_song_is_requested_then_said_downloaded_once_navidrome_has_it(watching, clock, said):
    # SoulSync: waiting, then done; Navidrome: nothing before, nothing, then Gooba.
    soulsync = FakeSoulSync([WAITING, DONE])
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[], [], [GOOBA]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "Gooba by 6ix9ine has been downloaded."
    assert said == [("kitchen", "Gooba by 6ix9ine requested."), ("kitchen", "Gooba by 6ix9ine has been downloaded.")]
    assert soulsync.requested == [("Gooba", "6ix9ine")]


def test_done_in_soulsync_isnt_downloaded_until_navidrome_has_it_and_it_scans_once(watching, clock, said):
    # SoulSync done at once; Navidrome has nothing for three minutes, then Gooba.
    library = FakeLibrary(clock, songs=[[]] * (1 + 180 // POLL_EVERY) + [[GOOBA]])
    downloads = watching(FakeSoulSync([DONE]), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert clock.said_at[-1] == 180 + POLL_EVERY
    assert library.scans == [SCAN_AFTER + POLL_EVERY]  # when, by the fake clock; once only


def test_a_song_that_came_down_clean_is_never_said_downloaded(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    downloads = watching(FakeSoulSync([DONE]), FakeLibrary(clock, songs=[[], [GOOBA_CLEAN]]))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "Gooba by 6ix9ine came down as a clean version."
    assert said == [GOOBA_REQUESTED, ("kitchen", "Gooba by 6ix9ine came down as a clean version.")]
    assert "Gooba (Clean)" in caplog.text  # logged, so the user can replace it


def test_an_old_clean_copy_isnt_taken_for_the_download(watching, clock, said):
    # Navidrome: [GOOBA_CLEAN] before, the same at the first poll, then [GOOBA_CLEAN, GOOBA].
    library = FakeLibrary(clock, songs=[[GOOBA_CLEAN], [GOOBA_CLEAN], [GOOBA_CLEAN, GOOBA]])
    downloads = watching(FakeSoulSync([WAITING, DONE]), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert clock.said_at[-1] == 2 * POLL_EVERY


def test_a_song_is_given_up_on_after_ten_minutes(watching, clock, said):
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync([WAITING]), library)

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "I couldn't download Gooba by 6ix9ine."
    assert said[-1] == ("kitchen", "I couldn't download Gooba by 6ix9ine.")
    assert 600 <= clock.given_up_at < 600 + POLL_EVERY
    # SoulSync never saying it's done, Navidrome is asked to scan once, in time for the last look.
    assert library.scans == [600 - SCAN_AFTER - POLL_EVERY]


def test_a_song_soulsync_never_says_is_done_is_found_by_the_scan_before_its_time_is_up(watching, clock, said):
    # Left "downloading" by SoulSync, and only in Navidrome once it has scanned.
    library = FakeLibrary(clock)
    library.songs = [[], lambda: [GOOBA] if library.scans else []]
    downloads = watching(FakeSoulSync([WAITING]), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert clock.said_at[-1] == 600 - SCAN_AFTER  # the poll after the scan
    assert library.scans == [600 - SCAN_AFTER - POLL_EVERY]


def test_there_is_only_ever_one_scan(watching, config, clock, said):
    # Scanned as its ten minutes ran out, the limit is raised to an hour, and SoulSync
    # then says it's done - with Navidrome still without it.
    clock.later.append((530, lambda: config.apply({("music", "song_download_minutes"): 60})))
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync([WAITING] * (600 // POLL_EVERY) + [DONE]), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert library.scans == [600 - SCAN_AFTER - POLL_EVERY]
    assert 3600 <= clock.given_up_at < 3600 + POLL_EVERY


def test_the_time_limits_are_read_as_they_are_now(watching, config, clock, said):
    # music.song_download_minutes changed from 10 to 2 a minute in.
    clock.later.append((60, lambda: config.apply({("music", "song_download_minutes"): 2})))
    downloads = watching(FakeSoulSync([WAITING]), FakeLibrary(clock))

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "I couldn't download Gooba by 6ix9ine.")
    assert 120 <= clock.given_up_at < 120 + POLL_EVERY


# An album

def test_an_album_is_downloaded_once_every_track_soulsync_named_is_in(watching, clock, said):
    # AlbumRequest(id, ["Dreams", "Go Your Own Way", "The Chain"], 3, 3); Navidrome's grow by one a
    # poll, with a bonus track SoulSync didn't name among them, which isn't one of the three.
    soulsync = FakeSoulSync(album=AlbumRequest("made-up-album", RUMOURS_TITLES, 3, 3))
    library = FakeLibrary(clock, tracks=[[], [DREAMS, SILVER_SPRINGS], [DREAMS, SILVER_SPRINGS, GO_YOUR_OWN_WAY],
                                         [DREAMS, SILVER_SPRINGS, GO_YOUR_OWN_WAY, THE_CHAIN]])
    downloads = watching(soulsync, library)

    assert downloads._watch(RUMOURS_WANTED, "kitchen") == "Rumours by Fleetwood Mac has been downloaded."
    assert said == [("kitchen", "Rumours by Fleetwood Mac requested."),
                    ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")]
    assert clock.said_at[-1] == 3 * POLL_EVERY
    assert soulsync.requested == [("Rumours", "Fleetwood Mac")]


def test_an_album_soulsync_only_counted_is_downloaded_once_that_many_new_tracks_are_in(watching, clock, said):
    # As SoulSync answers: the album's track count, how many it queued, and no titles. An
    # old clean copy of one track was in Navidrome before, and is neither one of the
    # three nor clean.
    old = _track(0, "Dreams", clean=True, album="Rumours (Clean)")
    library = FakeLibrary(clock, tracks=[[old], [old, DREAMS], [old, DREAMS, GO_YOUR_OWN_WAY],
                                         [old, DREAMS, GO_YOUR_OWN_WAY, THE_CHAIN]])
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 3, 3)), library)

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")
    assert clock.said_at[-1] == 3 * POLL_EVERY


def test_a_part_owned_album_is_downloaded_once_what_soulsync_queued_has_landed(watching, clock, said):
    # Two of its eleven tracks were in Navidrome already, so SoulSync queued nine.
    tracks = [_track(number, title) for number, title in enumerate(RUMOURS_ALL, 1)]
    had = [track for track in tracks if track["title"] in ("Dreams", "The Chain")]
    queued = [track for track in tracks if track not in had]
    library = FakeLibrary(clock, tracks=[had, had + queued[:5], had + queued])
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 9)), library)

    assert downloads._watch(RUMOURS_WANTED, "kitchen") == "Rumours by Fleetwood Mac has been downloaded."
    assert clock.said_at[-1] == 2 * POLL_EVERY  # long before its time limit

    # With only five of the nine in by its time limit, it's out of the nine.
    library = FakeLibrary(clock, tracks=[had, had + queued[:5]])
    watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 9)), library)._watch(RUMOURS_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "Only 5 of the 9 tracks of Rumours by Fleetwood Mac came down.")


def test_an_album_soulsync_queued_none_of_is_said_at_once(watching, clock, said):
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 11, 0)), FakeLibrary(clock))
    message = ("I couldn't download Rumours by Fleetwood Mac. SoulSync says it already has every track, "
               "or is already getting them.")

    assert downloads._watch(RUMOURS_WANTED, "kitchen") == message
    assert said == [("kitchen", message)]
    assert clock.time == 0  # not waited for at all


def test_a_clean_copy_and_the_explicit_one_of_a_track_count_once(watching, clock, said):
    # Dreams twice, once clean, and Go Your Own Way - but not The Chain.
    landed = [DREAMS, _track(5, "Dreams (Clean)"), GO_YOUR_OWN_WAY]
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 3, 3)), FakeLibrary(clock, tracks=[[], landed]))

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Only 2 of the 3 tracks of Rumours by Fleetwood Mac came down.")
    assert 1800 <= clock.given_up_at < 1800 + POLL_EVERY


def test_only_a_part_saying_clean_is_taken_off_a_title():
    # The clean bracket goes first, so the dashed part before it is read for what it is:
    # a live take or a remaster stays a track of its own, rather than merging with Dreams.
    assert _bare_title("Dreams - Live at the Forum (Clean)") == _bare_title("Dreams - Live at the Forum")
    assert _bare_title("Dreams - 2004 Remaster (Clean)") == _bare_title("Dreams - 2004 Remaster")
    assert _bare_title("Dreams - Live at the Forum (Clean)") != "dreams"
    # A clean part after an en dash, or before a credit, is still one.
    assert _bare_title("Dreams \u2013 Radio Edit") == _bare_title("Dreams - Radio Edit (feat. X)") == "dreams"


def test_a_live_take_of_a_track_is_a_track_of_its_own(watching, clock, said):
    # A deluxe edition's live Dreams beside the studio one.
    landed = [DREAMS, _track(5, "Dreams (Live)"), THE_CHAIN]
    downloads = watching(FakeSoulSync(album=AlbumRequest(None, [], 3, 3)), FakeLibrary(clock, tracks=[[], landed]))

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")
    assert clock.said_at[-1] == POLL_EVERY

    # A radio edit, though, is a clean copy of Dreams, so it and Dreams are one track.
    landed = [DREAMS, _track(6, "Dreams - Radio Edit"), _track(5, "Dreams (Live)"), THE_CHAIN]
    watching(FakeSoulSync(album=AlbumRequest(None, [], 4, 4)),
             FakeLibrary(clock, tracks=[[], landed]))._watch(RUMOURS_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "Only 3 of the 4 tracks of Rumours by Fleetwood Mac came down.")


def test_an_album_with_clean_tracks_says_how_many(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    two_clean = [DREAMS, _track(2, "Go Your Own Way", clean=True), _track(3, "The Chain", clean=True)]
    watching(FakeSoulSync(), FakeLibrary(clock, tracks=[[], two_clean]))._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded, but 2 of its tracks came down "
                                   "as clean versions.")
    assert "Go Your Own Way" in caplog.text and "The Chain" in caplog.text  # logged, to be replaced

    one_clean = [DREAMS, GO_YOUR_OWN_WAY, _track(3, "The Chain", clean=True)]
    watching(FakeSoulSync(), FakeLibrary(clock, tracks=[[], one_clean]))._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded, but one of its tracks came down "
                                   "as a clean version.")


@pytest.mark.parametrize("album", [AlbumRequest("made-up-album", RUMOURS_TITLES, 3, 3),
                                   AlbumRequest(None, [], 3, 3)], ids=["named", "counted"])
def test_a_track_which_came_down_both_clean_and_not_isnt_counted_clean(watching, clock, said, album):
    # Two copies of The Chain landed, the clean one first - the album has the explicit one.
    both = [DREAMS, GO_YOUR_OWN_WAY, _track(5, "The Chain", clean=True), THE_CHAIN]
    downloads = watching(FakeSoulSync(album=album), FakeLibrary(clock, tracks=[[], both]))

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Rumours by Fleetwood Mac has been downloaded.")


def test_part_of_an_album_by_the_time_limit_says_how_much(watching, clock, said):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock, tracks=[[], [DREAMS], [DREAMS, GO_YOUR_OWN_WAY]]))

    only_two = "Only 2 of the 3 tracks of Rumours by Fleetwood Mac came down."
    assert downloads._watch(RUMOURS_WANTED, "kitchen") == only_two
    assert said[-1] == ("kitchen", only_two)
    assert 1800 <= clock.given_up_at < 1800 + POLL_EVERY

    # And with none of it by then:
    watching(FakeSoulSync(), FakeLibrary(clock))._watch(RUMOURS_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "I couldn't download Rumours by Fleetwood Mac.")


def test_an_album_with_nothing_new_by_then_is_scanned_once_before_its_time_is_up(watching, clock, said):
    # SoulSync never says an album is done, as it gives nothing to follow it by.
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync(), library)

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert library.scans == [1800 - SCAN_AFTER - POLL_EVERY]
    assert said[-1] == ("kitchen", "I couldn't download Rumours by Fleetwood Mac.")


def test_an_album_soulsync_gives_up_on_says_how_much_came_down(watching, clock, said):
    downloads = watching(FakeSoulSync([WAITING, Progress("failed", NOT_FOUND)]),
                         FakeLibrary(clock, tracks=[[], [DREAMS]]))

    downloads._watch(RUMOURS_WANTED, "kitchen")

    assert said[-1] == ("kitchen", "Only 1 of the 3 tracks of Rumours by Fleetwood Mac came down.")
    assert clock.said_at[-1] == 2 * POLL_EVERY

    # And with none of it by then, why:
    watching(FakeSoulSync([Progress("failed", NOT_FOUND)]), FakeLibrary(clock))._watch(RUMOURS_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "I couldn't download Rumours by Fleetwood Mac. SoulSync couldn't find it.")


# When it goes wrong

def test_a_failure_is_said_with_soulsyncs_reason(watching, clock, said):
    # request_song raises SoulSyncError("SoulSync isn't answering.").
    soulsync = FakeSoulSync(fail=SoulSyncError(NOT_ANSWERING))
    assert (watching(soulsync, FakeLibrary(clock))._watch(GOOBA_WANTED, "kitchen")
            == "I couldn't download Gooba by 6ix9ine. SoulSync isn't answering.")
    assert said == [("kitchen", "I couldn't download Gooba by 6ix9ine. SoulSync isn't answering.")]

    # Apart, its progress "failed" - with a reason, and without one.
    said.clear()
    watching(FakeSoulSync([WAITING, Progress("failed", NOT_FOUND)]), FakeLibrary(clock))._watch(GOOBA_WANTED, "kitchen")
    assert said == [GOOBA_REQUESTED, ("kitchen", "I couldn't download Gooba by 6ix9ine. SoulSync couldn't find it.")]

    watching(FakeSoulSync([Progress("failed")]), FakeLibrary(clock))._watch(GOOBA_WANTED, "kitchen")
    assert said[-1] == ("kitchen", "I couldn't download Gooba by 6ix9ine.")

    # And an album SoulSync can't be asked for.
    watching(FakeSoulSync(fail=SoulSyncError(MCP_OFF)), FakeLibrary(clock))._watch(RUMOURS_WANTED, "bedroom")
    assert said[-1] == ("bedroom", f"I couldn't download Rumours by Fleetwood Mac. {MCP_OFF}")


def test_a_poll_that_fails_is_logged_and_the_watch_carries_on(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    # song_progress raises SoulSyncError once and find_songs requests.ConnectionError once, then all is well.
    soulsync = FakeSoulSync([SoulSyncError(NOT_ANSWERING), WAITING, DONE])
    error = requests.ConnectionError("http://navidrome.local:4533/rest/search3?t=made-up-token")
    library = FakeLibrary(clock, songs=[[], error, [], [GOOBA]])

    downloads = watching(soulsync, library)
    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert "Couldn't ask SoulSync how Gooba by 6ix9ine is getting on" in caplog.text
    assert "Navidrome request failed: ConnectionError" in caplog.text
    # Only the kind of error, never the request, which holds the signed-in token.
    assert "made-up-token" not in caplog.text


def test_a_scan_navidrome_refuses_is_tried_once_and_the_watch_carries_on(watching, clock, said):
    # Refused because Nova's user isn't an admin - which the library logs.
    library = FakeLibrary(clock, songs=[[]] * 8 + [[GOOBA]], scans_allowed=False)
    downloads = watching(FakeSoulSync([DONE]), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert library.scans == [SCAN_AFTER + POLL_EVERY]
    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]


def test_without_navidrome_before_the_request_anything_found_counts(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    library = FakeLibrary(clock, songs=[requests.ConnectionError(), [GOOBA]])
    downloads = watching(FakeSoulSync([DONE]), library)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert "Couldn't see what Navidrome had of Gooba by 6ix9ine" in caplog.text


def test_something_unforeseen_still_ends_in_an_answer(watching, clock, said, caplog):
    caplog.set_level(logging.INFO)
    downloads = watching(FakeSoulSync([RuntimeError("made-up bug")]), FakeLibrary(clock))

    assert downloads._watch(GOOBA_WANTED, "kitchen") == "I couldn't download Gooba by 6ix9ine."
    assert said == [GOOBA_REQUESTED, ("kitchen", "I couldn't download Gooba by 6ix9ine.")]
    assert "made-up bug" in caplog.text


def test_set_up_when_asked_but_not_by_the_watch_says_why_it_couldnt(make_config, clock, said):
    # soulsync.url emptied on the page between the request and its watch.
    downloads = Downloads(make_config(soulsync={"url": ""}),
                          lambda satellite, message: said.append((satellite, message)),
                          soulsync=lambda: None, library=lambda: FakeLibrary(clock), now=clock.now, wait=clock.wait)

    downloads._watch(GOOBA_WANTED, "kitchen")

    assert said == [("kitchen", f"I couldn't download Gooba by 6ix9ine. {NOT_SET_UP}")]


# Several at once, and stopping

def test_several_are_watched_at_once_and_the_same_one_once(running, clock, said):
    soulsync = FakeSoulSync()
    downloads = running(soulsync, FakeLibrary(clock))

    assert downloads.start(GOOBA_WANTED, "kitchen") and downloads.start(RUMOURS_WANTED, "bedroom")
    assert downloads.start(Wanted("gooba", "6IX9INE", "track"), "kitchen") is False
    assert sorted(downloads.waiting()) == ["Gooba by 6ix9ine", "Rumours by Fleetwood Mac"]
    # Each on a thread of its own, which never holds Nova's shutdown up.
    assert len(_watches()) == 2 and all(thread.daemon for thread in _watches())

    downloads.stop()
    assert _all_ended()
    # One request to SoulSync each.
    assert sorted(soulsync.requested) == [("Gooba", "6ix9ine"), ("Rumours", "Fleetwood Mac")]


def test_once_its_landed_it_can_be_asked_for_again(watching, clock, said):
    downloads = watching(FakeSoulSync([DONE]), FakeLibrary(clock, songs=[[], [GOOBA]]))

    assert downloads.start(GOOBA_WANTED, "kitchen")
    assert _all_ended()

    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert downloads.waiting() == []
    assert downloads.start(GOOBA_WANTED, "kitchen")
    assert _all_ended()


def test_stopping_forgets_whats_waiting_logs_it_and_says_nothing_more(running, clock, said, caplog):
    caplog.set_level(logging.INFO)
    downloads = running(FakeSoulSync(), FakeLibrary(clock))
    downloads.start(GOOBA_WANTED, "kitchen")
    downloads.start(RUMOURS_WANTED, "bedroom")

    downloads.stop()

    assert _all_ended()
    assert ("Forgot 2 downloads still waiting, as Nova is stopping: Gooba by 6ix9ine, Rumours by Fleetwood Mac"
            in caplog.text)
    assert [message for _, message in said if not message.endswith("requested.")] == []
    assert downloads.waiting() == []


def test_stopping_says_one_download_for_one_and_nothing_with_none(running, clock, caplog):
    caplog.set_level(logging.INFO)
    running(FakeSoulSync(), FakeLibrary(clock)).stop()
    assert "Forgot" not in caplog.text

    downloads = running(FakeSoulSync(), FakeLibrary(clock))
    downloads.start(GOOBA_WANTED, "kitchen")
    downloads.stop()

    assert "Forgot 1 download still waiting, as Nova is stopping: Gooba by 6ix9ine" in caplog.text


def test_an_outcome_reached_as_nova_stops_isnt_said(watching, clock, said):
    library = FakeLibrary(clock)
    downloads = watching(FakeSoulSync([DONE]), library)

    def stopped_then_landed():
        downloads.stop()
        return [GOOBA]

    library.songs = [[], stopped_then_landed]

    assert downloads._watch(GOOBA_WANTED, "kitchen") == ""
    assert said == [GOOBA_REQUESTED]


# Set up or not

def test_without_soulsync_or_navidrome_it_isnt_set_up(make_config, monkeypatch):
    assert NOT_SET_UP == ("Downloading music isn't set up. SoulSync's address goes in Settings, under Music, "
                          "and its key under Keys.")
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    config = make_config(soulsync={"url": "http://soulsync.local:8008"},
                         navidrome={"url": "http://navidrome.local:4533", "username": "nova"})
    downloads = Downloads(config, lambda satellite, message: None)
    assert downloads.problem() is None

    # Each missing in turn, and read as it is now.
    config.apply({("soulsync", "url"): ""})
    assert downloads.problem() == NOT_SET_UP
    config.apply({("soulsync", "url"): "http://soulsync.local:8008"})
    monkeypatch.delenv("SOULSYNC_API_KEY")
    assert downloads.problem() == NOT_SET_UP
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")

    config.apply({("navidrome", "url"): ""})
    assert downloads.problem() == "Navidrome's address isn't set. Add it in Settings, under Music."
    config.apply({("navidrome", "url"): "http://navidrome.local:4533", ("navidrome", "username"): ""})
    assert downloads.problem() == "Navidrome's user isn't set. Add it in Settings, under Music."
    config.apply({("navidrome", "username"): "nova"})
    monkeypatch.delenv("NAVIDROME_PASSWORD")
    assert downloads.problem() == "Navidrome's password isn't set. Add it under Keys, then restart Nova."


def test_soulsync_and_navidrome_are_made_from_the_settings_as_they_are_when_used(make_config, monkeypatch):
    monkeypatch.setenv("SOULSYNC_API_KEY", "made-up-key")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "made-up-password")
    config = make_config(soulsync={"url": "http://soulsync.local:8008"},
                         navidrome={"url": "http://navidrome.local:4533", "username": "nova"})
    downloads = Downloads(config, lambda satellite, message: None)

    config.apply({("soulsync", "url"): "http://music.local:8008", ("navidrome", "url"): "http://music.local:4533"})
    soulsync, library = downloads._soulsync(), downloads._library()

    assert isinstance(soulsync, SoulSync) and soulsync.url == "http://music.local:8008"
    # A library of its own, never the player's, whose session is busy playing.
    assert type(library) is NavidromeLibrary
    assert (library.url, library.username) == ("http://music.local:4533", "nova")
    assert downloads._library() is not library

    monkeypatch.delenv("SOULSYNC_API_KEY")
    monkeypatch.delenv("NAVIDROME_PASSWORD")
    assert downloads._soulsync() is None and downloads._library() is None


# Asked for by voice - "download X", and the offer after "play X" finds nothing

GOOBA_REQUEST = Action("request_download", {"query": "Gooba by 6ix9ine", "search_type": "track"})


def test_downloading_with_nothing_set_up_says_so(make_config, clock):
    soulsync = FakeSoulSync()
    downloads = Downloads(make_config(), lambda satellite, message: None, soulsync=lambda: soulsync,
                          library=lambda: FakeLibrary(clock), now=clock.now, wait=clock.wait)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen") == {"success": False, "message": NOT_SET_UP}
    assert downloads.request("Gooba by 6ix9ine", "track", "kitchen") == {"success": False, "message": NOT_SET_UP}
    assert soulsync.requested == [] and _watches() == []


def test_a_whole_artist_is_never_downloaded(watching, set_up, clock):
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock), set_up)

    for ask in (downloads.download, downloads.request):
        assert ask("Fleetwood Mac", "artist", "kitchen") == {
            "success": False, "message": "I only download songs and albums, not a whole artist."}
    assert soulsync.requested == [] and _watches() == []


@pytest.mark.parametrize("query", [None, "", "  "])
def test_nothing_to_download_is_asked_for(watching, set_up, clock, query):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock), set_up)
    assert downloads.download(query, "track", "kitchen") == {
        "success": False, "message": "What would you like me to download?"}


@pytest.mark.parametrize("had", [[GOOBA], [GOOBA_CLEAN, GOOBA]])
def test_something_already_had_is_never_requested(watching, set_up, clock, had):
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[had]), set_up)

    assert downloads.download("Gooba by 6ix9ine", None, "kitchen") == {
        "success": True, "message": "You've already got Gooba by 6ix9ine."}
    assert soulsync.requested == [] and _watches() == []


def test_already_got_names_what_navidrome_has(watching, set_up):
    # "download rumours" - named as Navidrome has it, not as it was asked for, and by the
    # album's artist, not its first track's.
    class Library:
        def find_album_tracks(self, title, artist):
            assert (title, artist) == ("rumours", None)
            return [{**DREAMS, "artist": "Stevie Nicks"}, GO_YOUR_OWN_WAY, THE_CHAIN]

    downloads = watching(FakeSoulSync(), Library(), set_up)

    assert downloads.download("rumours", "album", "kitchen") == {
        "success": True, "message": "You've already got Rumours by Fleetwood Mac."}


def test_only_a_clean_copy_had_offers_the_explicit_one(watching, set_up, clock):
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[GOOBA_CLEAN]]), set_up)

    reply = downloads.download("Gooba by 6ix9ine", "track", "kitchen")

    assert reply == {"success": True, "message": "You've only got a clean version of Gooba by 6ix9ine. "
                                                "Do you want me to download the explicit one?",
                     "ask_first": GOOBA_REQUEST}
    assert soulsync.requested == [] and _watches() == []


@pytest.mark.parametrize("editions", [
    # One album: a clean edition marks only the tracks it bleeped - the rest are as the explicit one's.
    [DREAMS, _track(5, "Gold Dust Woman", clean=True)],
    # Two, each with a clean track.
    [DREAMS, _track(5, "Gold Dust Woman", clean=True),
     _track(6, "Dreams", album="Rumours (Clean)"), _track(7, "Gold Dust Woman", clean=True, album="Rumours (Clean)")],
])
def test_an_album_with_a_clean_track_in_every_edition_offers_the_explicit_one(watching, set_up, clock, editions):
    library = FakeLibrary(clock, tracks=[editions])
    downloads = watching(FakeSoulSync(), library, set_up)

    reply = downloads.download("Rumours by Fleetwood Mac", "album", "kitchen")

    assert reply["message"] == ("You've only got a clean version of Rumours by Fleetwood Mac. "
                                "Do you want me to download the explicit one?")
    assert reply["ask_first"] == Action("request_download",
                                        {"query": "Rumours by Fleetwood Mac", "search_type": "album"})


@pytest.mark.parametrize("clean_edition", [
    [_track(5, "Dreams", album="Rumours (Clean)"), _track(6, "Gold Dust Woman", clean=True, album="Rumours (Clean)")],
    # Called the same, told apart by Navidrome's id.
    [_track(5, "Dreams", album_id="another"), _track(6, "Gold Dust Woman", clean=True, album_id="another")],
])
@pytest.mark.parametrize("clean_first", [True, False])
def test_an_album_had_explicit_beside_a_clean_edition_is_already_got(watching, set_up, clock, clean_edition,
                                                                     clean_first):
    explicit = [DREAMS, GO_YOUR_OWN_WAY, THE_CHAIN]
    soulsync = FakeSoulSync()
    library = FakeLibrary(clock, tracks=[clean_edition + explicit if clean_first else explicit + clean_edition])
    downloads = watching(soulsync, library, set_up)

    assert downloads.download("Rumours by Fleetwood Mac", "album", "kitchen") == {
        "success": True, "message": "You've already got Rumours by Fleetwood Mac."}
    assert soulsync.requested == [] and _watches() == []


def test_a_download_is_started_and_nothing_said_until_soulsync_answers(watching, set_up, clock, said):
    # Navidrome: nothing when asked, nothing when the watch starts, then Gooba.
    soulsync = FakeSoulSync([DONE])
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[], [], [GOOBA]]), set_up)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen") == {
        "success": True, "message": "Asked SoulSync for Gooba by 6ix9ine.", "later": True}

    # The rest is the watch's, said to the satellite that asked.
    assert _all_ended()
    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert soulsync.requested == [("Gooba", "6ix9ine")]


def test_a_yes_requests_without_looking_in_navidrome_again(watching, set_up, clock, said):
    # Only the clean copy, which the offer was about - and which isn't the download.
    library = FakeLibrary(clock, songs=[[GOOBA_CLEAN], [GOOBA_CLEAN, GOOBA]])
    soulsync = FakeSoulSync([DONE])
    downloads = watching(soulsync, library, set_up)

    assert downloads.request("Gooba by 6ix9ine", "track", "kitchen") == {
        "success": True, "message": "Asked SoulSync for Gooba by 6ix9ine.", "later": True}

    assert _all_ended()
    assert said == [GOOBA_REQUESTED, GOOBA_DOWNLOADED]
    assert soulsync.requested == [("Gooba", "6ix9ine")]


def test_the_same_thing_asked_for_while_it_waits_is_requested_once(running, set_up, clock):
    soulsync = FakeSoulSync()
    downloads = running(soulsync, FakeLibrary(clock), set_up)
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen")["later"]

    # Named as it's asked for this time.
    for ask in (downloads.download, downloads.request, functools.partial(downloads.download, asked=False)):
        assert ask("gooba by 6ix9ine", "track", "bedroom") == {
            "success": True, "message": "I'm already getting gooba by 6ix9ine."}
    # And "play" it says so, rather than that Navidrome hasn't got it.
    assert downloads.offer("gooba by 6ix9ine", "track") == {
        "success": True, "message": "I'm already getting gooba by 6ix9ine."}
    assert downloads.waiting() == ["Gooba by 6ix9ine"]

    downloads.stop()
    assert _all_ended()
    assert soulsync.requested == [("Gooba", "6ix9ine")]


def test_a_download_not_asked_for_in_words_is_offered_instead(watching, set_up, clock):
    # Navidrome: nothing, then only a clean copy, then the explicit one.
    soulsync = FakeSoulSync()
    downloads = watching(soulsync, FakeLibrary(clock, songs=[[], [GOOBA_CLEAN], [GOOBA]]), set_up)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen", asked=False) == {
        "success": True, "message": "I haven't got Gooba by 6ix9ine. Do you want me to download it?",
        "ask_first": GOOBA_REQUEST}
    # Looked for first, as for "download X".
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen", asked=False)["message"] == (
        "You've only got a clean version of Gooba by 6ix9ine. Do you want me to download the explicit one?")
    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen", asked=False)["message"] == (
        "You've already got Gooba by 6ix9ine.")
    assert soulsync.requested == [] and _watches() == []


def test_navidrome_out_of_reach_requests_nothing(watching, set_up, clock):
    soulsync = FakeSoulSync()
    library = FakeLibrary(clock, songs=[requests.ConnectionError("made-up refusal")])
    downloads = watching(soulsync, library, set_up)

    assert downloads.download("Gooba by 6ix9ine", "track", "kitchen") == {
        "success": False, "message": "I can't reach Navidrome right now."}
    assert soulsync.requested == [] and _watches() == []


def test_the_offer_is_only_for_a_song_or_album_and_can_be_switched_off(watching, set_up, clock):
    downloads = watching(FakeSoulSync(), FakeLibrary(clock), set_up)

    assert downloads.offer("Gooba by 6ix9ine", "track") == {
        "success": True, "message": "I haven't got Gooba by 6ix9ine. Do you want me to download it?",
        "ask_first": GOOBA_REQUEST}
    assert downloads.offer("Rumours by Fleetwood Mac", "album")["ask_first"] == Action(
        "request_download", {"query": "Rumours by Fleetwood Mac", "search_type": "album"})
    assert downloads.offer("Gooba", None)["message"] == "I haven't got Gooba. Do you want me to download it?"
    assert downloads.offer("Fleetwood Mac", "artist") is None and downloads.offer("jazz", "playlist") is None
    assert downloads.offer(None, None) is None

    # music.offer_downloads off on the page, used straight away.
    set_up.apply({("music", "offer_downloads"): False})
    assert downloads.offer("Gooba by 6ix9ine", "track") is None
    set_up.apply({("music", "offer_downloads"): True})
    assert downloads.offer("Gooba by 6ix9ine", "track") is not None

    # Or SoulSync not set up.
    set_up.apply({("soulsync", "url"): ""})
    assert downloads.offer("Gooba by 6ix9ine", "track") is None
    assert _watches() == []
