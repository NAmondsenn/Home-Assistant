"""
Spotify Controller Test
Tests how the controller signs in and keeps its token fresh, using fakes in place
of spotipy's client and OAuth classes so nothing goes to Spotify.
"""

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
    assert controller.play("Drake") == {"success": False, "message": "Not authenticated"}


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
