"""
Music Test
Checks how the music is held quiet while the assistant talks, and how the player is
chosen, using fakes in place of the players themselves.
"""

import logging
import time

import pytest

from voice_assistant.music import MusicHold, make_player, means_liked_songs, why_no_player


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


def test_the_music_comes_back_only_once_nothing_is_left_to_say():
    # A reminder queued behind a conversation used to start just as the music
    # came back after it.
    music = FakeMusic()
    hold = MusicHold(music)
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
    hold = MusicHold(music)
    hold.hold()
    hold.release()
    hold.wait()
    assert music.calls == ["pause", "resume"]


def test_a_timers_next_round_doesnt_bounce_the_music_back_on():
    music = FakeMusic()
    hold = MusicHold(music)
    hold.hold()
    hold.release(after=0.2)
    hold.hold()           # the next round, a moment later
    time.sleep(0.3)
    hold.wait()
    assert music.calls == ["pause"]


def test_without_a_player_holding_the_music_does_nothing():
    hold = MusicHold(None)
    hold.hold()
    hold.release()
    hold.wait()


def test_a_player_on_the_local_network_is_still_paused_offline():
    # Navidrome doesn't need the internet, so there's nothing to wait out.
    music = FakeMusic()
    music.needs_internet = False
    hold = MusicHold(music, online=lambda: False)
    hold.hold()
    hold.release()
    hold.wait()
    assert music.calls == ["pause", "resume"]


def test_spotify_is_left_alone_offline():
    music = FakeMusic()
    music.cancel_resume = lambda: music.calls.append("cancel")
    hold = MusicHold(music, online=lambda: False)
    hold.hold()
    hold.release()
    hold.wait()
    assert music.calls == ["cancel"]


def test_an_unknown_player_means_no_music(make_config):
    assert make_player(make_config(music={"player": "winamp"}), speaker=None, volume=None) is None


def test_navidrome_without_its_settings_means_no_music(make_config, monkeypatch):
    monkeypatch.delenv("NAVIDROME_PASSWORD", raising=False)
    assert make_player(make_config(music={"player": "navidrome"}, navidrome={"url": "http://nd:4533",
                                                                             "username": "nova"}),
                       speaker=None, volume=None) is None


def test_why_theres_no_music_is_said(make_config, monkeypatch):
    for key in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "NAVIDROME_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    spotify = make_config(music={"player": "spotify"})
    # Spotify is the player when none is chosen.
    assert why_no_player(make_config()) == "Spotify's client ID and secret aren't set. Add them under Keys, then restart Nova."
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "client-id")
    assert why_no_player(spotify) == "Spotify's client ID and secret aren't set. Add them under Keys, then restart Nova."
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)
    assert why_no_player(spotify) == ("SPOTIFY_REDIRECT_URI isn't set. setup.sh puts it in Nova's .env - "
                                      "add it there, then restart Nova.")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    assert why_no_player(spotify) == "Nova couldn't set up Spotify - Recent problems, in Status, says why."

    navidrome = make_config(music={"player": "navidrome"}, navidrome={"url": "", "username": "nova"})
    assert why_no_player(navidrome) == "Navidrome's address isn't set. Add it in Settings, under Music."
    no_user = make_config(music={"player": "navidrome"}, navidrome={"url": "http://nd:4533", "username": " "})
    assert why_no_player(no_user) == "Navidrome's user isn't set. Add it in Settings, under Music."
    with_address = make_config(music={"player": "navidrome"}, navidrome={"url": "http://nd:4533", "username": "nova"})
    assert why_no_player(with_address) == "Navidrome's password isn't set. Add it under Keys, then restart Nova."
    monkeypatch.setenv("NAVIDROME_PASSWORD", "secret")
    assert why_no_player(with_address) == "Nova couldn't set up Navidrome - Recent problems, in Status, says why."
    # Not one of the two, so neither's to blame.
    assert why_no_player(make_config(music={"player": "winamp"})) == (
        "Nova's music player isn't Navidrome or Spotify. Choose one in Settings, under Music.")


@pytest.mark.parametrize("player, navidrome, unset", [
    ("spotify", {}, "SPOTIFY_CLIENT_ID"),
    ("spotify", {}, "SPOTIFY_CLIENT_SECRET"),
    ("spotify", {}, "SPOTIFY_REDIRECT_URI"),
    ("navidrome", {"url": "", "username": "nova"}, None),
    ("navidrome", {"url": "http://nd:4533", "username": ""}, None),
    ("navidrome", {"url": "http://nd:4533", "username": "nova"}, "NAVIDROME_PASSWORD"),
])
def test_what_the_page_says_is_missing_is_what_kept_the_player_from_being_made(
        make_config, monkeypatch, caplog, player, navidrome, unset):
    # The two read the same checks, so the page can't say "couldn't set up" of a player
    # which was never going to be made, nor name something that's there.
    for key, value in [("SPOTIFY_CLIENT_ID", "client-id"), ("SPOTIFY_CLIENT_SECRET", "client-secret"),
                       ("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"), ("NAVIDROME_PASSWORD", "secret")]:
        monkeypatch.setenv(key, value)
    if unset:
        monkeypatch.delenv(unset)
    config = make_config(music={"player": player}, navidrome=navidrome)
    caplog.set_level(logging.WARNING)
    assert make_player(config, speaker=None, volume=None) is None
    said = why_no_player(config)
    assert "Recent problems" not in said and said in caplog.text


def test_liked_songs_are_recognised_when_misheard():
    assert means_liked_songs("leaked songs") and means_liked_songs("my favourites")
    assert means_liked_songs("light songs") and means_liked_songs("my light songs")
    assert not means_liked_songs("workout") and not means_liked_songs("car songs")
