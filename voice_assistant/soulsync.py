"""
SoulSync Module.

Asks SoulSync for a song or an album Navidrome hasn't got, and how it's getting on.
SoulSync finds music on Soulseek, tags it and files it straight into the folder
Navidrome scans, so a download needs no moving. Nothing here plays or deletes a file.

A song goes through SoulSync's REST API: a request, then its progress by the id
SoulSync gave it. An album goes through its MCP server, since the REST API has no
album request: find_album searches for it, request_album puts its tracks on
SoulSync's wishlist, and process_wishlist starts them downloading now rather than at
the wishlist's next run. SoulSync gives no id to follow an album by, so whether one
has come down is for Navidrome to say.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from voice_assistant import network
from voice_assistant.navidrome import BRACKETED, CLEAN_VERSIONS, DASH, VERSIONS, likeness, normalise

# Logger setup
logger = logging.getLogger(__name__)

# Seconds to wait for SoulSync's REST API, which only notes a request and answers.
REQUEST_TIMEOUT = 10

# Seconds to wait for its MCP tools: find_album searches MusicBrainz, beyond the
# local network, before it answers.
MCP_TIMEOUT = 30

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

# A song request's states before it ends. "downloading" can also be its last, with
# timed_out set, when SoulSync stops following a download that may still be running.
WAITING_STATES = ("queued", "searching", "downloading")


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


def _said(message: str) -> str:
    """SoulSync's own words as a reason, with one full stop: "Nobody had it" -> 'SoulSync said: Nobody had it.'"""
    message = message.strip()
    return f"SoulSync said: {message}" + ("" if message.endswith((".", "!", "?")) else ".")


def _names(text: str, words: str) -> bool:
    """Whether text has the word or words, whole: "inexplicit" has no "explicit"."""
    return f" {words} " in f" {normalise(text)} "


def _edition_notes(candidate: Dict) -> List[str]:
    """
    What says which edition an album find_album found is: its MusicBrainz
    disambiguation, a free note such as "clean, deluxe edition", and each bracketed
    or dashed part of its title, as version_of finds a track's - after a hyphen, an
    en dash or an em dash alike. Each is read on its own, so a ")" in the
    disambiguation can't run into the title's brackets.
    """
    title = candidate.get("title") or ""
    notes = [text for brackets in BRACKETED.findall(title) for text in brackets if text]
    dashed = DASH.split(title, 1)
    if len(dashed) == 2:
        notes.append(dashed[1])
    return notes + [str(candidate.get("disambiguation") or "")]


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
        artist_score = max((likeness(str(name), artist) for name in candidate.get("artists") or []),
                           default=0) if artist else 0
        # Asked for by an artist, an album is theirs only with every word of their
        # name, as navidrome's _asked_for has it - Mac Miller's isn't Fleetwood
        # Mac's. Downloaded, an album nobody asked for would be in the library for good.
        if not title_score or (artist and artist_score < 2):
            continue
        is_album = str(candidate.get("type") or "").lower() == "album"
        explicit = any(_names(note, "explicit") for note in _edition_notes(candidate))
        scored.append(((title_score * 2 + artist_score, is_album, explicit, -index), candidate))
    return max(scored, key=lambda scored_candidate: scored_candidate[0])[1] if scored else None


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

    def _send(self, path: str, timeout: float, body: Optional[Dict] = None) -> Tuple[int, Any]:
        """
        One request to SoulSync - a POST of the body, or a GET without one.

        Args:
            path: Where on SoulSync, e.g. "/api/v1/request".
            timeout: Seconds to wait for its answer.
            body: The JSON to post, or None to GET.

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
            if body is None:
                response = self._session.get(f"{self.url}{path}", timeout=timeout)
            else:
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
        now and then a plain string.

        Args:
            status: The status code it answered with.
            answer: The JSON it answered with, or None.

        Raises:
            SoulSyncError: If it was an error.
        """
        error = answer.get("error") if isinstance(answer, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if status == 401 or code == "AUTH_REQUIRED":
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

    def request_song(self, title: str, artist: Optional[str]) -> str:
        """
        Asks SoulSync for a song.

        Args:
            title: The song's title as asked for.
            artist: Its artist, or None if only the title was given.

        Returns:
            SoulSync's id for the request, for song_progress.

        Raises:
            SoulSyncError: If SoulSync couldn't be asked, or refused.
        """
        body = {"query": f"{artist} - {title}" if artist else title, "title": title}
        if artist:
            body["artist"] = artist
        data = self._read(*self._send("/api/v1/request", REQUEST_TIMEOUT, body)).get("data")
        request_id = data.get("request_id") if isinstance(data, dict) else None
        if not request_id:
            logger.error("SoulSync took a song request without giving it an id")
            raise SoulSyncError()
        return str(request_id)

    def song_progress(self, request_id: str) -> Progress:
        """
        How a song request is getting on.

        Args:
            request_id: The id request_song gave.

        Returns:
            Progress "waiting" until SoulSync says it's done or failed. A download
            SoulSync stopped following, and a request it has cleared away, are
            "waiting" too, since either may have come down - Navidrome decides.

        Raises:
            SoulSyncError: If SoulSync couldn't be asked.
        """
        status, answer = self._send(f"/api/v1/request/{quote(request_id, safe='')}", REQUEST_TIMEOUT)
        if status == 404:
            return Progress("waiting")
        data = self._read(status, answer).get("data")
        data = data if isinstance(data, dict) else {}
        state = data.get("status")
        if state == "completed":
            return Progress("done")
        if state == "not_found":
            return Progress("failed", NOT_FOUND)
        if state == "failed":
            return Progress("failed", _said(data["error"]) if data.get("error") else None)
        if state not in WAITING_STATES:
            logger.warning(f"SoulSync gave a song request a state Nova doesn't know, so it's waited on: {state}")
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
        found = self._mcp("find_album", {"query": f"{artist} - {title}" if artist else title})
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
