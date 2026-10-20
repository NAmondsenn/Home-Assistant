"""
Spotify Controller Test
Tests how the controller signs in and keeps its token fresh, using fakes in place
of spotipy's client and OAuth classes so nothing goes to Spotify.
"""

import time

import pytest

from voice_assistant import spotify_controller


class FakeCache:
    """Stands in for the token cache file."""

    def __init__(self, token):
        self.token = token

    def get_cached_token(self):
        return self.token


class FakeOAuth:
    """
    Stands in for SpotifyOAuth. get_cached_token() is the call which refreshes over
    the network, so it's counted - startup shouldn't make it.
    """

    def __init__(self, cached, refreshed):
        self.cache_handler = FakeCache(cached)
        self.refreshed = refreshed
        self.refreshes = 0

    def get_cached_token(self):
        self.refreshes += 1
        return self.refreshed


class FakeSpotify:
    """Stands in for spotipy.Spotify, keeping what it was constructed with."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def make_controller(monkeypatch, make_config):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    monkeypatch.setattr(spotify_controller.spotipy, "Spotify", FakeSpotify)

    def factory(cached=None, refreshed=None):
        oauth = FakeOAuth(cached, refreshed)
        monkeypatch.setattr(spotify_controller, "SpotifyOAuth", lambda **kwargs: oauth)
        controller = spotify_controller.SpotifyController(config=make_config(spotify={"device_name": "Kitchen"}))
        controller.oauth = oauth
        return controller

    return factory


SAVED = {"access_token": "old", "refresh_token": "refresh"}


def test_client_fetches_a_fresh_token_for_every_request(make_controller):
    controller = make_controller(cached=SAVED, refreshed={"access_token": "new"})
    kwargs = controller.sp.kwargs
    # A fixed access token would stop working after an hour.
    assert "auth" not in kwargs
    assert kwargs["auth_manager"].get_access_token(as_dict=False) == "new"
    assert kwargs["requests_timeout"] == spotify_controller.REQUEST_TIMEOUT


def test_starting_up_doesnt_go_over_the_network(make_controller):
    controller = make_controller(cached=SAVED, refreshed={"access_token": "new"})
    assert controller.oauth.refreshes == 0


def test_a_lost_login_fails_clearly_instead_of_waiting_for_a_browser(make_controller):
    controller = make_controller(cached=SAVED, refreshed=None)
    with pytest.raises(RuntimeError, match="authorising again"):
        controller.sp.kwargs["auth_manager"].get_access_token(as_dict=False)


def test_without_a_cached_login_it_says_so(make_controller):
    controller = make_controller(cached=None)
    assert controller.sp is None
    assert controller.play("Drake") == {"success": False, "message": spotify_controller.NOT_SIGNED_IN}


class FakePlayback:
    """Stands in for the parts of the client pause_for_speech uses."""

    def __init__(self, device):
        self.device = device
        self.paused = False

    def current_playback(self):
        return {"is_playing": True, "device": {"name": self.device}}

    def pause_playback(self):
        self.paused = True


@pytest.mark.parametrize("device, paused", [("Kitchen", True), ("kitchen", True), ("Nathaniel's phone", False)])
def test_only_music_on_this_speaker_is_paused_for_speech(make_controller, device, paused):
    # Talking to the assistant used to pause music on whichever device was active,
    # including a phone in another room.
    controller = make_controller(cached=SAVED)
    controller.sp = FakePlayback(device)
    controller.pause_for_speech()
    assert controller.sp.paused is paused and controller.was_playing() is paused


# Holding the music while there's something to say

class FakeMusic:
    """Stands in for SpotifyController's pause and resume, noting the order they happen in."""

    def __init__(self, pause_takes=0.0):
        self.pause_takes = pause_takes
        self.calls = []

    def pause_for_speech(self):
        time.sleep(self.pause_takes)
        self.calls.append("pause")

    def resume_after_speech(self):
        self.calls.append("resume")


def test_the_music_comes_back_only_once_nothing_is_left_to_say(monkeypatch):
    # A reminder queued behind a conversation used to start just as the music
    # came back after it.
    music = FakeMusic()
    hold = spotify_controller.MusicHold(music)
    hold.hold()           # the conversation
    hold.hold()           # a reminder waiting for it to finish
    hold.release()
    hold.wait()
    assert music.calls == ["pause"]

    hold.release()
    hold.wait()
    assert music.calls == ["pause", "resume"]


def test_a_resume_never_overtakes_the_pause_its_undoing():
    music = FakeMusic(pause_takes=0.2)
    hold = spotify_controller.MusicHold(music)
    hold.hold()
    hold.release()
    hold.wait()
    assert music.calls == ["pause", "resume"]


def test_a_timers_next_round_doesnt_bounce_the_music_back_on():
    music = FakeMusic()
    hold = spotify_controller.MusicHold(music)
    hold.hold()
    hold.release(after=0.2)
    hold.hold()           # the next round, a moment later
    time.sleep(0.3)
    hold.wait()
    assert music.calls == ["pause"]


def test_without_spotify_holding_the_music_does_nothing():
    hold = spotify_controller.MusicHold(None)
    hold.hold()
    hold.release()
    hold.wait()


def test_the_token_cache_is_kept_private(make_controller, monkeypatch, tmp_path):
    # It holds the refresh token, but spotipy wrote it readable by anyone on the machine.
    cache = tmp_path / ".spotify_cache"
    cache.write_text("{}")
    cache.chmod(0o644)
    monkeypatch.setattr(spotify_controller, "PROJECT_DIRECTORY", str(tmp_path))

    make_controller(cached=SAVED)
    assert cache.stat().st_mode & 0o777 == 0o600


# Picking what to play

def _track(name, *artists):
    return {"name": name, "artists": [{"name": artist} for artist in artists]}


@pytest.mark.parametrize("query, names, matches", [
    ("Gooba", ("GOOBA", "6ix9ine"), True),
    ("bohemian rhapsody", ("Shape of You", "Ed Sheeran"), False),
    # Nothing long enough to judge by, so it's given the benefit of the doubt.
    ("it", ("Anything", "Anyone"), True),
])
def test_a_result_has_to_share_a_word_with_what_was_asked_for(query, names, matches):
    # Whisper mishears names, and Spotify always returns something.
    assert spotify_controller.SpotifyController._looks_like_match(query, *names) is matches


@pytest.mark.parametrize("name, normalised", [
    ("The Chronic", "chronic"), ("the", "the"), ("Don't Stop Me Now!", "don t stop me now"),
])
def test_titles_are_compared_by_their_words(name, normalised):
    assert spotify_controller.SpotifyController._normalise(name) == normalised


def test_a_karaoke_version_is_skipped_for_the_real_thing(make_controller):
    controller = make_controller(cached=SAVED)
    karaoke = _track("Shake It Off (Originally Performed by Taylor Swift)", "Karaoke Hits")
    real = _track("Shake It Off", "Taylor Swift")
    assert controller._pick_track([karaoke, real], "shake it off", None) is real


def test_karaoke_is_played_when_its_asked_for(make_controller):
    controller = make_controller(cached=SAVED)
    karaoke = _track("Shake It Off (Karaoke Version)", "Karaoke Hits")
    assert controller._pick_track([karaoke], "shake it off karaoke", None) is karaoke


def test_a_named_artist_has_to_be_credited_unless_nothing_else_will_do(make_controller):
    controller = make_controller(cached=SAVED)
    cover = _track("Hallelujah", "Jeff Buckley")
    original = _track("Hallelujah", "Leonard Cohen")
    assert controller._pick_track([cover, original], "hallelujah", "Leonard Cohen") is original
    # The artist may just have been misheard, so the song is still played.
    assert controller._pick_track([cover], "hallelujah", "Leonard Coen") is cover


def test_nothing_is_picked_when_the_query_was_misheard(make_controller):
    controller = make_controller(cached=SAVED)
    assert controller._pick_track([_track("Shape of You", "Ed Sheeran")], "bohemian rhapsody", None) is None


@pytest.mark.parametrize("name, liked", [
    ("liked songs", True), ("Leaked songs", True), ("my favourites", True), ("favourites", True),
    ("chill vibes", False),
])
def test_liked_songs_are_recognised_even_when_misheard(name, liked):
    assert spotify_controller.SpotifyController._means_liked_songs(name) is liked


def test_offline_the_music_is_left_alone():
    # Pausing and resuming would only wait out timeouts, holding up a timer being set.
    class FakeOfflineMusic(FakeMusic):
        def cancel_resume(self):
            self.calls.append("cancel resume")

    music = FakeOfflineMusic()
    hold = spotify_controller.MusicHold(music, online=lambda: False)
    hold.hold()
    hold.release()
    hold.wait()
    assert music.calls == ["cancel resume"]
