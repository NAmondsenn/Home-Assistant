"""
Music Module.

Which plays the music - Navidrome, your own server, or Spotify - and keeping it
quiet while the assistant has something to say.

Both players do the same things the same way, so actions.py doesn't mind which it
has: play, play_playlist, pause, skip, previous, restart, repeat, shuffle,
current_track, get_volume, set_volume, and pause_for_speech / resume_after_speech
/ cancel_resume / was_playing for MusicHold. Each also has a name, for saying what
went wrong, and says whether it needs the internet.
"""

import difflib
import logging
import os
import threading
from typing import Optional

# Logger setup
logger = logging.getLogger(__name__)

PLAYERS = ("navidrome", "spotify")

# What people call their saved or starred songs when asking for them like a playlist -
# and, since it arrives from speech, what speech-to-text makes of "liked songs": "light songs"
# is too far from it to be caught by the likeness below, and "car songs" too near.
LIKED_SONGS_NAMES = ("liked songs", "liked music", "my likes", "likes",
                     "saved songs", "saved tracks", "saved music",
                     "favourites", "favorites", "favourite songs", "favorite songs",
                     "starred songs", "starred", "light songs", "lite songs")


def means_liked_songs(name: str) -> bool:
    """
    Whether a requested "playlist" is really the user's liked songs.

    Matched loosely, since this arrives from speech like anything else - "liked
    songs" comes back as "leaked songs" often enough to be worth allowing for.

    Args:
        name: The playlist name as the user said it.

    Returns:
        True if they meant their liked songs.
    """
    wanted = name.strip().lower()
    if wanted.startswith("my "):
        wanted = wanted[3:]
    return any(wanted == liked or difflib.SequenceMatcher(None, wanted, liked).ratio() >= 0.8
               for liked in LIKED_SONGS_NAMES)


def make_player(config, speaker, volume):
    """
    The music player music.player in config.yaml names.

    Args:
        config: Configs loaded from config.yaml.
        speaker: The Speaker, which Navidrome plays through.
        volume: The VolumeControl, which keeps Navidrome's volume.

    Returns:
        The player, or None if it isn't set up - the assistant carries on without music.
    """
    choice = (config.section("music").get("player") or "spotify").strip().lower()
    if choice not in PLAYERS:
        logger.warning(f"music.player is '{choice}', which isn't one of {', '.join(PLAYERS)} - no music")
        return None

    try:
        if choice == "navidrome":
            from voice_assistant.navidrome import NavidromePlayer
            return NavidromePlayer.from_config(config, os.getenv("NAVIDROME_PASSWORD"), speaker, volume)
        from voice_assistant.spotify_controller import SpotifyController
        return SpotifyController(config=config)
    except Exception as e:
        logger.warning(f"Couldn't set up {choice}, so there's no music: {e}")
        return None


class MusicHold:
    """
    Keeps the music paused while the assistant has something to say - a
    conversation, or a timer or reminder going off - and lets it back only once
    nothing is left to say.

    Announcements used to play over the music at full music volume, and one queued
    behind a conversation started just as the music came back afterwards. Now each
    holds the music, and holds are counted, so a reminder waiting for a
    conversation to finish keeps the music paused through both.
    """

    def __init__(self, player, online=None):
        """
        Args:
            player: The music player, or None, when this does nothing.
            online: Says whether the internet can be reached - network.online. With
                no network, a player which needs the internet is left alone rather
                than everything waiting out a request which can't get through.
        """
        self.player = player
        self._online = online or (lambda: True)
        self._holds = 0
        self._lock = threading.Lock()
        # The last pause or resume started. Each waits for the one before it, so a
        # resume can never overtake the pause it's undoing.
        self._change: Optional[threading.Thread] = None

    def _reachable(self) -> bool:
        return not getattr(self.player, "needs_internet", True) or self._online()

    def hold(self):
        """Pauses the music, if this is the first hold. Doesn't wait for the player."""
        with self._lock:
            self._holds += 1
            if self._holds == 1 and self.player and self._reachable():
                self._start_change(self.player.pause_for_speech)

    def release(self, after: float = 0.0):
        """
        Lets go of a hold, resuming the music if it was the last.

        Args:
            after: Seconds to wait first, in case something else is about to be
                said - a plain timer's next round comes three seconds later, and
                the music shouldn't bounce on and off in between.
        """
        if after > 0:
            timer = threading.Timer(after, self.release)
            timer.daemon = True
            timer.start()
            return

        with self._lock:
            self._holds = max(0, self._holds - 1)
            if self._holds == 0 and self.player:
                if self._reachable():
                    self._start_change(self.player.resume_after_speech)
                else:
                    self.player.cancel_resume()

    def wait(self):
        """
        Waits for any pause or resume still on its way to the player. Anything else
        sent to it waits for this first, so a slow pause can't land after a "play"
        request and stop the music which was just asked for.
        """
        change = self._change
        if change is not None:
            # Bounded, so a hung request can't hold the assistant up indefinitely.
            change.join(timeout=20)

    def _start_change(self, change):
        """
        Pauses or resumes on a thread of its own - for Spotify it goes through
        Spotify's servers, which takes a moment, or much longer when the network's
        struggling. Called with the lock held, so they start in the order asked.
        """
        previous = self._change

        def run():
            if previous is not None:
                previous.join(timeout=20)
            change()

        self._change = threading.Thread(target=run, daemon=True, name="music-hold")
        self._change.start()
