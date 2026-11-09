"""
Display Server Module
The clock displays' page and its API, from decisions 4, 5, 8 and 9 of docs/decisions/0013.

A clock is on home Wi-Fi, so unlike the settings page and the satellites' port this
listens on every address, on a port of its own - display.port, off while it's 0. The
page's own files are public, as they are in the repository; everything under
/display/api/ needs a clock's key, made on the settings page, as

    Authorization: Bearer <key>

looked up afresh on every request - so a clock removed, or given a new key, is refused
from that moment, a long poll already waiting included. Wrong keys are counted by the
address they come from, as the settings page's wrong passwords are, and an address with
too many is turned away unchecked for a minute.

With its key, a clock can wait for its satellite's state to change (see
display_feed.py), make Nova listen while a finger is held on it, step Nova's volume -
never the alarm's - and fetch the cover of what's playing. Nothing it sends says what
the brain fetches: a cover is only ever the one playing now.
"""

import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.display_feed import plays_here
from voice_assistant.page_password import WAIT, WrongTries
from voice_assistant.settings_page import (SAFETY_HEADERS, WEB_FOLDER, LogLimit, _held_back, change_volume,
                                           volume_levels)

logger = logging.getLogger(__name__)

# The page's own files, by the path each is asked for at - the page, its script, its
# touch handling, its style and its font, which the settings page has too.
FILES = {"/display": ("display.html", "text/html; charset=utf-8"),
         "/display.js": ("display.js", "text/javascript; charset=utf-8"),
         "/display-gestures.js": ("display-gestures.js", "text/javascript; charset=utf-8"),
         "/display.css": ("display.css", "text/css; charset=utf-8"),
         "/Outfit.woff2": ("Outfit.woff2", "font/woff2")}

API = "/display/api/"

# The settings page's, but for one thing: the page fetches each cover with its key and
# shows it from a blob: address, as an <img> can't send the key itself - so pictures may
# come from blob: as well as the page's own address. Sent with every answer.
DISPLAY_HEADERS = {
    **SAFETY_HEADERS,
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'"),
}

# The only kinds of picture a cover is served as. Not "image/svg+xml", which can hold a
# script, nor anything else Home Assistant might send, e.g. "text/html".
PICTURES = ("image/jpeg", "image/png", "image/webp", "image/gif")

# The most a hold or a volume press may send, in bytes - each is a word or two of JSON,
# e.g. {"action": "start"}.
MOST_BODY = 1024

# A key longer than this is no clock's - one is 43 characters - so it's counted as
# wrong without being looked at.
LONGEST_KEY = 256

# The most seconds a clock's long poll is held before it's answered with the state as it
# is, so a connection the Wi-Fi dropped without a word is let go of in good time.
LONGEST_WAIT = 25

# How often a long poll checks that its clock's key is still one, and whether the clock
# was changed on the settings page - a new brightness, say, is the clock's at once.
RECHECK = 1.0

# The seconds a connection may go without sending anything before it's closed - so a
# device which opens one and sends nothing, or a body shorter than it said, can't keep
# a thread waiting for good. A long poll's wait isn't one of these: it's the brain
# that's quiet then, not the clock.
REQUEST_TIMEOUT = 10

# How often the server checks whether it's been asked to stop, in seconds.
STOP_CHECK = 0.5

# A long poll's version, as the feed gives it - milliseconds since 1970, e.g.
# "1762650000000", so eighteen digits are enough for a long while yet.
_VERSION = re.compile("[0-9]{1,18}")


class DisplayServer:
    """The clock displays' page and API, on a thread of its own."""

    def __init__(self, brain, displays, feed, port: int, address: str = "0.0.0.0"):
        """
        Args:
            brain: The brain, for each satellite's session and Home Assistant.
            displays: The Displays - the clocks and their keys.
            feed: The DisplayFeed, for each satellite's state.
            port: Where to listen - 0 for any port that's free, as the tests do.
            address: Which of this machine's addresses to listen on - every one,
                unless told otherwise.
        """
        self.brain = brain
        self.displays = displays
        self.feed = feed
        self.port = port
        self.address = address
        self.wrong_tries = WrongTries()
        # At most a line a minute about each address's wrong keys, and each satellite's
        # failing state, so one clock asking over and over can't turn the log over.
        self.refusals = LogLimit()
        self.failures = LogLimit()
        self._stopping = threading.Event()
        self._server: Optional[ThreadingHTTPServer] = None

    @classmethod
    def from_config(cls, config, brain, displays, feed) -> Optional["DisplayServer"]:
        """
        Args:
            config: Nova's config, for display.port.
            brain: The brain.
            displays: The Displays.
            feed: The DisplayFeed.

        Returns:
            The server, not yet started - or None while display.port is 0, as it is
            until the user sets one, so nothing listens on the network unasked.
        """
        port = int(config.section("display").get("port") or 0)
        if not port:
            return None
        return cls(brain, displays, feed, port)

    @property
    def serving(self) -> bool:
        """Whether it's listening - between a start which could and stop."""
        return self._server is not None

    def start(self) -> bool:
        """
        Starts serving, on a thread of its own - or logs why it couldn't, e.g. the port being taken.

        Returns:
            Whether it's listening, so the brain starts the feed only for a server which is.
        """
        try:
            server = ThreadingHTTPServer((self.address, self.port), _Handler)
        except OSError as e:
            logger.error(f"Couldn't start the clock displays' server on port {self.port}: {e}")
            return False
        # So no connection holds the brain's shutdown: server_close() waits for each
        # connection's thread while block_on_close says to, and one opened and left quiet
        # would keep it waiting REQUEST_TIMEOUT. The brain's Python (3.13.5) leaves daemon
        # threads out of that wait already; block_on_close says so outright, rather than lean on it.
        server.daemon_threads = True
        server.block_on_close = False
        server.display_server = self
        self._stopping.clear()
        self._server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": STOP_CHECK},
                         name="display-server", daemon=True).start()
        logger.info(f"The clock displays' page is on port {self.port}")
        return True

    def stop(self):
        """Stops serving, and answers the long polls still waiting."""
        self._stopping.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # What the API does

    def satellite_of(self, display: Dict) -> Optional[str]:
        """
        Args:
            display: A clock, as Displays.find gives it.

        Returns:
            The satellite it's for - the brain's default for one given none - or None
            if there's none at all, or the satellites' file can't be read to tell
            which is the default.
        """
        if display["satellite"]:
            return display["satellite"]
        try:
            return self.brain.default_satellite()
        except (OSError, ValueError):
            return None

    def session_of(self, display: Dict):
        """The session of a clock's satellite, or None if it isn't connected."""
        satellite = self.satellite_of(display)
        return self.brain.session_for(satellite) if satellite else None

    def state(self, key: str, display: Dict, since: Optional[int]) -> Optional[Dict]:
        """
        A clock's long poll: its satellite's state as soon as it isn't the version the
        clock has, with the clock's own brightness and time before it goes home.

        Args:
            key: The clock's key, checked again every RECHECK seconds.
            display: The clock, as its key found it.
            since: The version the clock has, or None for the state now.

        Returns:
            The state - at once if since isn't its version, or the clock was changed on
            the settings page; else when it changes, or after LONGEST_WAIT as it is
            then. None if the key stopped being the clock's while it waited.

        Raises:
            Whatever the feed raises building a state - its first, on this thread.
        """
        if since is None:
            return self._with_settings(self.feed.state(self.satellite_of(display)), display)
        end = time.monotonic() + LONGEST_WAIT
        while True:
            waiting = max(0.0, min(RECHECK, end - time.monotonic()))
            slice_end = time.monotonic() + waiting
            satellite = self.satellite_of(display)
            state = self.feed.wait(satellite, since, waiting)
            now = self.displays.find(key)
            if now is None:
                return None
            if now != display:
                if self.satellite_of(now) != satellite:
                    state = self.feed.state(self.satellite_of(now))
                return self._with_settings(state, now)
            # The feed only answers before its time with the same version when it's
            # stopping, as the brain does - then so does the poll, rather than ask again
            # and again until LONGEST_WAIT.
            if (state["version"] != since or time.monotonic() >= end or time.monotonic() < slice_end
                    or self._stopping.is_set()):
                return self._with_settings(state, display)

    @staticmethod
    def _with_settings(state: Dict, display: Dict) -> Dict:
        return dict(state, brightness=display["brightness"], home_after=display["home_after"])

    def hold(self, display: Dict, action: str) -> Dict:
        """
        A finger held on a clock, or lifted.

        Args:
            display: The clock.
            action: "start" as the finger is held, "end" as it's lifted.

        Returns:
            {"listening": bool} - whether Nova started listening for it. Never for an
            "end", nor while Nova is already in a conversation, nor on a satellite
            that isn't connected.
        """
        session = self.session_of(display)
        if session is None:
            return {"listening": False}
        if action == "start":
            return {"listening": bool(session.listen_now())}
        session.stop_held()
        return {"listening": False}

    def step_volume(self, display: Dict, direction: str) -> Optional[Dict]:
        """
        A volume button pressed on a clock: the music's volume a step while Nova's own
        music plays on its satellite, else Nova's voice - never the alarm's, so timers
        and alarms stay loud. The beep is played at the new level, as the settings
        page's sliders do.

        Args:
            display: The clock.
            direction: "up" or "down".

        Returns:
            {"category": "music" or "general", "percent": its level now} - or None if
            the satellite isn't connected.
        """
        satellite = self.satellite_of(display)
        session = self.brain.session_for(satellite) if satellite else None
        if session is None:
            return None
        music = session.music_player
        try:
            playing = music.now_playing() if music is not None else None
        except HOME_FAILURES:
            # E.g. Spotify out of reach - its music can't be changed then either, so
            # the button changes Nova's voice, which it can.
            playing = None
        # Spotify's music on another of the account's devices isn't Nova's here, and the
        # button would step this satellite's speaker, not the one playing - so it's the voice's.
        category = ("music" if music is not None and plays_here(playing) and playing.get("playing") is True
                    else "general")
        level = volume_levels(session.volume, music)[category]
        step = session.volume.step if direction == "up" else -session.volume.step
        percent = int(max(0, min(100, round(level + step))))
        result = change_volume(session.volume, music, category, percent)
        if result.get("success"):
            session.boop(percent / 100 if category == "music" else session.volume.get(category))
        try:
            self.feed.poke(satellite)
        except Exception as e:
            # The volume has changed all the same, and the next pass shows it.
            self.note_failure(satellite, e)
        return {"category": category, "percent": int(volume_levels(session.volume, music)[category])}

    def cover(self, display: Dict, kind: Optional[str], cover_id: Optional[str]) -> Optional[Tuple[bytes, str]]:
        """
        The cover a clock's state names, as a picture and its type.

        Args:
            display: The clock.
            kind: "nova" for Nova's own player on the clock's satellite, "home" for a
                Jellyfin player in Home Assistant.
            cover_id: The cover's id for "nova", e.g. "al-1"; the player's entity for
                "home", e.g. "media_player.office_tv".

        Returns:
            The picture and its content type, e.g. "image/jpeg" - or None if that isn't
            what's playing now, or it couldn't be fetched, or it isn't a picture.
        """
        if not cover_id:
            return None
        if kind == "nova":
            found = self._novas_cover(display, cover_id)
        elif kind == "home":
            found = self._jellyfins_cover(cover_id)
        else:
            return None
        # It's served from the clock page's own address, so anything but a plain picture
        # - a page Home Assistant sent, say - could otherwise run as the clock's page does.
        if found is None or found[1].split(";")[0].strip().lower() not in PICTURES:
            return None
        return found

    def _novas_cover(self, display: Dict, cover_id: str) -> Optional[Tuple[bytes, str]]:
        session = self.session_of(display)
        music = session.music_player if session is not None else None
        if music is None:
            return None
        try:
            playing = music.now_playing()
        except HOME_FAILURES:
            return None
        if not playing or not playing.get("cover") or str(playing["cover"]) != cover_id:
            return None
        return music.cover()

    def _jellyfins_cover(self, entity_id: str) -> Optional[Tuple[bytes, str]]:
        # The player's own picture, as Home Assistant says it is now - never a path the
        # clock sent, so it can't choose what the brain fetches with Nova's token.
        home = self.brain.home
        if home is None:
            return None
        try:
            picture = next((player["picture"] for player in home.jellyfin_media()
                            if player["entity_id"] == entity_id), None)
            return home.picture(picture) if picture else None
        except HOME_FAILURES:
            # ValueError among them: picture() refuses anything but a media player's
            # picture, or one too big.
            return None

    def note_failure(self, satellite: Optional[str], error: Exception):
        """Logs a state which couldn't be built for a clock - a line a minute at most, without its traceback."""
        held = self.failures.note(str(satellite))
        if held is not None:
            logger.error(f"Couldn't build the {satellite} satellite's state for a clock display: "
                         f"{type(error).__name__}: {error}{_held_back(held)}")


class _Handler(BaseHTTPRequestHandler):
    """One request from a clock."""

    server_version = "Nova"
    sys_version = ""
    timeout = REQUEST_TIMEOUT

    def log_message(self, format, *args):
        # A clock asks every 25 seconds at least - the journal would be nothing else.
        pass

    def do_GET(self):
        url = self._url()
        if url is None:
            return
        if url.path in FILES:
            self._send_file(*FILES[url.path])
            return
        if not url.path.startswith(API):
            self._not_found()
            return
        let_in = self._let_in()
        if let_in is None:
            return
        key, display = let_in
        server = self.server.display_server
        # Blank values kept, so "?since=" is refused as the mistake it is, not taken for no version.
        query = parse_qs(url.query, keep_blank_values=True)
        if url.path == API + "state":
            self._answer_state(server, key, display, query)
        elif url.path == API + "cover":
            found = server.cover(display, (query.get("kind") or [None])[0], (query.get("id") or [None])[0])
            if found is None:
                self._send_json({"message": "That isn't playing now."}, 404)
            else:
                self._send(200, *found)
        else:
            self._not_found()

    def do_POST(self):
        url = self._url()
        if url is None:
            return
        if not url.path.startswith(API):
            self._not_found()
            return
        let_in = self._let_in()
        if let_in is None:
            return
        _, display = let_in
        server = self.server.display_server
        if url.path not in (API + "hold", API + "volume"):
            self._not_found()
            return
        body = self._body()
        if body is None:
            return
        if url.path == API + "hold":
            if body.get("action") not in ("start", "end"):
                self._send_json({"message": 'A hold is "start" or "end".'}, 400)
                return
            self._send_json(server.hold(display, body["action"]))
            return
        if body.get("direction") not in ("up", "down"):
            self._send_json({"message": 'The volume goes "up" or "down".'}, 400)
            return
        stepped = server.step_volume(display, body["direction"])
        if stepped is None:
            self._send_json({"message": "The clock's satellite isn't connected."}, 409)
        else:
            self._send_json(stepped)

    def _url(self):
        """What's asked for, split - or None, having answered 404, if it can't be, e.g. "http://[x"."""
        try:
            return urlsplit(self.path)
        except ValueError:
            self._not_found()
            return None

    def _let_in(self) -> Optional[Tuple[str, Dict]]:
        """
        Checks the request's key, counting it as a try from its address.

        Returns:
            The key and the clock it belongs to - or None, having answered 401 for a
            wrong or missing key, or 429 while the address is waiting.
        """
        server = self.server.display_server
        address = self.client_address[0]
        scheme, _, key = (self.headers.get("Authorization") or "").partition(" ")
        key = key.strip() if scheme.lower() == "bearer" else ""
        if len(key) > LONGEST_KEY:
            key = ""
        found = []

        def check() -> bool:
            display = server.displays.find(key)
            if display is not None:
                found.append(display)
            return display is not None

        # Every request is a try, counted before it's checked - see WrongTries.attempt -
        # so many sent at once can't all get past the limit.
        result = server.wrong_tries.attempt(address, check)
        if result == "right":
            return key, found[0]
        if result == "waiting":
            self._send_json({"message": "Too many wrong keys - try again in a minute."}, 429,
                            {"Retry-After": str(WAIT)})
            return None
        held = server.refusals.note(address)
        if held is not None:
            logger.warning(f"Clock display: refused a wrong key from {address}{_held_back(held)}")
        self._send_json({"message": "That isn't a clock's key."}, 401, {"WWW-Authenticate": "Bearer"})
        return None

    def _answer_state(self, server: DisplayServer, key: str, display: Dict, query: Dict):
        given = query.get("since")
        if given is not None and not _VERSION.fullmatch(given[0]):
            self._send_json({"message": "since is the version the clock has, a whole number."}, 400)
            return
        try:
            state = server.state(key, display, int(given[0]) if given is not None else None)
        except Exception as e:
            # E.g. the satellites' file unreadable as the state is first built. The
            # clock asks again in a moment, so a line a minute is enough to say so.
            server.note_failure(server.satellite_of(display), e)
            self._send_json({"message": "Nova couldn't build the clock's state."}, 500)
            return
        if state is None:
            self._send_json({"message": "That isn't a clock's key."}, 401, {"WWW-Authenticate": "Bearer"})
        else:
            self._send_json(state)

    def _body(self) -> Optional[Dict]:
        """
        Returns:
            What the clock sent, as a JSON object - or None, having answered 400 for
            anything else, or nothing at all if it never finished sending it.
        """
        # As the settings page reads one: a negative length would be read to the end of
        # the connection, and isdecimal() turns down "²", which int() can't read.
        length = self.headers.get("Content-Length") or "0"
        if not length.isdecimal() or len(length) > 9 or int(length) > MOST_BODY:
            self._send_json({"message": "That didn't make sense."}, 400)
            return None
        try:
            sent = self.rfile.read(int(length))
        except OSError:
            # It said more than it sent, and went quiet - REQUEST_TIMEOUT has passed.
            self.close_connection = True
            return None
        try:
            body = json.loads(sent)
        # RecursionError is a kilobyte of "[[[[", nested deeper than Python's json goes.
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            self._send_json({"message": "That didn't make sense."}, 400)
            return None
        return body

    def _send_file(self, name: str, content_type: str):
        try:
            with open(os.path.join(WEB_FOLDER, name), "rb") as file:
                self._send(200, file.read(), content_type)
        except FileNotFoundError:
            self._not_found()

    def _not_found(self):
        self._send_json({"message": "There's nothing here."}, 404)

    def _send_json(self, data: Dict, status: int = 200, headers: Optional[Dict[str, str]] = None):
        self._send(status, json.dumps(data).encode(), "application/json", headers)

    def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**(headers or {}), **DISPLAY_HEADERS}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
