"""
Settings Page Module.

A page for looking after Nova from a phone or a laptop: how it's doing, each
satellite's microphone mute, what's playing on it, the lights and Home Assistant's
scripts and scenes, its volumes, the timers and alarms, what Nova remembers, its keys
and its main settings. The brain serves it, for every satellite - see brain.py - and
what's a satellite's own is looked up in its session at each request, so the page
never holds one which has gone.

Only the user gets in. It listens on this machine alone, and Tailscale passes the
tailnet's requests on to it over HTTPS, with its own certificate -

    tailscale serve --bg --https=443 http://127.0.0.1:8765

- saying whose they are: only the logins in settings_page.allow are let in. A change
also has to come from the page itself, with a header only it sends, so that another
website the user happens to visit can't make one in their name.

It can be served through Nginx Proxy Manager too, at an address of the user's own, on a
second port - settings_page.proxy_port - which answers only NPM's address, in
settings_page.proxy_from. There, Tailscale's header is never trusted, whatever it says:
before anything but the page's own files, the page's password has to be given, for a
login kept in a cookie - see page_password.py. A login can do everything the tailnet
can, but replacing the password also needs the current one.

Keys are only ever written, never shown back - the page just says whether each is
set, and a new one is used once Nova restarts, which the page can do. Settings are
saved to this install's own ~/.config/nova/config.yaml, the one before kept beside it
as config.yaml.bak, and most are used straight away, from the next moment they're
needed, on every satellite - see Brain.apply_settings. Those which make a connection or
a player, such as a server's address, wait for a restart, as one swapped half way is
where things go wrong, and the page says which each setting is.
"""

import hashlib
import json
import logging
import math
import os
import re
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import parse_qs, urlsplit

import requests
import yaml

from voice_assistant import paths, storage, timeline
from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.clock import format_duration
from voice_assistant.config import CHOICES, LIMITS, Config, ConfigError, check, clashes, limit_problem
from voice_assistant.devices import AppDevice
from voice_assistant.google_calendar import READ_FAILURES, ClientRefused, GoogleSignInError, SignInNeeded
from voice_assistant.music import _chosen, why_no_player
from voice_assistant.ntfy import RETOPIC
from voice_assistant.page_password import LOGIN_DAYS, Logins, PagePassword, WrongTries
from voice_assistant.spotify_controller import SpotifySignIn, SpotifySignInError, brains_speaker, spotify_oauth
from voice_assistant.undo import UndoShelf

# Logger setup
logger = logging.getLogger(__name__)

# The page itself, by the path it's asked for at.
WEB_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
FILES = {"/": ("settings.html", "text/html; charset=utf-8"),
         "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
         "/settings.css": ("settings.css", "text/css; charset=utf-8"),
         "/icon.svg": ("icon.svg", "image/svg+xml"),
         # Outfit, by The Outfit Project Authors, under the SIL Open Font License -
         # see web/Outfit-OFL.txt.
         "/Outfit.woff2": ("Outfit.woff2", "font/woff2")}

# The files the page itself is made of, whose hash tells a page left open across an
# update that it's out of date - see page_version.
PAGE_FILES = ("settings.html", "settings.js", "settings.css")

# Sent with every answer: nothing but the page's own files may run in it, no other
# site may show it in a frame, and nothing is kept in a cache.
SAFETY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# The settings on the page - the ones most likely to change - what each is for, and
# whether it's used "now", straight away, or once Nova restarts - those which make a
# connection or a player.
EDITABLE = [
    ("music", "player", "What plays the music", "restart"),
    ("navidrome", "url", "Navidrome's address", "restart"),
    ("navidrome", "username", "Navidrome's user for Nova", "restart"),
    ("home_assistant", "url", "Home Assistant's address", "restart"),
    ("ntfy", "url", "Your ntfy server", "restart"),
    ("ntfy", "alerts", "Topics read out as they arrive, separated by commas", "restart"),
    ("ntfy", "phone", "Topic for sending things to your phone", "now"),
    ("ntfy", "reminders", "Topic for reminders nobody answered", "now"),
    ("ntfy", "restarts", "Topic for Nova restarting after a problem", "now"),
    ("ntfy", "quiet_from", "Night starts at", "now"),
    ("ntfy", "quiet_until", "Night ends at", "now"),
    ("thresholds", "silence_duration", "Seconds of quiet which end what you're saying", "now"),
    ("thresholds", "recording_timeout", "Longest request, in seconds", "now"),
    ("thresholds", "wake_word_confidence", "How sure Nova must be it heard its wake word", "now"),
    ("thresholds", "media_wake_word_confidence",
     "How sure Nova must be it heard its wake word while something's playing in the room", "now"),
    ("thresholds", "voice_check", "How sure Nova must be it's your voice (0 for no check)", "now"),
    ("logging", "log_transcripts", "Keep what's said in the logs", "now"),
    ("sounds", "listening_enabled", "The blip when Nova's listening", "now"),
    ("sounds", "chime_enabled", "The chime for done", "now"),
    ("sounds", "reminder_enabled", "The sound before alerts and warnings", "now"),
    ("web_search", "enabled", "Search the web when asked to", "now"),
    ("conversation", "history_max_age", "Seconds a conversation's thread is kept", "now"),
    ("spotify", "pause_while_speaking", "Pause Spotify while Nova talks", "now"),
    ("voice", "speed", "Speaking speed", "now"),
    ("mqtt", "host", "Home Assistant's MQTT host", "restart"),
    ("mqtt", "port", "Its port", "restart"),
    ("mqtt", "username", "Nova's MQTT user", "restart"),
]
# How the page says which each is.
NOW = "Used straight away"
LATER = "Used once Nova restarts"

# The keys, and what each is for. Claude's can be under an older name too.
KEYS = [("ANTHROPIC_API_KEY", "Claude"), ("HA_TOKEN", "Home Assistant (an administrator's token)"),
        ("MQTT_PASSWORD", "Home Assistant's MQTT"), ("NAVIDROME_PASSWORD", "Navidrome"), ("NTFY_TOKEN", "ntfy"),
        ("SPOTIFY_CLIENT_ID", "Spotify client ID"), ("SPOTIFY_CLIENT_SECRET", "Spotify client secret"),
        ("GOOGLE_CLIENT_ID", "Google client ID"), ("GOOGLE_CLIENT_SECRET", "Google client secret")]
OLDER_NAMES = {"ANTHROPIC_API_KEY": ["Claude_API_Key"]}

# Said in Calendars and lists, and to a sign-in, until Nova has Google's keys - read as it
# starts, as the calendar is made then.
NO_GOOGLE = "Google's client ID and secret go under Keys, then restart Nova."
# Said of the music when Nova's config isn't known, so neither player can be blamed - the
# page's own words for it, in settings.js.
NO_PLAYER = "Nova has no music player set up. Choose one in Settings, under Music."
# What the page says of the parts which need Home Assistant, before it is set up.
NO_HOME = "Nova isn't connected to Home Assistant. Add its address in Settings, and its token in Keys."
CANT_REACH_HOME = "Nova can't reach Home Assistant just now."
# What the page says when Google no longer takes Nova's sign-in, or its client.
SIGN_IN_AGAIN = "Nova needs signing in to Google again."
CHECK_GOOGLES_KEYS = "Google didn't accept the client ID or secret - check both under Keys."
GOOGLE_OUT_OF_REACH = "Nova can't reach Google just now."

# The most a request may send, in bytes.
MOST_BODY = 64 * 1024

# The cookie an NPM login is kept in, and the longest password or cookie looked at -
# anything longer is refused unchecked, so a megabyte sent as a guess costs nothing.
LOGIN_COOKIE = "nova_login"
LONGEST_PASSWORD = 1024
LONGEST_COOKIE = 256
MOST_DEVICE_KEY = 64
# What's open through NPM before a login: the page's own files, all public in the
# repository, so the login form can be the page's own, and asking whether it's logged in.
# Logging out is open too, so a cookie whose login has ended can still be cleared.
OPEN_THROUGH_NPM = {"GET": set(FILES) | {"/api/login-state"}, "POST": {"/api/login", "/api/logout"}}
TOO_MANY_TRIES = "Too many wrong passwords - try again in a minute."
# How long a device that isn't NPM may keep its thread while what it sent is thrown
# away, in seconds all told, and how many may at once - beyond that, the 403 is sent
# and the connection closed straight away. Otherwise one device sending a byte a
# second on many connections could hold a thread for each, until the brain couldn't
# start any more.
TURN_AWAY_WAIT = 1
TURN_AWAYS_AT_ONCE = 16

# What setup.sh starts this install's own settings file with, written back at every
# save, as yaml leaves comments out.
SETTINGS_HEADER = ("# This install's own settings, on top of the defaults in the repository's\n"
                   "# config.yaml - see there for every setting and what it's for.\n")

# How many of the latest problems in the log are shown, and how much of the end of
# it is read to find them.
PROBLEMS_SHOWN = 8
LOG_TAIL = 256 * 1024

# How many of the latest replies the typical time is worked out from.
REPLIES_COUNTED = 200

# How often the server checks whether it's been asked to stop, in seconds.
STOP_CHECK = 0.5

# How long the user's playlists are kept before being asked for again, in seconds -
# they seldom change, and the page asks what's playing every few seconds.
PLAYLISTS_KEPT = 300

# What the page's music buttons do, as the actions asking out loud would.
PLAYBACK = ("pause", "skip", "previous")

# What's said when the satellites' passwords file can't be read - rather than that
# there's no such satellite, which would send the user looking for one.
SATELLITES_UNREADABLE = "The satellites' file can't be read, so nothing was changed."


def _kind(default: Any) -> str:
    """The kind of value a setting holds, as the page shows it."""
    if isinstance(default, bool):
        return "yes/no"
    if isinstance(default, (int, float)):
        return "number"
    if isinstance(default, list):
        return "list"
    return "text"


def _from_page(given: Any, default: Any) -> Any:
    """
    A setting as the page sent it, as the kind config.yaml holds.

    Raises:
        ValueError if it can't be.
    """
    if isinstance(default, bool):
        if not isinstance(given, bool):
            raise ValueError("not yes or no")
        return given
    if isinstance(default, (int, float)):
        try:
            number = float(given)
        except OverflowError:
            # A whole number too big for a float, which the page's JSON can send - out of
            # any range, as limit_problem says of it.
            return given
        return int(number) if isinstance(default, int) and number.is_integer() else number
    if isinstance(default, list):
        items = given if isinstance(given, list) else str(given).split(",")
        return [str(item).strip() for item in items if str(item).strip()]
    return str(given).strip()


def _secrets_set(path: str) -> Dict[str, bool]:
    """Which keys the secrets file has a value for - the names only, never the values."""
    found = {}
    try:
        with open(path) as file:
            for line in file:
                matched = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
                if matched:
                    found[matched[1]] = bool(matched[2].strip().strip("'\""))
    except FileNotFoundError:
        pass
    return found


def volume_levels(volume, music) -> Dict[str, int]:
    """
    Each of Nova's volumes, as a percentage - for the page, and for Home Assistant
    every second. The music's is Navidrome's own, but Spotify's would mean asking
    Spotify's servers every time, so Nova's own note of it is used instead.

    Args:
        volume: The VolumeControl.
        music: The music player, or None.

    Returns:
        Each level by its category - "general", "alarm" and "music".
    """
    levels = {category: volume.percent(category) for category in volume.CATEGORIES}
    if music is not None and not getattr(music, "needs_internet", True):
        levels["music"] = music.get_volume()
    return levels


def change_volume(volume, music, category: str, percent: int) -> Dict:
    """
    Changes one of Nova's volumes, from the page or Home Assistant. The music's belongs
    to the music player, which also changes the song playing - the same way asking
    Nova out loud does.

    Args:
        volume: The VolumeControl.
        music: The music player, or None.
        category: "general", "alarm" or "music".
        percent: The new level, from 0 to 100.

    Returns:
        Dict with 'success' and a 'message'.
    """
    if category != "music" or music is None:
        return volume.set(category, percent)
    result = music.set_volume(percent)
    # Spotify's level is shown from Nova's own note of it (see volume_levels), so the
    # note follows - or Home Assistant's slider would spring back to the old level.
    if result.get("success") and getattr(music, "needs_internet", True):
        volume.set("music", percent)
    return result


def _number(value: Any) -> Optional[float]:
    """
    What the page sent as a number, or None if it isn't one - a yes or no, text which
    isn't a number, or one which isn't finite, such as the NaN and Infinity which JSON
    lets through. A number as text, e.g. "40", counts.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


class LogLimit:
    """
    At most one log line a minute about each thing, e.g. each device turned away - so
    one device asking over and over can't turn the log over, losing what explains a
    real problem, or fill the page's list of problems. Each is forgotten a minute after
    its last line, so it holds no more than were logged about in the last minute; what
    was held back from one that then went quiet goes unsaid.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic):
        """
        Args:
            now: The time, in seconds.
        """
        self.now = now
        self._lock = threading.Lock()
        # When each thing's last line was, and how many have been held back since.
        self._last: Dict[str, Tuple[float, int]] = {}

    def note(self, key: str) -> Optional[int]:
        """
        Args:
            key: What it's about, e.g. the device's address.

        Returns:
            None if it shouldn't be logged, or how many about it have been held back
            since its last line - 0 for a first.
        """
        with self._lock:
            now = self.now()
            self._last = {seen: (when, held) for seen, (when, held) in self._last.items()
                          if seen == key or now - when < 60}
            when, held = self._last.get(key, (None, 0))
            if when is not None and now - when < 60:
                self._last[key] = (when, held + 1)
                return None
            self._last[key] = (now, 0)
            return held


def _held_back(held: int) -> str:
    """What a line let through by a LogLimit adds about those held back, e.g. " (and 3 more since the last time)"."""
    return f" (and {held} more since the last time)" if held else ""


def page_version() -> str:
    """
    A short hash of the page's own files, for a page left open to tell it's been
    updated since it was loaded. It's asked for before a login through NPM, so it's of
    nothing but files served then anyway.

    Returns:
        Twelve hex digits, the same for the same files.

    Raises:
        OSError: One of the files couldn't be read.
    """
    digest = hashlib.sha256()
    for name in PAGE_FILES:
        with open(os.path.join(WEB_FOLDER, name), "rb") as file:
            # Each file's own hash, so a byte moved from the end of one to the start of
            # the next still makes a different version.
            digest.update(hashlib.sha256(file.read()).digest())
    return digest.hexdigest()[:12]


class SettingsPage:
    """What the page shows and changes, and the server it's served from."""

    def __init__(self, allow: List[str], port: int = 8765, running=None, brain=None,
                 wake_word: Optional[Callable[[], str]] = None, restart: Optional[Callable[[], str]] = None,
                 started: Optional[float] = None, proxy_port: int = 0, proxy_from: Optional[List[str]] = None,
                 password: Optional[PagePassword] = None, logins: Optional[Logins] = None):
        """
        Args:
            allow: The Tailscale logins let in.
            port: Where to listen, on this machine alone - 0 for any free one.
            running: The Config Nova started with, to tell when a restart is needed.
            brain: The Brain, which has what every satellite shares - the clock, memory,
                ntfy, speech-to-text and routines - and the session of each satellite
                connected, for its volumes, music, lights, mute and what it's doing.
            wake_word: What Nova's listening for - openWakeWord's stand-in, if Nova's
                own wake word model is missing.
            restart: Restarts the brain, returning what to tell the user - or None for Brain.restart.
            started: When the brain started, in seconds.
            proxy_port: Where to listen for Nginx Proxy Manager, on every address - 0
                for not at all.
            proxy_from: The addresses answered there - NPM's.
            password: The page's password, for NPM - or None for the one in Nova's config folder.
            logins: The logins it gives - or None for those in Nova's data folder.
        """
        self.allow = [login.strip().lower() for login in allow if login.strip()]
        self.port = port
        self.proxy_port = proxy_port
        self.proxy_from = [str(address).strip() for address in proxy_from or [] if str(address).strip()]
        self.password = password or PagePassword()
        self.logins = logins or Logins()
        self.wrong_tries = WrongTries()
        # The two warnings a device can cause through NPM without the password.
        self.turn_aways = LogLimit()
        self.refusals = LogLimit()
        # Held from a password's check to the login it gives, and through a change of
        # password - so a login made with the old one can't land after the change has
        # ended every login, and outlive it.
        self._password_lock = threading.Lock()
        self.running = running
        self.brain = brain
        # Each satellite's playlists, with when they were asked for.
        self._playlists: Dict[str, Tuple[List[str], float]] = {}
        self._listening_for = wake_word
        self._restart = restart
        self.started = started or time.time()
        # Worked out once, as the brain starts, rather than each time it's asked for -
        # an update is used from the restart after it, so that's when the page is told.
        self.page_version = page_version()
        with open(paths.DEFAULTS_FILE) as file:
            self.defaults = yaml.safe_load(file)
        self._keys_changed = False
        # The settings used straight away which the brain couldn't use when they were
        # saved, as (section, name) - so they wait for a restart, and the page says so.
        self._unused: Set[Tuple[str, str]] = set()
        # Held while the settings file or the keys are read, changed and written back,
        # as each request has a thread of its own - two saves at once, from two tabs or
        # two keys pasted quickly, could otherwise each write over the other.
        self._saving = threading.Lock()
        # Whether the satellites' file couldn't be read when last asked - see
        # _satellite_names, which says so once until it can be.
        self._satellites_unreadable = False
        # What was just removed, for a while, so the toast's Undo can put it back.
        self._undo = UndoShelf()
        self._spotify = self._spotify_sign_in(running)
        self._server: Optional[ThreadingHTTPServer] = None
        self._proxy_server: Optional[ThreadingHTTPServer] = None

    @staticmethod
    def _spotify_sign_in(running) -> Optional[SpotifySignIn]:
        """
        The sign-in to Spotify the page offers - when it's the player, and its keys
        were set as Nova started, as the satellites' Spotify is made from those too.
        Spotify always shows its page, so "Sign in again" can choose another account.

        Args:
            running: The Config Nova started with, or None.
        """
        if running is None or (running.section("music").get("player") or "spotify").strip().lower() != "spotify":
            return None
        try:
            return SpotifySignIn(spotify_oauth(show_dialog=True))
        except ValueError:
            return None

    @classmethod
    def from_config(cls, config, brain, **parts) -> Optional["SettingsPage"]:
        """
        The page settings_page in config.yaml describes, or None if it's switched off.

        Args:
            config: Configs loaded from config.yaml.
            brain: The Brain the page looks after the satellites of.
            parts: Everything else the page needs - see __init__.
        """
        settings = config.section("settings_page")
        port = int(settings.get("port") or 0)
        if not port:
            return None
        allow = settings.get("allow") or []
        if not allow:
            logger.warning("The settings page lets nobody in until settings_page.allow names your Tailscale login")
        return cls(allow, port, running=config, brain=brain, proxy_port=int(settings.get("proxy_port") or 0),
                   proxy_from=settings.get("proxy_from") or [], **parts)

    # Serving it

    def start(self):
        """Starts serving the page, on a thread of its own - and for NPM on another, if it's set up."""
        self._server = self._serve("127.0.0.1", self.port, "tailnet")
        if self._server is not None:
            self.port = self._server.server_address[1]
            logger.info(f"The settings page is on port {self.port}")
        if not self.proxy_port:
            return
        if not self.proxy_from:
            logger.warning("The settings page isn't served to Nginx Proxy Manager until settings_page.proxy_from "
                           "names its address")
            return
        self._proxy_server = self._serve("0.0.0.0", self.proxy_port, "npm")
        if self._proxy_server is not None:
            self.proxy_port = self._proxy_server.server_address[1]
            logger.info(f"The settings page is on port {self.proxy_port} for Nginx Proxy Manager")

    def _serve(self, address: str, port: int, route: str) -> Optional[ThreadingHTTPServer]:
        """
        Args:
            address: Where to listen.
            port: The port.
            route: "tailnet" or "npm", which says how a request on it is let in.

        Returns:
            The server, serving on a thread of its own - or None if it couldn't start.
        """
        try:
            server = ThreadingHTTPServer((address, port), _Handler)
        except OSError as e:
            logger.error(f"Couldn't start the settings page on port {port}: {e}")
            return None
        server.daemon_threads = True
        server.page = self
        server.route = route
        server.turning_away = threading.BoundedSemaphore(TURN_AWAYS_AT_ONCE)
        name = "settings-page" if route == "tailnet" else f"settings-page-{route}"
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": STOP_CHECK},
                         name=name, daemon=True).start()
        return server

    def stop(self):
        """Stops serving it."""
        for server in (self._server, self._proxy_server):
            if server is not None:
                server.shutdown()
                server.server_close()

    # What it shows

    def _session(self, satellite: Optional[str]):
        """
        Args:
            satellite: A satellite's name, or None for the default.

        Returns:
            Its session if it's connected - else None - and the name the request is
            for, which is None if there are no satellites at all. With the
            satellites' file unreadable, there's no telling which is the default,
            so a request for it has no session - and the page still loads.
        """
        try:
            return self.brain.session_for(satellite), satellite or self.brain.default_satellite()
        except (OSError, ValueError):
            return self.brain.session_for(satellite) if satellite else None, satellite

    @staticmethod
    def _not_connected(name: Optional[str]) -> Dict:
        """What to say when a change is for a satellite which isn't connected."""
        if name is None:
            return {"success": False, "message": "There's no satellite to do that on."}
        return {"success": False, "message": f"{name} isn't connected."}

    def _satellites(self, names: List[str]) -> List[Dict]:
        """
        Each satellite the brain lets in, whether it's the default, and whether it's
        connected - with what it's doing, its mute and, if Spotify is the player, the
        speaker it plays on. Never its password.

        Args:
            names: The satellites, as the brain has them - the first is the default.
        """
        found = []
        default = names[0] if names else None
        # With Navidrome as the player, Spotify plays nowhere, so none is said. Spotify
        # is the player when none is chosen, as with no config at all.
        spotify = self.brain.config is None or _chosen(self.brain.config) == "spotify"
        for name in names:
            session = self.brain.session_for(name)
            # One which doesn't say plays on the brain's own - see SpotifyController.
            speaker = (session.satellite.speaker_name or brains_speaker(self.brain.config)
                       if session is not None and spotify else None)
            found.append({"name": name, "connected": session is not None,
                          "activity": session.activity if session is not None else "idle",
                          "muted": session is not None and session.muted, "default": name == default,
                          "spotify_speaker": speaker})
        return found

    def state(self, satellite: Optional[str] = None) -> Dict:
        """
        Everything on the page - never a key's value, only whether it's set.

        Args:
            satellite: The satellite whose volumes, mute and timers are shown, or None
                for the default. Those are empty while it isn't connected. One nobody
                has is taken for the default, as the page may be looking after one
                removed on another device - and says which it is showing.
        """
        names = self._satellite_names()
        if satellite and names is not None and satellite not in names:
            satellite = None
        session, chosen = self._session(satellite)
        saved, problem = self._saved()
        settings = []
        for section, name, label, used in EDITABLE:
            default = self.defaults[section][name]
            value = saved.section(section).get(name) if saved else default
            setting = {"key": f"{section}.{name}", "label": label, "kind": _kind(default), "value": value,
                       "used": NOW if used == "now" else LATER}
            # One of a few words, which the page offers as a list to pick from.
            if (section, name) in CHOICES:
                setting["choices"] = list(CHOICES[(section, name)])
            # What it may be, for the page to check as it's typed - the same as saving does.
            limit = LIMITS.get((section, name))
            if limit is not None:
                setting["form"] = limit.form
                if limit.form == "number":
                    setting.update({"min": limit.low, "max": limit.high, "whole": limit.whole})
            settings.append(setting)
        secrets = _secrets_set(paths.SECRETS_FILE)
        keys = [{"name": name, "label": label,
                 "set": any(secrets.get(spelling) for spelling in [name] + OLDER_NAMES.get(name, []))}
                for name, label in KEYS]
        routines = self.brain.routines
        return {"status": self._status(),
                "satellites": self._satellites(names) if names is not None else [],
                # Said as that, rather than as there being no satellites.
                "satellites_unreadable": names is None,
                "satellite": chosen,
                "wake_word": self._wake_word(),
                "muted": session is not None and session.muted,
                "volumes": volume_levels(session.volume, session.music_player) if session is not None else {},
                "clock": self.brain.clock.overview(satellite=chosen),
                "memories": self.brain.memory.facts(),
                "routines": routines.all() if routines is not None else [],
                "keys": keys,
                "settings": settings,
                "settings_problem": problem,
                "restart_needed": self._restart_needed(saved),
                "page": self.page_version}

    def live(self, satellite: Optional[str] = None) -> Dict:
        """
        What a satellite's Nova is doing right now, and whether Stop has anything to
        stop on any satellite - asked for every second or two while the page is open.
        """
        session, _ = self._session(satellite)
        try:
            stoppable = self.brain.stoppable()
        except (OSError, ValueError):
            # The satellites' file can't be read - which state() says.
            stoppable = False
        return {"activity": session.activity if session is not None else "idle", "stoppable": stoppable}

    def _wake_word(self) -> str:
        """What wakes Nova, for the page to say it's waiting for it."""
        return self._listening_for() if self._listening_for else self.defaults["assistant"]["wake_word"]

    def music_state(self, satellite: Optional[str] = None) -> Dict:
        """
        What's playing on a satellite, and the playlists to pick from - asked for every
        few seconds while the page is open, apart from everything else, so a slow
        music player doesn't hold the rest up.
        """
        session, name = self._session(satellite)
        music = session.music_player if session is not None else None
        if session is None:
            # Said before anything about the player, which a satellite can't be blamed
            # for not having while it's away - and a name nobody has isn't away.
            names = self._satellite_names() if satellite else None
            if names is not None and satellite not in names:
                shown = {"available": False, "why": f"There's no satellite called {satellite}."}
            else:
                shown = {"available": False, "why": "No satellite is connected just now."}
        elif music is None:
            shown = {"available": False, "why": self._why_no_player()}
        else:
            shown = {"available": True, "player": getattr(music, "name", "Music"), "now": music.now_playing(),
                     "playlists": self._user_playlists(name, music)}
        # Offered whatever the satellites are doing, as it's the brain's, for them all.
        if self._spotify is not None:
            shown["spotify"] = {"signed_in": self._spotify.signed_in()}
        return shown

    def _why_no_player(self) -> str:
        """Why the satellite has no player, or - with Nova's config not known - that it hasn't."""
        return why_no_player(self.running) if self.running is not None else NO_PLAYER

    def _user_playlists(self, name: str, music) -> List[str]:
        kept = self._playlists.get(name)
        if kept is None or time.monotonic() - kept[1] > PLAYLISTS_KEPT:
            kept = self._playlists[name] = (music.playlists(), time.monotonic())
        return kept[0]

    def cover(self, satellite: Optional[str] = None) -> Optional[Tuple[bytes, str]]:
        """The cover of what's playing on a satellite, as an image and its type, or None."""
        session, _ = self._session(satellite)
        music = session.music_player if session is not None else None
        return music.cover() if music is not None else None

    def home_state(self, satellite: Optional[str] = None) -> Dict:
        """
        The lights, and the scripts and scenes, from Home Assistant, as a satellite
        sees them - asked for apart from everything else, like the music.
        """
        session, _ = self._session(satellite)
        home = session.home if session is not None else None
        if home is None:
            return {"available": False}
        return {"available": True, "area": home.area, **home.overview()}

    def batteries_state(self) -> Dict:
        """
        Every battery Home Assistant has, with what the user chose for it - read fresh
        each time, as the choices are for the devices it shows now.

        Returns:
            {"available": True, "also_send_when_said": ..., "devices": [...]} in name
            order, each device with its id, name, level, charging state, "stale" (how
            old its reading is, in words, if it's gone stale), kind, enabled, marks,
            default_marks, send_to_phone and "changed" (whether any of those differ
            from the defaults, which is when the page offers to put them back). With
            more than one person in Home Assistant, each also has "person": the name
            of the one whose presence decides whether its warning is said aloud. Else
            {"available": False, "message": ...}.
        """
        watcher = self.brain.batteries
        if watcher is None:
            return {"available": False, "message": NO_HOME}
        try:
            found = watcher.read()
        except HOME_FAILURES as e:
            logger.warning(f"Settings page: couldn't read the batteries: {e}")
            return {"available": False, "message": CANT_REACH_HOME}
        choices = watcher.choices
        now = time.time()
        deciding = watcher.deciding_people(found)
        devices = []
        for battery in sorted(found, key=lambda b: (b.name.lower(), b.id)):
            age = now - battery.changed
            marks, default_marks = choices.marks_for(battery), choices.default_marks(battery)
            enabled, send_to_phone = choices.enabled(battery.id), choices.send_to_phone(battery)
            devices.append({
                "id": battery.id, "name": battery.name, "level": round(battery.level),
                "charging": battery.charging,
                "stale": format_duration(age) if age > watcher.stale_after else None,
                "kind": battery.kind, "enabled": enabled, "marks": marks, "default_marks": default_marks,
                "send_to_phone": send_to_phone,
                "changed": choices.changed(battery),
            })
            if battery.id in deciding:
                devices[-1]["person"] = deciding[battery.id]
        return {"available": True, "also_send_when_said": choices.also_send_when_said, "devices": devices}

    def calendars_state(self) -> Dict:
        """
        The Calendars and lists section's calendars: whether Nova is signed in to Google,
        and once it is, each calendar with what's chosen for it - from Google's list, read
        afresh once it's five minutes old. Each is shown by Google's own name, one named by
        an email address too, as it's the user's own page and they need to tell them apart -
        but never logged.

        Returns:
            {"available": True, "signed_in": True, "calendars": [{"id", "name", "primary",
            "writable", "whats_on", "activity"}], "new_events": the id of the calendar new
            events go to} - "activity" None for one that can't be one, see
            GoogleCalendar.is_activity - or {"available": True, "signed_in": True, "message": ...}
            with Google out of reach; {"available": True, "signed_in": False} before the
            sign-in, with a "message" once Google no longer takes it; or {"available": False,
            "message": ...} until Google's keys are set and Nova restarted.
        """
        calendar = self.brain.calendar
        if calendar is None:
            return {"available": False, "message": NO_GOOGLE}
        if not calendar.sign_in.signed_in():
            return {"available": True, "signed_in": False}
        listed = self._read_calendars(calendar)
        if isinstance(listed, str):
            # Never "Signed in" beside a sign-in Google no longer takes - the sign-in's button is the way back.
            return {"available": True, "signed_in": listed == GOOGLE_OUT_OF_REACH, "message": listed}
        left_out = calendar.left_out()
        new_events = calendar.new_events_calendar(listed)
        return {"available": True, "signed_in": True, "new_events": new_events["id"] if new_events else None,
                "calendars": [{**each, "whats_on": each["id"] not in left_out, "activity": calendar.is_activity(each)}
                              for each in listed]}

    def home_choices_state(self) -> Dict:
        """
        The Calendars and lists section's other parts, read afresh from Home Assistant:
        the lists, how each device is sent to, and the weather and the PCs when there is a
        choice to make.

        Returns:
            {"available": True, "lists": [{"id", "name", "count", "aliases", "main"}],
            "sending": [{"name", "how"}], "weather": [{"id", "name", "chosen"}] or None,
            "pcs": {"pcs": [{"device", "name", "wake"}], "buttons": [{"id", "name"}]} or
            None} - the weather only with more than one entity, the PCs only with a PC and
            more than one PC or more than one button. Else {"available": False, "message": ...}.
        """
        brain = self.brain
        if brain.home is None:
            return {"available": False, "message": NO_HOME}
        try:
            # Each read only if it's there - the brain makes all three with Home Assistant,
            # but one missing shouldn't take the others with it.
            lists = brain.lists.overview() if brain.lists is not None else []
            devices = [AppDevice.from_home(found) for found in brain.home.app_devices()]
            weather = brain.weather.entities() if brain.weather is not None else []
            pcs = brain.pc.overview() if brain.pc is not None else {"pcs": [], "buttons": []}
        except HOME_FAILURES as e:
            logger.warning(f"Settings page: couldn't read the lists, devices, weather and PCs: {e}")
            return {"available": False, "message": CANT_REACH_HOME}
        ntfy = brain.ntfy
        sending = []
        for device in sorted(devices, key=lambda each: (each.name.lower(), each.id)):
            if device.phone:
                how = ("Through ntfy, to the topic under Notifications" if ntfy and ntfy.phone
                       else "Through ntfy, once a topic for your phone is set under Notifications")
            elif device.notify:
                how = "Through Home Assistant"
            else:
                # Home Assistant has no service to send to it with.
                continue
            sending.append({"name": device.name, "how": how})
        choose_pc = bool(pcs["pcs"]) and (len(pcs["pcs"]) > 1 or len(pcs["buttons"]) > 1)
        return {"available": True, "lists": lists, "sending": sending,
                "weather": weather if len(weather) > 1 else None, "pcs": pcs if choose_pc else None}

    def _restart_needed(self, saved: Optional[Config]) -> bool:
        """
        Whether anything saved waits for a restart: a key, a setting which differs from
        the one running, or one the brain couldn't use straight away.

        Args:
            saved: The settings as saved now, or None if they can't be read.
        """
        # One used straight away is in the running settings once it's saved, so only one
        # which waits differs - or one the brain couldn't use, which is in them but unused.
        changed = saved is not None and self.running is not None and any(
            saved.section(section).get(name) != self.running.section(section).get(name)
            for section, name, _, _ in EDITABLE)
        return changed or self._keys_changed or bool(self._unused)

    def _saved(self):
        """The settings as saved now, and any problem reading them."""
        try:
            return Config(quiet=True), None
        except (ConfigError, OSError, ValueError) as e:
            return None, str(e)

    def _status(self) -> Dict:
        """How Nova's doing."""
        stt, ntfy = self.brain.stt, self.brain.ntfy
        reply = None
        try:
            latest = timeline.load(os.path.join(paths.LOGS_DIR, "latency.jsonl"), most=REPLIES_COUNTED)
            rows = timeline.summarise(latest)
            reply = next((row for row in rows if row["stage"] == "End of speech to reply ready"), None)
        except OSError:
            pass
        # Nemotron, on the brain, as Home Assistant's Backend sensor has it.
        speech_to_text = "ready" if getattr(stt, "loaded", False) else "missing"
        ntfy = None if ntfy is None else ("listening" if ntfy.listening else "not connected")
        return {"started": self.started, "reply_ms": reply["p50"] if reply else None,
                "replies": reply["count"] if reply else 0, "speech_to_text": speech_to_text, "ntfy": ntfy,
                "problems": self._problems()}

    @staticmethod
    def _problems() -> List[Dict]:
        """The latest warnings and errors in the brain's log, newest first."""
        try:
            with open(os.path.join(paths.LOGS_DIR, "backend.log"), "rb") as file:
                file.seek(max(0, os.fstat(file.fileno()).st_size - LOG_TAIL))
                lines = file.read().decode(errors="replace").splitlines()
        except OSError:
            return []
        problems = []
        for line in lines:
            found = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d):\d\d,\d+ - [\w.]+ - (WARNING|ERROR|CRITICAL) - (.*)", line)
            if found:
                problems.append({"time": found[1], "level": found[2].lower(), "message": found[3][:300]})
        return problems[-PROBLEMS_SHOWN:][::-1]

    # What it changes

    def set_volume(self, category: str, percent, who: str, satellite: Optional[str] = None) -> Dict:
        """
        Changes a satellite's volume, and plays the beep at its new level on its speaker
        - as the slider is dragged, so the user hears how loud it now is.
        """
        percent = int(percent)
        session, name = self._session(satellite)
        if session is None:
            return self._not_connected(name)
        music = session.music_player
        result = change_volume(session.volume, music, category, percent)
        if result.get("success"):
            session.boop(percent / 100 if category == "music" and music is not None else session.volume.get(category))
        logger.info(f"Settings page: {who} set the {name} satellite's {category} volume to {percent}%")
        return result

    def hear_voice(self, speed, who: str, satellite: Optional[str] = None) -> Dict:
        """
        Says a sample on a satellite at the speaking speed on the slider - before it's
        saved, so the user hears it first. Only the speed comes from the page, never
        what's said - see Session.hear_voice.

        Args:
            speed: How fast, as the page sent it.
            who: The Tailscale login asking, for the log.
            satellite: Which satellite, or None for the default.

        Returns:
            Dict with 'success' and a 'message' saying where it's said, or why not.
        """
        problem = limit_problem("voice", "speed", speed)
        if problem:
            return {"success": False, "message": f"Speaking speed {problem}."}
        session, name = self._session(satellite)
        if session is None:
            return self._not_connected(name)
        if not session.hear_voice(float(speed)):
            return {"success": False, "message": "Nova's busy - try again in a moment."}
        logger.info(f"Settings page: {who} heard the speaking speed {speed:g} on the {name} satellite")
        return {"success": True, "message": f"That's being said on {name}."}

    def set_muted(self, muted: bool, who: str, satellite: Optional[str] = None) -> Dict:
        """Mutes or unmutes a satellite's microphone - see Session.set_muted."""
        if not isinstance(muted, bool):
            return {"success": False, "message": "That didn't make sense."}
        session, name = self._session(satellite)
        if session is None:
            return self._not_connected(name)
        session.set_muted(muted)
        logger.info(f"Settings page: {who} {'muted' if muted else 'unmuted'} the {name} satellite's microphone")
        if muted:
            return {"success": True, "message": f"Muted. Nova won't hear \"{self._wake_word()}\" until it's unmuted."}
        return {"success": True, "message": "Unmuted."}

    def add_alarm(self, at: str, repeat: Optional[str], who: str, satellite: Optional[str] = None) -> Dict:
        """Sets an alarm on a satellite - connected or not, as it rings from its copy while away."""
        found = re.fullmatch(r"(\d{1,2}):(\d\d)", (at or "").strip())
        if not found:
            return {"success": False, "message": "That isn't a time."}
        _, name = self._session(satellite)
        # Never one for a name nobody has: it would be saved, but never heard. A satellite
        # which is merely away does count, as the brain keeps its alarms.
        if name is not None and name not in self.brain.satellites():
            return {"success": False, "message": f"There's no satellite called {name}."}
        logger.info(f"Settings page: {who} set an alarm for {at}")
        return self.brain.clock.set_alarm(int(found[1]), int(found[2]), repeat or None, satellite=name)

    def remove(self, kind: str, item_id: int, who: str) -> Dict:
        """
        Removes a timer, reminder or alarm, keeping it a minute so it can be put back -
        see undo.

        Args:
            kind: "alarm", or "timer" for a timer or reminder.
            item_id: Its id, from the clock's overview.
            who: The Tailscale login removing it, for the log.

        Returns:
            Dict with 'success' and a 'message' - and once removed, 'undo', the token
            which puts it back. Never what was removed, which stays on the brain.
        """
        kind = "alarm" if kind == "alarm" else "timer"
        logger.info(f"Settings page: {who} removed {kind} {item_id}")
        result = self.brain.clock.remove(kind, item_id)
        answer = {"success": result["success"], "message": result["message"]}
        if result.get("removed") is not None:
            answer["undo"] = self._undo.put(kind, result["removed"])
        return answer

    def undo(self, token, who: str) -> Dict:
        """
        Puts back what was just removed, as the toast's Undo asks - once, and only for
        UNDO_KEPT seconds after. One refused - for a clash, or a satellite gone - can
        be tried again meanwhile.

        Args:
            token: The token remove answered with, as the page sent it.
            who: The Tailscale login asking, for the log.

        Returns:
            Dict with 'success' and a 'message'.
        """
        def put_back(kind: str, item: Union[Dict, Tuple[str, int]]) -> Dict:
            if kind == "fact":
                logger.info(f"Settings page: {who} undid forgetting something")
                return self.brain.memory.put_back(*item)
            logger.info(f"Settings page: {who} undid removing {kind} {item['id']}")
            # Put back for a satellite the brain no longer lets in, it would ring
            # nowhere - a missed wake-up.
            if item.get("satellite") and item["satellite"] not in self.brain.satellites():
                return {"success": False, "message": "That satellite's gone, so it can't ring there."}
            return self.brain.clock.put_back(kind, item)

        answer = self._undo.use(token, put_back)
        return answer if answer is not None else {"success": False, "message": "That can't be undone now."}

    def forget(self, fact: str, who: str) -> Dict:
        """
        Forgets a fact, keeping it a minute so it can be put back - see undo.

        Args:
            fact: The fact, as the page lists it.
            who: The Tailscale login forgetting it, for the log.

        Returns:
            Dict with 'success' and a 'message' - and once forgotten, 'undo', the token
            which puts it back. Never the fact, which stays on the brain.
        """
        logger.info(f"Settings page: {who} had something forgotten")
        result = self.brain.memory.forget(fact)
        answer = {"success": result["success"], "message": result["message"]}
        if result.get("forgotten") is not None:
            answer["undo"] = self._undo.put("fact", (result["forgotten"], result["at"]))
        return answer

    def set_key(self, name: str, value: str, who: str) -> Dict:
        """
        Saves a key to the secrets file, in place of any it had - never showing it,
        or logging it.
        """
        if name not in dict(KEYS):
            return {"success": False, "message": "That isn't one of Nova's keys."}
        value = str(value).strip()
        if not value or re.search(r"[\x00-\x1f]", value):
            return {"success": False, "message": "That doesn't look like a key."}
        # Quoted the way python-dotenv reads it back exactly, checked with the same
        # version Nova uses - except for this, which it reads as a variable whatever
        # the quotes, so it would quietly be something else by the time Nova used it.
        if "${" in value:
            return {"success": False, "message": "A key can't have ${ in it - Nova would read it as something else."}
        path = paths.SECRETS_FILE
        quoted = value.replace("\\", "\\\\").replace('"', '\\"')
        with self._saving:
            try:
                with open(path) as file:
                    lines = file.read().splitlines()
            except FileNotFoundError:
                lines = []
            kept = [line for line in lines if not re.match(rf"\s*(?:export\s+)?{re.escape(name)}\s*=", line)]
            storage.save_text(path, "\n".join(kept + [f'{name}="{quoted}"']) + "\n")
        self._keys_changed = True
        logger.info(f"Settings page: {who} set {name}")
        return {"success": True, "message": "Saved. It's used once Nova restarts."}

    def save_settings(self, changes: Dict[str, Any], who: str) -> Dict:
        """
        Saves settings to this install's own settings file, keeping it to what's
        different from config.yaml's defaults - checked the same way as when Nova
        starts, and for the settings which need each other, so a change it would refuse
        is never saved. Those used straight away then go into the running settings.

        Args:
            changes: Each setting, as "section.name", to what the page sent.
            who: The Tailscale login saving them, for the log.

        Returns:
            Dict with 'success' and a 'message' saying when each is used - and once
            saved, 'restart_needed', for whether any of it waits for a restart.
        """
        editable = {f"{section}.{name}": (section, name, label, used) for section, name, label, used in EDITABLE}
        unknown = [key for key in changes if key not in editable]
        if unknown:
            return {"success": False, "message": f"The page can't change {', '.join(unknown)}."}
        path = paths.CONFIG_FILE
        # The file read, written and the running settings changed under the one lock, so
        # two saves at once can't each leave the other out of either.
        with self._saving:
            try:
                with open(path) as file:
                    settings = yaml.safe_load(file) or {}
            except FileNotFoundError:
                settings = {}
            except (OSError, yaml.YAMLError) as e:
                return {"success": False, "message": f"Couldn't read {path}: {e}"}

            chosen = {}
            for key, given in changes.items():
                section, name, label, _ = editable[key]
                default = self.defaults[section][name]
                try:
                    value = _from_page(given, default)
                except (TypeError, ValueError):
                    # Only a switch or a number can be sent as the wrong kind.
                    kind = "yes or no" if isinstance(default, bool) else "a number"
                    return {"success": False, "message": f"{label} should be {kind}."}
                problem = limit_problem(section, name, value)
                if problem:
                    return {"success": False, "message": f"{label} {problem}."}
                chosen[key] = value
                if value == default:
                    settings.get(section, {}).pop(name, None)
                else:
                    settings.setdefault(section, {})[name] = value
            settings = {section: values for section, values in settings.items() if values}
            try:
                check(settings, self.defaults, path)
            except ConfigError as e:
                return {"success": False, "message": str(e)}
            # As Nova will run with them: config.yaml's, with the install's on top.
            whole = {section: {**defaults, **(settings.get(section) or {})}
                     for section, defaults in self.defaults.items() if isinstance(defaults, dict)}
            missing = clashes(whole)
            if missing:
                return {"success": False, "message": " ".join(sentence for _, sentence in missing)}

            if os.path.exists(path):
                shutil.copyfile(path, path + ".bak")
                os.chmod(path + ".bak", 0o600)
            storage.save_text(path, SETTINGS_HEADER + yaml.safe_dump(settings, sort_keys=False, allow_unicode=True))

            now, waiting = {}, []
            for key, value in chosen.items():
                section, name, label, used = editable[key]
                if self.running is not None and value == self.running.section(section).get(name):
                    continue
                if used == "now" and self.running is not None:
                    now[(section, name)] = value
                else:
                    waiting.append(label)
            unused = self._apply(now) if now else []
            # What the brain couldn't use waits too, so "straight away" is only ever said of what was.
            waiting += [label for section, name, label, _ in EDITABLE if (section, name) in unused]
            used = len(now) - len(unused)
            restart_needed = self._restart_needed(self._saved()[0])
        logger.info(f"Settings page: {who} changed {', '.join(changes)}")

        if not waiting:
            # A restart still owed - for a hand edit, or an earlier save - is said too, as
            # the page offers it beside this message.
            message = "Saved. It's used straight away." + (
                " An earlier change is used once Nova restarts." if restart_needed else "")
        elif not used:
            message = "Saved. It's used once Nova restarts" + (
                ", as it couldn't be used straight away." if unused else ".")
        else:
            labels = waiting[0] if len(waiting) == 1 else f"{', '.join(waiting[:-1])} and {waiting[-1]}"
            message = (f"Saved. {labels} {'is' if len(waiting) == 1 else 'are'} used once Nova restarts, "
                       f"and the rest straight away.")
            if unused:
                message += " Some of it couldn't be used straight away."
        return {"success": True, "message": message, "restart_needed": restart_needed}

    def _apply(self, changes: Dict[Tuple[str, str], Any]) -> List[Tuple[str, str]]:
        """
        Has the brain use settings just saved, noting any it couldn't - they're in the
        file, so they're used once Nova restarts. Called with _saving held.

        Args:
            changes: Each setting used straight away, as (section, name), to its value.

        Returns:
            Those the brain couldn't use.
        """
        try:
            unused = list(self.brain.apply_settings(changes))
        except Exception:
            logger.exception("Settings saved on the page couldn't be used straight away, so wait for a restart")
            unused = list(changes)
        told = set(changes)
        # ntfy is told all of its settings together, so one which failed before and is
        # passed on again with another is used now too.
        if any(section == "ntfy" for section, _ in told) and not any(section == "ntfy" for section, _ in unused):
            told |= {("ntfy", name) for name in RETOPIC}
        self._unused = (self._unused - told) | set(unused)
        return unused

    def restart(self, who: str) -> Dict:
        logger.info(f"Settings page: {who} restarted Nova")
        return {"success": True, "message": (self._restart or self.brain.restart)()}

    def press_stop(self, who: str) -> Dict:
        """
        Stop, as Home Assistant's: silences whatever rings, and stops Nova talking, on
        every satellite - see Brain.stop_everywhere. Not stop(), which stops serving
        the page.

        Args:
            who: The Tailscale login pressing it, for the log.

        Returns:
            Dict with 'success' and 'message'.
        """
        logger.info(f"Settings page: {who} pressed Stop")
        # It may have stopped by itself since the page last looked.
        if not self.brain.stop_everywhere():
            return {"success": True, "message": "Nothing's ringing or speaking."}
        return {"success": True, "message": "Stopped."}

    def play(self, action: str, who: str, query: str = "", playlist: str = "", satellite: Optional[str] = None) -> Dict:
        """
        Plays, pauses, skips or goes back, or plays something or a playlist - through
        the same actions as asking out loud, so that a pause from the page isn't undone
        once the assistant finishes talking. What's asked for isn't logged, as what's
        said isn't unless the user wants it.

        Args:
            action: "play" - with a query, or none to carry on - "playlist", or one of
                PLAYBACK.
            who: Whose login asked.
            query: What to play, as it would be said.
            playlist: The playlist's name.
            satellite: Where it plays, or None for the default.
        """
        session, name = self._session(satellite)
        if session is None:
            return self._not_connected(name)
        if session.music_player is None or session.actions is None:
            return {"success": False, "message": self._why_no_player()}
        if action == "play":
            result = session.actions.play_music(str(query).strip() or None)
        elif action == "playlist":
            result = session.actions.play_playlist(str(playlist))
        elif action in PLAYBACK:
            result = session.actions.control_playback(action)
        else:
            return {"success": False, "message": "That isn't something the music does."}
        logger.info(f"Settings page: {who} asked the music to {action}")
        return {"success": bool(result.get("success")), "message": result.get("message") or ""}

    def start_spotify_sign_in(self, who: str) -> Dict:
        """
        Starts signing in to Spotify, for every satellite.

        Returns:
            Dict with 'success', and the 'url' of Spotify's page to sign in on.
        """
        if self._spotify is None:
            return {"success": False, "message": "Nova isn't set up to sign in to Spotify."}
        url = self._spotify.start()
        logger.info(f"Settings page: {who} started signing in to Spotify")
        return {"success": True, "url": url}

    def finish_spotify_sign_in(self, address: str, who: str) -> Dict:
        """
        Finishes signing in to Spotify, from the address Spotify sent the browser to -
        which isn't logged, as it has the code in it. Each satellite's Spotify uses the
        sign-in from its next command.

        Args:
            address: The address, as pasted.
            who: Whose login it is, for the log.

        Returns:
            Dict with 'success' and a 'message'.
        """
        if self._spotify is None:
            return {"success": False, "message": "Nova isn't set up to sign in to Spotify."}
        try:
            self._spotify.finish(address)
        except SpotifySignInError as e:
            logger.info(f"Settings page: {who} couldn't sign in to Spotify")
            return {"success": False, "message": str(e)}
        # Anything else's own words aren't for the page - a pasted "http://[x" gives
        # urllib's "Invalid IPv6 URL", and an answer from Spotify that isn't JSON one of
        # requests'. Neither is logged with its words, which could be the address's.
        except ValueError as e:
            logger.warning(f"Settings page: {who} couldn't sign in to Spotify: {type(e).__name__}")
            return {"success": False, "message": "Spotify didn't accept that sign-in - start again."}
        except requests.RequestException as e:
            logger.warning(f"Settings page: couldn't reach Spotify to sign in: {type(e).__name__}")
            return {"success": False, "message": "Couldn't reach Spotify - try again."}
        # The old account's playlists are no use if this is another's.
        self._playlists.clear()
        logger.info(f"Settings page: {who} signed in to Spotify")
        return {"success": True, "message": "Signed in to Spotify."}

    def start_google_sign_in(self, who: str) -> Dict:
        """
        Starts signing in to Google, for the calendar.

        Returns:
            Dict with 'success', and the 'url' of Google's page to sign in on.
        """
        calendar = self.brain.calendar
        if calendar is None:
            return {"success": False, "message": NO_GOOGLE}
        url = calendar.sign_in.start()
        logger.info(f"Settings page: {who} started signing in to Google")
        return {"success": True, "url": url}

    def finish_google_sign_in(self, address: str, who: str) -> Dict:
        """
        Finishes signing in to Google, from the address Google sent the browser to -
        which isn't logged, as it has the code in it. The calendar uses the sign-in from
        its next request.

        Args:
            address: The address, as pasted.
            who: Whose login it is, for the log.

        Returns:
            Dict with 'success' and a 'message'.
        """
        calendar = self.brain.calendar
        if calendar is None:
            return {"success": False, "message": NO_GOOGLE}
        try:
            calendar.sign_in.finish(address)
        except GoogleSignInError as e:
            logger.info(f"Settings page: {who} couldn't sign in to Google")
            return {"success": False, "message": str(e)}
        # As for Spotify, anything else's own words aren't for the page, or the log.
        except ValueError as e:
            logger.warning(f"Settings page: {who} couldn't sign in to Google: {type(e).__name__}")
            return {"success": False, "message": "Google didn't accept that sign-in - start again."}
        except ClientRefused:
            # Mistyped at first setup, or the client deleted or its secret made anew - trying again won't help.
            logger.warning(f"Settings page: Google didn't accept the client ID or secret for {who}'s sign-in")
            return {"success": False, "message": CHECK_GOOGLES_KEYS}
        except SignInNeeded:
            logger.info(f"Settings page: Google didn't accept {who}'s sign-in")
            return {"success": False, "message": "Google didn't accept that sign-in - start again."}
        except requests.RequestException as e:
            # Not the error itself, which could quote what Google answered.
            logger.warning(f"Settings page: couldn't reach Google to sign in: {type(e).__name__}")
            return {"success": False, "message": "Couldn't reach Google - try again."}
        # The calendars of the account before are no use if this is another's.
        calendar.read_list_again()
        logger.info(f"Settings page: {who} signed in to Google")
        return {"success": True, "message": "Signed in to Google."}

    def choose_calendar(self, calendar_id, who: str, whats_on=None, activity=None) -> Dict:
        """
        Chooses whether a calendar is in what's on, and whether it's an activity - see
        GoogleCalendar.choose. It takes effect straight away: an activity's words are
        heard, and Claude's told of it, from the next request. The types are checked here,
        as bool("false") would be True.

        Args:
            calendar_id: The calendar's id, which Google has to be listing.
            who: The login making the change.
            whats_on: Whether it's read for what's on, or None to leave it.
            activity: Whether it's an activity, or None to leave it.

        Returns:
            {"success": ..., "message": ...}
        """
        if not isinstance(calendar_id, str) or (whats_on is None and activity is None) or any(
                value is not None and not isinstance(value, bool) for value in (whats_on, activity)):
            return {"success": False, "message": "That didn't make sense."}
        return self._calendar_change(lambda calendar: calendar.choose(calendar_id, whats_on=whats_on,
                                                                      activity=activity),
                                     f"Settings page: {who} changed a calendar's choices")

    def set_new_events(self, calendar_id, who: str) -> Dict:
        """
        Chooses the calendar new events go to, from the next one added - see
        GoogleCalendar.set_new_events.

        Args:
            calendar_id: The calendar's id - one of the user's own, which Google has to be listing.
            who: The login making the change.

        Returns:
            {"success": ..., "message": ...}
        """
        if not isinstance(calendar_id, str):
            return {"success": False, "message": "That didn't make sense."}
        return self._calendar_change(lambda calendar: calendar.set_new_events(calendar_id),
                                     f"Settings page: {who} changed where new events go")

    def _calendar_change(self, change: Callable[[Any], None], logged: str) -> Dict:
        """
        Makes a change to the calendars' choices, saying how it went. Google's list is read
        first - after a restart nothing's known of it - and before _saving is held, so a
        slow Google never holds up every other save. What's logged never names the
        calendar, as its id or its name can be an email address.
        """
        calendar = self.brain.calendar
        if calendar is None:
            return {"success": False, "message": NO_GOOGLE}
        listed = self._read_calendars(calendar)
        if isinstance(listed, str):
            return {"success": False, "message": listed}
        with self._saving:
            try:
                change(calendar)
            except ValueError as e:
                return {"success": False, "message": str(e)}
            except OSError as e:
                logger.error(f"Settings page: couldn't save a calendar change: {e}")
                return {"success": False, "message": "Couldn't save that."}
        logger.info(logged)
        return {"success": True, "message": "Saved. It's used straight away."}

    @staticmethod
    def _read_calendars(calendar) -> Union[List[Dict], str]:
        """
        Google's list of the calendars - see GoogleCalendar.calendars - or what to say
        when it can't be had.
        """
        try:
            return calendar.calendars()
        except ClientRefused:
            logger.warning("Settings page: Google didn't accept the client ID or secret for the calendars")
            return CHECK_GOOGLES_KEYS
        except SignInNeeded:
            logger.info("Settings page: Google no longer accepts Nova's sign-in for the calendars")
            return SIGN_IN_AGAIN
        except READ_FAILURES as e:
            # Only the kind of failure - a requests error names the address, with a calendar's id in it.
            logger.warning(f"Settings page: couldn't read the calendars from Google: {type(e).__name__}")
            return GOOGLE_OUT_OF_REACH

    def choose_main_list(self, list_id, who: str) -> Dict:
        """
        Chooses the to-do list meant when none is named, from the next request.

        Args:
            list_id: The list's entity id, which Home Assistant has to be showing.
            who: The login making the change.

        Returns:
            {"success": ..., "message": ...}
        """
        return self._home_choice(
            "lists", lambda lists: lists.overview(), lambda lists, seen: lists.choose_main(list_id, seen),
            (list_id,), f"Settings page: {who} chose the main list")

    def choose_weather(self, entity_id, who: str) -> Dict:
        """Chooses the weather entity the forecast uses, from the next request - see choose_main_list."""
        return self._home_choice(
            "weather", lambda weather: weather.entities(), lambda weather, seen: weather.choose(entity_id, seen),
            (entity_id,), f"Settings page: {who} chose the weather")

    def choose_wake(self, device, button, who: str) -> Dict:
        """
        Chooses the Wake-on-LAN button which wakes a PC, from the next "turn on the PC".

        Args:
            device: The PC's Home Assistant device id.
            button: The button's entity id - never logged, as it holds the PC's hardware address.
            who: The login making the change.

        Returns:
            {"success": ..., "message": ...} - naming any PC the button was taken off, as
            one button wakes one PC.
        """
        def choose(pc, seen):
            taken_from = pc.choose_wake(device, button, seen)
            if taken_from:
                has = "has" if len(taken_from) == 1 else "have"
                return f"Saved - {' and '.join(taken_from)} no longer {has} a Wake-on-LAN button."
            return None

        return self._home_choice("pc", lambda pc: pc.overview(), choose, (device, button),
                                 f"Settings page: {who} chose which button wakes a PC")

    def _home_choice(self, part: str, read: Callable[[Any], Any], apply: Callable[[Any, Any], Optional[str]],
                     given: Tuple, logged: str) -> Dict:
        """
        Makes a choice among what Home Assistant has, saying how it went. Home Assistant is
        read first, and before _saving is held, so a slow one never holds up every other
        save - and a choice is checked against what it shows now, never trusted. The types
        are checked here, as a list or a number isn't an id.

        Args:
            part: The brain's part the choice is of - "lists", "weather" or "pc".
            read: Reads what there is to choose from, given the part.
            apply: Makes the choice, given the part and what read gave - returning what
                to say in place of "Saved.", or None.
            given: What the page sent, each of which has to be text.
            logged: The line logged once it's saved.
        """
        brain = self.brain
        if not all(isinstance(each, str) for each in given):
            return {"success": False, "message": "That didn't make sense."}
        chosen_from = getattr(brain, part)
        if brain.home is None or chosen_from is None:
            return {"success": False, "message": "Nova isn't connected to Home Assistant."}
        try:
            seen = read(chosen_from)
        except HOME_FAILURES as e:
            logger.warning(f"Settings page: couldn't read what Home Assistant has to choose from: {e}")
            return {"success": False, "message": CANT_REACH_HOME}
        with self._saving:
            try:
                said = apply(chosen_from, seen)
            except ValueError as e:
                return {"success": False, "message": str(e)}
            except OSError as e:
                logger.error(f"Settings page: couldn't save a choice: {e}")
                return {"success": False, "message": "Couldn't save that."}
        logger.info(logged)
        return {"success": True, "message": said or "Saved. It's used straight away."}

    def set_light(self, entity_id: str, who: str, on: Optional[bool] = None, brightness=None,
                  hue=None, saturation=None, kelvin=None, satellite: Optional[str] = None) -> Dict:
        """
        Switches a light, or dims it, or sets its colour or its white - see
        HomeAssistant.set_light. Anything which isn't a number is turned away here, as
        a number which can't be one (JSON allows NaN and Infinity) would otherwise
        raise further in.
        """
        session, name = self._session(satellite)
        if session is None:
            return self._not_connected(name)
        if session.home is None:
            return {"success": False, "message": "Nova isn't connected to Home Assistant."}
        given = (brightness, hue, saturation, kelvin)
        numbers = [None if value is None else _number(value) for value in given]
        if (on is not None and not isinstance(on, bool)) or any(
                number is None and value is not None for number, value in zip(numbers, given)):
            return {"success": False, "message": "That didn't make sense."}
        brightness, hue, saturation, kelvin = numbers
        logger.info(f"Settings page: {who} changed {entity_id}")
        return session.home.set_light(str(entity_id), on=on, brightness=None if brightness is None else int(brightness),
                                   hue=hue, saturation=saturation, kelvin=kelvin)

    def set_battery(self, battery_id: str, who: str, enabled=None, marks=None, send_to_phone=None,
                    reset: bool = False) -> Dict:
        """
        Changes what's chosen for a battery - see BatteryChoices - or, with reset, puts
        its defaults back. It takes effect at the next check, with no restart. The
        types are checked here, as bool("false") would be True.

        Args:
            battery_id: The battery's sensor id. Home Assistant has to show it just now,
                as its kind says how many marks it may have.
            who: The login making the change.
            enabled: Whether its warnings are on, or None to leave it.
            marks: Its marks, a list of whole numbers, or None to leave them.
            send_to_phone: Whether its warnings go to the phone, or None to leave it.
            reset: Whether to forget the device's own choices instead.

        Returns:
            {"success": ..., "message": ...}
        """
        if (any(value is not None and not isinstance(value, bool) for value in (enabled, send_to_phone))
                or not isinstance(reset, bool)
                or (marks is not None and not isinstance(marks, list))):
            return {"success": False, "message": "That didn't make sense."}
        watcher = self.brain.batteries
        if watcher is None:
            return {"success": False, "message": "Nova isn't connected to Home Assistant."}
        try:
            battery = next((b for b in watcher.read() if b.id == str(battery_id)), None)
        except HOME_FAILURES as e:
            logger.warning(f"Settings page: couldn't read the batteries: {e}")
            return {"success": False, "message": "Nova can't reach Home Assistant just now."}
        if battery is None:
            return {"success": False, "message": "Home Assistant isn't showing that battery just now."}
        with self._saving:
            try:
                if reset:
                    watcher.choices.reset(battery.id)
                else:
                    watcher.choices.set(battery, enabled=enabled, marks=marks, send_to_phone=send_to_phone)
            except ValueError as e:
                return {"success": False, "message": str(e)}
            except OSError as e:
                logger.error(f"Settings page: couldn't save a battery change: {e}")
                return {"success": False, "message": "Couldn't save that."}
        logger.info(f"Settings page: {who} changed the warnings for {battery.id}")
        return {"success": True, "message": f"Saved. It's used straight away for {battery.name}."}

    def set_battery_also_send(self, on, who: str) -> Dict:
        """Sets whether a battery level Nova says aloud is also sent to the phone."""
        if not isinstance(on, bool):
            return {"success": False, "message": "That didn't make sense."}
        watcher = self.brain.batteries
        if watcher is None:
            return {"success": False, "message": "Nova isn't connected to Home Assistant."}
        with self._saving:
            try:
                watcher.choices.set_also_send_when_said(on)
            except OSError as e:
                logger.error(f"Settings page: couldn't save a battery change: {e}")
                return {"success": False, "message": "Couldn't save that."}
        logger.info(f"Settings page: {who} {'switched on' if on else 'switched off'} sending what Nova says about batteries")
        return {"success": True, "message": "Saved. It's used straight away."}

    def run(self, entity_id: str, who: str, satellite: Optional[str] = None, confirmed: bool = False) -> Dict:
        """
        Runs a script or sets a scene - see HomeAssistant.run, whose guard still decides.
        A script only with the page's yes, as one can do a lot at once; a scene is one tap.

        Args:
            entity_id: e.g. "script.movie_night" or "scene.relax".
            who: The Tailscale login, for the log.
            satellite: The satellite whose Home Assistant it is, or None for the default.
            confirmed: Whether the user said yes to running it - only True itself counts.

        Returns:
            Dict with 'success' and a 'message'.
        """
        # Only the page's own confirm() sends True, so a stray POST or an old tab's - or a
        # "true" that's merely truthy - is turned down before Home Assistant is asked.
        if str(entity_id).startswith("script.") and confirmed is not True:
            logger.warning(f"Settings page: refused running {entity_id} for {who} without a yes")
            return {"success": False, "message": "A script only runs once you've said yes. Reload the page to be asked."}
        session, name = self._session(satellite)
        if session is None:
            return self._not_connected(name)
        if session.home is None:
            return {"success": False, "message": "Nova isn't connected to Home Assistant."}
        logger.info(f"Settings page: {who} ran {entity_id}")
        return session.home.run(str(entity_id))

    def save_routine(self, routine: Dict, who: str) -> Dict:
        """Adds or changes one of the user's routines - see Routines.save."""
        if self.brain.routines is None or not isinstance(routine, dict):
            return {"success": False, "message": "That didn't make sense."}
        # Never one for a name nobody has: it would be saved, but never run. An empty
        # one is the default's, as Routines keeps it. One removed after this looks is
        # Routines' own to refuse - see its let_in.
        name = str(routine.get("satellite") or "").strip()
        names = self._satellite_names() if name else []
        if names is None:
            return {"success": False, "message": SATELLITES_UNREADABLE}
        if name and name not in names:
            return {"success": False, "message": f"There's no satellite called {name}."}
        result = self.brain.routines.save(routine)
        if result["success"]:
            logger.info(f"Settings page: {who} saved a routine")
        return {"success": result["success"], "message": result["message"]}

    def remove_routine(self, routine_id: str, who: str) -> Dict:
        if self.brain.routines is None:
            return {"success": False, "message": "That didn't make sense."}
        logger.info(f"Settings page: {who} deleted a routine")
        return self.brain.routines.remove(str(routine_id))

    def start_routine(self, routine_id: str, who: str) -> Dict:
        """
        Runs one of the user's routines now, on a thread of its own, as it waits for
        any conversation to finish first - see Brain.run_routine, which runs it on
        the satellite it names.
        """
        routines = self.brain.routines
        routine = routines.get(str(routine_id)) if routines is not None else None
        if routine is None:
            return {"success": False, "message": "That routine's gone."}
        threading.Thread(target=self.brain.run_routine, args=(routine,), daemon=True, name="routine").start()
        logger.info(f"Settings page: {who} ran a routine")
        return {"success": True, "message": f"Running {routine['name']}."}

    # The satellites. Their passwords' file keeps its own lock, which backend.py's
    # add-satellite and remove-satellite keep too (see SatellitePasswords), so none
    # of these holds _saving - which a satellite being let go would hold up.

    def _satellite_names(self) -> Optional[List[str]]:
        """
        Returns:
            The satellites, or None if their file can't be read - which is logged,
            so it's said as that, rather than as there being no such satellite.
        """
        try:
            names = self.brain.satellites()
        except (OSError, ValueError) as e:
            # The page asks for its state every twenty seconds, so it's said once,
            # until the file can be read again - and once more if it breaks again.
            if not self._satellites_unreadable:
                logger.error(f"Settings page: couldn't read the satellites' file: {e}")
            self._satellites_unreadable = True
            return None
        self._satellites_unreadable = False
        return names

    def add_satellite(self, name, who: str) -> Dict:
        """
        Lets a new satellite in, with a password made up for it.

        Args:
            name: Its Pi's machine name, as the page sent it.
            who: The Tailscale login adding it, for the log.

        Returns:
            Dict with 'success' and a 'message' - and once added, 'password', for the
            page to show once. Only this and new_password ever answer with one.
        """
        if not isinstance(name, str):
            return {"success": False, "message": "That didn't make sense."}
        name = name.strip()
        names = self._satellite_names()
        if names is None:
            return {"success": False, "message": SATELLITES_UNREADABLE}
        if name in names:
            return {"success": False, "message": f"There's already a satellite called {name}."}
        try:
            password = self.brain.add_satellite(name)
        except ValueError as e:
            return {"success": False, "message": str(e)}
        logger.info(f"Settings page: {who} added the {name} satellite")
        return {"success": True, "message": f"Added {name}. Its password is shown only this once.",
                "password": password}

    def new_password(self, name, who: str) -> Dict:
        """
        Gives a satellite a new password, letting it go until its setup has it.

        Args:
            name: The satellite, as the page sent it.
            who: The Tailscale login asking, for the log.

        Returns:
            Dict with 'success' and a 'message' - and once changed, 'password', for
            the page to show once.
        """
        if not isinstance(name, str):
            return {"success": False, "message": "That didn't make sense."}
        names = self._satellite_names()
        if names is None:
            return {"success": False, "message": SATELLITES_UNREADABLE}
        missing = {"success": False, "message": f"There's no satellite called {name}."}
        if name not in names:
            return missing
        try:
            password = self.brain.renew_password(name)
        except ValueError:
            # Removed since.
            return missing
        logger.info(f"Settings page: {who} gave the {name} satellite a new password")
        return {"success": True, "message": f"{name} has a new password. It's shown only this once.",
                "password": password}

    def remove_satellite(self, name, who: str) -> Dict:
        """
        Stops letting a satellite in, letting it go if it's connected - its timers,
        alarms and routines going to the default. See Brain.remove_satellite.

        Args:
            name: The satellite, as the page sent it.
            who: The Tailscale login removing it, for the log.

        Returns:
            Dict with 'success' and a 'message'.
        """
        if not isinstance(name, str):
            return {"success": False, "message": "That didn't make sense."}
        names = self._satellite_names()
        if names is None:
            return {"success": False, "message": SATELLITES_UNREADABLE}
        emptied = self.brain.remove_satellite(name) if name in names else None
        if emptied is None:
            return {"success": False, "message": f"There's no satellite called {name}."}
        # Added again with the same name, it shouldn't be offered its old playlists.
        self._playlists.pop(name, None)
        logger.info(f"Settings page: {who} removed the {name} satellite")
        if not emptied:
            # Said whatever the question said, which was asked from what the page
            # last heard - connected then, it may have gone since.
            return {"success": True, "message": f"Removed {name}. It wasn't connected, so it still has a copy of its "
                                                f"alarms for the next week - wipe it or connect it before switching "
                                                f"it on."}
        return {"success": True, "message": f"Removed {name}."}

    def make_default(self, name, who: str) -> Dict:
        """
        Makes a satellite the default - for ntfy's alerts, the spoken warnings and
        routines without a satellite. See Brain.make_default.

        Args:
            name: The satellite, as the page sent it.
            who: The Tailscale login asking, for the log.

        Returns:
            Dict with 'success' and a 'message'.
        """
        if not isinstance(name, str):
            return {"success": False, "message": "That didn't make sense."}
        names = self._satellite_names()
        if names is None:
            return {"success": False, "message": SATELLITES_UNREADABLE}
        missing = {"success": False, "message": f"There's no satellite called {name}."}
        if name not in names:
            return missing
        try:
            self.brain.make_default(name)
        except ValueError:
            # Removed since.
            return missing
        logger.info(f"Settings page: {who} made the {name} satellite the default")
        return {"success": True, "message": f"{name} is the default now."}

    # The page's password, for NPM

    def _try_password(self, typed: str, device: str) -> Optional[str]:
        """
        Args:
            typed: What the user typed, no longer than LONGEST_PASSWORD.
            device: The device it came from, e.g. from X-Real-IP.

        Returns:
            None if it's the password, or what to tell the user if it isn't or wasn't checked.
        """
        outcome = self.wrong_tries.attempt(device, lambda: self.password.check(typed))
        if outcome == "waiting":
            return TOO_MANY_TRIES
        if outcome == "wrong":
            # Never what was typed - it may be the password with a typo.
            logger.warning(f"Settings page: a wrong page password from {device} through NPM")
            return "That's not the password."
        return None

    def log_in(self, typed, device: str) -> Tuple[Dict, Optional[str]]:
        """
        Logs a device in through NPM, if it gives the password.

        Args:
            typed: What the user typed.
            device: The device it came from, e.g. from X-Real-IP.

        Returns:
            What to tell the user, and the new login's token if it's logged in.

        Raises:
            TypeError: If what was typed isn't text.
            OSError: If the login couldn't be saved.
        """
        if not isinstance(typed, str):
            raise TypeError("not a password")
        if not self.password.is_set():
            return {"success": False, "message": "Set a page password at the page's Tailscale address first."}, None
        if len(typed) > LONGEST_PASSWORD:
            return {"success": False, "message": "That's not the password."}, None
        with self._password_lock:
            problem = self._try_password(typed, device)
            if problem:
                return {"success": False, "message": problem}, None
            token = self.logins.make()
        logger.info("Settings page: password logged in")
        return {"success": True, "message": "Logged in."}, token

    def log_out_everywhere(self, who: str) -> Dict:
        """
        Ends every login through NPM, whichever device - for a lost phone, say.

        Raises:
            OSError: If the logins couldn't be saved.
        """
        self.logins.end_all()
        logger.info(f"Settings page: {who} logged out every device")
        return {"success": True, "message": "Every device is logged out."}

    def set_page_password(self, new, who: str, current=None, device: Optional[str] = None) -> Tuple[Dict, Optional[str]]:
        """
        Sets or replaces the page's password, which logs out every device. Through NPM
        it also needs the current one, so a login left open on a device can't lock the
        user out - and that device is logged in again, so they aren't thrown out of the
        page they changed it on.

        Args:
            new: The new password.
            who: Who's setting it, for the log.
            current: The current password, through NPM.
            device: The device through NPM, e.g. from X-Real-IP - or None from the tailnet.

        Returns:
            What to tell the user, and a new login's token for the device through NPM.

        Raises:
            TypeError: If either password isn't text.
        """
        if not isinstance(new, str) or (device is not None and not isinstance(current, str)):
            raise TypeError("not a password")
        if len(new) > LONGEST_PASSWORD:
            return {"success": False, "message": f"The page password can be at most {LONGEST_PASSWORD} characters."}, None
        with self._password_lock:
            return self._change_page_password(new, who, current, device)

    def _change_page_password(self, new: str, who: str, current, device: Optional[str]) -> Tuple[Dict, Optional[str]]:
        """set_page_password's work, with _password_lock held - see there."""
        if device is not None:
            if len(current) > LONGEST_PASSWORD:
                return {"success": False, "message": "That's not the current password."}, None
            problem = self._try_password(current, device)
            if problem:
                if problem != TOO_MANY_TRIES:
                    problem = "That's not the current password."
                return {"success": False, "message": problem}, None
        problem = self.password.set(new, self.logins)
        if problem:
            return {"success": False, "message": problem}, None
        logger.info(f"Settings page: {who} set the page password")
        if device is None:
            return {"success": True, "message": "The page password is set, and every device using it is logged out."}, None
        try:
            token = self.logins.make()
        except OSError as e:
            logger.error(f"Settings page: couldn't keep this device logged in after the new page password: {e}")
            return {"success": True, "message": "The page password is set, but this device couldn't stay logged "
                                                "in - log in again with the new one."}, None
        return {"success": True, "message": "The page password is set, and every other device is logged out."}, token


# What each kind of change is sent to, and what it's given.
ACTIONS: Dict[str, Callable[[SettingsPage, Dict, str], Dict]] = {
    "/api/volume": lambda page, body, who: page.set_volume(body["category"], body["percent"], who, body.get("satellite")),
    "/api/alarms": lambda page, body, who: page.add_alarm(body["time"], body.get("repeat"), who, body.get("satellite")),
    "/api/remove": lambda page, body, who: page.remove(body["kind"], int(body["id"]), who),
    "/api/undo": lambda page, body, who: page.undo(body["token"], who),
    "/api/forget": lambda page, body, who: page.forget(body["fact"], who),
    "/api/keys": lambda page, body, who: page.set_key(body["name"], body["value"], who),
    "/api/settings": lambda page, body, who: page.save_settings(body["changes"], who),
    "/api/restart": lambda page, body, who: page.restart(who),
    "/api/stop": lambda page, body, who: page.press_stop(who),
    "/api/mute": lambda page, body, who: page.set_muted(body["muted"], who, body.get("satellite")),
    "/api/voice/hear": lambda page, body, who: page.hear_voice(body["speed"], who, body.get("satellite")),
    "/api/music": lambda page, body, who: page.play(
        body["action"], who, body.get("query", ""), body.get("playlist", ""), body.get("satellite")),
    "/api/spotify/start": lambda page, body, who: page.start_spotify_sign_in(who),
    "/api/spotify/finish": lambda page, body, who: page.finish_spotify_sign_in(body["address"], who),
    "/api/google/start": lambda page, body, who: page.start_google_sign_in(who),
    "/api/google/finish": lambda page, body, who: page.finish_google_sign_in(body["address"], who),
    "/api/calendars": lambda page, body, who: page.choose_calendar(
        body["id"], who, body.get("whats_on"), body.get("activity")),
    "/api/calendars/new-events": lambda page, body, who: page.set_new_events(body["id"], who),
    "/api/lists/main": lambda page, body, who: page.choose_main_list(body["id"], who),
    "/api/weather": lambda page, body, who: page.choose_weather(body["id"], who),
    "/api/pcs/wake": lambda page, body, who: page.choose_wake(body["device"], body["button"], who),
    "/api/light": lambda page, body, who: page.set_light(
        body["id"], who, body.get("on"), body.get("brightness"), body.get("hue"), body.get("saturation"),
        body.get("kelvin"), body.get("satellite")),
    "/api/batteries": lambda page, body, who: page.set_battery(
        body["id"], who, body.get("enabled"), body.get("marks"), body.get("send_to_phone"), body.get("reset", False)),
    "/api/batteries/also": lambda page, body, who: page.set_battery_also_send(body["on"], who),
    "/api/run": lambda page, body, who: page.run(
        body["id"], who, body.get("satellite"), body.get("confirmed", False)),
    "/api/routines": lambda page, body, who: page.save_routine(body["routine"], who),
    "/api/routines/remove": lambda page, body, who: page.remove_routine(body["id"], who),
    "/api/routines/run": lambda page, body, who: page.start_routine(body["id"], who),
    "/api/satellites/add": lambda page, body, who: page.add_satellite(body["name"], who),
    "/api/satellites/password": lambda page, body, who: page.new_password(body["name"], who),
    "/api/satellites/remove": lambda page, body, who: page.remove_satellite(body["name"], who),
    "/api/satellites/default": lambda page, body, who: page.make_default(body["name"], who),
    "/api/logout-everywhere": lambda page, body, who: page.log_out_everywhere(who),
}


class _Handler(BaseHTTPRequestHandler):
    """One request to the page."""

    server_version = "Nova"
    sys_version = ""

    def log_message(self, format, *args):
        # Every request would otherwise go to the journal; the changes are logged anyway.
        pass

    def handle(self):
        # Through NPM, the connection's own address comes first, before any of the
        # request is read, so even nonsense gets nothing but a 403 - X-Real-IP, which
        # says which device NPM is passing on, is only to be believed from NPM itself.
        address = self.client_address[0]
        if self.server.route == "npm" and address not in self.server.page.proxy_from:
            held = self.server.page.turn_aways.note(address)
            if held is not None:
                logger.warning(f"Settings page: turned away {address}, which isn't NPM's{_held_back(held)}")
            self._turn_away()
            return
        super().handle()

    def _turn_away(self):
        """Answers a bare 403 without reading the request, and closes the connection."""
        headers = {"Server": self.server_version, "Content-Type": "text/plain; charset=utf-8",
                   "Content-Length": "0", "Connection": "close", **SAFETY_HEADERS}
        answer = "HTTP/1.1 403 Forbidden\r\n" + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        # Taken before the 403 is written, so a device that has it knows its slot is held.
        draining = self.server.turning_away.acquire(blocking=False)
        try:
            self.wfile.write((answer + "\r\n").encode())
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            # What was sent is thrown away unread, for a moment at most - closing with
            # it unread would reset the connection, which can lose the 403 before it's
            # read. With every slot taken, that's risked rather than wait.
            deadline = time.monotonic() + TURN_AWAY_WAIT
            while draining and (left := deadline - time.monotonic()) > 0:
                self.connection.settimeout(left)
                if not self.connection.recv(65536):
                    break
        except OSError:
            pass
        finally:
            if draining:
                self.server.turning_away.release()

    def do_GET(self):
        page = self.server.page
        try:
            url = urlsplit(self.path)
        except ValueError:
            # E.g. "http://[x", which can't be split - nothing anyone is let in to.
            url = None
        path = url.path if url else None
        if not self._let_in(page, path):
            return
        if url is None:
            self._send_json({"success": False, "message": "That didn't make sense."}, 400)
            return
        # Which satellite the page is looking at, if it has a choice of them.
        satellite = (parse_qs(url.query).get("satellite") or [None])[0]
        if path in FILES:
            name, content_type = FILES[path]
            with open(os.path.join(WEB_FOLDER, name), "rb") as file:
                self._send(200, file.read(), content_type)
        elif path == "/api/login-state":
            self._send_json({"route": self.server.route, "password_set": page.password.is_set(),
                             "logged_in": self.logged_in, "page": page.page_version})
        elif path == "/api/state":
            self._send_json(page.state(satellite))
        elif path == "/api/live":
            self._send_json(page.live(satellite))
        elif path == "/api/music":
            self._send_json(page.music_state(satellite))
        elif path == "/api/home":
            self._send_json(page.home_state(satellite))
        elif path == "/api/batteries":
            self._send_json(page.batteries_state())
        elif path == "/api/calendars":
            self._send_json(page.calendars_state())
        elif path == "/api/home-choices":
            self._send_json(page.home_choices_state())
        elif path == "/api/cover":
            # Whatever's playing now - the page adds its id only so a new one is fetched.
            cover = page.cover(satellite)
            if cover is None:
                self._send_json({"success": False, "message": "Nothing's playing with a cover."}, 404)
            else:
                self._send(200, *cover)
        else:
            self._send_json({"success": False, "message": "There's nothing here."}, 404)

    def do_POST(self):
        page = self.server.page
        if not self._let_in(page, self.path) or not self._from_the_page():
            return
        # The cookie a change sets, if it logs in or out.
        self.cookie: Optional[str] = None
        action = ACTIONS.get(self.path) or self._LOGIN_ACTIONS.get(self.path)
        if action is None:
            self._send_json({"success": False, "message": "That isn't something the page does."}, 400)
            return
        # A negative length would be read to the end of the connection, which never comes.
        # isdecimal(), not isdigit(): "²" is a digit to isdigit(), but int() raises on it.
        length = self.headers.get("Content-Length") or "0"
        if not length.isdecimal() or int(length) > MOST_BODY:
            self._send_json({"success": False, "message": "That didn't make sense."}, 400)
            return
        try:
            body = json.loads(self.rfile.read(int(length)) or b"{}")
            if self.path in self._LOGIN_ACTIONS:
                result = action(self, page, body)
            else:
                result = action(page, body, self.login)
        # OverflowError is int() of JSON's Infinity.
        except (KeyError, TypeError, ValueError, OverflowError):
            result = {"success": False, "message": "That didn't make sense."}
        except OSError as e:
            logger.error(f"Settings page: couldn't save a change: {e}")
            result = {"success": False, "message": "Couldn't save that."}
            self.cookie = None
        self._send_json(result, cookie=self.cookie)

    def _let_in(self, page: SettingsPage, path: Optional[str]) -> bool:
        """
        Whether this is someone allowed in - on the tailnet as Tailscale says, having
        passed the request on, and through NPM with a login, or for what's open
        before one.

        Args:
            page: The page.
            path: What's asked for, without its query - None if it can't be told.
        """
        if self.server.route == "npm":
            return self._let_in_through_npm(page, path)
        self.logged_in = True
        self.login = self.headers.get("Tailscale-User-Login", "").strip().lower()
        if self.login and self.login in page.allow:
            return True
        logger.warning(f"Settings page: turned away {self.login or 'someone not on the tailnet'}")
        self._send(403, b"This page is only for the Tailscale logins in settings_page.allow.",
                   "text/plain; charset=utf-8")
        return False

    def _let_in_through_npm(self, page: SettingsPage, path: Optional[str]) -> bool:
        """
        Whether this is someone allowed in through NPM. Tailscale's header is never
        read here: anyone on the network could send it straight to this port.

        Args:
            page: The page.
            path: What's asked for, without its query.
        """
        # Only believed as every request here is from NPM itself - see handle.
        # Cut short as it's kept in the wrong-tries count and written to the log, where
        # a 10,000-character header would otherwise be 10,000 characters of both.
        self.device = (self.headers.get("X-Real-IP") or "").strip()[:MOST_DEVICE_KEY] or self.client_address[0]
        self.token = self._login_token()
        self.logged_in = page.logins.check(self.token)
        self.login = "password" if self.logged_in else f"someone at {self.device} not logged in"
        if self.logged_in or path in OPEN_THROUGH_NPM.get(self.command, ()):
            return True
        self._send_json({"success": False, "message": "Log in first."}, 401)
        return False

    def _login_token(self) -> Optional[str]:
        """
        Returns:
            The login's token from the request's cookie, or None if it has none, or
            one too long to be one.
        """
        # Split by hand rather than with http.cookies, which gives up on the whole
        # header at the first cookie it can't read - one another service behind NPM
        # set on the whole domain, say, which would log the user out every time.
        for cookie in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = cookie.partition("=")
            if name.strip() == LOGIN_COOKIE:
                value = value.strip()
                return value if 0 < len(value) <= LONGEST_COOKIE else None
        return None

    @staticmethod
    def _login_cookie(token: str) -> str:
        """The cookie for a login - or, for "", the one which ends it in the browser."""
        age = LOGIN_DAYS * 86400 if token else 0
        return f"{LOGIN_COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={age}"

    # The changes which log in or out, which need more of the request than ACTIONS are given.

    def _log_in(self, page: SettingsPage, body: Dict) -> Dict:
        if self.server.route != "npm":
            return {"success": False, "message": "There's no logging in here - Tailscale says who you are."}
        result, token = page.log_in(body["password"], self.device)
        if token:
            self.cookie = self._login_cookie(token)
        return result

    def _log_out(self, page: SettingsPage, body: Dict) -> Dict:
        if self.server.route != "npm":
            return {"success": True, "message": "There's nothing to log out of here - Tailscale lets you in."}
        if self.logged_in:
            page.logins.end(self.token)
        self.cookie = self._login_cookie("")
        return {"success": True, "message": "Logged out."}

    def _set_page_password(self, page: SettingsPage, body: Dict) -> Dict:
        through_npm = self.server.route == "npm"
        result, token = page.set_page_password(body["password"], self.login, body.get("current"),
                                               self.device if through_npm else None)
        if token:
            self.cookie = self._login_cookie(token)
        return result

    _LOGIN_ACTIONS = {"/api/login": _log_in, "/api/logout": _log_out, "/api/page-password": _set_page_password}

    def _from_the_page(self) -> bool:
        """
        Whether a change comes from the page itself: only it sends X-Nova, which no
        other site can without the page agreeing to it, and a browser says when a
        request comes from another site.
        """
        if self.headers.get("X-Nova") == "page" and self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin":
            return True
        # Through NPM, any device can cause this without the password, so each is held to
        # a line a minute; on the tailnet only the user can, so every one is logged, as before.
        held = self.server.page.refusals.note(self.login) if self.server.route == "npm" else 0
        if held is not None:
            logger.warning(f"Settings page: refused a change for {self.login} which didn't come from the page"
                           f"{_held_back(held)}")
        self._send(403, b"Changes can only be made from the page itself.", "text/plain; charset=utf-8")
        return False

    def _send_json(self, data: Dict, status: int = 200, cookie: Optional[str] = None):
        self._send(status, json.dumps(data).encode(), "application/json", cookie)

    def _send(self, status: int, body: bytes, content_type: str, cookie: Optional[str] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for name, value in SAFETY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
