"""
Spotify Controller Test
Tests how the controller signs in and keeps its token fresh, using fakes in place
of spotipy's client and OAuth classes so nothing goes to Spotify.
"""

import json
import logging
import os
import stat
import threading
import time
from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

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

    def factory(cached=None, refreshed=None, **device_name):
        oauth = FakeOAuth(cached, refreshed)

        def make_oauth(**kwargs):
            oauth.kwargs = kwargs
            return oauth

        monkeypatch.setattr(spotify_controller, "SpotifyOAuth", make_oauth)
        controller = spotify_controller.SpotifyController(config=make_config(spotify={"device_name": "Kitchen"}),
                                                          **device_name)
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


def test_refreshing_the_sign_in_has_a_time_limit(make_controller):
    # Every call refreshes an expired token with spotipy's own request, which waited for
    # ever - so the first command after a stall could hang until the watchdog restarted
    # the brain. The client's own time limit doesn't cover it.
    controller = make_controller(cached=SAVED)
    assert controller.oauth.kwargs["requests_timeout"] == spotify_controller.REQUEST_TIMEOUT


def test_the_token_file_is_given_as_a_cache_handler(make_controller):
    # spotipy warns that a cache_path is deprecated, and takes a CacheFileHandler instead.
    controller = make_controller(cached=SAVED)
    assert "cache_path" not in controller.oauth.kwargs
    handler = controller.oauth.kwargs["cache_handler"]
    assert isinstance(handler, spotify_controller.CacheFileHandler)
    assert handler.cache_path == spotify_controller.paths.SPOTIFY_TOKEN_FILE


@pytest.mark.skipif(not getattr(spotify_controller.spotipy, "__file__", None), reason="needs spotipy itself")
def test_spotipys_own_sign_in_keeps_the_time_limit(monkeypatch, make_config, tmp_path):
    # Against the real SpotifyOAuth where it's installed, which passes it on as each
    # token request's timeout - as 2.26.0, the brain's, does.
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    monkeypatch.setattr(spotify_controller.paths, "SPOTIFY_TOKEN_FILE", str(tmp_path / "spotify_token.json"))
    monkeypatch.setattr(spotify_controller.spotipy, "Spotify", FakeSpotify)
    controller = spotify_controller.SpotifyController(config=make_config(spotify={"device_name": "Kitchen"}))
    assert controller.sp_oauth.requests_timeout == spotify_controller.REQUEST_TIMEOUT


def test_the_controller_plays_on_its_satellites_speaker(make_controller):
    # The name a satellite said as it connected, over the brain's own spotify.device_name.
    assert make_controller(cached=SAVED, device_name="Bedroom Pi").device_name == "Bedroom Pi"
    # From a satellite which doesn't say, the brain's own - as before satellites said.
    assert make_controller(cached=SAVED, device_name=None).device_name == "Kitchen"
    assert make_controller(cached=SAVED).device_name == "Kitchen"


def test_starting_up_doesnt_go_over_the_network(make_controller):
    controller = make_controller(cached=SAVED, refreshed={"access_token": "new"})
    assert controller.oauth.refreshes == 0


def test_a_lost_login_fails_clearly_instead_of_waiting_for_a_browser(make_controller):
    controller = make_controller(cached=SAVED, refreshed=None)
    with pytest.raises(RuntimeError, match="settings page"):
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


# Signing in

class FakeCacheFile:
    """Stands in for spotipy's cache file handler, reading the file itself."""

    def __init__(self, cache_path):
        self.cache_path = str(cache_path)

    def get_cached_token(self):
        try:
            with open(self.cache_path) as file:
                return json.load(file)
        except FileNotFoundError:
            return None


class FakeSignInOAuth:
    """
    Stands in for SpotifyOAuth through a sign-in, as spotipy 2.26.0 on the brain has
    it: show_dialog only on the constructor, not get_authorize_url, and the token
    written with the default permissions - or, as save_fails, not written at all,
    which spotipy only warns about.
    """

    def __init__(self, cache_path):
        self.cache_handler = FakeCacheFile(cache_path)
        self.asked = []
        self.refuse = False
        # What Spotify's refusal says as its error, which is its own text, not Nova's.
        self.refused_with = "invalid_grant"
        self.save_fails = False

    def get_authorize_url(self, state=None):
        return f"https://accounts.spotify.com/authorize?client_id=client-id&state={state}"

    @staticmethod
    def parse_auth_response_url(url):
        form = dict(parse_qsl(urlsplit(url).query))
        if "error" in form:
            raise spotify_controller.SpotifyOauthError(f"Received error from auth server: {form['error']}",
                                                       error=form["error"])
        return tuple(form.get(param) for param in ["state", "code"])

    def get_access_token(self, code=None, as_dict=True, check_cache=True):
        self.asked.append((code, as_dict, check_cache))
        if self.refuse:
            raise spotify_controller.SpotifyOauthError(f"error: {self.refused_with}", error=self.refused_with)
        if self.save_fails:
            return "the-access-token"
        with open(self.cache_handler.cache_path, "w") as file:
            json.dump({"access_token": "the-access-token", "refresh_token": "the-refresh-token"}, file)
        os.chmod(self.cache_handler.cache_path, 0o644)
        return "the-access-token"


CALLBACK = "http://127.0.0.1:8888/callback"


def _state(url):
    return parse_qs(urlsplit(url).query)["state"][0]


def test_a_sign_in_gets_and_keeps_the_token_privately(tmp_path):
    oauth = FakeSignInOAuth(tmp_path / "spotify_token.json")
    sign_in = spotify_controller.SpotifySignIn(oauth)
    assert not sign_in.signed_in()
    state = _state(sign_in.start())
    sign_in.finish(f" {CALLBACK}?code=the-code&state={state} ")
    assert oauth.asked == [("the-code", False, False)]
    # The cache holds the refresh token, as good as a password to the account.
    assert stat.S_IMODE(os.stat(oauth.cache_handler.cache_path).st_mode) == 0o600
    assert sign_in.signed_in()


def test_an_address_from_another_sign_in_is_refused(tmp_path):
    oauth = FakeSignInOAuth(tmp_path / "spotify_token.json")
    sign_in = spotify_controller.SpotifySignIn(oauth)
    refused = "That address isn't from the sign-in this page started - start again."
    sign_in.start()
    with pytest.raises(ValueError, match=refused):
        sign_in.finish(f"{CALLBACK}?code=the-code&state=someone-elses")
    sign_in.start()
    with pytest.raises(ValueError, match=refused):
        sign_in.finish(f"{CALLBACK}?code=the-code")
    # Nothing started at all.
    with pytest.raises(ValueError, match=refused):
        sign_in.finish(f"{CALLBACK}?code=the-code&state=")
    assert oauth.asked == []

    state = _state(sign_in.start())
    with pytest.raises(ValueError, match="hasn't got Spotify's code in it - copy all of it"):
        sign_in.finish(f"{CALLBACK}?state={state}")
    state = _state(sign_in.start())
    sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")
    with pytest.raises(ValueError, match=refused):
        sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")
    assert len(oauth.asked) == 1


def test_a_cancelled_spotify_sign_in_is_answered(tmp_path):
    oauth = FakeSignInOAuth(tmp_path / "spotify_token.json")
    sign_in = spotify_controller.SpotifySignIn(oauth)
    state = _state(sign_in.start())
    with pytest.raises(ValueError, match="Spotify didn't accept that sign-in - start again."):
        sign_in.finish(f"{CALLBACK}?error=access_denied&state={state}")
    # Used up, so the address can't be tried again with a code put in.
    with pytest.raises(ValueError, match="isn't from the sign-in this page started"):
        sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")
    assert oauth.asked == []

    # Spotify turning the code down is answered the same way.
    oauth.refuse = True
    state = _state(sign_in.start())
    with pytest.raises(ValueError, match="Spotify didn't accept that sign-in - start again."):
        sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")
    assert not sign_in.signed_in()


@pytest.mark.parametrize("refused_by_spotify", [False, True])
def test_a_refusals_error_is_logged_only_as_an_error_code(tmp_path, caplog, refused_by_spotify):
    # A pasted error= is anything the user's browser had in it, and Spotify's own answer
    # is its text, not Nova's - a new line in either could forge a log line.
    caplog.set_level(logging.INFO)
    oauth = FakeSignInOAuth(tmp_path / "spotify_token.json")
    sign_in = spotify_controller.SpotifySignIn(oauth)
    for error, logged in [("access_denied", "access_denied"),
                          ("bad\nINFO forged line", "an unreadable error")]:
        caplog.clear()
        state = _state(sign_in.start())
        if refused_by_spotify:
            oauth.refuse, oauth.refused_with = True, error
            address = f"{CALLBACK}?code=the-code&state={state}"
        else:
            address = f"{CALLBACK}?error={quote(error)}&state={state}"
        with pytest.raises(ValueError, match="Spotify didn't accept that sign-in - start again."):
            sign_in.finish(address)
        assert f"Spotify turned the sign-in down: {logged}" in caplog.text
        assert "forged" not in caplog.text


def test_a_second_spotify_start_refuses_the_first_starts_address(tmp_path):
    # The button tapped twice: only the newest sign-in's address is taken.
    oauth = FakeSignInOAuth(tmp_path / "spotify_token.json")
    sign_in = spotify_controller.SpotifySignIn(oauth)
    first = _state(sign_in.start())
    sign_in.start()
    with pytest.raises(ValueError, match="That address isn't from the sign-in this page started - start again."):
        sign_in.finish(f"{CALLBACK}?code=the-code&state={first}")
    assert oauth.asked == []


def test_a_started_spotify_sign_in_runs_out(tmp_path, monkeypatch):
    # Kept for ever, a sign-in started and left would still take its address days later.
    now = [1000.0]
    monkeypatch.setattr(spotify_controller.time, "monotonic", lambda: now[0])
    oauth = FakeSignInOAuth(tmp_path / "spotify_token.json")
    sign_in = spotify_controller.SpotifySignIn(oauth)

    state = _state(sign_in.start())
    now[0] += 601
    with pytest.raises(ValueError, match="That sign-in ran out - start again."):
        sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")
    assert oauth.asked == [] and not sign_in.signed_in()

    # Just inside the ten minutes, it's taken.
    state = _state(sign_in.start())
    now[0] += 599
    sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")
    assert sign_in.signed_in()


def test_a_sign_in_that_couldnt_be_kept_isnt_said_to_have_worked(tmp_path):
    cache = tmp_path / "spotify_token.json"
    oauth = FakeSignInOAuth(cache)
    oauth.save_fails = True
    sign_in = spotify_controller.SpotifySignIn(oauth)
    state = _state(sign_in.start())
    with pytest.raises(ValueError, match="Couldn't save the sign-in - check Nova's data folder."):
        sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")
    # An older sign-in still in the cache isn't taken for the new one.
    cache.write_text(json.dumps({"access_token": "an-older-token", "refresh_token": "older"}))
    state = _state(sign_in.start())
    with pytest.raises(ValueError, match="Couldn't save the sign-in"):
        sign_in.finish(f"{CALLBACK}?code=the-code&state={state}")


def test_two_finishes_at_once_use_the_state_once(tmp_path):
    oauth = FakeSignInOAuth(tmp_path / "spotify_token.json")
    sign_in = spotify_controller.SpotifySignIn(oauth)
    address = f"{CALLBACK}?code=the-code&state={_state(sign_in.start())}"
    together = threading.Barrier(8)

    def finish():
        together.wait()
        try:
            sign_in.finish(address)
        except ValueError:
            pass

    threads = [threading.Thread(target=finish) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(oauth.asked) == 1


def test_signing_in_again_lets_another_account_be_chosen(make_controller):
    # Spotify skips its page for an account which has agreed before, unless asked to
    # show it - and spotipy 2.26.0 takes that only on the constructor.
    controller = make_controller(cached=SAVED)
    assert not controller.oauth.kwargs.get("show_dialog", False)
    spotify_controller.spotify_oauth(show_dialog=True)
    assert controller.oauth.kwargs["show_dialog"] is True


def test_a_new_sign_in_drops_what_was_kept_of_the_account_before(make_controller, tmp_path):
    controller = make_controller(cached=SAVED)
    controller.sp = FakeSpotifyAPI()
    controller.play_playlist("workout")
    sign_in = spotify_controller.SpotifySignIn(FakeSignInOAuth(tmp_path / "spotify_token.json"))
    sign_in.finish(f"{CALLBACK}?code=the-code&state={_state(sign_in.start())}")
    controller.sp.calls.clear()
    controller.play_playlist("workout")
    assert "playlists" in controller.sp.calls and "devices" in controller.sp.calls


class SpotifySaving:
    """
    Stands in for SpotifyOAuth as spotipy 2.26.0 has it, saving every token through the
    cache handler spotify_oauth gives it: a sign-in's from its code, and a refresh's from
    the refresh token in the file - with something done while Spotify answers the refresh.
    """

    def __init__(self, cache_handler):
        self.cache_handler = cache_handler
        self.meanwhile = lambda: None

    def get_authorize_url(self, state=None):
        return f"https://accounts.spotify.com/authorize?client_id=client-id&state={state}"

    parse_auth_response_url = staticmethod(FakeSignInOAuth.parse_auth_response_url)

    def get_access_token(self, code=None, as_dict=True, check_cache=True):
        self.cache_handler.save_token_to_cache({"access_token": "new-access", "refresh_token": "new-account"})
        return "new-access"

    def get_cached_token(self):
        refresh_token = self.cache_handler.get_cached_token()["refresh_token"]
        self.meanwhile()
        refreshed = {"access_token": f"refreshed-{refresh_token}", "refresh_token": refresh_token}
        self.cache_handler.save_token_to_cache(refreshed)
        return refreshed


def test_a_refresh_under_way_as_another_account_signs_in_doesnt_save_over_it(monkeypatch, tmp_path):
    # It read the account before's token, and saved once Spotify answered, it put that
    # account back - after the page had said "Signed in to Spotify".
    token_file = tmp_path / "spotify_token.json"
    token_file.write_text(json.dumps({"access_token": "old-access", "refresh_token": "old-account"}))
    monkeypatch.setattr(spotify_controller.paths, "SPOTIFY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", CALLBACK)
    monkeypatch.setattr(spotify_controller, "SpotifyOAuth", lambda **kwargs: SpotifySaving(kwargs["cache_handler"]))
    page = spotify_controller.SpotifySignIn(spotify_controller.spotify_oauth(show_dialog=True))
    refreshing = spotify_controller.spotify_oauth()
    failed = []

    def sign_in():
        try:
            page.finish(f"{CALLBACK}?code=the-code&state={_state(page.start())}")
        except Exception as e:
            failed.append(e)

    def signed_in_on_the_page():
        thread = threading.Thread(target=sign_in)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()

    refreshing.meanwhile = signed_in_on_the_page
    # The command it was for still has its token.
    assert spotify_controller._RefreshingAuth(refreshing).get_access_token() == "refreshed-old-account"
    assert failed == [] and json.loads(token_file.read_text())["refresh_token"] == "new-account"

    # One begun after the sign-in saves as ever.
    refreshing.meanwhile = lambda: None
    assert spotify_controller._RefreshingAuth(refreshing).get_access_token() == "refreshed-new-account"
    assert json.loads(token_file.read_text())["access_token"] == "refreshed-new-account"


def test_a_command_as_another_account_signs_in_still_finds_the_speaker(make_controller, monkeypatch):
    # The sign-in drops the speaker kept between the command seeing it's recent and reading it.
    controller = make_controller(cached=SAVED)
    controller._device = ("speaker-id", time.monotonic())
    fresh = controller._fresh

    def dropped_meanwhile(cached):
        recent = fresh(cached)
        controller._device = None
        return recent

    monkeypatch.setattr(controller, "_fresh", dropped_meanwhile)
    assert controller._find_device() == "speaker-id"


def test_spotipy_never_logs_a_secret_whatever_novas_level(caplog):
    # Its own debug lines have the client's secret, in the auth header, and the code.
    caplog.set_level(logging.DEBUG)
    for name in ("spotipy", "spotipy.oauth2", "urllib3", "urllib3.connectionpool"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


def test_a_session_uses_a_sign_in_made_after_it_started(make_controller, monkeypatch):
    controller = make_controller(cached=None)
    assert controller.play("Halo") == {"success": False, "message": spotify_controller.NOT_SIGNED_IN}
    # Signed in on the page since, which writes the cache this reads.
    controller.oauth.cache_handler.token = SAVED
    monkeypatch.setattr(spotify_controller.spotipy, "Spotify", lambda **kwargs: FakeSpotifyAPI())
    assert controller.play("Halo")["success"]


def test_nova_says_spotify_is_signed_in_to_on_the_settings_page(make_controller):
    assert spotify_controller.NOT_SIGNED_IN == "Spotify needs signing in again on the settings page."
    controller = make_controller(cached=SAVED, refreshed=None)
    with pytest.raises(RuntimeError, match=spotify_controller.NOT_SIGNED_IN):
        controller.sp.kwargs["auth_manager"].get_access_token(as_dict=False)


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


def test_spotify_pausing_is_switched_off_straight_away(make_controller):
    controller = make_controller(cached=SAVED)
    controller.sp = FakePlayback("Kitchen")
    controller.config.apply({("spotify", "pause_while_speaking"): False})
    controller.pause_for_speech()
    assert controller.sp.paused is False
