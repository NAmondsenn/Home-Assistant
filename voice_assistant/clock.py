"""
Clock Module
Handles countdown timers and reminders.

Timers run on a background thread, so they can go off while the assistant is busy
listening for the wake word. When one finishes, it calls the callback it was given,
which is what actually speaks the announcement.
"""

import os
import re
import json
import logging
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional

# Logger setup
logger = logging.getLogger(__name__)

# Resolves paths relative to the project root, so saved timers live inside the project like everything else.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMERS_FILE = os.path.join(PROJECT_DIRECTORY, "data", "timers.json")

# A timer which came due while the assistant was off is still announced if it was
# recent, anything older is ignored.
MISSED_TIMER_GRACE_SECONDS = 3600
# Waits until NTP corrects the time, so the clock is accurate on startup.
PLAUSIBLE_TIME_AFTER = 1735689600


def format_duration(seconds: int) -> str:
    """
    Turns a number of seconds into a more human form.

    Args:
        seconds: The duration in seconds.

    Returns:
        A spoken-style description of the duration.
    """
    seconds = int(round(seconds))
    if seconds <= 0:
        return "no time"

    # Largest unit first, so the two most significant ones can be picked out below.
    units = (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1))

    parts = []
    remaining = seconds
    for name, size in units:
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count} {name}{'s' if count != 1 else ''}")

    # Only the two largest units are spoken.
    parts = parts[:2]

    return " and ".join(parts)


class Clock:
    """
    Runs countdown timers and reminders.

    A timer optionally carries a label, which turns it into a reminder: the label
    is spoken when it goes off, and can be used to refer to it later ("cancel the
    pizza timer").
    """

    def __init__(self, on_timer_finished: Optional[Callable[[str], None]] = None,
                 check_interval: float = 0.5):
        """
        Args:
            on_timer_finished: Called with the announcement text when a timer goes
                               off. This is how the assistant speaks it - the Clock
                               itself has no audio.
            check_interval: How often the background thread checks for due timers.
        """
        self.on_timer_finished = on_timer_finished
        self.check_interval = check_interval

        self._timers: Dict[int, Dict] = {}
        self._next_id = 1
        # Guards the timers dict, since the background thread and the assistant's main loop both touch it.
        self._lock = threading.Lock()

        # Restores timers saved before the last shutdown, so they survive restarts.
        self._missed: List[Dict] = []
        self._load()

        self._running = True
        # A daemon thread so it never keeps the assistant alive on shutdown.
        self._thread = threading.Thread(target=self._run, daemon=True, name="clock")
        self._thread.start()

        logger.info("Clock started")

    def _save(self):
        """
        Writes the pending timers to the disk.

        Written to a temporary file and then renamed, which is atomic on Linux, so
        losing power mid-write leaves the previous file intact rather than a
        half-written one. The caller is expected to already hold the lock.
        """
        try:
            os.makedirs(os.path.dirname(TIMERS_FILE), exist_ok=True)

            # Written into the same directory as the target, since a rename is only atomic within one filesystem.
            handle, temp_path = tempfile.mkstemp(dir=os.path.dirname(TIMERS_FILE),
                                                 prefix=".timers-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w") as f:
                    json.dump({"next_id": self._next_id,
                               "timers": list(self._timers.values())}, f)
                    # Flushed to the disk itself, not just the OS cache, so a power
                    # cut straight after saving can't lose the timer.
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, TIMERS_FILE)
            except Exception:
                # Never leaves the temporary file behind if the write failed.
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
        except Exception as e:
            # Saving is best-effort: a failure here shouldn't stop a timer working
            # for the rest of this session.
            logger.error(f"Could not save timers: {e}")

    def _load(self):
        """
        Restores timers from disk.

        Timers which came due while the assistant was off are kept aside to be
        announced late, as long as they aren't older than the grace period.
        """
        if not os.path.exists(TIMERS_FILE):
            return

        try:
            with open(TIMERS_FILE) as f:
                data = json.load(f)
        except Exception as e:
            # A corrupt file shouldn't stop the assistant starting.
            logger.error(f"Could not read saved timers, ignoring them: {e}")
            return

        now = time.time()
        restored = missed = dropped = 0

        for timer in data.get("timers", []):
            # Skips anything malformed rather than trusting the file's contents.
            if not isinstance(timer, dict) or "due_at" not in timer:
                continue

            try:
                timer["due_at"] = float(timer["due_at"])
                timer["duration"] = int(timer.get("duration", 0))
                timer["id"] = int(timer.get("id", 0))
            except (TypeError, ValueError):
                continue

            label = timer.get("label")
            timer["label"] = str(label) if label else None

            if timer["due_at"] > now:
                self._timers[timer["id"]] = timer
                restored += 1
            elif now - timer["due_at"] <= MISSED_TIMER_GRACE_SECONDS:
                # Recently missed, so it's still worth mentioning once started.
                self._missed.append(timer)
                missed += 1
            else:
                dropped += 1

        # Keeps IDs unique across restarts, so a restored timer can't be confused with a new one.
        self._next_id = max([int(data.get("next_id", 1))]
                            + [t["id"] + 1 for t in self._timers.values()] or [1])

        if restored or missed or dropped:
            logger.info(f"Restored {restored} timer(s), {missed} missed, {dropped} too old")

    def _run(self):
        """
        Background loop which fires timers as they come due.

        Callbacks are made outside the lock, so a slow announcement doesn't block
        the assistant from setting or cancelling other timers meanwhile.
        """
        while self._running:
            now = time.time()

            # Waits for the system clock to be believable before firing anything.
            # Without this, a Pi which hasn't reached an NTP server yet would think
            # every pending timer is overdue and announce them all at once.
            if now < PLAUSIBLE_TIME_AFTER:
                logger.warning("System clock not set yet, holding timers")
                time.sleep(self.check_interval)
                continue

            due = []

            # Timers which came due while the assistant was off are announced first,
            # with a note that they're late so the user isn't misled.
            if self._missed:
                with self._lock:
                    missed, self._missed = self._missed, []
                for timer in missed:
                    late = format_duration(now - timer["due_at"])
                    base = timer["label"] or f"your {format_duration(timer['duration'])} timer"
                    self._announce(f"While I was off: {base}. That was {late} ago.")

            with self._lock:
                for timer_id, timer in list(self._timers.items()):
                    if timer["due_at"] <= now:
                        due.append(self._timers.pop(timer_id))
                if due:
                    self._save()

            for timer in due:
                self._announce(f"Reminder: {timer['label']}" if timer["label"] else
                               f"Your {format_duration(timer['duration'])} timer is up.")

            time.sleep(self.check_interval)

    def _announce(self, message: str):
        """
        Passes a message to the callback which speaks it.

        Args:
            message: What to say.
        """
        logger.info(f"Timer finished: {message}")

        if self.on_timer_finished:
            try:
                self.on_timer_finished(message)
            except Exception as e:
                # A failed announcement must not kill the timer thread.
                logger.error(f"Timer announcement failed: {e}")

    def set_timer(self, duration_seconds: int, label: Optional[str] = None) -> Dict:
        """
        Start a countdown timer.

        Args:
            duration_seconds: How long to count down for.
            label: What to say when it goes off. Without one it's a plain timer,
                   with one it's a reminder.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        try:
            duration_seconds = int(duration_seconds)
        except (TypeError, ValueError):
            return {"success": False, "message": "Sorry, I didn't catch how long for."}

        if duration_seconds <= 0:
            return {"success": False, "message": "That timer needs to be longer than nothing."}

        with self._lock:
            timer_id = self._next_id
            self._next_id += 1
            self._timers[timer_id] = {
                "id": timer_id,
                "label": label,
                "duration": duration_seconds,
                "due_at": time.time() + duration_seconds,
            }
            # Saved immediately, so the timer survives a restart moments later.
            self._save()

        spoken_duration = format_duration(duration_seconds)
        logger.info(f"Timer set for {spoken_duration}" + (f" ({label})" if label else ""))

        if label:
            return {"success": True, "message": f"I'll remind you in {spoken_duration}."}
        return {"success": True, "message": f"Timer set for {spoken_duration}."}

    def list_timers(self) -> Dict:
        """
        Report the timers which are still running, and how long is left on each.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._lock:
            timers = sorted(self._timers.values(), key=lambda t: t["due_at"])

        if not timers:
            return {"success": True, "message": "You don't have any timers running."}

        now = time.time()
        # Each timer is named the same way it can be cancelled, so "the 2 minute
        # timer" is both what the user hears and what they can say back.
        described = [f"{self._describe(timer)}, "
                     f"{format_duration(max(0, timer['due_at'] - now))} left"
                     for timer in timers]

        if len(described) == 1:
            return {"success": True, "message": f"One timer: {described[0]}."}
        return {"success": True, "message": f"{len(described)} timers: " + "; ".join(described) + "."}

    def _describe(self, timer: Dict) -> str:
        """
        Names a timer for speech: its label if it has one, otherwise its length.

        The length is made singular ("10 minute timer", not "10 minutes timer"),
        since it's being used as a description rather than a duration.
        """
        if timer["label"]:
            return f"{timer['label']} reminder"

        length = format_duration(timer["duration"])
        for unit in ("seconds", "minutes", "hours", "days"):
            length = length.replace(unit, unit[:-1])
        return f"{length} timer"

    def cancel_timer(self, label: Optional[str] = None, cancel_all: bool = False) -> Dict:
        """
        Cancel a timer.

        A timer can be identified by its label, or for timers set without one,
        by how long it was set for, e.g. "the ten-minute timer".

        Args:
            label: Which timer to cancel, matched loosely since it comes from speech.
            cancel_all: Cancel every timer, regardless of label.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._lock:
            if not self._timers:
                return {"success": False, "message": "There aren't any timers to cancel."}

            if cancel_all:
                count = len(self._timers)
                self._timers.clear()
                self._save()
                if count == 1:
                    return {"success": True, "message": "Timer cancelled."}
                return {"success": True, "message": f"Cancelled all {count} timers."}

            # Asks the user which timer to cancel if they didn't specify one.
            if not label:
                if len(self._timers) == 1:
                    timer = self._timers.popitem()[1]
                    self._save()
                    return {"success": True, "message": f"Cancelled the {self._describe(timer)}."}

                options = ", ".join(self._describe(t) for t in self._timers.values())
                return {"success": False,
                        "message": f"You have {len(self._timers)} timers: {options}. Which one?"}

            wanted = label.strip().lower()

            wanted_numbers = set(re.findall(r"\d+", wanted))

            for timer_id, timer in list(self._timers.items()):
                timer_label = (timer["label"] or "").lower()
                duration_text = format_duration(timer["duration"]).lower()

                matches_label = timer_label and (wanted in timer_label or timer_label in wanted)
                # Unlabelled timers are matched on their length instead, which needs both the number and the unit to agree.
                matches_duration = (not timer_label
                                    and wanted_numbers & set(re.findall(r"\d+", duration_text))
                                    and any(unit in wanted for unit in
                                            ("second", "minute", "hour", "day")))

                if matches_label or matches_duration:
                    self._timers.pop(timer_id)
                    self._save()
                    return {"success": True, "message": f"Cancelled the {self._describe(timer)}."}

            return {"success": False, "message": f"I couldn't find a timer for {label}."}

    def stop(self):
        """Stop the background thread, used when the assistant shuts down."""
        self._running = False
        logger.info("Clock stopped")
