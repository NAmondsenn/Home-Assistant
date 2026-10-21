"""
Dropouts Module.

Counts the times the speaker ran dry or the microphone overflowed, and says so in
the log - the crackle in a reply, or the lost start of "Hey Nova", which otherwise
leave no trace. How far ahead the speaker is kept (speaker.OUTPUT_LATENCY), and the
shared output's period (/etc/asound.conf), trade how soon a stopped sound goes
quiet against how easily it runs dry, so this is what shows whether they're right.
"""

import logging
import time

# Logger setup
logger = logging.getLogger(__name__)

# The most often a stream's dropouts are mentioned in the log, in seconds. A
# struggling stream can drop out dozens of times a second, and logging each one
# from its audio thread would only make it worse.
REPORT_EVERY_SECONDS = 60.0


class Dropouts:
    """The dropouts on one audio stream."""

    def __init__(self, what: str):
        """
        Args:
            what: What happened, as said in the log, e.g. "The speaker ran dry".
        """
        self.what = what
        self.total = 0
        self._reported_at = None

    def note(self, now: float = None):
        """
        Counts one dropout, logging it unless one was logged in the last minute.
        Called from the stream's own audio thread, so it only ever does a little.

        Args:
            now: The time.monotonic() it happened at, for the tests.
        """
        now = time.monotonic() if now is None else now
        self.total += 1
        if self._reported_at is None or now - self._reported_at >= REPORT_EVERY_SECONDS:
            self._reported_at = now
            logger.warning(f"{self.what} - {self.total} time(s) since Nova started")
