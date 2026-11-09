"""
Downloads Module.

Watches a song or album SoulSync was asked for until Navidrome has it, then says
how it went - on the satellite that asked, through the say it's given. Each request
is watched on a thread of its own, so waiting never holds up a conversation, and
several can wait at once.

SoulSync saying a download is done isn't enough: it's downloaded only once Navidrome
has it, and only what wasn't in Navidrome when it was asked for counts, so an old
clean copy isn't taken for the explicit one just asked for. Nothing here plays or
deletes a file. See docs/decisions/0012-music-fetched-by-voice.md.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Union

import requests

from voice_assistant import music
from voice_assistant.navidrome import (VERSIONS, NavidromeError, NavidromeLibrary, _says_version, is_clean, likeness,
                                       normalise, split_by)
from voice_assistant.soulsync import AlbumRequest, Progress, SoulSync, SoulSyncError

# Logger setup
logger = logging.getLogger(__name__)

# Seconds between asking SoulSync and Navidrome how a download is getting on. Both
# are on the local network, and a couple of small requests a poll are nothing to either.
POLL_EVERY = 15

# Seconds after SoulSync says it's done before Navidrome, still without it, is asked
# to scan its folders. It finds new files in its own time, which can be a while.
SCAN_AFTER = 60

NOT_SET_UP = ("Downloading music isn't set up. SoulSync's address goes in Settings, under Music, "
              "and its key under Keys.")

# Said after "I couldn't download X." when SoulSync queued none of an album: it only
# queues the tracks it hasn't got and isn't already getting.
NOTHING_QUEUED = "SoulSync says it already has every track, or is already getting them."

# The time limits, in minutes from the request, when the settings don't say - as
# config.yaml has them.
SONG_MINUTES = 10
ALBUM_MINUTES = 30

# What asking Navidrome can raise: out of reach, an error answered, or an answer
# that wasn't JSON.
NAVIDROME_ERRORS = (requests.RequestException, NavidromeError, ValueError)

# A bracketed part of a title, as navidrome.version_of finds them.
BRACKETED = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]")


@dataclass(frozen=True)
class Wanted:
    """
    A song or album asked for, as it was asked for.

    Attributes:
        title: The song's title or the album's name.
        artist: Its artist, or None if only the title was given.
        kind: "track" or "album".
    """

    title: str
    artist: Optional[str]
    kind: str

    @classmethod
    def from_query(cls, query: str, search_type: Optional[str]) -> "Wanted":
        """
        What a request asks for: "Gooba by 6ix9ine" is Gooba, by 6ix9ine.

        Args:
            query: The title, with " by " and the artist if one was given.
            search_type: "album" for an album; anything else is a song.

        Returns:
            The Wanted.
        """
        title, artist = split_by(query)
        return cls(title, artist, "album" if search_type == "album" else "track")

    @property
    def name(self) -> str:
        """How it's named in what's said: "Gooba by 6ix9ine", or the title alone."""
        return f"{self.title} by {self.artist}" if self.artist else self.title

    @property
    def key(self) -> str:
        """
        The same for the same request however it was spelt or cased - "gooba by
        6IX9INE" is Gooba by 6ix9ine - to tell whether it's already waiting.
        """
        return f"{self.kind}: {normalise(self.name)}"


def _couldnt(wanted: Wanted, reason: Optional[str]) -> str:
    """'I couldn't download X.', then the reason if there is one."""
    return f"I couldn't download {wanted.name}." + (f" {reason}" if reason else "")


def _says_a_version(part: str) -> bool:
    """Whether a bracketed or dashed part of a title says which version it is, as version_of reads one."""
    return any(_says_version(part, version) for version in VERSIONS)


def _bare_title(title: str) -> str:
    """
    A track's title without any part saying which version it is, normalised, so each
    track is counted once: "Dreams (Clean)" and "Dreams - Radio Edit" are "dreams",
    as Dreams is. "Dreams (2004 Remaster)" keeps its remaster, which is no version.
    """
    head, _, tail = title.partition(" - ")
    if tail and _says_a_version(tail):
        title = head
    title = BRACKETED.sub(lambda part: "" if _says_a_version(part.group(1) or part.group(2) or "")
                          else part.group(0), title)
    return normalise(title)


def _album_tracks(request: AlbumRequest, new: List[Dict]) -> List[Dict]:
    """
    The new tracks which are the album's, one a title: a clean copy and the explicit
    one of the same track - "Dreams (Clean)" and "Dreams" - are one track, the
    explicit one, as the album has it. With SoulSync's titles, only a new track with
    every word of one of them counts.

    Args:
        request: The AlbumRequest SoulSync gave.
        new: The tracks of the album in Navidrome which weren't there before the request.

    Returns:
        The tracks which have landed, one for each title.
    """
    # Sorted is stable, so this only moves the clean ones after the rest.
    explicit_first = sorted(new, key=is_clean)
    if request.tracks:
        explicit_first = [track for track in explicit_first
                          if any(likeness(track.get("title") or "", title) >= 2 for title in request.tracks)]
    tracks: Dict[str, Dict] = {}
    for track in explicit_first:
        tracks.setdefault(_bare_title(track.get("title") or ""), track)
    return list(tracks.values())


def _clean_ones(wanted: Wanted, tracks: List[Dict]) -> List[Dict]:
    """The clean versions among the tracks which came down - logged, so the user can replace them."""
    clean = [track for track in tracks if is_clean(track)]
    if clean:
        logger.warning(f"Clean versions came down for {wanted.name}, to be replaced by hand: "
                       + "; ".join(f"{track.get('title')} on {track.get('album')}" for track in clean))
    return clean


def _song_outcome(wanted: Wanted, new: List[Dict]) -> Optional[str]:
    """
    What to say of a song once a copy of it is new in Navidrome - None while none
    is. One that isn't clean is the download; if every one is, it came down clean.
    """
    if not new:
        return None
    if all(is_clean(track) for track in new):
        _clean_ones(wanted, new)
        return f"{wanted.name} came down as a clean version."
    return f"{wanted.name} has been downloaded."


def _album_downloaded(wanted: Wanted, tracks: List[Dict]) -> str:
    """What to say of an album which has landed, with how many of its tracks came down clean."""
    clean = len(_clean_ones(wanted, tracks))
    if clean == 1:
        return f"{wanted.name} has been downloaded, but one of its tracks came down as a clean version."
    if clean:
        return f"{wanted.name} has been downloaded, but {clean} of its tracks came down as clean versions."
    return f"{wanted.name} has been downloaded."


class Downloads:
    """Watches each download asked for, on a thread of its own, and says how it went."""

    def __init__(self, config, say: Callable[[Optional[str], str], None],
                 soulsync: Optional[Callable[[], Optional[SoulSync]]] = None,
                 library: Optional[Callable[[], Optional[NavidromeLibrary]]] = None,
                 now: Callable[[], float] = time.monotonic, wait: Optional[Callable[[float], bool]] = None):
        """
        Args:
            config: Configs loaded from config.yaml. The time limits are read from it
                at each check, so a change on the settings page is used straight away.
            say: Says a message about a download, given the satellite that asked -
                or None - and the message.
            soulsync: Makes the SoulSync to ask, or gives None if it isn't set up. By
                default from soulsync.url and SOULSYNC_API_KEY as they are then.
            library: Makes the NavidromeLibrary to look in, or gives None if it isn't
                set up. By default one of its own, from navidrome.url,
                navidrome.username and NAVIDROME_PASSWORD as they are then - never
                the player's, as a requests session isn't safe to share between threads.
            now: The time, in seconds, for the time limits.
            wait: Waits the seconds given between polls, and says whether Nova is
                stopping - by default the wait of the event stop sets, which it
                cuts short.
        """
        self.config = config
        self._say = say
        self._soulsync = soulsync or (lambda: SoulSync.from_config(config, os.getenv("SOULSYNC_API_KEY")))
        self._library = library or self._own_library
        self._now = now
        self._stopping = threading.Event()
        self._wait = wait or self._stopping.wait
        self._lock = threading.Lock()
        # What's waiting, by its key, in the order it was asked for.
        self._waiting: Dict[str, Wanted] = {}

    def _own_library(self) -> Optional[NavidromeLibrary]:
        """A NavidromeLibrary from the settings as they are now, or None if they're incomplete."""
        if music._missing(self.config, "navidrome"):
            return None
        settings = self.config.section("navidrome")
        return NavidromeLibrary(settings["url"].strip(), settings["username"].strip(), os.getenv("NAVIDROME_PASSWORD"))

    def problem(self) -> Optional[str]:
        """
        What stops a download being asked for, as it's said.

        Returns:
            NOT_SET_UP without SoulSync's address or key, else what Navidrome is
            missing, as the settings page says it - or None if nothing is.
        """
        address = (self.config.section("soulsync").get("url") or "").strip()
        if not address or not (os.getenv("SOULSYNC_API_KEY") or "").strip():
            return NOT_SET_UP
        return music._missing(self.config, "navidrome")

    def start(self, wanted: Wanted, satellite: Optional[str]) -> bool:
        """
        Starts watching a request, on a thread of its own.

        Args:
            wanted: What was asked for.
            satellite: The satellite that asked, which is told how it went - or None.

        Returns:
            False if the same request is already waiting, so nothing was started;
            else True.
        """
        with self._lock:
            if wanted.key in self._waiting:
                return False
            self._waiting[wanted.key] = wanted
        threading.Thread(target=self._watch, args=(wanted, satellite), daemon=True, name="download").start()
        return True

    def waiting(self) -> List[str]:
        """The names of the requests waiting, in the order they were asked for."""
        with self._lock:
            return [wanted.name for wanted in self._waiting.values()]

    def stop(self):
        """
        Ends every watch, as Nova stops. Whatever is still waiting is forgotten, and
        logged, and nothing more is said of it - SoulSync carries on, so anything
        which lands later is simply in Navidrome.
        """
        with self._lock:
            forgotten = [wanted.name for wanted in self._waiting.values()]
            self._waiting.clear()
            self._stopping.set()
        if forgotten:
            logger.warning(f"Forgot {len(forgotten)} download{'' if len(forgotten) == 1 else 's'} still waiting, "
                           f"as Nova is stopping: {', '.join(forgotten)}")

    def _watch(self, wanted: Wanted, satellite: Optional[str]) -> str:
        """
        The whole of one request: asks SoulSync for it, says it's requested, then
        asks SoulSync and Navidrome every POLL_EVERY seconds until it has landed,
        failed or run out of time, and says which.

        Args:
            wanted: What was asked for.
            satellite: The satellite that asked, or None.

        Returns:
            The last message said, or "" if Nova stopped first.
        """
        try:
            return self._follow(wanted, satellite)
        except Exception:
            # Anything unforeseen still ends in an answer, so nobody waits for one
            # which never comes.
            logger.exception(f"Watching the download of {wanted.name} failed")
            return self._tell(satellite, _couldnt(wanted, None))
        finally:
            with self._lock:
                if self._waiting.get(wanted.key) is wanted:
                    del self._waiting[wanted.key]

    def _follow(self, wanted: Wanted, satellite: Optional[str]) -> str:
        """The watch itself, as _watch describes it - which answers even when this goes wrong."""
        soulsync, library = self._soulsync(), self._library()
        if soulsync is None or library is None:
            # Set up when it was asked for, but not now: the settings changed in between.
            return self._tell(satellite, _couldnt(wanted, self.problem()))

        before = self._had(library, wanted)
        started = self._now()
        try:
            request = (soulsync.request_album(wanted.title, wanted.artist) if wanted.kind == "album"
                       else soulsync.request_song(wanted.title, wanted.artist))
        except SoulSyncError as e:
            return self._tell(satellite, _couldnt(wanted, e.reason))
        if isinstance(request, AlbumRequest) and request.queued <= 0:
            # Nothing of it is coming, so there's nothing to wait for.
            return self._tell(satellite, _couldnt(wanted, NOTHING_QUEUED))
        self._tell(satellite, f"{wanted.name} requested.")

        new: List[Dict] = []
        done_at: Optional[float] = None
        scanned = False
        while not self._wait(POLL_EVERY):
            if self._now() - started >= self._limit(wanted):
                new = self._look(library, wanted, before, new)
                return self._tell(satellite, self._last_word(wanted, request, new))
            if done_at is None:
                progress = self._progress(soulsync, wanted, request)
                if progress is not None and progress.state == "failed":
                    return self._tell(satellite, self._last_word(wanted, request, new, progress.reason))
                if progress is not None and progress.state == "done":
                    done_at = self._now()
            new = self._look(library, wanted, before, new)
            outcome = self._landed(wanted, request, new)
            if outcome:
                return self._tell(satellite, outcome)
            if done_at is not None and not scanned and self._now() - done_at >= SCAN_AFTER:
                # Once only: a scan Navidrome refused - Nova's user not an admin - or
                # one which didn't find it won't do better asked again.
                scanned = True
                logger.info(f"SoulSync is done with {wanted.name}, but Navidrome hasn't got it yet, "
                            f"so it's asked to scan")
                library.start_scan()
        return ""

    def _tell(self, satellite: Optional[str], message: str) -> str:
        """
        Says a message about a download, unless Nova is stopping - then nothing
        more is said of any of them.

        Returns:
            The message, or "" if it wasn't said.
        """
        if self._stopping.is_set():
            return ""
        logger.info(f"For {f'the {satellite} satellite' if satellite else 'no satellite'}: {message}")
        self._say(satellite, message)
        return message

    def _limit(self, wanted: Wanted) -> float:
        """The time limit for the kind asked for, in seconds, from the settings as they are now."""
        settings = self.config.section("music")
        if wanted.kind == "album":
            return float(settings.get("album_download_minutes", ALBUM_MINUTES)) * 60
        return float(settings.get("song_download_minutes", SONG_MINUTES)) * 60

    @staticmethod
    def _find(library: NavidromeLibrary, wanted: Wanted) -> List[Dict]:
        """Every copy of the song, or every track of the album, Navidrome has."""
        if wanted.kind == "album":
            return library.find_album_tracks(wanted.title, wanted.artist)
        return library.find_songs(wanted.title, wanted.artist)

    def _had(self, library: NavidromeLibrary, wanted: Wanted) -> Set[str]:
        """
        Navidrome's ids for what it had of the request before it was made - none if
        Navidrome couldn't be asked, when anything of it found later counts.
        """
        try:
            return {track.get("id") for track in self._find(library, wanted)}
        except NAVIDROME_ERRORS as e:
            NavidromeLibrary._failure_message(e)
            logger.warning(f"Couldn't see what Navidrome had of {wanted.name} before asking for it, "
                           f"so anything of it found counts as the download")
            return set()

    def _look(self, library: NavidromeLibrary, wanted: Wanted, before: Set[str], last: List[Dict]) -> List[Dict]:
        """
        What Navidrome has of the request now which it hadn't before - or, if it
        can't be asked this time, what it had last time, and the watch carries on.
        """
        try:
            found = self._find(library, wanted)
        except NAVIDROME_ERRORS as e:
            NavidromeLibrary._failure_message(e)
            logger.warning(f"Couldn't look for {wanted.name} in Navidrome this time, so the watch carries on")
            return last
        return [track for track in found if track.get("id") not in before]

    @staticmethod
    def _progress(soulsync: SoulSync, wanted: Wanted, request: Union[str, AlbumRequest]) -> Optional[Progress]:
        """How SoulSync says the request is getting on - None if it can't be asked this time."""
        try:
            if wanted.kind == "album":
                return soulsync.album_progress(request)
            return soulsync.song_progress(request)
        except SoulSyncError:
            logger.warning(f"Couldn't ask SoulSync how {wanted.name} is getting on this time, so the watch carries on")
            return None

    @staticmethod
    def _landed(wanted: Wanted, request: Union[str, AlbumRequest], new: List[Dict]) -> Optional[str]:
        """
        What to say if the request has landed, or None to wait on. A song has once
        a copy of it is new in Navidrome; an album once as many of its tracks are
        new as SoulSync queued - nine of Rumours' eleven, with the other two had.
        """
        if wanted.kind != "album":
            return _song_outcome(wanted, new)
        tracks = _album_tracks(request, new)
        return _album_downloaded(wanted, tracks) if len(tracks) >= request.queued else None

    @staticmethod
    def _last_word(wanted: Wanted, request: Union[str, AlbumRequest], new: List[Dict],
                   reason: Optional[str] = None) -> str:
        """
        What to say once the watch ends without seeing all of it land - failed, or
        out of time: what did land, or that it couldn't be downloaded.

        Args:
            wanted: What was asked for.
            request: SoulSync's id for a song, or its AlbumRequest.
            new: What's new in Navidrome, at its last look.
            reason: Why SoulSync said it failed, if it did and said why.
        """
        if wanted.kind != "album":
            return _song_outcome(wanted, new) or _couldnt(wanted, reason)
        tracks = _album_tracks(request, new)
        if tracks and len(tracks) < request.queued:
            _clean_ones(wanted, tracks)
            return f"Only {len(tracks)} of the {request.queued} tracks of {wanted.name} came down."
        if tracks:
            # All of it, at the last look.
            return _album_downloaded(wanted, tracks)
        return _couldnt(wanted, reason)
