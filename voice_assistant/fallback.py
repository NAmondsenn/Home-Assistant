"""
Fallback Module.

What the satellite does while it can't reach its brain on PVE2. The brain owns the
timers and alarms and each satellite's volumes and mute; the satellite keeps a copy
of each only so that an alarm set for 7am still rings at 7am if the server is down,
at the volume the user last chose. It also keeps a recorded "I can't reach the
server", since without the brain there is no voice to say it.

The copies are saved, so a satellite which restarts while the server is still down
carries on where it was rather than forgetting what it has already rung.
"""

import logging
import os
import threading
import time
import wave
from typing import Dict, List, Optional

from voice_assistant import paths
from voice_assistant.speaker import Playback, Speaker
from voice_assistant.storage import load_json, save_json

# Logger setup
logger = logging.getLogger(__name__)

# How late an alarm may be and still ring. A satellite which was switched off all
# night shouldn't start ringing yesterday's 7am alarm at noon.
GRACE = 600

# How long a fallback alarm rings if nobody dismisses it, as the brain's does.
RING_LIMIT = 600


class AlarmCopy:
    """The timers and alarms the brain last sent, and what rang from them since."""

    def __init__(self, path: str = os.path.join(paths.DATA_DIR, "alarm_copy.json")):
        """
        Args:
            path: Where the copy is saved.
        """
        self.path = path
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict] = {}
        self._rung: List[str] = []
        self._report: Dict[str, List[str]] = {"rang": [], "dismissed": []}
        self._load()

    def _load(self):
        try:
            data = load_json(self.path, {})
            entries = {entry["id"]: entry for entry in data.get("entries", [])}
            rung = [str(id) for id in data.get("rung", [])]
            report = data.get("report", {})
            self._report = {
                "rang": [str(id) for id in report.get("rang", [])],
                "dismissed": [str(id) for id in report.get("dismissed", [])],
            }
            self._entries, self._rung = entries, rung
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            logger.error(f"Could not read the alarm copy, starting empty: {e}")

    def _save(self):
        try:
            save_json(self.path, {
                "entries": list(self._entries.values()),
                "rung": self._rung,
                "report": self._report,
            })
        except (OSError, TypeError) as e:
            # Best-effort - the copy in memory still rings this run.
            logger.error(f"Could not save the alarm copy: {e}")

    def replace(self, entries: List[Dict]):
        """
        Swaps in the brain's latest copy.

        Args:
            entries: Dicts of id, due (epoch seconds), kind and sound.
        """
        with self._lock:
            self._entries = {entry["id"]: dict(entry) for entry in entries}
            # An occurrence's id is unchanged once it rings, so what rang stays
            # rung if the brain still lists it, and is forgotten once it doesn't.
            self._rung = [id for id in self._rung if id in self._entries]
            self._save()

    def due(self, now: float) -> List[Dict]:
        """
        Args:
            now: The time, in epoch seconds.

        Returns:
            The entries due by now which haven't rung and aren't over GRACE late.
        """
        with self._lock:
            return [dict(entry) for entry in self._entries.values()
                    if entry["id"] not in self._rung and entry["due"] <= now <= entry["due"] + GRACE]

    def rang(self, id: str):
        """Records that an entry has started ringing, so it isn't rung again."""
        with self._lock:
            if id not in self._rung:
                self._rung.append(id)
            if id not in self._report["rang"]:
                self._report["rang"].append(id)
            self._save()

    def dismissed(self, id: str):
        """Records that the user dismissed an entry which was ringing."""
        with self._lock:
            if id not in self._report["dismissed"]:
                self._report["dismissed"].append(id)
            self._save()

    def away_report(self) -> Dict[str, List[str]]:
        """
        Returns:
            The ids which rang while the brain was away and the ids dismissed, for
            the brain to settle on reconnecting.
        """
        with self._lock:
            return {"rang": list(self._report["rang"]), "dismissed": list(self._report["dismissed"])}

    def clear_report(self):
        """Forgets the report, once the brain has been told it."""
        with self._lock:
            self._report = {"rang": [], "dismissed": []}
            self._save()


class StateCopy:
    """The volumes and mute the brain last sent for this satellite."""

    def __init__(self, path: str = os.path.join(paths.DATA_DIR, "satellite_state.json")):
        """
        Args:
            path: Where the copy is saved.
        """
        self.path = path
        self._lock = threading.Lock()
        self._state: Dict = {"volumes": {}, "muted": False}
        try:
            loaded = load_json(path, {})
            self._state = {"volumes": dict(loaded.get("volumes", {})), "muted": bool(loaded.get("muted", False))}
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.error(f"Could not read the satellite's saved state, using defaults: {e}")

    def update(self, state: Dict):
        """
        Args:
            state: {"volumes": {category: 0-1}, "muted": bool}, as the brain sends it.
        """
        with self._lock:
            self._state = {"volumes": dict(state.get("volumes", {})), "muted": bool(state.get("muted", False))}
            try:
                save_json(self.path, self._state)
            except (OSError, TypeError) as e:
                logger.error(f"Could not save the satellite's state: {e}")

    def volume(self, category: str) -> float:
        """
        Args:
            category: "general", "alarm" or "music".

        Returns:
            The volume from 0 to 1, or full volume if the brain never sent one - an
            alarm which can't be heard is worse than one which is too loud.
        """
        with self._lock:
            return float(self._state["volumes"].get(category, 1.0))

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._state["muted"]


class FallbackRinger:
    """Rings the copy's alarms and timers when they fall due."""

    def __init__(self, speaker: Speaker, sounds_folder: str, copy: AlarmCopy, state: StateCopy):
        """
        Args:
            speaker: Plays the sounds.
            sounds_folder: Where an entry's sound file is.
            copy: The entries to ring.
            state: Gives the alarm volume.
        """
        self.speaker = speaker
        self.sounds_folder = sounds_folder
        self.copy = copy
        self.state = state
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._playbacks: Dict[str, Optional[Playback]] = {}
        self._lock = threading.Lock()

    @property
    def ringing(self) -> bool:
        """Whether anything is still ringing."""
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            return bool(self._threads)

    def check(self, now: float):
        """
        Starts ringing whatever has fallen due. Called about once a second.

        Args:
            now: The time, in epoch seconds.
        """
        for entry in self.copy.due(now):
            # Marked first, so the next check a second later doesn't ring it twice.
            self.copy.rang(entry["id"])
            logger.info(f"Ringing {entry['id']} from the satellite's copy")
            with self._lock:
                if not any(thread.is_alive() for thread in self._threads):
                    self._stop.clear()
                thread = threading.Thread(target=self._ring, args=(entry,), daemon=True, name=f"ring-{entry['id']}")
                self._threads.append(thread)
            thread.start()

    def _ring(self, entry: Dict):
        """Plays an entry's sound over and over until dismissed or RING_LIMIT."""
        filename = os.path.join(self.sounds_folder, entry["sound"])
        deadline = time.monotonic() + RING_LIMIT
        while not self._stop.is_set() and time.monotonic() < deadline:
            playback = self.speaker.play_file(
                filename, volume=self.state.volume("alarm"), kind="announcement")
            with self._lock:
                self._playbacks[entry["id"]] = playback
            if playback is None:
                # A missing file mustn't spin the thread.
                self._stop.wait(1.0)
                continue
            while not self._stop.is_set() and not playback.done() and time.monotonic() < deadline:
                playback.wait(0.2)
        with self._lock:
            playback = self._playbacks.pop(entry["id"], None)
        if playback is not None and not playback.done():
            playback.stop()

    def dismiss(self):
        """Stops everything which is ringing, and records it as dismissed."""
        with self._lock:
            threads = list(self._threads)
        ids = [thread.name[len("ring-"):] for thread in threads if thread.is_alive()]
        self._stop.set()
        for thread in threads:
            thread.join(timeout=2.0)
        for id in ids:
            self.copy.dismissed(id)


class Phrase:
    """The recorded "I can't reach my server", the brain's own if it has sent one."""

    def __init__(self, path: str = os.path.join(paths.DATA_DIR, "fallback_phrase.wav"),
                 default: str = os.path.join(paths.SOUNDS_DIR, "cant_reach_server.wav")):
        """
        Args:
            path: Where the brain's copy is kept.
            default: The recording which ships with Nova, for until the brain sends one.
        """
        self.path = path
        self.default = default

    def replace(self, audio: bytes, rate: int):
        """
        Keeps the brain's version of the phrase, in its voice.

        Args:
            audio: 16-bit little-endian mono audio.
            rate: Its sample rate.
        """
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        temporary = self.path + ".tmp"
        try:
            with wave.open(temporary, "wb") as file:
                file.setnchannels(1)
                file.setsampwidth(2)
                file.setframerate(rate)
                file.writeframes(audio)
            os.replace(temporary, self.path)
        except (OSError, wave.Error) as e:
            logger.error(f"Could not keep the brain's phrase: {e}")
            if os.path.exists(temporary):
                os.remove(temporary)

    def play(self, speaker: Speaker, volume: float) -> Optional[Playback]:
        """
        Args:
            speaker: Plays it.
            volume: How loud, 0 to 1.

        Returns:
            The Playback, or None if neither recording could be played.
        """
        # Not cached - the brain's copy can be replaced while the file stays the same name.
        if os.path.exists(self.path):
            playback = speaker.play_file(self.path, volume=volume, kind="reply", cache=False)
            if playback is not None:
                return playback
        return speaker.play_file(self.default, volume=volume, kind="reply")
