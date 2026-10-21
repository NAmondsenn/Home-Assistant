"""
Navidrome Module.

Plays music from a Navidrome server - your own music, on your own network - through
the assistant's own speaker.

Navidrome speaks the Subsonic API, which this uses to find what to play and to
fetch it. Each song is asked for as an MP3 - Navidrome converts anything else as it
goes - decoded by ffmpeg, and played through speaker.py like everything else the
assistant says. So pausing it while the assistant talks is instant and needs no
internet, where every Spotify command makes a round trip through Spotify's servers.
"""

import hashlib
import logging
import os
import queue
import random
import re
import secrets
import subprocess
import threading
import unicodedata
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np
import requests

from voice_assistant import network
from voice_assistant.music import means_liked_songs

# Logger setup
logger = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "nova"

# Seconds to wait for Navidrome before giving up. It's on the local network, so
# anything slower than this means it's down.
REQUEST_TIMEOUT = 5

# Seconds of a song decoded ahead of what's playing - see Speaker.play_stream.
DECODE_AHEAD = 5.0

# "Previous" this far into a song starts it again, as on most players; sooner than
# this, it goes back to the one before.
RESTART_AFTER = 3.0

# How many of an artist's albums are read when playing the artist, and how many
# songs are queued at most, so a big library isn't read all at once.
MOST_ALBUMS = 10
MOST_SONGS = 200

# Songs in a row which wouldn't play before the queue is given up on, so a broken
# library on repeat can't spin through itself forever.
MOST_FAILURES = 3

# Subsonic's error code for a wrong username or password.
WRONG_LOGIN = 40

# Words too common in playlist names to tell one from another on their own. Asked
# for "light songs" - Whisper's "liked songs" - "Car Songs" was played, on the
# strength of "songs" alone.
GENERIC_PLAYLIST_WORDS = {"songs", "song", "music", "playlist", "playlists", "mix", "tracks", "track",
                          "list", "my", "some", "tunes"}


class NavidromeError(Exception):
    """Navidrome answered, but with an error - a wrong password, say."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def normalise(name: str) -> str:
    """
    A name reduced to plain lowercase words, for comparing what was said with
    what's in the library: "Beyoncé - Halo (Live)!" -> "beyonce halo live".
    """
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    name = re.sub(r"\s+", " ", name).strip()
    return name[4:] if name.startswith("the ") else name


def likeness(found: str, wanted: str) -> int:
    """
    How well a name in the library matches one asked for: 3 for the same, 2 if
    every word asked for is in it, 1 if some are, 0 if none.
    """
    found, wanted = normalise(found), normalise(wanted)
    if not wanted:
        return 0
    if found == wanted:
        return 3
    found_words, wanted_words = set(found.split()), set(wanted.split())
    if wanted_words <= found_words:
        return 2
    return 1 if found_words & wanted_words else 0


def split_by(query: str) -> Tuple[str, Optional[str]]:
    """'Gooba by 6ix9ine' -> ('Gooba', '6ix9ine'); a title alone has no artist."""
    parts = re.split(r"\s+by\s+", query.strip(), flags=re.IGNORECASE)
    if len(parts) >= 2 and parts[-1]:
        return " by ".join(parts[:-1]), parts[-1]
    return query.strip(), None


def distinctive(name: str) -> str:
    """
    A playlist name without the words almost every playlist name has - "Car Songs"
    -> "car" - so sharing only "songs" doesn't make two names a match.
    """
    return " ".join(word for word in normalise(name).split() if word not in GENERIC_PLAYLIST_WORDS)


def best(items: List[Dict], name_key: str, name: str, artist: Optional[str]) -> Optional[Dict]:
    """
    The item which best matches a name, and the artist if one was given - or None
    if nothing matches the name at all.
    """
    scored = [(likeness(item.get(name_key, ""), name) * 2
               + (likeness(item.get("artist", ""), artist) if artist else 0), index, item)
              for index, item in enumerate(items) if likeness(item.get(name_key, ""), name)]
    if not scored:
        return None
    # The highest score, and the earliest of equals - Navidrome lists its best guesses first.
    return max(scored, key=lambda scored_item: (scored_item[0], -scored_item[1]))[2]


class NavidromePlayer:
    """Finds music on Navidrome and plays it through the assistant's speaker."""

    name = "Navidrome"
    # It's on the local network, so it carries on with the internet down.
    needs_internet = False

    def __init__(self, url: str, username: str, password: str, speaker, volume):
        """
        Args:
            url: Navidrome's address, e.g. "http://navidrome.local:4533".
            username: The Navidrome user to sign in as.
            password: Its password.
            speaker: The Speaker to play through.
            volume: The VolumeControl, which keeps the music's own volume.
        """
        self.url = url.rstrip("/")
        self.username = username
        self._password = password
        self.speaker = speaker
        self.volume = volume
        self._session = requests.Session()

        self._lock = threading.Condition()
        # What's playing and what's next, in the order it'll be played, and the
        # order it was in before any shuffling.
        self._queue: List[Dict] = []
        self._unshuffled: List[Dict] = []
        self._index = 0
        # Changed by anything which changes what plays next - skipping, a new
        # request - so the song which was stopped for it doesn't move the queue on
        # as well, as if it had finished by itself.
        self._turn = 0
        self._playback = None
        self._paused = False
        # While the assistant is talking, and whether the music carries on after it -
        # see pause_for_speech.
        self._talking = False
        self._resume_after = False
        self._interrupted = False
        self._repeat = "off"
        self._shuffle = False

        threading.Thread(target=self._play_queue, daemon=True, name="navidrome").start()

    @classmethod
    def from_config(cls, config, password: Optional[str], speaker, volume) -> Optional["NavidromePlayer"]:
        """
        The Navidrome config.yaml points at, or None if it isn't set up.

        Args:
            config: Configs loaded from config.yaml - navidrome.url and username.
            password: NAVIDROME_PASSWORD from .env.
            speaker: The Speaker to play through.
            volume: The VolumeControl.

        Returns:
            A NavidromePlayer, or None without an address, a username and a password.
        """
        settings = config.section("navidrome")
        url, username = (settings.get("url") or "").strip(), (settings.get("username") or "").strip()
        if not url or not username:
            logger.warning("music.player is navidrome, but navidrome.url or navidrome.username isn't set")
            return None
        if not password:
            logger.warning("music.player is navidrome, but NAVIDROME_PASSWORD isn't in .env")
            return None
        logger.info(f"Navidrome at {url}, as {username}")
        return cls(url, username, password, speaker, volume)

    # Talking to Navidrome

    def _sign_in(self) -> Dict[str, str]:
        """
        The parameters which sign each request in. The password itself is never
        sent - only a hash of it with a random salt, new each time, as the Subsonic
        API has it.
        """
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode()).hexdigest()
        return {"u": self.username, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME, "f": "json"}

    def _call(self, endpoint: str, **params) -> Dict:
        """
        One Subsonic API request.

        Raises:
            requests.RequestException: If Navidrome couldn't be reached.
            NavidromeError: If it answered with an error.
        """
        # On the local network, so it's still asked with only the internet down - but
        # not with no network at all, which only waited out looking its name up. See
        # HomeAssistant._request.
        if not network.online():
            raise requests.ConnectionError("No network connection")
        response = self._session.get(f"{self.url}/rest/{endpoint}", params={**self._sign_in(), **params},
                                     timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        body = response.json().get("subsonic-response") or {}
        if body.get("status") != "ok":
            error = body.get("error") or {}
            raise NavidromeError(error.get("message") or "unknown error", error.get("code"))
        return body

    @staticmethod
    def _failure_message(error: Exception) -> str:
        """
        What to say when Navidrome couldn't be asked. Only the kind of error is
        logged, never the error itself, which can hold the request's address -
        and with it the signed-in token.
        """
        logger.error(f"Navidrome request failed: {type(error).__name__}"
                     + (f" ({error})" if isinstance(error, NavidromeError) else ""))
        if isinstance(error, NavidromeError) and error.code == WRONG_LOGIN:
            return "Navidrome didn't accept my password. NAVIDROME_PASSWORD in the .env file needs checking."
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return "I can't reach Navidrome right now."
        return "Navidrome had a problem with that."

    # Finding music

    def _search(self, query: str, songs: int = 0, albums: int = 0, artists: int = 0) -> Dict:
        return self._call("search3", query=query, songCount=songs, albumCount=albums,
                          artistCount=artists).get("searchResult3") or {}

    def _album_songs(self, album_id: str) -> List[Dict]:
        return self._call("getAlbum", id=album_id).get("album", {}).get("song") or []

    def _find_song(self, query: str) -> Tuple[List[Dict], str]:
        """
        A song, followed by more by the same artist, shuffled - as Spotify carries on
        with something similar once the song asked for has finished.
        """
        title, artist = split_by(query)
        found = self._search(f"{title} {artist}" if artist else title, songs=20).get("song") or []
        if not found and artist:
            found = self._search(title, songs=20).get("song") or []
        song = best(found, "title", title, artist)
        if not song:
            return [], ""
        more = [other for other in self._search(song.get("artist", ""), songs=50).get("song") or []
                if other.get("id") != song.get("id")]
        random.shuffle(more)
        return [song] + more, f"{song.get('title')} by {song.get('artist')}"

    def _find_artist(self, query: str) -> Tuple[List[Dict], str]:
        """Everything by an artist, shuffled."""
        artist = best(self._search(query, artists=5).get("artist") or [], "name", query, None)
        if not artist:
            return [], ""
        albums = self._call("getArtist", id=artist["id"]).get("artist", {}).get("album") or []
        songs = [song for album in albums[:MOST_ALBUMS] for song in self._album_songs(album["id"])]
        random.shuffle(songs)
        return songs, artist.get("name", query)

    def _find_album(self, query: str) -> Tuple[List[Dict], str]:
        """An album, in order."""
        title, artist = split_by(query)
        albums = self._search(f"{title} {artist}" if artist else title, albums=10).get("album") or []
        album = best(albums, "name", title, artist)
        if not album:
            return [], ""
        return self._album_songs(album["id"]), f"{album.get('name')} by {album.get('artist')}"

    def _find_playlist(self, name: str) -> Tuple[List[Dict], str]:
        """One of the user's playlists, or their starred songs, shuffled."""
        if means_liked_songs(name):
            return self._liked_songs()
        playlists = self._call("getPlaylists").get("playlists", {}).get("playlist") or []
        wanted = distinctive(name)
        playlist = wanted and best([{**playlist, "distinctive": distinctive(playlist.get("name", ""))}
                                    for playlist in playlists], "distinctive", wanted, None)
        if not playlist:
            return [], ""
        songs = self._call("getPlaylist", id=playlist["id"]).get("playlist", {}).get("entry") or []
        random.shuffle(songs)
        return songs, playlist.get("name", name)

    def _find_genre(self, genre: str) -> Tuple[List[Dict], str]:
        """A genre or mood: a playlist of that name if there is one, or songs tagged with it."""
        songs, said = self._find_playlist(genre)
        if songs:
            return songs, said
        songs = self._call("getSongsByGenre", genre=genre, count=MOST_SONGS).get("songsByGenre", {}).get("song") or []
        random.shuffle(songs)
        return songs, f"some {genre}"

    def _liked_songs(self) -> Tuple[List[Dict], str]:
        songs = self._call("getStarred2").get("starred2", {}).get("song") or []
        random.shuffle(songs)
        return songs, "your liked songs"

    # What the assistant asks for

    def play(self, query: Optional[str] = None, search_type: Optional[str] = None) -> Dict:
        """
        Plays something, or carries on with what was playing.

        Args:
            query: What to play, e.g. "Gooba by 6ix9ine", or None to carry on - or,
                with nothing to carry on with, to play the liked songs.
            search_type: "track", "artist", "album" or "playlist" (a genre or mood).

        Returns:
            Dict with 'success' and a 'message' - silent when carrying on, since the
            music coming back says so.
        """
        try:
            if not query:
                with self._lock:
                    if self._index < len(self._queue):
                        self._carry_on()
                        return {"success": True, "message": "Resumed playback", "silent": True}
                songs, said = self._liked_songs()
                if not songs:
                    songs = self._call("getRandomSongs", size=MOST_SONGS).get("randomSongs", {}).get("song") or []
                    said = "something from your library"
            elif search_type == "artist":
                songs, said = self._find_artist(query)
            elif search_type == "album":
                songs, said = self._find_album(query)
            elif search_type == "playlist":
                songs, said = self._find_genre(query)
            else:
                songs, said = self._find_song(query)
        except (requests.RequestException, NavidromeError, ValueError) as e:
            return {"success": False, "message": self._failure_message(e)}

        if not songs:
            return {"success": False, "message": f"I couldn't find {query or 'anything to play'} on Navidrome."}
        self._start(songs[:MOST_SONGS])
        logger.info(f"Playing {said} ({len(songs)} song{'s' if len(songs) != 1 else ''} queued)")
        return {"success": True, "message": f"Playing {said}"}

    def play_playlist(self, name: str) -> Dict:
        """
        Plays one of the user's playlists, shuffled.

        Args:
            name: The playlist name as the user said it.
        """
        try:
            songs, said = self._find_playlist(name)
        except (requests.RequestException, NavidromeError, ValueError) as e:
            return {"success": False, "message": self._failure_message(e)}
        if not songs:
            return {"success": False, "message": f"I couldn't find a playlist called {name}."}
        self._start(songs[:MOST_SONGS])
        return {"success": True, "message": f"Playing {said}"}

    def pause(self) -> Dict:
        with self._lock:
            self._set_paused(True)
        return {"success": True, "message": "Paused", "chime": True}

    def skip(self) -> Dict:
        with self._lock:
            if self._index >= len(self._queue):
                return {"success": False, "message": "There's nothing playing to skip."}
            following = self._index + 1
            if following >= len(self._queue) and self._repeat == "all":
                following = 0
            self._jump(following)
        return {"success": True, "message": "Skipped", "silent": True}

    def previous(self) -> Dict:
        with self._lock:
            if not self._queue:
                return {"success": False, "message": "There's nothing to go back to."}
            # Once the queue has run out, the last song is the one to go back to.
            index = min(self._index, len(self._queue) - 1)
            played = self._playback.position / self.speaker.sample_rate if self._playback else 0
            if played > RESTART_AFTER or index == 0:
                self._jump(index)
                return {"success": True, "message": "Starting it again", "silent": True}
            self._jump(index - 1)
        return {"success": True, "message": "Previous track", "silent": True}

    def restart(self) -> Dict:
        with self._lock:
            if self._index >= len(self._queue):
                return {"success": False, "message": "There's nothing playing to start again."}
            self._jump(self._index)
        return {"success": True, "message": "Starting it again", "silent": True}

    def repeat(self, mode: str) -> Dict:
        mode = "all" if mode in ("all", "context", "playlist", "album") else mode
        messages = {"off": "Repeat off", "track": "Repeating this track", "all": "Repeating everything"}
        if mode not in messages:
            return {"success": False, "message": f"I don't know how to repeat '{mode}'."}
        with self._lock:
            self._repeat = mode
        return {"success": True, "message": messages[mode], "chime": True}

    def shuffle(self, enabled: bool) -> Dict:
        """Shuffles what's still to come, or puts it back in its own order."""
        with self._lock:
            self._shuffle = enabled
            if self._index < len(self._queue):
                current = self._queue[self._index]
                if enabled:
                    rest = self._queue[self._index + 1:]
                    random.shuffle(rest)
                    self._queue = self._queue[:self._index + 1] + rest
                else:
                    self._queue = list(self._unshuffled)
                    self._index = next((index for index, song in enumerate(self._queue) if song is current), 0)
        return {"success": True, "message": "Shuffle on" if enabled else "Shuffle off", "chime": True}

    def current_track(self) -> Dict:
        with self._lock:
            if self._index >= len(self._queue):
                return {"success": False, "message": "Nothing playing"}
            song = self._queue[self._index]
        return {"success": True, "track": song.get("title", "something"), "artist": song.get("artist", "someone")}

    def get_volume(self) -> int:
        return self.volume.percent("music")

    def set_volume(self, percent) -> Dict:
        result = self.volume.set("music", percent)
        with self._lock:
            if self._playback is not None:
                self._playback.volume = self.volume.get("music")
        return result

    # Making way for the assistant - see MusicHold

    def pause_for_speech(self):
        """
        Pauses the music while the assistant talks. Anything asked for meanwhile -
        a new song, the next one - waits too, so it starts once the reply is over
        rather than under it.
        """
        with self._lock:
            self._interrupted = self._index < len(self._queue) and not self._paused
            # Only music which was actually playing comes back afterwards. Music the
            # user had already stopped used to start again after the next thing they
            # said, because every conversation ended by un-pausing it.
            self._resume_after = self._interrupted
            self._talking = True
            self._set_paused(True)

    def resume_after_speech(self):
        """Lets the music carry on, if it was playing or asked for while the assistant talked."""
        with self._lock:
            self._talking = False
            if self._resume_after:
                self._resume_after = False
                self._set_paused(False)

    def cancel_resume(self):
        """Keeps the music paused once the assistant has finished - it was asked to pause."""
        with self._lock:
            self._resume_after = False

    def _carry_on(self):
        """
        Plays on - straight away, or once the assistant has finished talking. Called
        with the lock held.
        """
        if self._talking:
            self._resume_after = True
        else:
            self._set_paused(False)

    def was_playing(self) -> bool:
        """Whether music was playing when the user started speaking."""
        return self._interrupted

    def close(self):
        """Stops the music."""
        with self._lock:
            self._queue, self._index = [], 0
            self._jump(0)

    # Playing the queue - called with the lock held

    def _set_paused(self, paused: bool):
        self._paused = paused
        if self._playback is not None:
            self._playback.paused = paused

    def _start(self, songs: List[Dict]):
        """Plays these instead of whatever was queued - paused while the assistant is talking."""
        with self._lock:
            self._unshuffled = list(songs)
            self._queue = list(songs)
            self._paused = self._talking
            if self._talking:
                self._resume_after = True
            self._jump(0)

    def _jump(self, index: int):
        """Plays the song at this place in the queue, stopping the one playing."""
        self._index = index
        self._turn += 1
        if self._playback is not None:
            self._playback.stop()
        self._lock.notify_all()

    def _play_queue(self):
        """Plays the queue, a song at a time, for as long as the assistant runs."""
        failures = 0
        while True:
            with self._lock:
                while self._index >= len(self._queue):
                    self._lock.wait()
                song, turn = self._queue[self._index], self._turn
                playback = self.speaker.play_stream(self._decode(song), volume=self.volume.get("music"),
                                                    kind="music", paused=self._paused, most_ahead=DECODE_AHEAD)
                self._playback = playback

            # Not playback.wait(), which gives up on a sound which stops moving -
            # as a paused song rightly does, for as long as it likes.
            while not playback.wait(timeout=1.0):
                pass

            with self._lock:
                if self._turn != turn:
                    continue
                # It finished by itself: on to the next, unless none of it played.
                failures = failures + 1 if playback.duration == 0 else 0
                if failures >= MOST_FAILURES:
                    logger.error(f"{failures} songs in a row wouldn't play, so the queue is stopped")
                    self._queue, failures = [], 0
                elif self._repeat != "track":
                    following = self._index + 1
                    self._index = 0 if following >= len(self._queue) and self._repeat == "all" else following

    def _open_song(self, song_id: str) -> requests.Response:
        """Starts downloading a song from Navidrome, as an MP3 whatever it's stored as."""
        if not network.online():
            raise requests.ConnectionError("No network connection")
        response = self._session.get(f"{self.url}/rest/stream",
                                     params={**self._sign_in(), "id": song_id, "format": "mp3", "maxBitRate": 320},
                                     timeout=REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()
        if not response.headers.get("Content-Type", "").startswith("audio"):
            response.close()
            raise NavidromeError("Navidrome sent something other than audio")
        return response

    def _decode(self, song: Dict) -> Iterator[np.ndarray]:
        """
        A song as stereo audio at the speaker's rate, a piece at a time.

        The download and the decoding are kept apart: the song downloads as fast as
        Navidrome sends it, into memory - a few megabytes - while ffmpeg only
        decodes as fast as it's played. Feeding the download straight into ffmpeg
        would hold the connection still for as long as the music was paused, and
        Navidrome would give up on it.
        """
        response = ffmpeg = None
        try:
            response = self._open_song(song["id"])
            ffmpeg = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "2",
                 "-ar", str(self.speaker.sample_rate), "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            downloaded: "queue.Queue[Optional[bytes]]" = queue.Queue()
            threading.Thread(target=self._download, args=(response, downloaded), daemon=True,
                             name="navidrome-download").start()
            threading.Thread(target=self._pass_on, args=(downloaded, ffmpeg.stdin), daemon=True,
                             name="navidrome-decode").start()

            frame = 8  # two channels of four-byte samples
            leftover = b""
            decoded = False
            while True:
                data = os.read(ffmpeg.stdout.fileno(), 65536)
                if not data:
                    break
                data = leftover + data
                whole = len(data) - len(data) % frame
                leftover = data[whole:]
                if whole:
                    decoded = True
                    yield np.frombuffer(data[:whole], dtype="<f4").reshape(-1, 2)
            if not decoded:
                ffmpeg.wait()
                logger.error(f"Couldn't play '{song.get('title')}': "
                             f"{ffmpeg.stderr.read(2000).decode(errors='replace').strip()}")
        except (requests.RequestException, NavidromeError, OSError) as e:
            self._failure_message(e)
        finally:
            if ffmpeg is not None:
                if ffmpeg.poll() is None:
                    ffmpeg.kill()
                ffmpeg.wait()
                ffmpeg.stdout.close()
                ffmpeg.stderr.close()
            if response is not None:
                response.close()

    @staticmethod
    def _download(response: requests.Response, into: "queue.Queue[Optional[bytes]]"):
        """Downloads a song into memory as fast as Navidrome sends it."""
        try:
            for chunk in response.iter_content(65536):
                into.put(chunk)
        except (requests.RequestException, OSError, ValueError) as e:
            logger.warning(f"A song stopped downloading part way: {type(e).__name__}")
        finally:
            into.put(None)

    @staticmethod
    def _pass_on(downloaded: "queue.Queue[Optional[bytes]]", into):
        """Hands the download to ffmpeg as fast as it decodes it."""
        try:
            while (chunk := downloaded.get()) is not None:
                into.write(chunk)
        except (BrokenPipeError, ValueError, OSError):
            # ffmpeg was stopped - the song was skipped.
            pass
        finally:
            try:
                into.close()
            except OSError:
                pass
