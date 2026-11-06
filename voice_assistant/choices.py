"""
Choices Module
One helper for choices kept in the data folder, from decision 1 of docs/decisions/0009.

Nova's lists, its weather place, the batteries' marks and the PC's wake settings are all
choices someone makes once and expects to find after a restart. Each used to need its own
lock, its own load and its own "save, or keep it in memory" code. ChoiceStore does that
once, so a feature says only which keys it keeps and what each starts as.
"""

import copy
import logging
import threading
from typing import Any, Callable, Dict

from voice_assistant import storage

logger = logging.getLogger(__name__)


class ChoiceStore:
    """
    A dictionary of choices kept in one JSON file, with one lock guarding it and every save.

    Only the defaults' keys are kept, so an old or hand-edited file can't bring in a key
    nothing reads. Nor can it bring in a value of another type than its default's, unless
    the default is None - a choice not yet made. A change is made to a copy and saved
    before it is kept, so a save that fails leaves the state as it was. The state itself is
    never changed in place, which lets a reader holding the lock look at it without a copy.
    """

    def __init__(self, path: str, defaults: Dict[str, Any]):
        """
        Args:
            path: The file to keep the choices in, e.g. batteries.json.
            defaults: Each key kept and what it starts as, e.g. {"held": {}}.
        """
        self.path = path
        self.lock = threading.RLock()
        self._state = copy.deepcopy(defaults)
        try:
            saved = storage.load_json(path, {})
        except (OSError, ValueError) as e:
            # Better to start with the defaults than to refuse to start.
            logger.warning(f"Couldn't read {path}, starting with the defaults: {e}")
            saved = {}
        if not isinstance(saved, dict):
            logger.warning(f"{path} isn't a dictionary of choices, starting with the defaults")
            saved = {}
        for key, default in defaults.items():
            if key not in saved:
                continue
            # A default of None is a choice not yet made, such as a weather place, which is a name once it is.
            # An exact type, as True == 1 and a whole number isn't a switch.
            if default is not None and type(saved[key]) is not type(default):
                logger.warning(f"{path} has '{key}' as a {type(saved[key]).__name__}, not a "
                               f"{type(default).__name__}, so it starts as the default")
                continue
            self._state[key] = saved[key]

    @property
    def state(self) -> Dict:
        """The live state. Only read it under lock, and never change it in place."""
        return self._state

    def get(self, key: str) -> Any:
        """Returns a copy of one key's value, so the caller can't change the state by accident."""
        with self.lock:
            return copy.deepcopy(self._state[key])

    def change(self, change: Callable[[Dict], object]):
        """
        Applies a change to a copy of the state, saves it, and only then keeps it.

        Args:
            change: Called with the copy, which it changes in place.

        Raises:
            OSError: If the file can't be written. Nothing is changed.
            TypeError: If the change leaves something JSON can't hold. Nothing is changed.
        """
        with self.lock:
            new = copy.deepcopy(self._state)
            change(new)
            storage.save_json(self.path, new)
            self._state = new

    def keep(self, change: Callable[[Dict], object], what: str):
        """
        Makes a change, or, if it can't be saved, keeps it in memory alone.

        Args:
            change: Called with a copy of the state, which it changes in place.
            what: What is being kept, for the warning, e.g. "held warning".

        Raises:
            TypeError: If the change leaves something JSON can't hold. Nothing is changed.
        """
        with self.lock:
            new = copy.deepcopy(self._state)
            change(new)
            try:
                storage.save_json(self.path, new)
            except OSError as e:
                logger.warning(f"Couldn't save the {what}: {e}")
            self._state = new
