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


def _duration_units(text: str) -> set:
    """
    Extracts the (number, unit) pairs mentioned in a duration phrase, e.g.
    {("1", "hour"), ("30", "minute")} for "1 hour and 30 minutes".

    Used to match a spoken duration against a timer's actual length exactly,
    rather than by loose digit/word overlap (which could match "30 minutes"
    against both a 30 minute timer and a 1 hour 30 minute timer).
    """
    return set(re.findall(r"(\d+)\s*(second|minute|hour|day)", text))


def _time_of_day_candidates(text: str) -> set:
    """
    Extracts 24-hour (hour, minute) times mentioned in text, such as "5:40" or
    "5:40 pm". Requires a colon or an am/pm marker, so a bare number isn't
    mistaken for a time - "the 10 minute timer" mentions neither, so it's read
    as a duration, not "10 o'clock".

    A 12-hour time given without am/pm is ambiguous from speech alone, so both
    readings (AM and PM) are returned as candidates.
    """
    candidates = set()

    for hour_s, minute_s, meridiem in re.findall(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", text):
        hour, minute = int(hour_s), int(minute_s)
        if hour > 23 or minute > 59:
            continue
        if meridiem:
            candidates.add(((hour % 12) + (12 if meridiem == "pm" else 0), minute))
        elif hour <= 12:
            candidates.add((hour % 12, minute))
            candidates.add(((hour % 12) + 12, minute))
        else:
            candidates.add((hour, minute))

    # A bare "5 pm" / "5 am" with no colon is also a valid time reference.
    for hour_s, meridiem in re.findall(r"\b(\d{1,2})\s*(am|pm)\b", text):
        hour = int(hour_s)
        if 1 <= hour <= 12:
            candidates.add(((hour % 12) + (12 if meridiem == "pm" else 0), 0))

    return candidates


class Clock:
    """
    Runs countdown timers and reminders.

    A timer optionally carries a label, which turns it into a reminder: the label
    is spoken when it goes off, and can be used to refer to it later ("cancel the
    pizza timer").
    """

    def __init__(self, on_timer_finished: Optional[Callable[[str, str, bool], None]] = None,
                 check_interval: float = 0.5, config=None):
        """
        Args:
            on_timer_finished: Called with (message, sound, boost) when a timer
                               goes off. This is how the assistant speaks it - the
                               Clock itself has no audio.
            check_interval: How often the background thread checks for due timers.
        """
        self.on_timer_finished = on_timer_finished
        self.check_interval = check_interval

        self._timers: Dict[int, Dict] = {}
        self._next_id = 1
        # Guards the timers dict, since the background thread and the assistant's main loop both touch it.
        self._lock = threading.Lock()

        # An unlabelled reminder waiting to be named. Holds the exact time it was
        # originally due, so naming it later doesn't drift from re-deriving "in 20
        # minutes" a second time once a few seconds have passed. Only one at a
        # time - asking for a second reminder before naming the first replaces it.
        self._pending_reminder: Optional[Dict] = None

        # A timer/alarm rings until it's dismissed, since it's meant to be acted on
        # right away. A reminder comes back every so often for a few minutes and
        # then gives up, since it exists because the user will be doing something else.
        timers_config = config.section("timers") if config else {}
        # Each time something goes off it announces itself twice in a row - sound,
        # message, brief pause, then the same again.
        self.announcements = max(1, int(timers_config.get("announcements", 2)))
        self.repeat_gap = max(0.0, float(timers_config.get("repeat_gap", 1.5)))
        # A reminder comes back every so often until it's acknowledged, since it
        # exists because the user will be busy - but not forever, so it can be
        # asked about later rather than nagging indefinitely.
        self.reminder_rounds = max(1, int(timers_config.get("reminder_rounds", 7)))
        self.reminder_round_gap = max(1.0, float(timers_config.get("reminder_round_gap", 30)))
        # A timer/alarm never gives up on its own, so this is just the breather
        # between rounds while it keeps going - not a cap like reminder_round_gap.
        self.timer_repeat_gap = max(0.5, float(timers_config.get("timer_repeat_gap", 3.0)))
        # How long a timer/alarm can ring unanswered before it jumps to full volume,
        # regardless of the configured alarm level. Reminders never do this - they
        # keep their own, gentler volume throughout.
        self.timer_volume_boost_after = max(0.0, float(timers_config.get("timer_volume_boost_after", 120)))

        # Timers which have gone off and are waiting to be repeated or dismissed.
        self._ringing: List[Dict] = []
        # Timers which were never acknowledged, so the user can ask what they missed.
        self._unacknowledged: List[Dict] = []

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
                    sound = "announcement_sound.wav" if timer["label"] else "timer_beep.wav"
                    self._announce(f"While I was off: {base}. That was {late} ago.", sound)

            with self._lock:
                for timer_id, timer in list(self._timers.items()):
                    if timer["due_at"] <= now:
                        due.append(self._timers.pop(timer_id))
                if due:
                    self._save()

            for timer in due:
                # Registered as ringing before it speaks, so dismissing it part way
                # through stops it immediately.
                rounds, gap = self._repeat_settings(timer)
                entry = {"timer": timer, "left": None if rounds is None else rounds - 1,
                        "gap": gap, "next_at": now + gap,
                        # Kept fixed for as long as this entry rings, so the volume
                        # boost below can tell how long it's been going regardless
                        # of how many rounds that took.
                        "first_rung_at": now}
                with self._lock:
                    self._ringing.append(entry)

                self._sound_off(timer)

                # Timed from when it actually finished speaking, not from when it
                # was due. An announcement held back while the user was talking to
                # the assistant would otherwise be followed immediately by the next
                # one, bunching them together.
                entry["next_at"] = time.time() + gap

                # rounds is None for a timer/alarm, which never gives up on its own -
                # see _repeat_settings.
                if rounds is not None and rounds <= 1:
                    with self._lock:
                        finished = entry in self._ringing
                        if finished:
                            self._ringing.remove(entry)
                    if finished:
                        self._record_unacknowledged(timer)

            # Repeats anything which went off and hasn't been dismissed yet.
            repeat_now = []
            with self._lock:
                for entry in list(self._ringing):
                    if entry["next_at"] <= now:
                        self._ringing.remove(entry)
                        repeat_now.append(entry)

            for entry in repeat_now:
                if entry["left"] is not None:
                    entry["left"] -= 1
                # left of None means it never gives up on its own - a timer/alarm
                # rings until it's actually dismissed, rather than for a fixed count.
                still_going = entry["left"] is None or entry["left"] > 0

                # Once a timer or alarm (not a reminder - those keep their own,
                # gentler volume) has gone unanswered for a couple of minutes, it's
                # worth making sure it's actually being heard.
                boost = (not entry["timer"]["label"]
                        and time.time() - entry["first_rung_at"] >= self.timer_volume_boost_after)

                # Put back before it speaks, so it can be dismissed mid-round.
                with self._lock:
                    self._ringing.append(entry)
                self._sound_off(entry["timer"], repeat=True, boost=boost)

                if still_going:
                    # Timed from when it finished speaking, so a round delayed by a
                    # conversation doesn't run straight into the next one.
                    entry["next_at"] = time.time() + entry["gap"]
                else:
                    # Its last go, and still nothing back, so it's noted as missed.
                    with self._lock:
                        finished = entry in self._ringing
                        if finished:
                            self._ringing.remove(entry)
                    if finished:
                        self._record_unacknowledged(entry["timer"])

            time.sleep(self.check_interval)

    def _repeat_settings(self, timer: Dict):
        """
        How persistently a timer should keep announcing itself.

        Args:
            timer: The timer which finished.

        Returns:
            Tuple of (how many rounds of announcements, seconds between rounds).
            None for the rounds means it never gives up on its own: a plain timer
            (and, once built, an alarm) exists to be acted on right away, so it
            keeps going - with barely a pause between rounds - until it's actually
            dismissed. A reminder exists because the user will likely be doing
            something else, so it comes back every so often for a while and then
            gives up, rather than nagging indefinitely.
        """
        if timer["label"]:
            return self.reminder_rounds, self.reminder_round_gap
        return None, self.timer_repeat_gap

    def _sound_off(self, timer: Dict, repeat: bool = False, boost: bool = False):
        """
        Announces a timer which has come due.

        Announced twice in a row - sound, message, brief pause, then the same again
        - so a moment's inattention doesn't mean missing it entirely.

        Args:
            timer: The timer which finished.
            repeat: True if this is a later round, which doesn't change the wording:
                    the user hasn't heard it yet, or they'd have dismissed it.
            boost: True once a timer/alarm has been ringing unanswered long enough
                   that it should jump to full volume, regardless of the configured
                   alarm level. Never set for a reminder - see _repeat_settings.
        """
        # A labelled timer is a reminder, so the label is the message. It's
        # introduced rather than spoken bare, since a phrase out of nowhere isn't
        # obviously a reminder to someone across the room.
        if timer["label"]:
            message = f"This is a reminder: {timer['label']}"
            sound = "announcement_sound.wav"
        else:
            message = f"Timer for {format_duration(timer['duration'])}."
            # A plain timer (and, once built, an alarm) uses its own more
            # insistent sound rather than the general announcement chime, since
            # it's something to act on right away rather than hear about eventually.
            sound = "timer_beep.wav"

        for announcement in range(self.announcements):
            # Stops mid-round if the user dismisses it, rather than talking over them.
            if announcement and not self._is_ringing(timer):
                return
            self._announce(message, sound, boost)
            if announcement < self.announcements - 1:
                time.sleep(self.repeat_gap)

    def _is_ringing(self, timer: Dict) -> bool:
        """Whether a timer is still waiting to be acknowledged."""
        with self._lock:
            return any(entry["timer"] is timer for entry in self._ringing)

    def _record_unacknowledged(self, timer: Dict):
        """
        Notes a timer which went off without the user acknowledging it, so they can
        ask what they missed later.

        Args:
            timer: The timer which was never dismissed.
        """
        with self._lock:
            timer["missed_at"] = time.time()
            self._unacknowledged.append(timer)
            # Only the recent ones are worth keeping, so this can't grow forever.
            self._unacknowledged = self._unacknowledged[-10:]

    # Words which mean "make it stop" when something is going off. Kept deliberately
    # broad, since anything said to a nagging reminder is meant to silence it.
    DISMISSAL_WORDS = {
        "stop", "stopped", "cancel", "cancelled", "dismiss", "dismissed", "okay", "ok",
        "alright", "right", "fine", "yes", "yep", "yeah", "yep", "done", "finished",
        "enough", "quiet", "shush", "shut", "off", "thanks", "thank", "got", "heard",
        "understood", "silence", "mute", "no",
    }

    def is_ringing(self) -> bool:
        """Whether a timer or reminder is going off and waiting to be acknowledged."""
        with self._lock:
            return bool(self._ringing)

    @classmethod
    def looks_like_dismissal(cls, text: str) -> bool:
        """
        Whether something said while a timer is going off was meant to stop it.

        Only used when something is actually ringing, so it can afford to be
        generous: nearly anything short said to a reminder means "I've heard you".
        Longer sentences are left to the model, since those are more likely to be a
        real request than an acknowledgement.

        Args:
            text: What the user said.

        Returns:
            True if it should be treated as dismissing the alarm.
        """
        words = re.findall(r"[a-z']+", text.lower())
        if not words or len(words) > 5:
            return False
        return bool(set(words) & cls.DISMISSAL_WORDS)

    def dismiss(self) -> Dict:
        """
        Acknowledges anything currently going off, stopping it repeating.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._lock:
            ringing = self._ringing
            self._ringing = []
            # Dismissing also clears what was missed, since the user has now heard it.
            missed = len(self._unacknowledged)
            self._unacknowledged = []

        if not ringing and not missed:
            return {"success": False, "message": "Nothing's going off."}

        logger.info(f"Dismissed {len(ringing)} ringing timer(s)")
        return {"success": True, "message": "Okay.", "chime": True}

    def missed(self) -> Dict:
        """
        Reports timers which went off without being acknowledged.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._lock:
            missed = list(self._unacknowledged)
            self._unacknowledged = []

        if not missed:
            return {"success": True, "message": "You haven't missed anything."}

        now = time.time()
        described = [f"{self._describe(t)}, {format_duration(now - t['missed_at'])} ago"
                     for t in missed]
        return {"success": True, "message": "You missed " + "; ".join(described) + "."}

    def _announce(self, message: str, sound: str = "announcement_sound.wav", boost: bool = False):
        """
        Passes a message to the callback which speaks it.

        Args:
            message: What to say.
            sound: Which sound effect should introduce the message.
            boost: True if it should be spoken at full volume regardless of the
                   configured alarm level - see _sound_off.
        """
        logger.info(f"Timer finished: {message}")

        if self.on_timer_finished:
            try:
                self.on_timer_finished(message, sound, boost)
            except Exception as e:
                # A failed announcement must not kill the timer thread.
                logger.error(f"Timer announcement failed: {e}")

    def set_timer(self, duration_seconds: int, label: Optional[str] = None,
                  is_reminder: bool = False) -> Dict:
        """
        Start a countdown timer, or a reminder if it carries a label.

        Args:
            duration_seconds: How long to count down for.
            label: What to say when it goes off. Without one it's a plain timer,
                   with one it's a reminder.
            is_reminder: True when the user explicitly asked for a "reminder" (or
                         to be "reminded"), even if they haven't named it yet. A
                         reminder is identified by that name when it's cancelled
                         or announced, so one can't be created without one - if
                         this is True and no label was given, the user is asked
                         to name it instead of a nameless reminder being set.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        try:
            duration_seconds = int(duration_seconds)
        except (TypeError, ValueError):
            return {"success": False, "message": "Sorry, I didn't catch how long for."}

        if duration_seconds <= 0:
            return {"success": False, "message": "That timer needs to be longer than nothing."}

        due_at = time.time() + duration_seconds

        # A reminder is identified by its name, so one asked for without a name
        # can't actually be set yet. The exact time is remembered so naming it
        # later (see name_pending_reminder) doesn't have to be re-derived from
        # how long ago this was asked, which would drift by however long that
        # takes.
        if is_reminder and not label:
            with self._lock:
                self._pending_reminder = {"due_at": due_at, "duration": duration_seconds}
            return {"success": False, "message": "What would you like to name this reminder?"}

        with self._lock:
            # Two timers due in the same minute would be indistinguishable once
            # they're both running, so this is caught up front rather than
            # leaving the user with two timers they can't tell apart.
            due_local = time.localtime(due_at)
            for existing in self._timers.values():
                existing_local = time.localtime(existing["due_at"])
                if (due_local.tm_year, due_local.tm_yday, due_local.tm_hour, due_local.tm_min) == \
                   (existing_local.tm_year, existing_local.tm_yday, existing_local.tm_hour, existing_local.tm_min):
                    return {"success": False,
                            "message": f"You already have a {self._describe(existing)} "
                                       f"for {self._spoken_due(existing['due_at'])}."}

            timer_id = self._next_id
            self._next_id += 1
            self._timers[timer_id] = {
                "id": timer_id,
                "label": label,
                "duration": duration_seconds,
                "due_at": due_at,
            }
            # Saved immediately, so the timer survives a restart moments later.
            self._save()

        spoken_duration = format_duration(duration_seconds)
        logger.info(f"Timer set for {spoken_duration}" + (f" ({label})" if label else ""))

        if label:
            return {"success": True, "message": f"I'll remind you in {spoken_duration}."}
        return {"success": True, "message": f"Timer set for {spoken_duration}."}

    def name_pending_reminder(self, label: Optional[str] = None) -> Dict:
        """
        Finishes creating the reminder which was most recently asked to be named
        (see set_timer's is_reminder), using the exact time it was originally due
        rather than reworking "in 20 minutes" a second time now that a few
        seconds have passed answering the question.

        Args:
            label: What to say when it goes off.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        if not label:
            return {"success": False, "message": "What would you like to name this reminder?"}

        with self._lock:
            pending = self._pending_reminder
            self._pending_reminder = None

        if not pending:
            # Nothing's actually waiting on a name - it was already named, or
            # this was called without the question having been asked first.
            return {"success": False, "message": "I'm not waiting on a name for anything right now."}

        remaining = pending["due_at"] - time.time()
        if remaining <= 0:
            return {"success": False, "message": "Sorry, that reminder's time has already passed."}

        return self.set_timer(remaining, label, is_reminder=True)

    def describe_all(self) -> List[str]:
        """
        Plain-text descriptions of every running timer and reminder - the same
        wording list_timers would speak, but as a list rather than one sentence.

        Used to give something else (the LLM) visibility of what's actually
        running, so it can be matched against even when speech-to-text mangles
        a label.
        """
        with self._lock:
            timers = sorted(self._timers.values(), key=lambda t: t["due_at"])
        return [self._list_entry(timer) for timer in timers]

    def list_timers(self) -> Dict:
        """
        Report the timers and reminders which are still running, and the clock
        time each is due to go off.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._lock:
            timers = sorted(self._timers.values(), key=lambda t: t["due_at"])

        if not timers:
            return {"success": True, "message": "You don't have any timers running."}

        described = [self._list_entry(timer) for timer in timers]

        if len(described) == 1:
            return {"success": True, "message": f"One timer: {described[0]}."}
        return {"success": True, "message": f"{len(described)} timers: " + ", ".join(described) + "."}

    def _spoken_due(self, due_at: float) -> str:
        """
        Formats an absolute due time for speech, e.g. "5:40 PM", or "5:40 PM on
        Tuesday" if it's due on a different day to today.
        """
        due = time.localtime(due_at)
        now = time.localtime()
        spoken = time.strftime("%-I:%M %p", due)
        if (due.tm_year, due.tm_yday) != (now.tm_year, now.tm_yday):
            spoken += time.strftime(" on %A", due)
        return spoken

    def _list_entry(self, timer: Dict) -> str:
        """
        Describes one timer or reminder for the "list timers" response: its name
        (its label for a reminder, its length for a plain timer), followed by the
        clock time it's due - e.g. "10 minute timer for 5:40 PM" or "pizza for
        5:40 PM".
        """
        name = timer["label"] if timer["label"] else self._describe(timer)
        return f"{name} for {self._spoken_due(timer['due_at'])}"

    def _deleted_message(self, timer: Dict) -> str:
        """Spoken confirmation that a timer or reminder has been cancelled."""
        kind = "Reminder" if timer["label"] else "Timer"
        return f"{kind} for {self._spoken_due(timer['due_at'])} deleted."

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
        Cancel a timer or reminder.

        A timer can be identified by its label, by how long it was set for (e.g.
        "the ten-minute timer"), or by the clock time it's due to go off (e.g.
        "the 5:40 one"). If more than one running timer matches what was said,
        the user is asked which one they meant rather than guessing.

        Args:
            label: Which timer to cancel, matched loosely since it comes from speech.
            cancel_all: Cancel every timer, regardless of label.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        # Something going off right now is dismissed rather than cancelled, since
        # "cancel that" while a reminder is sounding means "stop it", and there's
        # no pending timer left to cancel by then.
        with self._lock:
            ringing = bool(self._ringing)
        if ringing:
            return self.dismiss()

        with self._lock:
            if not self._timers:
                return {"success": False, "message": "There aren't any timers to cancel."}

            if cancel_all:
                count = len(self._timers)
                self._timers.clear()
                self._save()
                if count == 1:
                    return {"success": True, "message": "Timer cancelled."}
                return {"success": True, "message": f"All {count} timers and reminders deleted."}

            # Asks the user which timer to cancel if they didn't specify one.
            if not label:
                if len(self._timers) == 1:
                    timer = self._timers.popitem()[1]
                    self._save()
                    return {"success": True, "message": self._deleted_message(timer)}

                options = ", ".join(self._describe(t) for t in self._timers.values())
                return {"success": False,
                        "message": f"You have {len(self._timers)} timers: {options}. Which one?"}

            wanted = label.strip().lower()

            # The next upcoming timer / reminder can be cancelled by saying "cancel the next ...".
            if any(word in wanted for word in ("next", "upcoming", "soonest", "first")):
                if "reminder" in wanted:
                    candidates = [t for t in self._timers.values() if t["label"]]
                elif "timer" in wanted:
                    candidates = [t for t in self._timers.values() if not t["label"]]
                else:
                    candidates = list(self._timers.values())

                # Fallback incase a "reminder" was set without a label.
                candidates = candidates or list(self._timers.values())

                if candidates:
                    timer = min(candidates, key=lambda t: t["due_at"])
                    self._timers.pop(timer["id"], None)
                    self._save()
                    return {"success": True, "message": self._deleted_message(timer)}

            wanted_units = _duration_units(wanted)
            wanted_times = _time_of_day_candidates(wanted)

            # Every timer matching what was said, rather than stopping at the first
            # one - two 10 minute timers both match "the 10 minute timer" equally,
            # so which one gets cancelled must be asked rather than guessed.
            matches = []
            for timer in self._timers.values():
                timer_label = (timer["label"] or "").lower()
                duration_text = format_duration(timer["duration"]).lower()
                due_local = time.localtime(timer["due_at"])

                matches_label = bool(timer_label) and (wanted in timer_label or timer_label in wanted)
                # Unlabelled timers are matched on their length instead. Every number/unit pair
                # the user said has to match the timer's actual duration exactly, so "the 30
                # minute timer" can't also grab a 1 hour 30 minute timer just for sharing the
                # digit "30" and the word "minute".
                matches_duration = (not timer_label
                                    and bool(wanted_units)
                                    and wanted_units == _duration_units(duration_text))
                # A timer or reminder can also be identified by the clock time it's
                # due to go off, e.g. "cancel the 5:40 one".
                matches_time = (bool(wanted_times)
                                and (due_local.tm_hour, due_local.tm_min) in wanted_times)

                if matches_label or matches_duration or matches_time:
                    matches.append(timer)

            if len(matches) == 1:
                timer = matches[0]
                self._timers.pop(timer["id"], None)
                self._save()
                return {"success": True, "message": self._deleted_message(timer)}

            if len(matches) > 1:
                # More than one running timer matches what was said, so it asks
                # rather than cancelling whichever happened to be found first.
                kind = self._describe(matches[0])
                options = ", ".join(f"one for {self._spoken_due(t['due_at'])}" for t in matches)
                return {"success": False,
                        "message": f"There are {len(matches)} {kind}s: {options}. "
                                   f"Which would you like to cancel?"}

            # Nothing matched, but with a single timer running there's nothing else
            # the user could have meant, so it deletes the timer.
            if len(self._timers) == 1:
                timer = self._timers.popitem()[1]
                self._save()
                return {"success": True, "message": self._deleted_message(timer)}

            return {"success": False, "message": f"I couldn't find a timer for {label}."}

    def stop(self):
        """Stop the background thread, used when the assistant shuts down."""
        self._running = False
        logger.info("Clock stopped")
