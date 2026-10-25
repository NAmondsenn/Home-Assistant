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

Keys are only ever written, never shown back - the page just says whether each is
set, and a new one is used once Nova restarts, which the page can do. Settings are
saved to this install's own ~/.config/nova/config.yaml, the one before kept beside it
as config.yaml.bak, and most are used straight away, from the next moment they're
needed, on every satellite - see Brain.apply_settings. Those which make a connection or
a player, such as a server's address, wait for a restart, as one swapped half way is
where things go wrong, and the page says which each setting is.
"""

import json
import logging
import math
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import parse_qs, urlsplit

import yaml

from voice_assistant import paths, storage, timeline
from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.clock import format_duration
from voice_assistant.config import CHOICES, LIMITS, Config, ConfigError, check, clashes, limit_problem
from voice_assistant.ntfy import RETOPIC
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

# The most a request may send, in bytes.
MOST_BODY = 64 * 1024

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


class SettingsPage:
    """What the page shows and changes, and the server it's served from."""

    def __init__(self, allow: List[str], port: int = 8765, running=None, brain=None,
                 wake_word: Optional[Callable[[], str]] = None, restart: Optional[Callable[[], str]] = None,
                 started: Optional[float] = None):
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
        """
        self.allow = [login.strip().lower() for login in allow if login.strip()]
        self.port = port
        self.running = running
        self.brain = brain
        # Each satellite's playlists, with when they were asked for.
        self._playlists: Dict[str, Tuple[List[str], float]] = {}
        self._listening_for = wake_word
        self._restart = restart
        self.started = started or time.time()
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
        # What was just removed, for a while, so the toast's Undo can put it back.
        self._undo = UndoShelf()
        self._server: Optional[ThreadingHTTPServer] = None

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
        return cls(allow, port, running=config, brain=brain, **parts)

    # Serving it

    def start(self):
        """Starts serving the page, on a thread of its own."""
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        except OSError as e:
            logger.error(f"Couldn't start the settings page on port {self.port}: {e}")
            return
        self._server.daemon_threads = True
        self._server.page = self
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": STOP_CHECK},
                         name="settings-page", daemon=True).start()
        logger.info(f"The settings page is on port {self.port}")

    def stop(self):
        """Stops serving it."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

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
        connected - with what it's doing and its mute if so. Never its password.

        Args:
            names: The satellites, as the brain has them - the first is the default.
        """
        found = []
        default = names[0] if names else None
        for name in names:
            session = self.brain.session_for(name)
            found.append({"name": name, "connected": session is not None,
                          "activity": session.activity if session is not None else "idle",
                          "muted": session is not None and session.muted, "default": name == default})
        return found

    def state(self, satellite: Optional[str] = None) -> Dict:
        """
        Everything on the page - never a key's value, only whether it's set.

        Args:
            satellite: The satellite whose volumes, mute and timers are shown, or None
                for the default. Those are empty while it isn't connected.
        """
        session, chosen = self._session(satellite)
        names = self._satellite_names()
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
                "restart_needed": self._restart_needed(saved)}

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
        if music is None:
            return {"available": False}
        return {"available": True, "player": getattr(music, "name", "Music"), "now": music.now_playing(),
                "playlists": self._user_playlists(name, music)}

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
            return {"available": False, "message": "Nova isn't connected to Home Assistant. "
                    "Add its address in Settings, and its token in Keys."}
        try:
            found = watcher.read()
        except HOME_FAILURES as e:
            logger.warning(f"Settings page: couldn't read the batteries: {e}")
            return {"available": False, "message": "Nova can't reach Home Assistant just now."}
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
            return {"success": False, "message": "Nova has no music player set up."}
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
            return self.brain.satellites()
        except (OSError, ValueError) as e:
            logger.error(f"Settings page: couldn't read the satellites' file: {e}")
            return None

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
        if name not in names or not self.brain.remove_satellite(name):
            return {"success": False, "message": f"There's no satellite called {name}."}
        logger.info(f"Settings page: {who} removed the {name} satellite")
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
}


class _Handler(BaseHTTPRequestHandler):
    """One request to the page."""

    server_version = "Nova"
    sys_version = ""

    def log_message(self, format, *args):
        # Every request would otherwise go to the journal; the changes are logged anyway.
        pass

    def do_GET(self):
        page = self.server.page
        if not self._let_in(page):
            return
        url = urlsplit(self.path)
        path = url.path
        # Which satellite the page is looking at, if it has a choice of them.
        satellite = (parse_qs(url.query).get("satellite") or [None])[0]
        if path in FILES:
            name, content_type = FILES[path]
            with open(os.path.join(WEB_FOLDER, name), "rb") as file:
                self._send(200, file.read(), content_type)
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
        if not self._let_in(page) or not self._from_the_page():
            return
        action = ACTIONS.get(self.path)
        if action is None:
            self._send_json({"success": False, "message": "That isn't something the page does."}, 400)
            return
        # A negative length would be read to the end of the connection, which never comes.
        length = self.headers.get("Content-Length") or "0"
        if not length.isdigit() or int(length) > MOST_BODY:
            self._send_json({"success": False, "message": "That didn't make sense."}, 400)
            return
        try:
            body = json.loads(self.rfile.read(int(length)) or b"{}")
            result = action(page, body, self.login)
        # OverflowError is int() of JSON's Infinity.
        except (KeyError, TypeError, ValueError, OverflowError):
            result = {"success": False, "message": "That didn't make sense."}
        except OSError as e:
            logger.error(f"Settings page: couldn't save a change: {e}")
            result = {"success": False, "message": "Couldn't save that."}
        self._send_json(result)

    def _let_in(self, page: SettingsPage) -> bool:
        """
        Whether this is someone allowed in - as Tailscale says, having passed the
        request on.
        """
        self.login = self.headers.get("Tailscale-User-Login", "").strip().lower()
        if self.login and self.login in page.allow:
            return True
        logger.warning(f"Settings page: turned away {self.login or 'someone not on the tailnet'}")
        self._send(403, b"This page is only for the Tailscale logins in settings_page.allow.",
                   "text/plain; charset=utf-8")
        return False

    def _from_the_page(self) -> bool:
        """
        Whether a change comes from the page itself: only it sends X-Nova, which no
        other site can without the page agreeing to it, and a browser says when a
        request comes from another site.
        """
        if self.headers.get("X-Nova") == "page" and self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin":
            return True
        logger.warning(f"Settings page: refused a change for {self.login} which didn't come from the page")
        self._send(403, b"Changes can only be made from the page itself.", "text/plain; charset=utf-8")
        return False

    def _send_json(self, data: Dict, status: int = 200):
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SAFETY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
