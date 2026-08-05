"""
Volume Module
Keeps track of how loud each kind of sound should be.

There are three separate levels, and changing one never affects the others:

    general - the assistant's voice, and Spotify playback with it
    alarm - timers going off, kept separate so an alarm can be loud,
            even when everything else is quiet
    reminder - reminder announcements, which follow the general level
               unless the user sets them to something of their own

Levels are saved to data/volumes.json, so a change made by voice survives a
restart. They deliberately aren't in config.yaml: they're changed by speaking
rather than by editing a file, and a config setting which quietly stopped taking
effect after the first volume change would be misleading.
"""

import os
import json
import logging
import tempfile
from typing import Callable, Dict, Optional

# Logger setup
logger = logging.getLogger(__name__)

# Resolves paths relative to the project root.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOLUMES_FILE = os.path.join(PROJECT_DIRECTORY, "data", "volumes.json")


class VolumeControl:
    """
    Controls the volume levels for the assistant, spotify, reminders and alarms.
    This remembers changes made by voice, so a restart doesn't change them.
    """

    CATEGORIES = ("general", "alarm", "reminder")

    # Starting levels for a machine which has never had a volume change. "reminder"
    # is None on purpose: with no level of its own, it follows the general one.
    DEFAULT_LEVELS = {"general": 0.7, "alarm": 1.0, "reminder": None}

    def __init__(self, config=None, on_general_change: Optional[Callable[[float], None]] = None):
        """
        Args:
            config: Configs loaded from config.yaml, for the step size.
            on_general_change: Called with the new level whenever the general
                               volume changes, so Spotify can be brought in line.
        """
        sounds_config = config.section("sounds") if config else {}
        self.on_general_change = on_general_change

        # Fixed step size for volume controls, as a percentage point.
        self.step = self._clamp_step(sounds_config.get("volume_step", 10))

        self._levels: Dict[str, Optional[float]] = dict(self.DEFAULT_LEVELS)

        # Anything the user has changed by voice overrides those defaults.
        self._load()

        logger.info(f"Volume: general {self.percent('general')}%, "
                    f"alarm {self.percent('alarm')}%, reminder {self.percent('reminder')}%")

    @staticmethod
    def _clamp_step(step) -> float:
        """Keeps the step sensible, between 1 and 50 percentage points."""
        try:
            return max(1.0, min(50.0, float(step)))
        except (TypeError, ValueError):
            return 10.0

    @staticmethod
    def _clamp(level) -> float:
        """Keeps a level within 0 and 1, so playback can never be asked to distort."""
        try:
            return max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return 0.7

    def _load(self):
        """Restores levels saved by a previous run, ignoring a missing or broken file."""
        if not os.path.exists(VOLUMES_FILE):
            return

        try:
            with open(VOLUMES_FILE) as f:
                saved = json.load(f)
        except Exception as e:
            logger.error(f"Could not read saved volumes, using defaults: {e}")
            return

        for category in self.CATEGORIES:
            if category in saved:
                value = saved[category]
                # None is meaningful for the reminder level, so it's kept as-is.
                self._levels[category] = None if value is None else self._clamp(value)

    def _save(self):
        """
        Writes the levels to disk.

        Written to a temporary file and renamed, which is atomic on Linux, so an
        interrupted write can't leave a corrupt file behind.
        """
        try:
            os.makedirs(os.path.dirname(VOLUMES_FILE), exist_ok=True)

            handle, temp_path = tempfile.mkstemp(dir=os.path.dirname(VOLUMES_FILE),
                                                 prefix=".volumes-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w") as f:
                    json.dump(self._levels, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, VOLUMES_FILE)
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
        except Exception as e:
            # Saving is best-effort - a failure here shouldn't stop the volume
            # working for the rest of this session.
            logger.error(f"Could not save volumes: {e}")

    def get(self, category: str = "general") -> float:
        """
        The level for a kind of sound, as a number between 0 and 1.

        Args:
            category: 'general', 'alarm' or 'reminder'.

        Returns:
            The level to multiply audio by. The reminder level falls back to the
            general one when the user hasn't set it separately.
        """
        level = self._levels.get(category)
        if level is None:
            return self._levels["general"]
        return level

    def percent(self, category: str = "general") -> int:
        """The level as a percentage, for saying out loud."""
        return int(round(self.get(category) * 100))

    def set(self, category: str, percent) -> Dict:
        """
        Set a level.

        Args:
            category: 'general', 'alarm' or 'reminder'.
            percent: The new level, 0 to 100.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        if category not in self.CATEGORIES:
            return {"success": False, "message": f"I don't have a {category} volume."}

        try:
            percent = float(percent)
        except (TypeError, ValueError):
            return {"success": False, "message": "Sorry, I didn't catch what to set it to."}

        self._levels[category] = self._clamp(percent / 100.0)
        self._save()

        # Spotify is brought in line here rather than by the caller, so it can't be
        # forgotten wherever the volume happens to be changed from.
        if category == "general" and self.on_general_change:
            try:
                self.on_general_change(self._levels["general"])
            except Exception as e:
                logger.warning(f"Could not pass the volume change on: {e}")

        new_percent = self.percent(category)
        logger.info(f"{category.capitalize()} volume set to {new_percent}%")

        if category == "general":
            return {"success": True, "message": f"Volume {new_percent} percent."}
        return {"success": True, "message": f"{category.capitalize()} volume {new_percent} percent."}

    def adjust(self, category: str, change_percent) -> Dict:
        """
        Move a level up or down, for "turn it up a bit" rather than a specific number.

        Args:
            category: 'general', 'alarm' or 'reminder'.
            change_percent: How much to change by, positive or negative.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        if category not in self.CATEGORIES:
            return {"success": False, "message": f"I don't have a {category} volume."}

        try:
            change_percent = float(change_percent)
        except (TypeError, ValueError):
            return {"success": False, "message": "Sorry, I didn't catch how much by."}

        # Adjusting the reminder level for the first time starts from whatever it
        # was following, so it doesn't jump.
        return self.set(category, self.percent(category) + change_percent)

    def reset_reminder(self) -> Dict:
        """Puts the reminder level back to following the general one."""
        self._levels["reminder"] = None
        self._save()
        logger.info("Reminder volume follows the general volume again")
        return {"success": True, "message": "Reminders will follow the main volume."}
