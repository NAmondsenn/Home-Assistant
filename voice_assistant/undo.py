"""
Undo Module.

What was just removed on the settings page - an alarm, a timer or a reminder - kept
for a minute, so the page's Undo can put it back (see 0009, decision 4). Each is
kept behind a token of its own, which the page is given in place of what was
removed, and which works once it has put it back - one refused, as it would clash
with something set since, can be tried again until its time is up. The page offers
Undo for ten seconds (UNDO_SHOWN, in web/settings.js), and the brain keeps each for
UNDO_KEPT.

Kept in memory only, never written down, so a restart forgets it all - what was
removed before it stays removed.
"""

import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# How long what was removed can be put back, in seconds.
UNDO_KEPT = 60.0
# The most kept at once - far more than anyone removes in a minute, but a limit, so the
# page can't grow it without end.
MOST_KEPT = 50


class UndoShelf:
    """What was just removed, by the token which puts it back."""

    def __init__(self, keep_for: float = UNDO_KEPT, clock: Callable[[], float] = time.monotonic):
        """
        Args:
            keep_for: How long each is kept, in seconds.
            clock: What tells the time, in seconds - steady, so the wall clock being
                set while one is kept can't keep it longer, or lose it sooner.
        """
        self.keep_for = keep_for
        self._clock = clock
        # Each token, to the kind of thing kept, the thing itself and when it was put.
        self._kept: Dict[str, Tuple[str, Any, float]] = {}
        # The page answers each request on a thread of its own, so two taps of Undo
        # could otherwise both find what one was meant to take.
        self._lock = threading.Lock()

    def put(self, kind: str, item: Any) -> str:
        """
        Keeps what was removed, letting go of anything kept too long already.

        Args:
            kind: What it is, e.g. "alarm" or "timer".
            item: It, as it was.

        Returns:
            The token which takes it back, unguessable.
        """
        token = secrets.token_urlsafe(16)
        with self._lock:
            now = self._clock()
            self._kept = {kept: entry for kept, entry in self._kept.items() if now - entry[2] <= self.keep_for}
            self._kept[token] = (kind, item, now)
            while len(self._kept) > MOST_KEPT:
                del self._kept[min(self._kept, key=lambda kept: self._kept[kept][2])]
        return token

    def take(self, token: Any) -> Optional[Tuple[str, Any]]:
        """
        Takes back what a token keeps, once only.

        Args:
            token: As the page sent it - anything at all.

        Returns:
            (kind, item), or None if the token isn't one, has been used, or is too old.
        """
        claimed = self._claim(token)
        return None if claimed is None else claimed[:2]

    def use(self, token: Any, undo: Callable[[str, Any], Dict]) -> Optional[Dict]:
        """
        Puts back what a token keeps, with undo - using the token up only if that
        worked. Meanwhile it's taken off the shelf, so a second tap at the same moment
        finds nothing rather than putting it back twice.

        Args:
            token: As the page sent it - anything at all.
            undo: Called with the kind and the item, answering with 'success' and a
                'message'.

        Returns:
            What undo answered, or None if the token isn't one, has been used, or is
            too old.
        """
        claimed = self._claim(token)
        if claimed is None:
            return None
        answer = None
        try:
            answer = undo(claimed[0], claimed[1])
            return answer
        finally:
            if not (answer and answer.get("success")):
                with self._lock:
                    if self._clock() - claimed[2] <= self.keep_for:
                        self._kept[token] = claimed

    def _claim(self, token: Any) -> Optional[Tuple[str, Any, float]]:
        """Takes a token's entry off the shelf, if it has one still in time."""
        if not isinstance(token, str):
            return None
        given = token.encode()
        with self._lock:
            # Each compared in the same time, however much of it is right, so the page's
            # answers can't be timed to find one.
            found = None
            for kept in self._kept:
                if secrets.compare_digest(kept.encode(), given):
                    found = kept
            if found is None:
                return None
            entry = self._kept.pop(found)
            if self._clock() - entry[2] > self.keep_for:
                return None
            return entry
