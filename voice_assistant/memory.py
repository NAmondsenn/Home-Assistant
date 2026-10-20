"""
Memory Module.

What Nova knows about the user, so it can be more of a personal assistant: short
facts, worded as Nova would say them back - "You work at Tesco.", "Your sister is
called Amy." - picked up as they come up in conversation - Claude saves them itself, and says "I'll remember that" - or
when the user says "remember that...". They're given to Claude with every request
(see LLMHandler._system_prompt), and the user can hear them ("what do you know
about me?"), and have them forgotten one at a time or all at once.

Kept in memory.json in the data folder (see paths.py), readable by this user only,
and never written to the logs while logging.log_transcripts is off.
"""

import difflib
import json
import logging
import os
import re
import tempfile
import threading
from typing import Dict, List

from voice_assistant import paths
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

MEMORY_FILE = os.path.join(paths.DATA_DIR, "memory.json")

# The most facts kept - they're all sent with every request, so this stops that
# growing without end.
MOST_FACTS = 100

# How alike something the user asks to forget has to be to a fact for it to be that one.
FORGET_LIKENESS = 0.5

# The most facts read out at once, for "what do you know about me?".
MOST_SAID = 10


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", text.lower())).strip()


class Memory:
    """The facts Nova knows about the user."""

    def __init__(self, path: str = None):
        """
        Args:
            path: Where they're kept - memory.json in the data folder unless given.
        """
        self.path = path or MEMORY_FILE
        self._lock = threading.Lock()
        self._facts: List[str] = self._load()

    def _load(self) -> List[str]:
        try:
            with open(self.path) as file:
                facts = json.load(file).get("facts", [])
            return [fact for fact in facts if isinstance(fact, str) and fact.strip()]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, AttributeError) as e:
            logger.error(f"Couldn't read what I remember, so starting afresh: {e}")
            return []

    def _save(self, facts: List[str], path: str = None):
        """
        Writes the facts to disk, readable by this user only. Written to a temporary
        file and renamed, which is atomic on Linux, so an interrupted write can't
        leave a corrupt file behind.
        """
        path = path or self.path
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".memory-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as file:
                json.dump({"facts": facts}, file, indent=1)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            if os.path.exists(temporary):
                os.remove(temporary)
            raise

    def facts(self) -> List[str]:
        """Everything remembered, oldest first."""
        with self._lock:
            return list(self._facts)

    def remember(self, fact: str) -> Dict:
        """
        Keeps a fact about the user.

        Args:
            fact: e.g. "You work at Tesco."

        Returns:
            Dict with 'success' and a 'message' to say.
        """
        fact = re.sub(r"\s+", " ", (fact or "")).strip()
        if not fact:
            return {"success": False, "message": "I didn't catch what to remember."}
        with self._lock:
            if _words(fact) in {_words(known) for known in self._facts}:
                return {"success": True, "message": "I already knew that.", "note": True}
            if len(self._facts) >= MOST_FACTS:
                return {"success": False, "message": "My memory's full. Ask me to forget something first."}
            facts = self._facts + [fact]
            try:
                self._save(facts)
            except OSError as e:
                logger.error(f"Couldn't save what I remember: {e}")
                return {"success": False, "message": "Sorry, I couldn't save that."}
            self._facts = facts
        logger.info(f"Remembered: {fact}", extra=TRANSCRIPT)
        return {"success": True, "message": "I'll remember that.", "note": True}

    def forget(self, what: str) -> Dict:
        """
        Forgets the fact which best matches what the user said.

        Args:
            what: Roughly how they put it, e.g. "that I work at Tesco".

        Returns:
            Dict with 'success' and a 'message' to say.
        """
        wanted = _words(what or "")
        with self._lock:
            if not wanted or not self._facts:
                return {"success": False, "message": "I don't remember anything like that."}
            likeness = [difflib.SequenceMatcher(None, wanted, _words(fact)).ratio() for fact in self._facts]
            best = max(range(len(likeness)), key=likeness.__getitem__)
            if likeness[best] < FORGET_LIKENESS:
                return {"success": False, "message": "I don't remember anything like that."}
            forgotten = self._facts[best]
            facts = self._facts[:best] + self._facts[best + 1:]
            self._save(facts)
            self._facts = facts
        logger.info(f"Forgot: {forgotten}", extra=TRANSCRIPT)
        return {"success": True, "message": f"Okay, I've forgotten that {forgotten[0].lower()}{forgotten[1:]}"}

    def forget_everything(self) -> Dict:
        """
        Forgets every fact - keeping one copy beside the file, memory.json.old, in
        case it was a mishearing.
        """
        with self._lock:
            if not self._facts:
                return {"success": True, "message": "I don't remember anything about you yet."}
            self._save(self._facts, path=self.path + ".old")
            self._save([])
            count, self._facts = len(self._facts), []
        logger.info(f"Forgot all {count} facts (a copy is in {self.path}.old)")
        return {"success": True, "message": "Okay, I've forgotten everything I knew about you."}

    def describe(self) -> Dict:
        """What's remembered, as something to say - the most recent few, if there are many."""
        facts = self.facts()
        if not facts:
            return {"success": True, "message": "I don't know anything about you yet. Tell me things, and I'll remember them."}
        said = facts[-MOST_SAID:]
        more = f" And {len(facts) - len(said)} more things." if len(facts) > len(said) else ""
        return {"success": True, "message": "Here's what I know. " + " ".join(said) + more}
