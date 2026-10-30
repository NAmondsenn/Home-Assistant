"""
Media Watch Module
What's playing in each satellite's room, from decisions 1 and 2 of docs/decisions/0010.

Speech from a film or a video can sound enough like "Hey Nova" to wake it, so while
something plays in a satellite's room that satellite is told to be surer of its wake word.
Home Assistant is the only source: any media player it puts in the room counts while it's
playing, so a TV added later counts by itself, with no list kept anywhere. Nova's own
music doesn't count - a player on any satellite's own device, or Spotify playing on one
of the satellites' own speakers.
"""

import logging
import threading
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.home_assistant import HomeAssistant

logger = logging.getLogger(__name__)


def _what_counts(players: List[Dict]) -> List[Tuple]:
    """
    What a list of players is compared by: each one's entity, room, device and source, in
    order. Not its name - a player renamed while it plays changes nothing for the wake word.
    """
    return sorted((p["entity_id"], p["area"] or "", p["device"] or "", p["source"] or "") for p in players)


class MediaWatcher:
    """
    Reads every few seconds which media players are playing, wherever they are, and says
    when that changes. Which of them count is worked out for each satellite as it asks -
    see playing_in. Whenever Home Assistant can't be read, nothing is playing - Nova keeps
    its normal wake word, as it would with no watcher.
    """

    def __init__(self, home: HomeAssistant, on_change: Callable[[], None], every: float = 3.0):
        """
        Args:
            home: Home Assistant, for what's playing.
            on_change: Called each time what's playing changes - a player starting or
                stopping, or moving room, device or source.
            every: Seconds between checks.
        """
        self._home = home
        self._on_change = on_change
        self._every = every
        self._players: List[Dict] = []
        self._down = False
        # The kind of error the thread last logged, while it keeps happening - see _pass.
        self._failing: Optional[type] = None
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def playing(self) -> List[Dict]:
        """
        Returns:
            Every player playing, as HomeAssistant.playing_media() last gave them - empty
            before the first read, or after one which failed.
        """
        return list(self._players)

    def playing_in(self, area: Optional[str], devices: Iterable[Optional[str]],
                   speakers: Iterable[Optional[str]]) -> List[str]:
        """
        What counts as playing for one satellite.

        Args:
            area: The satellite's room, e.g. "Bedroom", or None if it isn't known.
            devices: Every connected satellite's own device id - None for one not known.
            speakers: Every connected satellite's Spotify speaker, e.g. "Voice-Assistant".

        Returns:
            The names of the players in that room, on none of the satellites' devices, and
            not Spotify playing on a satellite's own speaker, in order - e.g. ["Bedroom TV"].
            Empty if the room isn't known.
        """
        if not area:
            return []
        own_devices = {device for device in devices if device}
        own_speakers = {speaker.strip().lower() for speaker in speakers if speaker}
        names = []
        for player in self._players:
            source = player["source"]
            if player["area"] != area or player["device"] in own_devices:
                continue
            if isinstance(source, str) and source.strip().lower() in own_speakers:
                continue
            names.append(player["name"])
        return sorted(names)

    def check(self):
        """
        Reads what's playing once, and calls on_change if it has changed. Home Assistant not
        answering is warned of once until it does again, and counts as nothing playing.

        Raises:
            Exception: Whatever on_change raises - a bug, which the thread logs.
        """
        found = self._read()
        changed = _what_counts(found) != _what_counts(self._players)
        # Kept before on_change is called, as what it sends the satellites asks playing_in().
        self._players = found
        if changed:
            self._on_change()

    def start(self):
        """Starts the daemon thread which checks every few seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="media")
        self._thread.start()

    def stop(self):
        """Stops the thread, waiting briefly for a check already under way to end."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        while not self._stopping.is_set():
            self._pass()
            self._stopping.wait(self._every)

    def _pass(self):
        """One pass of the thread, in which no error may end the thread."""
        try:
            self.check()
        except Exception as e:
            # A bug would otherwise fill the log with the same traceback every few seconds.
            if type(e) is not self._failing:
                logger.exception("Checking what's playing failed, will try again")
            self._failing = type(e)
        else:
            self._failing = None

    def _read(self) -> List[Dict]:
        """
        Returns:
            Every player playing - none if Home Assistant can't be read.
        """
        try:
            found = self._home.playing_media()
        except HOME_FAILURES as e:
            # A line every three seconds for a night of Home Assistant being restarted would bury the log.
            if not self._down:
                logger.warning(f"Home Assistant didn't answer, so nothing counts as playing: {e}")
            self._down = True
            return []
        if self._down:
            logger.info("Home Assistant answers again, so what's playing is watched again.")
            self._down = False
        return found
