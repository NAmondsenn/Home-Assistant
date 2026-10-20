"""
Navidrome Test
Checks how music is found on Navidrome and how the queue plays, using a fake in
place of Navidrome's API and of the speaker, so nothing is sent or played. The one
test which decodes real audio needs ffmpeg, and is skipped anywhere it isn't.
"""

import copy
import hashlib
import io
import shutil
import threading
import time
import wave

import pytest
import requests

np = pytest.importorskip("numpy")

from voice_assistant import volume as volume_module
from voice_assistant.navidrome import NavidromePlayer, best, likeness, split_by
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
    assert player.play("Gooba by 6ix9ine") == {"success": False,
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
