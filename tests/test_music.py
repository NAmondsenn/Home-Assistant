"""
Music Test
Checks how the music is held quiet while the assistant talks, and how the player is
chosen, using fakes in place of the players themselves.
"""

import time

from voice_assistant.music import MusicHold, make_player, means_liked_songs


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


def test_liked_songs_are_recognised_when_misheard():
    assert means_liked_songs("leaked songs") and means_liked_songs("my favourites")
    assert means_liked_songs("light songs") and means_liked_songs("my light songs")
    assert not means_liked_songs("workout") and not means_liked_songs("car songs")
