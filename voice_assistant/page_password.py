"""
Page Password Module.

The settings page's own password, for when it's reached through Nginx Proxy Manager
rather than the tailnet, and what follows from it: the logins it gives, and the count
of wrong tries that slows down guessing.

The password is kept only as a salted scrypt hash, in a file of its own in the config
folder, and each login only as the SHA-256 of its token, in the data folder - both
readable by Nova's user only. So neither file lets anyone who reads it in. A file that
can't be read lets nobody in rather than everybody.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

from voice_assistant import paths
from voice_assistant.storage import load_json, save_json, save_text

logger = logging.getLogger(__name__)

# The shortest password the page will take.
MIN_LENGTH = 12
# How long a login lasts from when it's made - using it doesn't extend it.
LOGIN_DAYS = 30
# Wrong tries in a row from one device before its tries are refused for WAIT seconds.
TRIES_EACH = 5
# Wrong tries within WAIT seconds from every device together before every try is
# refused for WAIT seconds - a backstop against one guesser using many addresses.
TRIES_ALL = 30
WAIT = 60

# scrypt's cost: 16 MiB of memory and a few tens of milliseconds a check, which is
# nothing for one login but makes each guess at a stolen hash dear.
_N, _R, _P = 2**14, 8, 1
_SALT_BYTES = 16
_HASH_BYTES = 32


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=length)


class PagePassword:
    """The settings page's password, kept as a hash."""

    def __init__(self, path: str = paths.PAGE_PASSWORD_FILE):
        """
        Args:
            path: The file the hash is kept in.
        """
        self.path = path

    def _read(self) -> Optional[Tuple[int, int, int, bytes, bytes]]:
        """
        Returns:
            The hash's parameters, salt and hash, or None if there's no password or
            its file can't be read or isn't one this module wrote.
        """
        try:
            with open(self.path) as file:
                text = file.read().strip()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            # Only the kind of error - the text of a decoding error quotes the file.
            logger.warning(f"Could not read the page password's file, so it's as if none is set "
                           f"({type(e).__name__})")
            return None
        try:
            name, n, r, p, salt, digest = text.split("$")
            if name != "scrypt":
                raise ValueError
            n, r, p = int(n), int(r), int(p)
            salt = base64.b64decode(salt, validate=True)
            digest = base64.b64decode(digest, validate=True)
            if n < 2 or r < 1 or p < 1 or not salt or not digest:
                raise ValueError
        except ValueError:
            logger.warning("The page password's file isn't a hash Nova wrote, so it's as if none is set")
            return None
        return n, r, p, salt, digest

    def is_set(self) -> bool:
        """
        Returns:
            Whether there's a password that could be checked.
        """
        return self._read() is not None

    def check(self, password: str) -> bool:
        """
        Args:
            password: What the user typed.

        Returns:
            Whether it's the password - never when none is set or its file can't be read.
        """
        stored = self._read()
        if stored is None:
            return False
        n, r, p, salt, digest = stored
        try:
            given = _scrypt(password, salt, n, r, p, len(digest))
        except (ValueError, MemoryError):
            # Parameters scrypt won't take, e.g. an n that isn't a power of two.
            logger.warning("The page password's file has a hash Nova can't check, so it's as if none is set")
            return False
        return hmac.compare_digest(given, digest)

    def set(self, password: str) -> Optional[str]:
        """
        Saves a new password in place of any old one. Ending the logins the old one
        gave is the caller's part.

        Args:
            password: The new password.

        Returns:
            None if it was saved, or what to tell the user if it wasn't.

        Raises:
            OSError: If it couldn't be written - the old one, if any, stays.
        """
        if len(password) < MIN_LENGTH:
            return f"The page password needs at least {MIN_LENGTH} characters."
        salt = secrets.token_bytes(_SALT_BYTES)
        digest = _scrypt(password, salt, _N, _R, _P, _HASH_BYTES)
        encoded = [base64.b64encode(part).decode() for part in (salt, digest)]
        save_text(self.path, "$".join(["scrypt", str(_N), str(_R), str(_P), *encoded]) + "\n")
        return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Logins:
    """
    The page's logins: a token for each, kept by the browser as a cookie, and here only
    as its SHA-256 and when it ends.

    The file is read afresh on every check, so a login ended by one of these is ended
    for any other over the same file, and one made before a restart lasts through it.
    """

    def __init__(self, path: str = paths.PAGE_LOGINS_FILE, now: Callable[[], float] = time.time):
        """
        Args:
            path: The file they're kept in.
            now: The time, in seconds since the epoch - wall-clock time, as a login's
                end has to mean the same after a restart.
        """
        self.path = path
        self.now = now
        self._lock = threading.Lock()
        self._warned = False

    def _read(self) -> Dict[str, float]:
        """
        Returns:
            The logins that haven't ended, by their token's hash - none if the file
            can't be read, so a corrupt one lets nobody in.
        """
        try:
            stored = load_json(self.path, {})
            if not isinstance(stored, dict):
                raise ValueError("not an object")
            for digest, ends in stored.items():
                if len(digest) != 64 or isinstance(ends, bool) or not isinstance(ends, (int, float)):
                    raise ValueError("not a login")
        except (OSError, ValueError) as e:
            # Once until it's readable again, as it's read on every request.
            if not self._warned:
                logger.warning(f"Could not read the page's logins, so every login has ended "
                               f"({type(e).__name__})")
                self._warned = True
            return {}
        self._warned = False
        now = self.now()
        return {digest: ends for digest, ends in stored.items() if ends > now}

    def make(self) -> str:
        """
        Returns:
            A new login's token, for the cookie - it isn't kept anywhere else.

        Raises:
            OSError: If the logins couldn't be saved.
        """
        token = secrets.token_urlsafe(32)
        with self._lock:
            logins = self._read()
            logins[_token_hash(token)] = self.now() + LOGIN_DAYS * 86400
            save_json(self.path, logins)
        return token

    def check(self, token: Optional[str]) -> bool:
        """
        Args:
            token: The token from the request's cookie, or None if it had none.

        Returns:
            Whether it's a login that hasn't ended.
        """
        if not token:
            return False
        given = _token_hash(token)
        with self._lock:
            logins = self._read()
        # Compared with each in constant time, rather than looked up, so how long it
        # takes says nothing about how near a guess came.
        found = False
        for digest in logins:
            found |= hmac.compare_digest(given, digest)
        return found

    def end(self, token: str):
        """
        Ends one login - logging out this device.

        Args:
            token: Its token.

        Raises:
            OSError: If the logins couldn't be saved.
        """
        given = _token_hash(token)
        with self._lock:
            logins = self._read()
            kept = {digest: ends for digest, ends in logins.items()
                    if not hmac.compare_digest(given, digest)}
            save_json(self.path, kept)

    def end_all(self):
        """
        Ends every login, whichever device - for a lost phone, or a new password.

        Raises:
            OSError: If the logins couldn't be saved.
        """
        with self._lock:
            save_json(self.path, {})


class WrongTries:
    """
    Wrong passwords, counted per device and all together, in memory only. Each device
    is forgotten WAIT seconds after its last wrong try, so it holds no more than the
    devices seen in the last minute.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic):
        """
        Args:
            now: The time, in seconds - monotonic, so a clock change can't end a wait.
        """
        self.now = now
        self._lock = threading.Lock()
        # Each device's wrong tries in a row, and when its last one was.
        self._devices: Dict[str, Tuple[int, float]] = {}
        # When each wrong try from any device was, over the last WAIT seconds.
        self._recent: Deque[float] = deque()
        self._all_wait_until = 0.0

    def _forget_old(self, now: float):
        self._devices = {device: (count, last) for device, (count, last) in self._devices.items()
                         if now - last < WAIT}
        while self._recent and now - self._recent[0] >= WAIT:
            self._recent.popleft()

    def waiting(self, device: str) -> bool:
        """
        Args:
            device: The device's address, e.g. from X-Real-IP.

        Returns:
            Whether its try is to be refused unchecked.
        """
        with self._lock:
            now = self.now()
            self._forget_old(now)
            if now < self._all_wait_until:
                return True
            count, _ = self._devices.get(device, (0, 0.0))
            # Its last wrong try was within WAIT, or it would have been forgotten.
            return count >= TRIES_EACH

    def wrong(self, device: str):
        """
        Counts a wrong password.

        Args:
            device: The device's address.
        """
        with self._lock:
            now = self.now()
            self._forget_old(now)
            count, _ = self._devices.get(device, (0, 0.0))
            self._devices[device] = (count + 1, now)
            self._recent.append(now)
            if len(self._recent) >= TRIES_ALL:
                self._all_wait_until = now + WAIT

    def right(self, device: str):
        """
        Resets a device's count after its right password.

        Args:
            device: The device's address.
        """
        with self._lock:
            self._devices.pop(device, None)
