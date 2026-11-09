"""
Downloads Test
Checks the watch on a download - asked of SoulSync, said to be requested, then
followed until Navidrome has it or its time is up - with fakes in place of SoulSync
and Navidrome, and a fake clock which only moves on when the watch waits, so nothing
real is asked and no test waits out a poll.
"""

import logging
import threading

import pytest
import requests

from voice_assistant.downloads import NOT_SET_UP, POLL_EVERY, SCAN_AFTER, Downloads, Wanted
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


def _track(number, title, clean=False, album="Rumours"):
    """One of Rumours' tracks as Navidrome describes it - marked clean, or unmarked as most are."""
    return {"id": f"r{number}", "title": title, "artist": "Fleetwood Mac", "album": album,
            "explicitStatus": "clean" if clean else ""}


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
def watching(config, clock, said):
    """Makes Downloads which asks the fakes given, on the fake clock."""
    def make(soulsync, library):
        def say(satellite, message):
            said.append((satellite, message))
            clock.said_at.append(clock.time)

        return Downloads(config, say, soulsync=lambda: soulsync, library=lambda: library, now=clock.now,
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

    def make(soulsync, library):
        downloads = Downloads(config, lambda satellite, message: said.append((satellite, message)),
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
    # Navidrome is only asked to scan once SoulSync says it's done.
    assert library.scans == []


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
