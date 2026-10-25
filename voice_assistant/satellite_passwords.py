"""
Satellite Passwords Module.

The brain keeps one password for each satellite, so one satellite's can be changed or
taken away without touching the others. They are kept in satellites.json in Nova's
config folder, readable by this user only, in the order they were added - the first
satellite is the default one.
"""

import fcntl
import os
import re
import secrets
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional

from voice_assistant import paths
from voice_assistant.storage import load_json, save_json

# The shortest password the brain will accept. A guessable one defeats the handshake.
MIN_PASSWORD = 16

# A satellite's name is its Pi's machine name - one label of a hostname - as that's
# the name a satellite connects by unless backend.satellite says otherwise, and the
# one setup.sh asks the brain to add.
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def name_problem(name: str) -> Optional[str]:
    """
    What's wrong with a new satellite's name. Those added before the rule aren't
    checked again, so one already there keeps working.

    Args:
        name: The name, e.g. "bedroom".

    Returns:
        Why it can't be a satellite's name, as a sentence, or None if it can.
    """
    if NAME.fullmatch(name):
        return None
    return ("A satellite's name is its Pi's machine name: lower-case letters, digits and hyphens, "
            "not starting or ending with a hyphen.")


class SatellitePasswords:
    """The satellites the brain admits, and the password each one has to prove."""

    def __init__(self, path: str = os.path.join(paths.CONFIG_DIR, "satellites.json")):
        """
        Args:
            path: The file they are kept in.
        """
        self.path = path

    def _read(self) -> Dict[str, str]:
        return load_json(self.path, {})

    @contextmanager
    def _changing(self) -> Iterator[Dict[str, str]]:
        """
        Holds the file to itself while it's read, changed and written back. The page
        changes it in the brain's process and backend.py's add-satellite and
        remove-satellite in one of their own, so without it each could read before
        the other wrote, losing one change - a new password the page had just shown,
        say, put back to the old one by an add-satellite.

        It's a lock on a file of its own beside it, as the file itself is replaced at
        every save. flock rather than lockf, as flock also keeps two threads of the
        brain apart, each opening the lock afresh.

        Yields:
            The passwords, as they are now.
        """
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        lock = os.open(f"{self.path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self._read()
        finally:
            # Closing it lets go of the lock.
            os.close(lock)

    def get(self, name: str) -> Optional[str]:
        """
        Args:
            name: The satellite, e.g. "bedroom".

        Returns:
            Its password, or None if there is no such satellite.
        """
        return self._read().get(name)

    def add(self, name: str, password: Optional[str] = None) -> str:
        """
        Args:
            name: The new satellite.
            password: One it already has, to move it over without changing the satellite.
                Made up if not given.

        Returns:
            The password.

        Raises:
            ValueError: If the name is already there, or the password is too short.
        """
        with self._changing() as passwords:
            if name in passwords:
                raise ValueError(f"There is already a satellite called {name}")
            if password is None:
                password = secrets.token_urlsafe(32)
            if len(password) < MIN_PASSWORD:
                raise ValueError(f"A password has to be at least {MIN_PASSWORD} characters long")
            passwords[name] = password
            save_json(self.path, passwords)
        return password

    def renew(self, name: str) -> str:
        """
        Gives a satellite a new password in place of its old one, keeping its place in
        the order - so the default stays the default.

        Args:
            name: The satellite.

        Returns:
            The new password.

        Raises:
            ValueError: If there's no such satellite.
        """
        with self._changing() as passwords:
            if name not in passwords:
                raise ValueError(f"There is no satellite called {name}")
            passwords[name] = secrets.token_urlsafe(32)
            save_json(self.path, passwords)
            return passwords[name]

    def remove(self, name: str) -> bool:
        """
        Args:
            name: The satellite to stop admitting.

        Returns:
            Whether there was one to remove.
        """
        with self._changing() as passwords:
            if name not in passwords:
                return False
            del passwords[name]
            save_json(self.path, passwords)
        return True

    def make_default(self, name: str):
        """
        Makes a satellite the default, by moving it first in the file. Its password,
        and every other's, stays as it was.

        Args:
            name: The satellite.

        Raises:
            ValueError: If there's no such satellite.
        """
        with self._changing() as passwords:
            if name not in passwords:
                raise ValueError(f"There is no satellite called {name}")
            save_json(self.path, {name: passwords[name], **passwords})

    def names(self, missing_ok: bool = True) -> List[str]:
        """
        Args:
            missing_ok: Whether no file at all means no satellites. The brain asks
                otherwise wherever it would take a missing file for every satellite
                having been removed - which a save never leaves, so only a file
                moved by hand, or the wrong config folder, does.

        Returns:
            The satellites, in the order they were added. The first is the default.

        Raises:
            FileNotFoundError: If there's no file, and missing_ok is False.
            OSError or ValueError: If it can't be read.
        """
        passwords = load_json(self.path)
        if passwords is None:
            if not missing_ok:
                raise FileNotFoundError(f"There's no {self.path}")
            return []
        return list(passwords)
