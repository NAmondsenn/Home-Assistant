"""
SoulSync Module.

Asks SoulSync for a song or an album Navidrome hasn't got, and how it's getting on.
SoulSync finds music on Soulseek, tags it and files it straight into the folder
Navidrome scans, so a download needs no moving. Nothing here plays or deletes a file.

Both go on SoulSync's wishlist, which is what files a download into the library -
its REST API's own song request only downloads, and leaves the file where it landed.
A song goes through the REST API: a search for the track, the one pick_track chooses
put on the wishlist, and the wishlist started. An album goes through its MCP server,
since the REST API has no album request: find_album searches for it, request_album
puts its tracks on the wishlist, and process_wishlist starts them downloading now
rather than at the wishlist's next run. SoulSync gives no id to follow either by, so
whether one has come down is for Navidrome to say.
"""

import difflib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from voice_assistant import network
from voice_assistant.navidrome import (BRACKETED, CLEAN_VERSIONS, DASH, VERSIONS, artist_likeness, is_clean, likeness,
                                       normalise)

# Logger setup
logger = logging.getLogger(__name__)

# Seconds to wait for SoulSync's REST API, which only notes a request and answers.
REQUEST_TIMEOUT = 10

# Seconds to wait for its search for a song, which asks Deezer, beyond the local
# network, before it answers - as find_album asks MusicBrainz.
SEARCH_TIMEOUT = 30

# Seconds to wait for its MCP tools: find_album searches MusicBrainz, beyond the
# local network, before it answers.
MCP_TIMEOUT = 30

# How many songs a search is asked for: enough for pick_track to find the one asked
# for past the live and demo versions. "Gary Numan - M.E." found the studio one,
# then live and demo ones.
SEARCH_LIMIT = 10

# Why a song is on SoulSync's wishlist, as its own page shows.
WISHLIST_REASON = "Asked for by voice through Nova"

# How many albums find_album is asked for - the most it takes - so pick_album has
# every edition to choose from, past the clean and deluxe ones. A popular album
# has many: Thriller had eight releases of the album and two of the single.
FIND_ALBUM_LIMIT = 25

# The reasons said after "I couldn't download X."
NOT_ANSWERING = "SoulSync isn't answering."
KEY_REFUSED = "SoulSync didn't accept its key, so SOULSYNC_API_KEY under Keys needs checking."
NOT_FOUND = "SoulSync couldn't find it."
MCP_OFF = "SoulSync's MCP is off, so it can't download albums. Turn it on in SoulSync's settings."

# Words which, anywhere in a note on an album's edition, say it's had the swearing
# taken out - see _is_clean_edition.
CLEAN_EDITION_WORDS = CLEAN_VERSIONS

# The versions of an album which aren't the one asked for unless they're named.
OTHER_VERSIONS = tuple(version for version in VERSIONS if version not in CLEAN_VERSIONS)

# The versions of a song which aren't the one asked for unless they're named: an
# album's, a demo - a search for M.E. found "M.E. (Demo Version)" as well as the
# studio one - and an unplugged one. A part ending in "mix" is one too: see
# _another_version. Only for picking: navidrome.VERSIONS, which "play" uses, is as it was.
SONG_VERSIONS = OTHER_VERSIONS + ("demo", "unplugged")

# The parts which name the song itself, not another version of it: on an electronic
# release, "Strobe (Original Mix)" is the original.
ORIGINALS = ("original mix", "original version")

# How a part which credits another artist starts, once normalised - never a version,
# whatever the artist is called: "(with Little Mix)", "(feat. Live Wire)".
CREDITS = ("feat ", "ft ", "featuring ", "with ")

# How alike an artist's name must be to the one asked for (difflib's ratio, from 0 to
# 1) to be taken for a mishearing of it - and then only for exactly the title asked
# for. Speech to text heard Gunna as "Gunner", which is 0.73; Mac Miller is 0.26 from
# Fleetwood Mac.
CLOSE_SPELLING = 0.7


class SoulSyncError(Exception):
    """SoulSync couldn't be asked, or couldn't do what it was asked."""

    def __init__(self, reason: Optional[str] = None):
        """
        Args:
            reason: The sentence to say after "I couldn't download X.", or None if
                there's nothing more useful to say than that.
        """
        super().__init__(reason or "SoulSync failed")
        self.reason = reason


@dataclass
class Progress:
    """How a request is getting on: "waiting", "done" or "failed", with a reason if it failed."""

    state: str
    reason: Optional[str] = None


@dataclass
class SongRequest:
    """
    A song SoulSync found for what was asked for.

    Attributes:
        title: Its title as SoulSync has it - "endless" for "Endless".
        artist: Its first artist as SoulSync has it - "Gunna" for a misheard
            "Gunner" - or None if it gave none.
        track: The track as SoulSync's search found it, for its wishlist.
    """

    title: str
    artist: Optional[str]
    track: Dict


@dataclass
class AlbumRequest:
    """
    An album SoulSync was asked for.

    Attributes:
        id: SoulSync's id for the request, to follow it by - None, as it gives none.
        tracks: The titles of the album's tracks, as SoulSync gave them - empty, as
            it gives only how many there are.
        track_count: How many tracks SoulSync said the album has.
        queued: How many of them it queued to download - not the ones it already
            had, or was already getting. Those are what's waited for: with two of
            Rumours' eleven in the library, nine is all of it.
    """

    id: Optional[str]
    tracks: List[str]
    track_count: int
    queued: int


def _said(message: Any) -> str:
    """
    SoulSync's own words as a reason, with one full stop: "Nobody had it" ->
    'SoulSync said: Nobody had it.' Whatever it gave is read as text, so an error
    which comes as a number still ends in an answer: 42 -> 'SoulSync said: 42.'
    """
    message = str(message).strip()
    return f"SoulSync said: {message}" + ("" if message.endswith((".", "!", "?")) else ".")


def _names(text: str, words: str) -> bool:
    """Whether text has the word or words, whole: "inexplicit" has no "explicit"."""
    return f" {words} " in f" {normalise(text)} "


def _parts(title: str) -> List[str]:
    """
    Each bracketed or dashed part of a title - after a hyphen, an en dash or an em
    dash alike: "M.E. (Live 79)" -> ["Live 79"]. Each dashed part is one of its own,
    so a credit can't hide a version after it: "Gooba - feat. X - Live" -> ["feat. X",
    "Live"].
    """
    parts = [text for brackets in BRACKETED.findall(title) for text in brackets if text]
    return parts + DASH.split(title)[1:]


def _edition_notes(candidate: Dict) -> List[str]:
    """
    What says which edition an album find_album found is: its MusicBrainz
    disambiguation, a free note such as "clean, deluxe edition", and each bracketed
    or dashed part of its title. Each is read on its own, so a ")" in the
    disambiguation can't run into the title's brackets.
    """
    return _parts(candidate.get("title") or "") + [str(candidate.get("disambiguation") or "")]


def _is_clean_edition(candidate: Dict) -> bool:
    """
    Whether an album find_album found has had the swearing taken out. A clean word
    anywhere in a note on the edition counts - "Clean Edition", "clean / Target
    exclusive" - unlike in a track's title, where version_of takes "(feat. Clean
    Bandit)" for a credit.
    """
    return any(_names(note, word) for note in _edition_notes(candidate) for word in CLEAN_EDITION_WORDS)


def _version(candidate: Dict) -> Optional[str]:
    """
    Which other version of an album find_album found is - "live", "remix" and so on,
    as VERSIONS names them - from its edition notes, or from MusicBrainz's secondary
    types, which is where it says an album is live or a remix album.
    """
    notes = _edition_notes(candidate) + [str(kind) for kind in candidate.get("secondary_types") or []]
    return next((version for version in OTHER_VERSIONS if any(_names(note, version) for note in notes)), None)


def pick_album(candidates: List[Dict], title: str, artist: Optional[str]) -> Optional[Dict]:
    """
    The album find_album found which is the one asked for - never a clean edition,
    and a live album, a remix album or the like only when the title asked names it.
    Of the rest, the closest is scored as navidrome.best scores. Among equals, an
    album comes before a single or an EP of the same name - "Thriller" is both - then
    an edition MusicBrainz notes as explicit, then the earliest, since SoulSync lists
    its best guesses first.

    Args:
        candidates: Albums as find_album lists them, each with its title, artists,
            type, disambiguation and secondary types.
        title: The album as asked for.
        artist: Its artist, or None if only the title was given.

    Returns:
        One of the candidates, or None if none of them will do.
    """
    scored = []
    for index, candidate in enumerate(candidates):
        version = _version(candidate)
        if _is_clean_edition(candidate) or (version and not _names(title, version)):
            continue
        title_score = likeness(candidate.get("title") or "", title)
        # Each artist on their own, and - for "Jay-Z and Kanye West", whom MusicBrainz
        # lists as two - all of them together.
        names = [str(name) for name in candidate.get("artists") or []]
        names += [" ".join(names)] if len(names) > 1 else []
        artist_score = max((artist_likeness(name, artist) for name in names), default=0) if artist else 0
        # Asked for by an artist, an album is theirs only with every word of their
        # name, as navidrome's _asked_for has it - Mac Miller's isn't Fleetwood
        # Mac's. Downloaded, an album nobody asked for would be in the library for good.
        if not title_score or (artist and artist_score < 2):
            continue
        is_album = str(candidate.get("type") or "").lower() == "album"
        explicit = any(_names(note, "explicit") for note in _edition_notes(candidate))
        scored.append(((title_score * 2 + artist_score, is_album, explicit, -index), candidate))
    return max(scored, key=lambda scored_candidate: scored_candidate[0])[1] if scored else None


def _name_of(value: Any) -> str:
    """
    A name SoulSync gives either as it is or as a dict with a "name" - as an album
    comes from Deezer as a string, and from other sources as a dict.
    """
    if isinstance(value, dict):
        value = value.get("name")
    return str(value) if value else ""


def _artists(track: Dict) -> List[str]:
    """The names of a track's artists, as SoulSync's search lists them."""
    return [name for name in map(_name_of, track.get("artists") or []) if name]


def _spelt_like(name: str, artist: str) -> bool:
    """Whether an artist's name is spelt closely enough like the one asked for to be a mishearing of it."""
    return difflib.SequenceMatcher(None, normalise(name), normalise(artist)).ratio() >= CLOSE_SPELLING


def _another_version(parts: List[str], title: str) -> bool:
    """
    Whether a part of a song's name, or of its album's, says it's a version other than
    the one asked for: one of SONG_VERSIONS, or a mix - a part ending in "mix", as
    "Extended Mix" and "Club Mix" do - which the title asked doesn't name. A remix is
    a remix by any name, so "remix" asked for takes "(Made Up Remix)", but a mix has to
    be named as it is: "Blue Monday extended mix" isn't its club mix. An original mix,
    or an original version, is no other version at all - see ORIGINALS - and nor is
    a credit, however its artist is called - see CREDITS.

    Args:
        parts: The bracketed and dashed parts of the song's name and its album's.
        title: The song as asked for.
    """
    for part in parts:
        words = normalise(part)
        if words in ORIGINALS or words.startswith(CREDITS):
            continue
        versions = [version for version in SONG_VERSIONS if _names(words, version)]
        if not versions and words.endswith("mix"):
            versions = [words]
        if any(not _names(title, version) for version in versions):
            return True
    return False


def pick_track(candidates: List[Dict], title: str, artist: Optional[str]) -> Optional[Dict]:
    """
    The song SoulSync's search found which is the one asked for. It has every word of
    the title, and isn't a clean version, nor a live one, a remix, a demo, a mix or the
    like unless the title asked names it - by its own name or its album's, as a plain
    "M.E." on a live album is live. Asked for by an artist, it's theirs, with every word
    of their name, as an album is - or, with exactly the title asked for, by a name
    spelt closely like it, as speech to text heard Gunna as "Gunner".

    Of those, with only a title asked for, the first in SoulSync's order of its best
    guesses is taken: an exact title further down may be a cover, or another act's
    song. With an artist, it's taken in passes, the first of the earliest in SoulSync's
    order: the artist's own with exactly the title, then theirs with more words to it,
    then a close spelling. So Fleetwood Mac's own "Dreams" is taken over "Fleetwood
    Bac"'s listed above it, and Gunna's "endless" over his "Endless Love". Downloaded,
    a song nobody asked for would be in the library for good.

    Args:
        candidates: Songs as SoulSync's search lists them, each with its name,
            artists and album.
        title: The song as asked for.
        artist: Its artist, or None if only the title was given, when anyone's will do.

    Returns:
        One of the candidates, or None if none of them will do.
    """
    # Each candidate which will do at all, with whether its title is exactly the one
    # asked for, whether it's the artist's own, and whether it's by a close spelling.
    usable = []
    for candidate in candidates:
        name, album = _name_of(candidate.get("name")), _name_of(candidate.get("album"))
        title_score = likeness(name, title)
        if title_score < 2 or _another_version(_parts(name) + _parts(album), title):
            continue
        # As navidrome reads a track in the library: its title or its album's naming a clean version.
        if is_clean({"title": name, "album": album}):
            continue
        if not artist:
            return candidate
        # Each artist on their own, and all of them together, as pick_album has it.
        names = _artists(candidate)
        names += [" ".join(names)] if len(names) > 1 else []
        theirs = any(artist_likeness(found, artist) >= 2 for found in names)
        misheard = any(_spelt_like(found, artist) for found in names)
        usable.append((candidate, title_score == 3, theirs, misheard))
    # A close spelling only ever with exactly the title, so a guess at the artist and a
    # guess at the title never make a song nobody asked for.
    for will_do in (lambda exact, theirs, misheard: exact and theirs,
                    lambda exact, theirs, misheard: theirs,
                    lambda exact, theirs, misheard: exact and misheard):
        for candidate, exact, theirs, misheard in usable:
            if will_do(exact, theirs, misheard):
                return candidate
    return None


class SoulSync:
    """Asks SoulSync for songs and albums, and how they're getting on."""

    def __init__(self, url: str, key: str):
        """
        Args:
            url: SoulSync's address, e.g. "http://soulsync.local:8008".
            key: Its API key, SOULSYNC_API_KEY - one with admin scope, as a key that
                can only read can't request anything.
        """
        self.url = url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {key}"

    @classmethod
    def from_config(cls, config, key: Optional[str]) -> Optional["SoulSync"]:
        """
        The SoulSync config.yaml points at, or None if it isn't set up.

        Args:
            config: Configs loaded from config.yaml - soulsync.url.
            key: SOULSYNC_API_KEY from .env.

        Returns:
            A SoulSync, or None without both an address and a key.
        """
        url, key = (config.section("soulsync").get("url") or "").strip(), (key or "").strip()
        if not url or not key:
            if url or key:
                logger.warning("Downloading music needs both soulsync.url and SOULSYNC_API_KEY, and only one is set")
            return None
        return cls(url, key)

    # Talking to SoulSync

    def _send(self, path: str, timeout: float, body: Dict) -> Tuple[int, Any]:
        """
        One request to SoulSync: a POST of the body, as everything Nova asks of it is.

        Args:
            path: Where on SoulSync, e.g. "/api/v1/wishlist".
            timeout: Seconds to wait for its answer.
            body: The JSON to post.

        Returns:
            The status code, and the JSON SoulSync answered with - None if it wasn't JSON.

        Raises:
            SoulSyncError: If SoulSync couldn't be reached.
        """
        try:
            # On the local network, so it's still asked with only the internet down,
            # but not with no network at all - see NavidromeLibrary._call.
            if not network.online():
                raise requests.ConnectionError("No network connection")
            response = self._session.post(f"{self.url}{path}", json=body, timeout=timeout)
        except requests.RequestException as e:
            # Only the kind of error, never the error itself, which can hold the
            # request - and with it the key.
            logger.error(f"SoulSync request failed: {type(e).__name__}")
            raise SoulSyncError(NOT_ANSWERING if isinstance(e, (requests.ConnectionError, requests.Timeout))
                                else None) from None
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None

    @staticmethod
    def _read(status: int, answer: Any) -> Dict:
        """
        What SoulSync answered, if it wasn't an error. However it was asked, an
        error is in "error": a code and a message, from the REST API or JSON-RPC, or
        now and then a plain string. A 403 is a key it knows but won't let do this -
        one which can only read - so it's the key to check, as for a 401.

        Args:
            status: The status code it answered with.
            answer: The JSON it answered with, or None.

        Raises:
            SoulSyncError: If it was an error.
        """
        error = answer.get("error") if isinstance(answer, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if status in (401, 403) or code == "AUTH_REQUIRED":
            logger.error("SoulSync refused its key")
            raise SoulSyncError(KEY_REFUSED)
        if status >= 400 or error or not isinstance(answer, dict) or answer.get("success") is False:
            message = error.get("message") if isinstance(error, dict) else error if isinstance(error, str) else None
            logger.error(f"SoulSync answered with an error: HTTP {status}" + (f", {code}" if code else ""))
            raise SoulSyncError(_said(message) if message else None)
        return answer

    def _mcp(self, tool: str, arguments: Dict) -> Dict:
        """
        Calls one of SoulSync's MCP tools. Its MCP server answers each POST with
        one JSON-RPC reply, with no session to start first.

        Args:
            tool: The tool's name, e.g. "find_album".
            arguments: What it's called with.

        Returns:
            The tool's structuredContent.

        Raises:
            SoulSyncError: If it couldn't be called, or the tool failed.
        """
        status, answer = self._send("/mcp", MCP_TIMEOUT, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                          "params": {"name": tool, "arguments": arguments}})
        if status == 404:
            logger.error("SoulSync's MCP server is switched off")
            raise SoulSyncError(MCP_OFF)
        result = self._read(status, answer).get("result")
        if not isinstance(result, dict):
            logger.error(f"SoulSync's {tool} answered without a result")
            raise SoulSyncError()
        if result.get("isError"):
            text = " ".join(str(part.get("text") or "") for part in result.get("content") or []
                            if isinstance(part, dict)).strip()
            logger.error(f"SoulSync's {tool} failed")
            raise SoulSyncError(_said(text) if text else None)
        content = result.get("structuredContent")
        return content if isinstance(content, dict) else {}

    # What the assistant asks for

    def find_song(self, title: str, artist: Optional[str]) -> SongRequest:
        """
        Finds the song asked for: searches SoulSync for it, and takes the one
        pick_track chooses. Nothing is requested yet - see request_song - so Navidrome
        can be looked in again by the title asked for and SoulSync's artist.

        Args:
            title: The song's title as asked for.
            artist: Its artist, or None if only the title was given.

        Returns:
            The SongRequest, named as SoulSync has the song.

        Raises:
            SoulSyncError: If SoulSync couldn't be asked, or found nothing that will do.
        """
        status, answer = self._send("/api/v1/search/tracks", SEARCH_TIMEOUT,
                                    {"query": f"{artist} - {title}" if artist else title, "limit": SEARCH_LIMIT})
        data = self._read(status, answer).get("data")
        tracks = data.get("tracks") if isinstance(data, dict) else None
        tracks = [track for track in tracks if isinstance(track, dict)] if isinstance(tracks, list) else []
        track = pick_track(tracks, title, artist)
        if track is None:
            raise SoulSyncError(NOT_FOUND)
        artists = _artists(track)
        return SongRequest(_name_of(track.get("name")), artists[0] if artists else None, track)

    def request_song(self, song: SongRequest):
        """
        Asks SoulSync for a song find_song found: puts it on the wishlist, and starts
        the wishlist, which downloads it, tags it and files it into the library.

        Args:
            song: The SongRequest find_song gave.

        Raises:
            SoulSyncError: If SoulSync couldn't be asked, or refused.
        """
        track = dict(song.track)
        if isinstance(track.get("album"), str):
            # Given Deezer's plain string, SoulSync tagged the album as the song's own
            # title - M.E.'s as "M.E." rather than The Pleasure Principle.
            track["album"] = {"name": track["album"]}
        status, answer = self._send("/api/v1/wishlist", REQUEST_TIMEOUT,
                                    {"track_data": track, "source_type": "api", "failure_reason": WISHLIST_REASON})
        name = f"{song.title} by {song.artist}" if song.artist else song.title
        if status == 409:
            # Nothing is written, but it's on its way all the same.
            logger.info(f"{name} is already on SoulSync's wishlist, so it's waited for as it is")
        else:
            self._read(status, answer)
            logger.info(f"SoulSync is getting {name} ({track.get('id')})")
        try:
            self._read(*self._send("/api/v1/wishlist/process", REQUEST_TIMEOUT, {}))
        except SoulSyncError:
            # It's on the wishlist either way, so it still comes down - only later.
            logger.warning("SoulSync's wishlist couldn't be started, so the song waits for its next run")

    def song_progress(self, request: SongRequest) -> Progress:
        """
        How a song request is getting on - always "waiting", as for an album: the
        wishlist gives no request to follow, so the watch goes by Navidrome alone.

        Args:
            request: The SongRequest find_song gave.
        """
        return Progress("waiting")

    def request_album(self, title: str, artist: Optional[str]) -> AlbumRequest:
        """
        Asks SoulSync for an album: finds it, requests the one pick_album chooses,
        and starts it downloading.

        Args:
            title: The album as asked for.
            artist: Its artist, or None if only the title was given.

        Returns:
            The AlbumRequest, with how many tracks SoulSync said the album has and
            how many of them it queued.

        Raises:
            SoulSyncError: If SoulSync couldn't be asked, found nothing that will
                do, or refused.
        """
        found = self._mcp("find_album", {"query": f"{artist} - {title}" if artist else title,
                                         "limit": FIND_ALBUM_LIMIT})
        albums = found.get("albums") if isinstance(found.get("albums"), list) else []
        album = pick_album([album for album in albums if isinstance(album, dict)], title, artist)
        if album is None or not album.get("album_id"):
            raise SoulSyncError(NOT_FOUND)
        requested = self._mcp("request_album", {"album_id": album["album_id"]})
        track_count = requested.get("tracks") or album.get("tracks") or 0
        # An answer which doesn't say how many it queued is taken as the whole album.
        queued = track_count if requested.get("queued") is None else requested["queued"]
        logger.info(f"SoulSync is getting {album.get('title')} by {', '.join(map(str, album.get('artists') or []))} "
                    f"({album['album_id']}): {queued} of {track_count} tracks queued")
        try:
            self._mcp("process_wishlist", {})
        except SoulSyncError:
            # It's on the wishlist either way, so it still comes down - only later.
            logger.warning("SoulSync's wishlist couldn't be started, so the album waits for its next run")
        return AlbumRequest(None, [], int(track_count), int(queued))

    def album_progress(self, request: AlbumRequest) -> Progress:
        """
        How an album request is getting on - always "waiting", as SoulSync gives no
        request to follow, so the watch goes by Navidrome alone.

        Args:
            request: The AlbumRequest request_album gave.
        """
        return Progress("waiting")
