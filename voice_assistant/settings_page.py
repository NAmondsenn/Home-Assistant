"""
Settings Page Module.

A page for looking after Nova from a phone or a laptop: how it's doing, its microphone's
mute, what's playing, the lights and Home Assistant's scripts and scenes, its volumes,
its timers and alarms, what it remembers, its keys and its main settings. Nova serves
it itself, so a Pi on its own has it too.

Only the user gets in. It listens on this machine alone, and Tailscale passes the
tailnet's requests on to it over HTTPS, with its own certificate -

    tailscale serve --bg --https=443 http://127.0.0.1:8765

- saying whose they are: only the logins in settings_page.allow are let in. A change
also has to come from the page itself, with a header only it sends, so that another
website the user happens to visit can't make one in their name.

Keys are only ever written, never shown back - the page just says whether each is
set. Settings are saved to this install's own ~/.config/nova/config.yaml, the one
before kept beside it as config.yaml.bak, and both take effect once Nova restarts,
which the page can do.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from voice_assistant import paths, storage, timeline
from voice_assistant.config import CHOICES, Config, ConfigError, check

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

# The settings on the page - the ones most likely to change - and what each is for.
EDITABLE = [
    ("weather", "location", "Your town or city, for the weather"),
    ("music", "player", "What plays the music"),
    ("navidrome", "url", "Navidrome's address"),
    ("navidrome", "username", "Navidrome's user for Nova"),
    ("home_assistant", "url", "Home Assistant's address"),
    ("backend", "address", "Nova's backend, as name:port"),
    ("ntfy", "url", "Your ntfy server"),
    ("ntfy", "alerts", "Topics read out as they arrive, separated by commas"),
    ("ntfy", "phone", "Topic for sending things to your phone"),
    ("ntfy", "reminders", "Topic for reminders nobody answered"),
    ("ntfy", "restarts", "Topic for Nova restarting after a problem"),
    ("ntfy", "quiet_from", "Night starts at, as 23:00"),
    ("ntfy", "quiet_until", "Night ends at, as 07:00"),
    ("thresholds", "silence_duration", "Seconds of quiet which end what you're saying"),
    ("logging", "log_transcripts", "Keep what's said in the logs"),
]

# The keys, and what each is for. Claude's can be under an older name too.
KEYS = [("ANTHROPIC_API_KEY", "Claude"), ("HA_TOKEN", "Home Assistant"), ("MQTT_PASSWORD", "Home Assistant's MQTT"),
        ("NAVIDROME_PASSWORD", "Navidrome"), ("NTFY_TOKEN", "ntfy"), ("NOVA_BACKEND_TOKEN", "Nova's backend"),
        ("SPOTIFY_CLIENT_ID", "Spotify client ID"), ("SPOTIFY_CLIENT_SECRET", "Spotify client secret")]
OLDER_NAMES = {"ANTHROPIC_API_KEY": ["Claude_API_Key"]}

# The most a request may send, in bytes.
MOST_BODY = 64 * 1024

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
        number = float(given)
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


def restart_service() -> str:
    """
    Restarts Nova through systemd, when that's what's running it - see
    voice-assistant.service.

    Returns:
        What to tell the user.
    """
    if not os.getenv("INVOCATION_ID"):
        return "Nova isn't running as a service here, so restart it yourself."
    subprocess.Popen(["systemctl", "--user", "restart", "voice-assistant"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "Restarting - give it half a minute."


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


class SettingsPage:
    """What the page shows and changes, and the server it's served from."""

    def __init__(self, allow: List[str], port: int = 8765, running=None, volume=None, clock=None, memory=None,
                 ntfy=None, stt=None, music=None, actions=None, home=None, routines=None, wake=None,
                 run_routine: Optional[Callable[[Dict], None]] = None,
                 boop: Optional[Callable[[float], None]] = None,
                 activity: Optional[Callable[[], str]] = None, wake_word: Optional[Callable[[], str]] = None,
                 restart: Callable[[], str] = restart_service, started: Optional[float] = None):
        """
        Args:
            allow: The Tailscale logins let in.
            port: Where to listen, on this machine alone - 0 for any free one.
            running: The Config Nova started with, to tell when a restart is needed.
            volume: The VolumeControl.
            clock: The Clock, for the timers and alarms.
            memory: The Memory, for what Nova remembers.
            ntfy: The Ntfy, or None, for whether it's listening.
            stt: The speech-to-text, for whether the backend is answering.
            music: The music player, or None - it keeps the music's volume itself,
                and says what's playing.
            actions: The ActionExecutor, which the music buttons go through, as
                asking out loud does.
            home: The HomeAssistant, or None, for the lights and the scripts and scenes.
            routines: The user's Routines, or None.
            wake: The WakeWordDetector, which keeps whether the microphone's muted.
            run_routine: Runs a routine by itself, as at its time - SmartAssistant.run_routine.
            boop: Plays the volume beep at a level from 0 to 1 on Nova's speaker, so a
                change made on the page is heard at its new level.
            activity: What Nova is doing - "idle", "listening", "thinking" or "speaking".
            wake_word: What Nova's listening for - openWakeWord's stand-in, if Nova's
                own wake word model is missing.
            restart: Restarts Nova, returning what to tell the user.
            started: When Nova started, in seconds.
        """
        self.allow = [login.strip().lower() for login in allow if login.strip()]
        self.port = port
        self.running = running
        self.volume, self.clock, self.memory, self.ntfy, self.stt = volume, clock, memory, ntfy, stt
        self.music, self.actions, self.home = music, actions, home
        self.routines, self._run_routine = routines, run_routine
        self.wake = wake
        self._playlists: Optional[Tuple[List[str], float]] = None
        self._boop = boop
        self._activity = activity
        self._listening_for = wake_word
        self._restart = restart
        self.started = started or time.time()
        with open(paths.DEFAULTS_FILE) as file:
            self.defaults = yaml.safe_load(file)
        self._keys_changed = False
        self._server: Optional[ThreadingHTTPServer] = None

    @classmethod
    def from_config(cls, config, **parts) -> Optional["SettingsPage"]:
        """
        The page settings_page in config.yaml describes, or None if it's switched off.

        Args:
            config: Configs loaded from config.yaml.
            parts: Everything else the page needs - see __init__.
        """
        settings = config.section("settings_page")
        port = int(settings.get("port") or 0)
        if not port:
            return None
        allow = settings.get("allow") or []
        if not allow:
            logger.warning("The settings page lets nobody in until settings_page.allow names your Tailscale login")
        return cls(allow, port, running=config, **parts)

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

    def state(self) -> Dict:
        """Everything on the page - never a key's value, only whether it's set."""
        saved, problem = self._saved()
        settings = []
        for section, name, label in EDITABLE:
            default = self.defaults[section][name]
            value = saved.section(section).get(name) if saved else default
            setting = {"key": f"{section}.{name}", "label": label, "kind": _kind(default), "value": value}
            # One of a few words, which the page offers as a list to pick from.
            if (section, name) in CHOICES:
                setting["choices"] = list(CHOICES[(section, name)])
            settings.append(setting)
        changed = saved is not None and self.running is not None and any(
            saved.section(section).get(name) != self.running.section(section).get(name)
            for section, name, _ in EDITABLE)
        secrets = _secrets_set(paths.SECRETS_FILE)
        keys = [{"name": name, "label": label,
                 "set": any(secrets.get(spelling) for spelling in [name] + OLDER_NAMES.get(name, []))}
                for name, label in KEYS]
        return {"status": self._status(),
                "wake_word": self._wake_word(),
                "muted": self.wake is not None and self.wake.muted,
                "volumes": volume_levels(self.volume, self.music),
                "clock": self.clock.overview(),
                "memories": self.memory.facts(),
                "routines": self.routines.all() if self.routines is not None else [],
                "keys": keys,
                "settings": settings,
                "settings_problem": problem,
                "restart_needed": changed or self._keys_changed}

    def live(self) -> Dict:
        """What Nova is doing right now - asked for every second or two while the page is open."""
        return {"activity": self._activity() if self._activity else "idle"}

    def _wake_word(self) -> str:
        """What wakes Nova, for the page to say it's waiting for it."""
        return self._listening_for() if self._listening_for else self.defaults["assistant"]["wake_word"]

    def music_state(self) -> Dict:
        """
        What's playing, and the playlists to pick from - asked for every few seconds
        while the page is open, apart from everything else, so a slow music player
        doesn't hold the rest up.
        """
        if self.music is None:
            return {"available": False}
        return {"available": True, "player": getattr(self.music, "name", "Music"), "now": self.music.now_playing(),
                "playlists": self._user_playlists()}

    def _user_playlists(self) -> List[str]:
        if self._playlists is None or time.monotonic() - self._playlists[1] > PLAYLISTS_KEPT:
            self._playlists = (self.music.playlists(), time.monotonic())
        return self._playlists[0]

    def cover(self) -> Optional[Tuple[bytes, str]]:
        """The cover of what's playing, as an image and its type, or None."""
        return self.music.cover() if self.music is not None else None

    def home_state(self) -> Dict:
        """
        The lights, and the scripts and scenes, from Home Assistant - asked for apart
        from everything else, like the music.
        """
        if self.home is None:
            return {"available": False}
        return {"available": True, **self.home.overview()}

    def _saved(self):
        """The settings as saved now - which take effect at the next restart - and any problem reading them."""
        try:
            return Config(quiet=True), None
        except (ConfigError, OSError, ValueError) as e:
            return None, str(e)

    def _status(self) -> Dict:
        """How Nova's doing."""
        reply = None
        try:
            rows = timeline.summarise(timeline.load(os.path.join(paths.LOGS_DIR, "latency.jsonl"))[-REPLIES_COUNTED:])
            reply = next((row for row in rows if row["stage"] == "End of speech to reply ready"), None)
        except OSError:
            pass
        backend = None
        if hasattr(self.stt, "reachable"):
            backend = "answering" if self.stt.reachable else "not answering"
        ntfy = None if self.ntfy is None else ("listening" if self.ntfy.listening else "not connected")
        return {"started": self.started, "reply_ms": reply["p50"] if reply else None,
                "replies": reply["count"] if reply else 0, "backend": backend, "ntfy": ntfy,
                "problems": self._problems()}

    @staticmethod
    def _problems() -> List[Dict]:
        """The latest warnings and errors in the log, newest first."""
        try:
            with open(os.path.join(paths.LOGS_DIR, "smart_assistant.log"), "rb") as file:
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

    def set_volume(self, category: str, percent, who: str) -> Dict:
        """
        Changes a volume, and plays the beep at its new level on Nova's speaker - as
        the slider is dragged, so the user hears how loud it now is.
        """
        percent = int(percent)
        result = change_volume(self.volume, self.music, category, percent)
        if result.get("success") and self._boop:
            self._boop(percent / 100 if category == "music" and self.music is not None else self.volume.get(category))
        logger.info(f"Settings page: {who} set the {category} volume to {percent}%")
        return result

    def set_muted(self, muted: bool, who: str) -> Dict:
        """Mutes or unmutes the microphone - see WakeWordDetector.set_muted."""
        if self.wake is None or not isinstance(muted, bool):
            return {"success": False, "message": "That didn't make sense."}
        self.wake.set_muted(muted)
        logger.info(f"Settings page: {who} {'muted' if muted else 'unmuted'} the microphone")
        if muted:
            return {"success": True, "message": f"Muted. Nova won't hear \"{self._wake_word()}\" until it's unmuted."}
        return {"success": True, "message": "Unmuted."}

    def add_alarm(self, at: str, repeat: Optional[str], who: str) -> Dict:
        found = re.fullmatch(r"(\d{1,2}):(\d\d)", (at or "").strip())
        if not found:
            return {"success": False, "message": "That isn't a time."}
        logger.info(f"Settings page: {who} set an alarm for {at}")
        return self.clock.set_alarm(int(found[1]), int(found[2]), repeat or None)

    def remove(self, kind: str, item_id: int, who: str) -> Dict:
        logger.info(f"Settings page: {who} removed {kind} {item_id}")
        return self.clock.remove(kind, item_id)

    def forget(self, fact: str, who: str) -> Dict:
        logger.info(f"Settings page: {who} had something forgotten")
        return self.memory.forget(fact)

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
        try:
            with open(path) as file:
                lines = file.read().splitlines()
        except FileNotFoundError:
            lines = []
        quoted = value.replace("\\", "\\\\").replace('"', '\\"')
        kept = [line for line in lines if not re.match(rf"\s*(?:export\s+)?{re.escape(name)}\s*=", line)]
        storage.save_text(path, "\n".join(kept + [f'{name}="{quoted}"']) + "\n")
        self._keys_changed = True
        logger.info(f"Settings page: {who} set {name}")
        return {"success": True, "message": "Saved. It's used once Nova restarts."}

    def save_settings(self, changes: Dict[str, Any], who: str) -> Dict:
        """
        Saves settings to this install's own settings file, keeping it to what's
        different from config.yaml's defaults - checked the same way as when Nova
        starts, so a change it would refuse is never saved.
        """
        editable = {f"{section}.{name}": (section, name) for section, name, _ in EDITABLE}
        unknown = [key for key in changes if key not in editable]
        if unknown:
            return {"success": False, "message": f"The page can't change {', '.join(unknown)}."}
        path = paths.CONFIG_FILE
        try:
            with open(path) as file:
                settings = yaml.safe_load(file) or {}
        except FileNotFoundError:
            settings = {}
        except (OSError, yaml.YAMLError) as e:
            return {"success": False, "message": f"Couldn't read {path}: {e}"}

        for key, given in changes.items():
            section, name = editable[key]
            default = self.defaults[section][name]
            try:
                value = _from_page(given, default)
            except (TypeError, ValueError):
                return {"success": False, "message": f"{key} should be {_kind(default)}."}
            if value == default:
                settings.get(section, {}).pop(name, None)
            else:
                settings.setdefault(section, {})[name] = value
        settings = {section: values for section, values in settings.items() if values}
        try:
            check(settings, self.defaults, path)
        except ConfigError as e:
            return {"success": False, "message": str(e)}

        if os.path.exists(path):
            shutil.copyfile(path, path + ".bak")
            os.chmod(path + ".bak", 0o600)
        storage.save_text(path, yaml.safe_dump(settings, sort_keys=False, allow_unicode=True))
        logger.info(f"Settings page: {who} changed {', '.join(changes)}")
        return {"success": True, "message": "Saved. It takes effect once Nova restarts."}

    def restart(self, who: str) -> Dict:
        logger.info(f"Settings page: {who} restarted Nova")
        return {"success": True, "message": self._restart()}

    def play(self, action: str, who: str, query: str = "", playlist: str = "") -> Dict:
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
        """
        if self.music is None or self.actions is None:
            return {"success": False, "message": "Nova has no music player set up."}
        if action == "play":
            result = self.actions.play_music(str(query).strip() or None)
        elif action == "playlist":
            result = self.actions.play_playlist(str(playlist))
        elif action in PLAYBACK:
            result = self.actions.control_playback(action)
        else:
            return {"success": False, "message": "That isn't something the music does."}
        logger.info(f"Settings page: {who} asked the music to {action}")
        return {"success": bool(result.get("success")), "message": result.get("message") or ""}

    def set_light(self, entity_id: str, who: str, on: Optional[bool] = None, brightness=None) -> Dict:
        """Switches or dims a light - see HomeAssistant.set_light."""
        if self.home is None:
            return {"success": False, "message": "Nova isn't connected to Home Assistant."}
        if on is not None and not isinstance(on, bool):
            return {"success": False, "message": "That didn't make sense."}
        logger.info(f"Settings page: {who} changed {entity_id}")
        return self.home.set_light(str(entity_id), on=on, brightness=None if brightness is None else int(brightness))

    def run(self, entity_id: str, who: str) -> Dict:
        """Runs a script or sets a scene - see HomeAssistant.run."""
        if self.home is None:
            return {"success": False, "message": "Nova isn't connected to Home Assistant."}
        logger.info(f"Settings page: {who} ran {entity_id}")
        return self.home.run(str(entity_id))

    def save_routine(self, routine: Dict, who: str) -> Dict:
        """Adds or changes one of the user's routines - see Routines.save."""
        if self.routines is None or not isinstance(routine, dict):
            return {"success": False, "message": "That didn't make sense."}
        result = self.routines.save(routine)
        if result["success"]:
            logger.info(f"Settings page: {who} saved a routine")
        return {"success": result["success"], "message": result["message"]}

    def remove_routine(self, routine_id: str, who: str) -> Dict:
        if self.routines is None:
            return {"success": False, "message": "That didn't make sense."}
        logger.info(f"Settings page: {who} deleted a routine")
        return self.routines.remove(str(routine_id))

    def start_routine(self, routine_id: str, who: str) -> Dict:
        """
        Runs one of the user's routines now, on a thread of its own, as it waits for
        any conversation to finish first - see SmartAssistant.run_routine.
        """
        routine = self.routines.get(str(routine_id)) if self.routines is not None else None
        if routine is None or self._run_routine is None:
            return {"success": False, "message": "That routine's gone."}
        threading.Thread(target=self._run_routine, args=(routine,), daemon=True, name="routine").start()
        logger.info(f"Settings page: {who} ran a routine")
        return {"success": True, "message": f"Running {routine['name']}."}


# What each kind of change is sent to, and what it's given.
ACTIONS: Dict[str, Callable[[SettingsPage, Dict, str], Dict]] = {
    "/api/volume": lambda page, body, who: page.set_volume(body["category"], body["percent"], who),
    "/api/alarms": lambda page, body, who: page.add_alarm(body["time"], body.get("repeat"), who),
    "/api/remove": lambda page, body, who: page.remove(body["kind"], int(body["id"]), who),
    "/api/forget": lambda page, body, who: page.forget(body["fact"], who),
    "/api/keys": lambda page, body, who: page.set_key(body["name"], body["value"], who),
    "/api/settings": lambda page, body, who: page.save_settings(body["changes"], who),
    "/api/restart": lambda page, body, who: page.restart(who),
    "/api/mute": lambda page, body, who: page.set_muted(body["muted"], who),
    "/api/music": lambda page, body, who: page.play(body["action"], who, body.get("query", ""), body.get("playlist", "")),
    "/api/light": lambda page, body, who: page.set_light(body["id"], who, body.get("on"), body.get("brightness")),
    "/api/run": lambda page, body, who: page.run(body["id"], who),
    "/api/routines": lambda page, body, who: page.save_routine(body["routine"], who),
    "/api/routines/remove": lambda page, body, who: page.remove_routine(body["id"], who),
    "/api/routines/run": lambda page, body, who: page.start_routine(body["id"], who),
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
        path = self.path.split("?", 1)[0]
        if path in FILES:
            name, content_type = FILES[path]
            with open(os.path.join(WEB_FOLDER, name), "rb") as file:
                self._send(200, file.read(), content_type)
        elif path == "/api/state":
            self._send_json(page.state())
        elif path == "/api/live":
            self._send_json(page.live())
        elif path == "/api/music":
            self._send_json(page.music_state())
        elif path == "/api/home":
            self._send_json(page.home_state())
        elif path == "/api/cover":
            # Whatever's playing now - the page adds its id only so a new one is fetched.
            cover = page.cover()
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
        length = int(self.headers.get("Content-Length") or 0)
        if action is None or length > MOST_BODY:
            self._send_json({"success": False, "message": "That isn't something the page does."}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            result = action(page, body, self.login)
        except (KeyError, TypeError, ValueError):
            result = {"success": False, "message": "That didn't make sense."}
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
