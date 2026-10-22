"""
Routines Module.

The user's own routines: several things done at once, from a few words - "good
morning" - or at a time of their choosing. Each step is something the user could
say to Nova - "what's the weather", "turn on the kitchen lights", "play my morning
playlist" - and is done just as if they had said it (see Conversation.run_routine),
so a routine can do anything Nova can, with nothing of its own to learn.

Kept in routines.json in the data folder (see paths.py), readable by this user only,
and made and changed from the settings page.
"""

import logging
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from voice_assistant import paths, storage
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

ROUTINES_FILE = os.path.join(paths.DATA_DIR, "routines.json")

# The most routines kept, steps in one, and ways to start one, and the longest name
# and step - far more than anyone would make by hand, but a limit.
MOST_ROUTINES = 30
MOST_STEPS = 10
MOST_PHRASES = 5
LONGEST_NAME = 60
LONGEST_STEP = 200

# The days each kind of repeat runs on, Monday being 0 - as for an alarm.
REPEATS = {"daily": range(7), "weekdays": range(5), "weekends": (5, 6)}

# A routine asked for by its name: "run my good morning routine".
BY_NAME = re.compile(r"(?:(?:run|start|do)\s+)?(?:(?:my|the)\s+)?(.+?)\s+routine")


def words(text: str) -> str:
    """Text as it's compared with what was said: in lower case, without punctuation."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", (text or "").lower())).strip()


def _same(one: str, other: str) -> bool:
    """
    Whether two of those are the same words, however they're split - speech-to-text
    hears "goodnight" as "Good night" one time and "Goodnight" the next.
    """
    return one.replace(" ", "") == other.replace(" ", "")


def _checked(given: Dict) -> Dict:
    """
    A routine as it's kept, from what the settings page sent.

    Raises:
        ValueError, saying what's wrong with it.
    """
    name = re.sub(r"\s+", " ", str(given.get("name") or "")).strip()
    if not name:
        raise ValueError("A routine needs a name.")
    if len(name) > LONGEST_NAME:
        raise ValueError("That name's too long.")

    phrases = given.get("phrases") or []
    if isinstance(phrases, str):
        phrases = phrases.split(",")
    phrases = list(dict.fromkeys(phrase for phrase in (words(str(said)) for said in phrases) if phrase))
    if len(phrases) > MOST_PHRASES:
        raise ValueError(f"A routine can be started {MOST_PHRASES} ways at most.")

    at = str(given.get("at") or "").strip()
    found = re.fullmatch(r"(\d{1,2}):(\d\d)", at)
    if at and not (found and int(found[1]) < 24 and int(found[2]) < 60):
        raise ValueError("That isn't a time.")
    repeat = str(given.get("repeat") or "daily")
    if repeat not in REPEATS:
        raise ValueError(f"It can run {', '.join(REPEATS)}.")

    steps = given.get("steps") or []
    if isinstance(steps, str):
        steps = steps.splitlines()
    steps = [step for step in (re.sub(r"\s+", " ", str(step)).strip() for step in steps) if step]
    if not steps:
        raise ValueError("A routine needs something to do - a step a line.")
    if len(steps) > MOST_STEPS:
        raise ValueError(f"A routine can have {MOST_STEPS} steps at most.")
    if any(len(step) > LONGEST_STEP for step in steps):
        raise ValueError("One of those steps is too long.")

    # The satellite it runs on at its time, by name - or None for the default one, the
    # first added (see SatellitePasswords).
    satellite = str(given.get("satellite") or "").strip()
    if len(satellite) > LONGEST_NAME:
        raise ValueError("That satellite's name is too long.")

    known = str(given.get("id") or "")
    return {"id": known if re.fullmatch(r"[a-z0-9]{1,16}", known) else uuid.uuid4().hex[:8], "name": name,
            "phrases": phrases, "at": f"{int(found[1]):02d}:{found[2]}" if at else "", "repeat": repeat,
            "steps": steps, "satellite": satellite or None}


class Routines:
    """The user's routines."""

    def __init__(self, path: str = None):
        """
        Args:
            path: Where they're kept - routines.json in the data folder unless given.
        """
        self.path = path or ROUTINES_FILE
        self._lock = threading.Lock()
        self._routines: List[Dict] = self._load()
        # The day each one last ran by itself, so it runs once at its time, however
        # often due() is asked within that minute.
        self._ran: Dict[str, str] = {}

    def _load(self) -> List[Dict]:
        try:
            kept = storage.load_json(self.path, default={}).get("routines", [])
        except (OSError, ValueError, AttributeError) as e:
            logger.error(f"Couldn't read the routines, so starting without them: {e}")
            return []
        # One at a time, so one which doesn't make sense doesn't lose the rest.
        routines = []
        for routine in kept:
            try:
                routines.append(_checked(routine))
            except (ValueError, AttributeError) as e:
                logger.error(f"Left out a routine which didn't make sense: {e}")
        return routines

    def all(self) -> List[Dict]:
        """Every routine, by name."""
        with self._lock:
            return [dict(routine) for routine in self._routines]

    def get(self, routine_id: str) -> Optional[Dict]:
        with self._lock:
            return next((dict(routine) for routine in self._routines if routine["id"] == routine_id), None)

    def called(self, text: str) -> Optional[Dict]:
        """
        The routine something said starts - by one of its own ways of being started,
        or asked for by name - or None.
        """
        said = words(text)
        named = BY_NAME.fullmatch(said)
        with self._lock:
            for routine in self._routines:
                if (any(_same(said, phrase) for phrase in routine["phrases"])
                        or (named and _same(named[1], words(routine["name"])))):
                    return dict(routine)
        return None

    def due(self, now: Optional[datetime] = None) -> List[Dict]:
        """The routines whose time it is, each only once that day."""
        now = now or datetime.now()
        at, today = now.strftime("%H:%M"), now.date().isoformat()
        found = []
        with self._lock:
            for routine in self._routines:
                if (routine["at"] == at and now.weekday() in REPEATS[routine["repeat"]]
                        and self._ran.get(routine["id"]) != today):
                    self._ran[routine["id"]] = today
                    found.append(dict(routine))
        return found

    def save(self, given: Dict) -> Dict:
        """
        Adds a routine, or changes one given with its id - checked first, so it can't
        be started by the same words as another.

        Returns:
            Dict with 'success', a 'message' and, once saved, the 'routine'.
        """
        try:
            routine = _checked(given)
        except ValueError as e:
            return {"success": False, "message": str(e)}
        with self._lock:
            others = [other for other in self._routines if other["id"] != routine["id"]]
            if len(others) >= MOST_ROUTINES:
                return {"success": False, "message": f"Nova keeps {MOST_ROUTINES} routines at most."}
            for other in others:
                if _same(words(other["name"]), words(routine["name"])):
                    return {"success": False, "message": f"There's already a routine called {other['name']}."}
                shared = next((phrase for phrase in routine["phrases"]
                               if any(_same(phrase, theirs) for theirs in other["phrases"])), None)
                if shared:
                    return {"success": False, "message": f"“{shared}” already starts {other['name']}."}
            routines = sorted(others + [routine], key=lambda kept: kept["name"].lower())
            try:
                storage.save_json(self.path, {"routines": routines}, indent=1)
            except OSError as e:
                logger.error(f"Couldn't save the routines: {e}")
                return {"success": False, "message": "Sorry, I couldn't save that."}
            self._routines = routines
        logger.info(f"Saved the {routine['name']} routine", extra=TRANSCRIPT)
        return {"success": True, "message": f"Saved {routine['name']}.", "routine": routine}

    def remove(self, routine_id: str) -> Dict:
        with self._lock:
            gone = next((routine for routine in self._routines if routine["id"] == routine_id), None)
            if gone is None:
                return {"success": False, "message": "That's already gone."}
            routines = [routine for routine in self._routines if routine is not gone]
            try:
                storage.save_json(self.path, {"routines": routines}, indent=1)
            except OSError as e:
                logger.error(f"Couldn't save the routines: {e}")
                return {"success": False, "message": "Sorry, I couldn't delete that."}
            self._routines = routines
        logger.info(f"Deleted the {gone['name']} routine", extra=TRANSCRIPT)
        return {"success": True, "message": f"Deleted {gone['name']}."}
