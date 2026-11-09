"""
Displays Module.

The brain keeps the clock displays it serves - a name, the satellite each one hears
for, its brightness and how soon it returns to the first page - and the key each
proves itself with. A key is shown once, when it's made, and kept only as its SHA-256,
so the file gives no clock away. They are kept in displays.json in Nova's config
folder, readable by this user only. See docs/decisions/0013-the-clock-display.md.
"""

import hashlib
import hmac
import os
import re
import secrets
import threading
from typing import Any, Dict, List, Optional, Tuple

from voice_assistant import paths
from voice_assistant.storage import load_json, save_json

# What a new clock starts with: dim enough for a bedside table, and back to the first
# page a minute after it was last touched.
DEFAULT_BRIGHTNESS = 70
DEFAULT_HOME_AFTER = 60

# More than a house has clocks, so a loop of adds can't fill the file.
MOST_DISPLAYS = 10

# Seconds a clock may stay on another page before it returns to the first.
SHORTEST_HOME_AFTER = 10
LONGEST_HOME_AFTER = 3600

# Long enough for "Kitchen counter", short enough to fit the page's row.
LONGEST_NAME = 30

# What only the page may change about a clock, and so what change() accepts.
_CHANGEABLE = ("satellite", "brightness", "home_after")

_NOT_A_SLUG_CHARACTER = re.compile(r"[^a-z0-9]+")


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _is_whole_number(value: Any) -> bool:
    # bool is an int to Python, but "brightness": true is a mistake, not 1.
    return isinstance(value, int) and not isinstance(value, bool)


def _listed(stored: Dict[str, Any]) -> Dict[str, Any]:
    """
    Args:
        stored: A clock as it's kept.

    Returns:
        The same without its key's hash, so no caller can pass it on by mistake.
    """
    return {"id": stored["id"], "name": stored["name"], "satellite": stored["satellite"],
            "brightness": stored["brightness"], "home_after": stored["home_after"]}


class Displays:
    """The clocks the brain serves, and the key each has to show."""

    def __init__(self, path: str = os.path.join(paths.CONFIG_DIR, "displays.json")):
        """
        Reads the file once, so a clock removed by hand needs the brain restarted;
        the settings page is the one way to change them while it runs.

        Args:
            path: The file they are kept in.

        Raises:
            OSError or ValueError: If the file is there but can't be read - a broken
                one is not taken for having no clocks, as the next add would
                overwrite it.
        """
        self.path = path
        self._lock = threading.Lock()
        stored = load_json(path, [])
        if not isinstance(stored, list) or not all(isinstance(one, dict) for one in stored):
            raise ValueError(f"{path} doesn't hold a list of clocks")
        self._stored: List[Dict[str, Any]] = stored

    def _save(self, stored: List[Dict[str, Any]]):
        """
        Writes the clocks, and only then keeps them in memory, so a clock which
        couldn't be saved isn't one the brain admits until it restarts.

        Raises:
            OSError: If they couldn't be saved.
        """
        save_json(self.path, stored)
        self._stored = stored

    def _unique_id(self, name: str, taken: List[str]) -> str:
        """
        Args:
            name: The clock's name, e.g. "Kitchen counter".
            taken: The ids already there.

        Returns:
            A short id for it that is in none of them, e.g. "kitchen-counter", or
            "kitchen-counter-2" if that was taken. A name with no letters or digits
            in it, e.g. "!!!", is "clock".
        """
        slug = _NOT_A_SLUG_CHARACTER.sub("-", name.lower()).strip("-") or "clock"
        candidate, number = slug, 1
        while candidate in taken:
            number += 1
            candidate = f"{slug}-{number}"
        return candidate

    def _find_id(self, stored: List[Dict[str, Any]], display_id: str) -> Dict[str, Any]:
        for one in stored:
            if one["id"] == display_id:
                return one
        raise ValueError("There is no such clock.")

    def listed(self) -> List[Dict[str, Any]]:
        """
        Returns:
            The clocks in the order they were added, each as {"id", "name",
            "satellite" (a satellite's name or None), "brightness" (0-100) and
            "home_after" (seconds)}. Never a key or its hash.
        """
        with self._lock:
            return [_listed(one) for one in self._stored]

    def add(self, name: str, satellite: Optional[str]) -> Tuple[Dict[str, Any], str]:
        """
        Args:
            name: What the page calls it, e.g. "Desk".
            satellite: The satellite whose microphone a hold on it listens through,
                or None for no satellite yet.

        Returns:
            The new clock, as listed, and its key - which is not kept, so this is the
            one time it can be shown.

        Raises:
            ValueError: If the name is empty or too long, the satellite isn't a name,
                or there are already as many clocks as MOST_DISPLAYS.
            OSError: If it couldn't be saved.
        """
        name = name.strip() if isinstance(name, str) else ""
        if not name or len(name) > LONGEST_NAME:
            raise ValueError(f"A clock's name has to be 1 to {LONGEST_NAME} characters.")
        if satellite is not None and (not isinstance(satellite, str) or not satellite):
            raise ValueError("A clock's satellite has to be a satellite's name, or none.")
        key = secrets.token_urlsafe(32)
        with self._lock:
            if len(self._stored) >= MOST_DISPLAYS:
                raise ValueError(f"Nova keeps at most {MOST_DISPLAYS} clocks - remove one first.")
            one = {"id": self._unique_id(name, [other["id"] for other in self._stored]),
                   "name": name, "satellite": satellite, "brightness": DEFAULT_BRIGHTNESS,
                   "home_after": DEFAULT_HOME_AFTER, "key_hash": _key_hash(key)}
            self._save(self._stored + [one])
            return _listed(one), key

    def renew(self, display_id: str) -> str:
        """
        Gives a clock a new key; the old one is refused from now on.

        Args:
            display_id: The clock's id.

        Returns:
            The new key, shown once as in add.

        Raises:
            ValueError: If there's no such clock.
            OSError: If it couldn't be saved.
        """
        key = secrets.token_urlsafe(32)
        with self._lock:
            self._find_id(self._stored, display_id)
            self._save([{**one, "key_hash": _key_hash(key)} if one["id"] == display_id else one
                        for one in self._stored])
        return key

    def change(self, display_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        """
        Args:
            display_id: The clock's id.
            changes: What to change, from "satellite" (a name, or None for none),
                "brightness" (a whole number, 0 to 100) and "home_after" (a whole
                number of seconds, 10 to 3600). Nothing is changed if any is wrong.

        Returns:
            The clock, as listed, with the changes.

        Raises:
            ValueError: If there's no such clock, or a change is to anything else or
                is out of its limits.
            OSError: If it couldn't be saved.
        """
        for field, value in changes.items():
            if field not in _CHANGEABLE:
                raise ValueError(f"A clock's {field} can't be changed here.")
            if field == "satellite" and value is not None and (not isinstance(value, str) or not value):
                raise ValueError("A clock's satellite has to be a satellite's name, or none.")
            if field == "brightness" and not (_is_whole_number(value) and 0 <= value <= 100):
                raise ValueError("A clock's brightness has to be a whole number from 0 to 100.")
            if field == "home_after" and not (
                    _is_whole_number(value) and SHORTEST_HOME_AFTER <= value <= LONGEST_HOME_AFTER):
                raise ValueError(f"A clock's time before it goes home has to be a whole number of seconds "
                                 f"from {SHORTEST_HOME_AFTER} to {LONGEST_HOME_AFTER}.")
        with self._lock:
            self._find_id(self._stored, display_id)
            changed = [{**one, **changes} if one["id"] == display_id else one for one in self._stored]
            self._save(changed)
            return _listed(self._find_id(changed, display_id))

    def remove(self, display_id: str):
        """
        Args:
            display_id: The clock's id.

        Raises:
            ValueError: If there's no such clock.
            OSError: If it couldn't be saved.
        """
        with self._lock:
            self._find_id(self._stored, display_id)
            self._save([one for one in self._stored if one["id"] != display_id])

    def find(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Args:
            key: What a clock showed, e.g. in its request.

        Returns:
            The clock it belongs to, as listed, or None if it's no clock's current key.
        """
        if not key:
            return None
        given = _key_hash(key)
        with self._lock:
            stored = self._stored
        # Compared with each in constant time, rather than looked up, so how long it
        # takes says nothing about how near a guess came.
        found = None
        for one in stored:
            if hmac.compare_digest(given, one["key_hash"]):
                found = one
        return _listed(found) if found else None
