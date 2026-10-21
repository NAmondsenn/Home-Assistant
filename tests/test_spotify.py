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

def test_the_token_cache_is_kept_private(make_controller, monkeypatch, tmp_path):
    # It holds the refresh token, but spotipy wrote it readable by anyone on the machine.
    cache = tmp_path / "spotify_token.json"
    cache.write_text("{}")
    cache.chmod(0o644)
    monkeypatch.setattr(spotify_controller.paths, "SPOTIFY_TOKEN_FILE", str(cache))

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


class FakeSpotifyAPI:
    """Stands in for the parts of the client a "play" uses, counting the round trips."""

    def __init__(self, search_takes=0.0):
        self.search_takes = search_takes
        self.calls = []

    def devices(self):
        self.calls.append("devices")
        return {"devices": [{"name": "Kitchen", "id": "kitchen-id"}]}

    def current_playback(self):
        self.calls.append("current_playback")
        return {"device": {"id": "kitchen-id"}}

    def search(self, q, limit, type):
        self.calls.append(f"search {type}")
        time.sleep(self.search_takes)
        if type == "artist":
            return {"artists": {"items": [{"name": "Someone Else", "uri": "artist"}]}}
        return {"tracks": {"items": [{"name": "Halo", "uri": "track", "artists": [{"name": "Beyonce"}],
                                      "album": {"uri": "album"}}]}}

    def start_playback(self, **kwargs):
        self.calls.append("start_playback")

    def current_user_playlists(self, limit, offset):
        self.calls.append("playlists")
        return {"items": [{"name": "Workout", "uri": "playlist"}]}

    def shuffle(self, state, device_id=None):
        pass


def test_playing_twice_doesnt_look_the_speaker_up_twice(make_controller):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeSpotifyAPI()
    controller.play("Halo")
    controller.sp.calls.clear()
    controller.play("Halo")
    assert "devices" not in controller.sp.calls and "current_playback" not in controller.sp.calls


def test_a_failed_play_looks_the_speaker_up_again(make_controller):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeSpotifyAPI()
    controller.play("Halo")
    controller.sp.start_playback = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Device not found"))
    controller.play("Halo")
    controller.sp = FakeSpotifyAPI()
    controller.play("Halo")
    assert controller.sp.calls[0] == "devices"


def test_the_artist_and_song_searches_run_together(make_controller):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeSpotifyAPI(search_takes=0.3)
    started = time.monotonic()
    assert controller.play("Halo")["success"]
    assert time.monotonic() - started < 0.55


def test_the_playlists_are_read_once_for_a_few_minutes(make_controller):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeSpotifyAPI()
    controller.play_playlist("workout")
    controller.play_playlist("workout")
    assert controller.sp.calls.count("playlists") == 1


# For the settings page

class FakeNowPlaying:
    """Stands in for the parts of the client the settings page uses."""

    def __init__(self, playback=None, playlists=()):
        self.playback = playback
        self.playlists = list(playlists)

    def current_playback(self):
        return self.playback

    def current_user_playlists(self, limit=50, offset=0):
        return {"items": self.playlists[offset:offset + limit]}


PLAYBACK = {"is_playing": True, "progress_ms": 61500, "item": {
    "name": "Gooba", "duration_ms": 125000, "artists": [{"name": "6ix9ine"}],
    "album": {"name": "Gooba", "images": [{"url": "https://i.scdn.co/image/big", "width": 640},
                                          {"url": "https://i.scdn.co/image/medium", "width": 300},
                                          {"url": "https://i.scdn.co/image/small", "width": 64}]}}}


class FakeImage:
    headers = {"Content-Type": "image/jpeg"}
    content = b"a cover"

    def raise_for_status(self):
        pass


def test_the_page_is_told_whats_playing_on_spotify(make_controller):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeNowPlaying(PLAYBACK)
    assert controller.now_playing() == {"track": "Gooba", "artist": "6ix9ine", "album": "Gooba", "playing": True,
                                        "position": 61.5, "duration": 125, "cover": "medium"}
    controller.sp = FakeNowPlaying(None)
    assert controller.now_playing() is None


def test_a_cover_only_ever_comes_from_where_spotify_said(make_controller, monkeypatch):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeNowPlaying(PLAYBACK)
    fetched = []
    monkeypatch.setattr(spotify_controller.requests, "get", lambda url, timeout=None: fetched.append(url) or FakeImage())

    assert controller.cover() is None
    controller.now_playing()
    assert controller.cover() == (b"a cover", "image/jpeg") and controller.cover() == (b"a cover", "image/jpeg")
    assert fetched == ["https://i.scdn.co/image/medium"]


def test_the_page_is_offered_the_spotify_playlists_by_name(make_controller):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeNowPlaying(playlists=[{"name": "Workout"}, {"name": "chill"}, None])
    assert controller.playlists() == ["chill", "Workout"]
