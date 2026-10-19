"""
Clock Module
Handles countdown timers, reminders and alarms.

All three run on a background thread, so they can go off while the assistant is
busy listening for the wake word. When one comes due, it calls the callback it was
given, which is what actually speaks the announcement.

Timers and reminders count down from a duration and are kept together; an alarm is
scheduled against the wall clock and can repeat, so it's kept separately (see
_alarms) and rings to its own, more insistent pattern.
"""

import os
import re
import json
import logging
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

# Logger setup
logger = logging.getLogger(__name__)

# Resolves paths relative to the project root, so saved timers live inside the project like everything else.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMERS_FILE = os.path.join(PROJECT_DIRECTORY, "data", "timers.json")
ALARMS_FILE = os.path.join(PROJECT_DIRECTORY, "data", "alarms.json")

# The days an alarm's repeat pattern can mean, as weekday() indices (0=Monday).
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Every other way a repeat pattern's days might arrive, mapped to the days meant:
# abbreviations ("mon", "thurs"), and the words for whole groups of days. "day" is
# what's left of "every day" once the "every" has been taken off.
REPEAT_WORDS = {name[:3]: (name,) for name in WEEKDAY_NAMES}
REPEAT_WORDS.update({
    "tues": ("tuesday",), "weds": ("wednesday",), "thur": ("thursday",), "thurs": ("thursday",),
    "weekday": WEEKDAY_NAMES[:5], "weekdays": WEEKDAY_NAMES[:5],
    "weekend": WEEKDAY_NAMES[5:], "weekends": WEEKDAY_NAMES[5:],
    "daily": WEEKDAY_NAMES, "everyday": WEEKDAY_NAMES, "day": WEEKDAY_NAMES,
})

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


def spoken_clock_time(hour: int, minute: int) -> str:
    """
    Formats a time of day for speech on the 12-hour clock, e.g. "7:05 AM".

    Built by hand rather than with strftime's "%-I", which drops the leading zero
    but only exists on Linux and macOS - Windows rejects it outright, which would
    stop this module (and its tests) running anywhere but the Pi.
    """
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def parse_repeat(repeat):
    """
    Turns an alarm's repeat pattern, however it was given, into the one form the
    clock keeps: None for a one-off, "daily", "weekdays", "weekends", or a list of
    weekday names in order (e.g. ["monday", "wednesday"]).

    Anything it can't read raises ValueError rather than being dropped. Quietly
    ignoring a day it didn't recognise ("mon", "mondays", "every day") used to leave
    an alarm with no days at all, which then rang every day while being described
    as a one-off.

    Args:
        repeat: None, a preset like "weekdays", a comma or "and" separated string
                of days ("tuesdays and thursdays"), or a list of day names.

    Returns:
        The repeat pattern in its stored form.
    """
    if repeat is None or repeat == "":
        return None

    if isinstance(repeat, str):
        words = re.split(r"[,/&]|\band\b", repeat.lower())
    elif isinstance(repeat, (list, tuple)):
        words = [str(word).lower() for word in repeat]
    else:
        raise ValueError(f"Unrecognised repeat pattern: {repeat!r}")

    days = set()
    for word in words:
        word = word.strip()
        if word.startswith("every "):
            word = word[len("every "):].strip()
        if not word:
            continue
        # A plural day ("mondays") means the same as the day itself.
        if word.endswith("s") and word[:-1] in WEEKDAY_NAMES:
            word = word[:-1]
        if word in WEEKDAY_NAMES:
            days.add(word)
        elif word in REPEAT_WORDS:
            days.update(REPEAT_WORDS[word])
        else:
            raise ValueError(f"Unrecognised day: {word!r}")

    if not days:
        raise ValueError(f"No days in repeat pattern: {repeat!r}")

    # Stored in the shortest form that means the same thing, so it's described
    # naturally ("every weekday" rather than listing Monday to Friday).
    ordered = [day for day in WEEKDAY_NAMES if day in days]
    if len(ordered) == 7:
        return "daily"
    if ordered == list(WEEKDAY_NAMES[:5]):
        return "weekdays"
    if ordered == list(WEEKDAY_NAMES[5:]):
        return "weekends"
    return ordered


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

    def __init__(self, on_timer_finished: Optional[Callable[..., None]] = None,
                 check_interval: float = 0.5, config=None, start: bool = True):
        """
        Args:
            on_timer_finished: Called with (message, sound, boost, still_wanted=...)
                               when a timer goes off. This is how the assistant speaks
                               it - the Clock itself has no audio.
            check_interval: How often the background thread checks for due timers.
            start: Whether to start the background thread. Only the tests turn
                   this off, so they can step the clock along with _tick() instead.
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

        # Each of the three is as persistent as its purpose needs, and none of them
        # ring indefinitely - past a certain point the moment has passed and it's
        # just noise. A timer keeps going with barely a pause, since it's meant to
        # be acted on right away. A reminder comes back every so often, since it
        # exists because the user will be doing something else. An alarm rings in
        # a few long cycles, since it has to assume you're asleep rather than busy.
        timers_config = config.section("timers") if config else {}
        # Each time something goes off it announces itself twice in a row - sound,
        # message, brief pause, then the same again.
        self.announcements = max(1, int(timers_config.get("announcements", 2)))
        self.repeat_gap = max(0.0, float(timers_config.get("repeat_gap", 1.5)))
        # A reminder comes back every so often until it's acknowledged, since it
        # exists because the user will be busy - but not forever, so it can be
        # asked about later rather than nagging indefinitely. Bounded by time
        # rather than by a round count for the same reason a timer is: a round
        # lasts as long as the label takes to say, so "pizza" and "take the
        # washing out before it rains" would otherwise run for different lengths.
        self.reminder_round_gap = max(1.0, float(timers_config.get("reminder_round_gap", 30)))
        self.reminder_give_up_after = max(1.0, float(timers_config.get("reminder_give_up_after", 300)))
        # Barely a pause for a plain timer, which is meant to be acted on now -
        # timer_give_up_after below is what actually stops it. Alarms have their
        # own pacing further down, and reminders their own above.
        self.timer_repeat_gap = max(0.5, float(timers_config.get("timer_repeat_gap", 3.0)))
        # How long a plain timer keeps ringing before it gives up.
        self.timer_give_up_after = max(1.0, float(timers_config.get("timer_give_up_after", 600)))
        # How long a plain timer can ring unanswered before it jumps to full volume,
        # regardless of the configured alarm level. Reminders never do this - they
        # keep their own, gentler volume throughout - and alarms don't need it,
        # since they start at full volume anyway.
        self.timer_volume_boost_after = max(0.0, float(timers_config.get("timer_volume_boost_after", 120)))
        # An alarm rings in cycles rather than one continuous stream: a stretch of
        # looping alarm sound, then a quiet stretch, repeated a few times, so it
        # doesn't just become background noise if you don't wake on the first go.
        self.alarm_ring_duration = max(10.0, float(timers_config.get("alarm_ring_duration", 150)))
        self.alarm_gap_duration = max(10.0, float(timers_config.get("alarm_gap_duration", 150)))
        self.alarm_max_cycles = max(1, int(timers_config.get("alarm_max_cycles", 3)))

        self._alarms: Dict[int, Dict] = {}
        self._next_alarm_id = 1

        # Timers which have gone off and are waiting to be repeated or dismissed.
        self._ringing: List[Dict] = []
        # Timers which were never acknowledged, so the user can ask what they missed.
        self._unacknowledged: List[Dict] = []

        # Restores timers saved before the last shutdown, so they survive restarts.
        self._missed: List[Dict] = []
        self._load()
        self._load_alarms()

        self._running = True
        # A daemon thread so it never keeps the assistant alive on shutdown.
        if start:
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

    def _save_alarms(self):
        """
        Writes the alarms to disk, the same atomic way _save writes timers. The
        caller is expected to already hold the lock.
        """
        try:
            os.makedirs(os.path.dirname(ALARMS_FILE), exist_ok=True)
            handle, temp_path = tempfile.mkstemp(dir=os.path.dirname(ALARMS_FILE),
                                                 prefix=".alarms-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w") as f:
                    json.dump({"next_id": self._next_alarm_id,
                               "alarms": list(self._alarms.values())}, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, ALARMS_FILE)
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
        except Exception as e:
            logger.error(f"Could not save alarms: {e}")

    def _load_alarms(self):
        """
        Restores alarms from disk.

        An alarm's saved due_at is trusted as-is, since a repeating one has it moved
        on to the next occurrence the moment it goes off (see _tick). One which came
        due while the assistant was off is mentioned once it's started, the same way
        a missed timer is, rather than being quietly moved to the following day - a
        one-off alarm is then done with, and a repeating one carries on from its
        next occurrence.
        """
        if not os.path.exists(ALARMS_FILE):
            return

        try:
            with open(ALARMS_FILE) as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Could not read saved alarms, ignoring them: {e}")
            return

        now = time.time()
        restored = missed = 0
        for alarm in data.get("alarms", []):
            if not isinstance(alarm, dict):
                continue
            try:
                hour, minute = int(alarm["hour"]), int(alarm["minute"])
                alarm_id = int(alarm.get("id", 0))
            except (TypeError, ValueError, KeyError):
                continue
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue

            # Normalised on the way in, so an alarm saved before repeat patterns were
            # checked ("every day") still repeats the way it was meant to.
            try:
                repeat = parse_repeat(alarm.get("repeat"))
            except ValueError:
                logger.warning(f"Alarm {alarm_id} had an unreadable repeat pattern "
                               f"{alarm.get('repeat')!r}, keeping it as a one-off")
                repeat = None

            try:
                due_at = float(alarm["due_at"])
            except (TypeError, ValueError, KeyError):
                # Saved without a usable due time, so the next one is worked out afresh.
                due_at = self._next_occurrence(hour, minute, repeat)

            alarm = {"id": alarm_id, "hour": hour, "minute": minute, "repeat": repeat, "due_at": due_at}

            if due_at <= now:
                # Recently missed, so it's still worth mentioning once started.
                if now - due_at <= MISSED_TIMER_GRACE_SECONDS:
                    self._missed.append({**alarm, "kind": "alarm"})
                    missed += 1
                if not repeat:
                    continue
                alarm["due_at"] = self._next_occurrence(hour, minute, repeat, after=max(due_at + 60, now))

            self._alarms[alarm_id] = alarm
            restored += 1

        self._next_alarm_id = max([int(data.get("next_id", 1))]
                                  + [a["id"] + 1 for a in self._alarms.values()] or [1])

        # Saved straight away if anything was missed, so a second restart soon after
        # doesn't mention the same missed alarm all over again.
        if missed:
            self._save_alarms()

        if restored or missed:
            logger.info(f"Restored {restored} alarm(s), {missed} missed")

    def _next_occurrence(self, hour: int, minute: int, repeat, after: Optional[float] = None) -> float:
        """
        The next epoch time an alarm with this hour/minute/repeat should fire, at
        or after the given moment (now, by default).

        Args:
            hour, minute: The time of day the alarm is set for.
            repeat: None for a one-off alarm, or a repeat pattern - see
                    _repeat_weekdays for the forms this can take.
            after: Only occurrences strictly after this moment are considered,
                   which is what lets a repeating alarm be advanced past a
                   particular firing without touching "now" at all.

        Returns:
            The next matching epoch time.
        """
        base = after if after is not None else time.time()
        candidate = datetime.fromtimestamp(base).replace(hour=hour, minute=minute,
                                                          second=0, microsecond=0)
        if candidate.timestamp() <= base:
            candidate += timedelta(days=1)

        allowed = self._repeat_weekdays(repeat)
        if allowed:
            # Bounded to a week of lookahead, so a malformed repeat pattern can't loop forever.
            for _ in range(7):
                if candidate.weekday() in allowed:
                    break
                candidate += timedelta(days=1)

        return candidate.timestamp()

    @staticmethod
    def _repeat_weekdays(repeat) -> set:
        """
        The set of weekday() indices (0=Monday..6=Sunday) a repeat pattern covers.
        An empty set means "no repeat" - just the one, already-computed occurrence.

        Args:
            repeat: None, "daily", "weekdays", "weekends", or a list of weekday
                    names (e.g. ["monday", "wednesday"]).
        """
        if repeat == "daily":
            return set(range(7))
        if repeat == "weekdays":
            return {0, 1, 2, 3, 4}
        if repeat == "weekends":
            return {5, 6}
        if isinstance(repeat, list):
            return {WEEKDAY_NAMES.index(day) for day in repeat if day in WEEKDAY_NAMES}
        return set()

    @staticmethod
    def _repeat_description(repeat) -> str:
        """Spoken description of a repeat pattern, e.g. "every weekday", or "" for none."""
        if not repeat:
            return ""
        if repeat == "daily":
            return "every day"
        if repeat == "weekdays":
            return "every weekday"
        if repeat == "weekends":
            return "every weekend"
        if isinstance(repeat, list):
            names = [day.capitalize() for day in repeat if day in WEEKDAY_NAMES]
            if not names:
                return ""
            if len(names) == 1:
                return f"every {names[0]}"
            return "every " + ", ".join(names[:-1]) + f" and {names[-1]}"
        return ""

    def _run(self):
        """Background loop which fires timers, reminders and alarms as they come due."""
        while self._running:
            self._tick(time.time())
            time.sleep(self.check_interval)

    def _tick(self, now: float):
        """
        One pass of the background loop: announces anything missed while the
        assistant was off, fires whatever has come due, and repeats anything still
        ringing. Kept separate from _run so the tests can step through it by hand.

        Callbacks are made outside the lock, so a slow announcement doesn't block
        the assistant from setting or cancelling other timers meanwhile.

        Args:
            now: The current time, as an epoch timestamp.
        """
        # Waits for the system clock to be believable before firing anything.
        # Without this, a Pi which hasn't reached an NTP server yet would think
        # every pending timer is overdue and announce them all at once.
        if now < PLAUSIBLE_TIME_AFTER:
            logger.warning("System clock not set yet, holding timers")
            return

        # Anything which came due while the assistant was off is announced first,
        # with a note that it's late so the user isn't misled. It's only mentioned,
        # not rung - by the time it's noticed, the moment's passed.
        if self._missed:
            with self._lock:
                missed, self._missed = self._missed, []
            for item in missed:
                late = format_duration(now - item["due_at"])
                if item.get("kind") == "alarm":
                    base, sound = f"your {self._describe_alarm(item)}", "announcement_sound.wav"
                else:
                    base = item["label"] or f"your {format_duration(item['duration'])} timer"
                    sound = "announcement_sound.wav" if item["label"] else "timer_beep.wav"
                self._announce(f"While I was off: {base}. That was {late} ago.", sound)

        due = []
        with self._lock:
            for timer_id, timer in list(self._timers.items()):
                if timer["due_at"] <= now:
                    due.append(self._timers.pop(timer_id))
            if due:
                self._save()

        # What rings is a copy of the alarm as it was when it came due. A repeating
        # alarm stays in the store and is moved on to its next occurrence straight
        # away, rather than once it stops ringing - otherwise a restart while it was
        # ringing (up to a quarter of an hour) would lose it from the file for good.
        due_alarms = []
        with self._lock:
            for alarm_id, alarm in list(self._alarms.items()):
                if alarm["due_at"] <= now:
                    due_alarms.append({**alarm, "kind": "alarm"})
                    if alarm.get("repeat"):
                        alarm["due_at"] = self._next_occurrence(alarm["hour"], alarm["minute"],
                                                                alarm["repeat"], after=alarm["due_at"] + 60)
                    else:
                        del self._alarms[alarm_id]
            if due_alarms:
                self._save_alarms()

        for timer in due:
            self._start_ringing(timer, "clock", now)
        for alarm in due_alarms:
            self._start_ringing(alarm, "alarm", now)

        # Repeats anything which went off and hasn't been dismissed yet.
        repeat_now = []
        with self._lock:
            for entry in list(self._ringing):
                if entry["next_at"] <= now:
                    self._ringing.remove(entry)
                    repeat_now.append(entry)

        for entry in repeat_now:
            self._continue_ringing(entry)

    def _start_ringing(self, item: Dict, kind: str, now: float):
        """
        Begins ringing something which has just come due - a timer, reminder, or
        alarm. Registered as ringing before it speaks, so dismissing it part way
        through stops it immediately.

        Args:
            item: The timer or alarm which finished.
            kind: "clock" for a timer/reminder, "alarm" for an alarm - decides
                  which rules and which sound apply.
            now: The moment it came due.
        """
        rounds, gap, give_up_after = self._repeat_settings(item, kind)
        entry = {"timer": item, "kind": kind, "left": None if rounds is None else rounds - 1,
                "gap": gap, "next_at": now + gap,
                # Kept fixed for as long as this entry rings, so the timer/reminder
                # volume boost below can tell how long it's been going regardless
                # of how many rounds that took.
                "first_rung_at": now,
                # Set for whatever is bounded by time rather than by a round count -
                # a timer or a reminder. An alarm stops when its cycles run out, so
                # it has no separate deadline to watch.
                "give_up_at": None if give_up_after is None else now + give_up_after,
                # True only while it's actually making noise - see _ring_once.
                "sounding": False}
        with self._lock:
            self._ringing.append(entry)

        still_ringing = self._ring_once(entry, repeat=False)

        # Timed from when it actually finished speaking, not from when it was
        # due. An announcement held back while the user was talking to the
        # assistant would otherwise be followed immediately by the next one,
        # bunching them together.
        entry["next_at"] = time.time() + gap

        # rounds is None for a timer or reminder, which are bounded by give_up_at
        # instead - see _repeat_settings. An alarm can end on the very first round
        # if rounds is 1, which the current default doesn't, but is possible.
        if not still_ringing or (rounds is not None and rounds <= 1):
            self._end_ringing(entry)

    def _continue_ringing(self, entry: Dict):
        """
        Rings the next round for something already ringing, having been put back
        in the queue for it (see _run). Ends it - dismissed or not - once its
        rounds run out, or once it's been ringing longer than it's allowed to.
        """
        # Put back before anything else, so it can be dismissed mid-round and so
        # _end_ringing below can find it to record it as missed.
        with self._lock:
            self._ringing.append(entry)

        # A timer or reminder is stopped by the clock rather than by a round count.
        # Checked before it speaks rather than after, so the last thing the user
        # hears is a full round and not one cut off part way through.
        if entry["give_up_at"] is not None and time.time() >= entry["give_up_at"]:
            self._end_ringing(entry)
            return

        if entry["left"] is not None:
            entry["left"] -= 1
        # left of None means it isn't counting rounds at all - give_up_at above is
        # what ends it instead.
        still_going = entry["left"] is None or entry["left"] > 0

        still_ringing = self._ring_once(entry, repeat=True)

        if still_going and still_ringing:
            # Timed from when it finished speaking, so a round delayed by a
            # conversation doesn't run straight into the next one.
            entry["next_at"] = time.time() + entry["gap"]
        else:
            self._end_ringing(entry)

    def _end_ringing(self, entry: Dict):
        """
        Ends one ringing entry which ran out without being acknowledged, noting it
        so the user can ask what they missed. Nothing needs rescheduling here -
        everything was already dealt with in its store when it started ringing.
        """
        with self._lock:
            finished = entry in self._ringing
            if finished:
                self._ringing.remove(entry)
        if not finished:
            # Already removed - dismiss() got there first and handled this itself.
            return

        self._record_unacknowledged(entry["timer"])

    def _repeat_settings(self, item: Dict, kind: str = "clock"):
        """
        How persistently something should keep announcing itself.

        Args:
            item: The timer or alarm which finished.
            kind: "clock" for a timer/reminder, "alarm" for an alarm.

        Returns:
            Tuple of (rounds, seconds between rounds, seconds before giving up).
            Exactly one of rounds / give-up-after is set, and it's that choice which
            says how the thing is bounded:

            A plain timer exists to be acted on right away, so it keeps going with
            barely a pause between rounds, until timer_give_up_after. A reminder
            exists because the user will likely be doing something else, so it
            comes back every so often until reminder_give_up_after. Both are
            measured in time, since a round lasts as long as its message takes to
            say and would otherwise mean different things for different messages.

            An alarm is counted in rounds instead, because a round *is* the unit
            there - a fixed-length stretch of ringing followed by a fixed silence,
            neither of which depends on what's being said - so "three cycles" is
            already an exact duration.
        """
        if kind == "alarm":
            return self.alarm_max_cycles, self.alarm_gap_duration, None
        if item["label"]:
            return None, self.reminder_round_gap, self.reminder_give_up_after
        return None, self.timer_repeat_gap, self.timer_give_up_after

    def _ring_once(self, entry: Dict, repeat: bool) -> bool:
        """
        Plays one round for whatever's ringing - a timer/reminder announcement, or
        one alarm cycle.

        Args:
            entry: The ringing entry (see _start_ringing).
            repeat: True if this is a later round.

        Returns:
            True if it's still ringing (not dismissed) by the end of the round.
        """
        item, kind = entry["timer"], entry["kind"]

        # Marks this entry as making noise right now rather than sitting in the
        # gap between rounds. A reminder is treated differently depending on which
        # of the two it's in when the user starts talking - see ringing_state.
        entry["sounding"] = True
        try:
            if kind == "alarm":
                # An alarm has to assume you're asleep, not just busy, so it's always
                # at full volume rather than waiting to see if a lower one gets heard.
                return self._ring_alarm(item)

            # Once a plain timer (not a reminder - those keep their own, gentler
            # volume) has gone unanswered for a couple of minutes, it's worth making
            # sure it's actually being heard.
            boost = (not item["label"]
                    and time.time() - entry["first_rung_at"] >= self.timer_volume_boost_after)
            self._sound_off(item, repeat=repeat, boost=boost)
            return self._is_ringing(item)
        finally:
            entry["sounding"] = False

    def _sound_off(self, timer: Dict, repeat: bool = False, boost: bool = False):
        """
        Announces a timer or reminder which has come due.

        Announced twice in a row - sound, message, brief pause, then the same again
        - so a moment's inattention doesn't mean missing it entirely.

        Args:
            timer: The timer which finished.
            repeat: True if this is a later round, which doesn't change the wording:
                    the user hasn't heard it yet, or they'd have dismissed it.
            boost: True once a timer has been ringing unanswered long enough that
                   it should jump to full volume, regardless of the configured
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
            # A plain timer uses its own more insistent sound rather than the
            # general announcement chime, since it's something to act on right
            # away rather than hear about eventually.
            sound = "timer_beep.wav"

        for announcement in range(self.announcements):
            # Stops mid-round if the user dismisses it, rather than talking over them.
            if announcement and not self._is_ringing(timer):
                return
            self._announce(message, sound, boost, still_wanted=lambda: self._is_ringing(timer))
            if announcement < self.announcements - 1:
                time.sleep(self.repeat_gap)

    def _ring_alarm(self, alarm: Dict) -> bool:
        """
        Rings an alarm for one cycle by looping alarm_sound.wav continuously for
        alarm_ring_duration, stopping early if dismissed. Each cycle opens with a
        spoken cut-in (so it's clear what's actually going off, and what time it
        is, even for a later cycle) then falls back to the raw sound alone, so
        nothing after that first loop is waiting on speech synthesis while it's
        trying to be loud.

        Args:
            alarm: The alarm which finished.

        Returns:
            True if it's still ringing (not dismissed) by the end of the cycle.
        """
        def still_wanted():
            return self._is_ringing(alarm)

        now = datetime.now()
        cutin = f"It's {spoken_clock_time(now.hour, now.minute)}. Time to get up."
        self._announce(cutin, "alarm_sound.wav", boost=True, still_wanted=still_wanted)
        if not self._is_ringing(alarm):
            return False

        end_at = time.time() + self.alarm_ring_duration
        while time.time() < end_at:
            if not self._is_ringing(alarm):
                return False
            self._announce(None, "alarm_sound.wav", boost=True, still_wanted=still_wanted)

        return self._is_ringing(alarm)

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

    # Words which carry no request of their own, so they don't stop something being
    # a bare acknowledgement. Several of the dismissal words above are ordinary
    # English ("off", "no", "right", "thanks"), so a phrase is only treated as a
    # dismissal when everything in it is one of these two sets - otherwise "turn
    # off the lights" would silence a timer and never reach the lights.
    # Words which turn down an offer. "no" is a dismissal word too - said to a
    # ringing timer it means "make it stop" - so when the assistant has just asked
    # a yes/no question, this is checked first and wins, or answering "no" to
    # "shall I turn that reminder off?" would turn it off.
    REFUSAL_WORDS = {
        "no", "nope", "nah", "not", "dont", "don't", "leave", "keep", "later",
        "ignore", "nevermind", "never",
    }

    FILLER_WORDS = {
        "a", "all", "am", "an", "and", "are", "as", "at", "be", "been", "but",
        "can", "could", "did", "do", "for", "from", "have", "has", "had", "hey", "i",
        "im", "i'm", "is", "it", "it's", "its", "ive", "i've", "just", "know", "let",
        "me", "mm", "mhm", "my", "now", "of", "oh", "please", "so", "than",
        "that", "that's", "thats", "the", "then", "there", "this", "to", "too", "up",
        "uh", "um", "very", "was", "well", "will", "would", "you", "your",
    }

    def is_ringing(self) -> bool:
        """Whether a timer or reminder is going off and waiting to be acknowledged."""
        with self._lock:
            return bool(self._ringing)

    @staticmethod
    def _is_insistent(entry: Dict) -> bool:
        """
        Whether a ringing entry is one which keeps demanding attention - a timer or
        an alarm - rather than a reminder, which says its piece and waits.
        """
        return entry["kind"] == "alarm" or not entry["timer"]["label"]

    def ringing_state(self) -> Dict:
        """
        What's going off right now, split by how it should be treated when the user
        starts talking. Meant to be read at the moment the wake word fires, since
        what matters is what the user could actually hear when they spoke.

        Returns:
            Dict with:
              'insistent'        - a timer or alarm is going off. Talking to the
                                   assistant at all is enough to stop these.
              'insistent_entries'- exactly which ones, for silence_insistent - so
                                   one which only starts during the conversation
                                   isn't silenced along with them.
              'reminder'         - the label of a reminder going off, or None. A
                                   reminder needs acknowledging in its own right.
              'reminder_sounding'- True if that reminder is mid-announcement rather
                                   than in the quiet gap between its rounds.
        """
        with self._lock:
            entries = list(self._ringing)

        reminders = [e for e in entries if not self._is_insistent(e)]
        insistent = [e for e in entries if self._is_insistent(e)]
        return {
            "insistent": bool(insistent),
            "insistent_entries": insistent,
            "reminder": reminders[0]["timer"]["label"] if reminders else None,
            "reminder_sounding": any(e.get("sounding") for e in reminders),
        }

    def silence_insistent(self, heard: Optional[List[Dict]] = None) -> bool:
        """
        Silences timers and alarms going off, leaving reminders alone.

        Used when the user has said something to the assistant while one is ringing:
        talking at all means they've heard it, whatever they actually asked for. A
        reminder is deliberately not included - it has to be acknowledged in its own
        right (see main.py) - and the missed list is left alone too, since this
        isn't the user saying they've caught up on what they missed.

        Args:
            heard: The ringing entries the user could hear when they started talking,
                as from ringing_state()["insistent_entries"]. Only those are
                silenced: a timer which came due while the assistant was replying
                hasn't been announced yet - announcements wait for the conversation
                to end - so it mustn't be silenced unheard. None silences all of them.

        Returns:
            True if anything was actually silenced.
        """
        with self._lock:
            keep, silenced = [], []
            for entry in self._ringing:
                wanted = self._is_insistent(entry) and (heard is None or any(entry is h for h in heard))
                (silenced if wanted else keep).append(entry)
            self._ringing = keep

        if silenced:
            logger.info(f"Silenced {len(silenced)} timer(s)/alarm(s) - the user was talking")
        return bool(silenced)

    @classmethod
    def looks_like_dismissal(cls, text: str) -> bool:
        """
        Whether something said while a timer is going off was *only* meant to stop
        it, with no other request attached.

        This is the shortcut which silences a timer without waiting on the model,
        so it has to be sure. Several dismissal words are ordinary English - "turn
        off the lights", "no music please", "right, play Spotify" all contain one -
        and taking those as a bare acknowledgement would silence the timer and
        throw the actual request away. So a phrase only counts here when every word
        in it is either a dismissal or a filler word, which leaves anything with
        real content in it ("lights", "music", "Spotify") to the model.

        Note this being False doesn't mean the timer keeps ringing: talking to the
        assistant at all counts as having heard it (see main.py). It only decides
        whether the request needs the model at all.

        Args:
            text: What the user said.

        Returns:
            True if it should be treated as dismissing the alarm and nothing else.
        """
        words = re.findall(r"[a-z']+", text.lower())
        # A generous cap: a pure acknowledgement is short, and this stops a long
        # mis-transcription which happens to be all common words from counting.
        if not words or len(words) > 6:
            return False
        # Has to actually acknowledge something - filler alone isn't a dismissal.
        if not set(words) & cls.DISMISSAL_WORDS:
            return False
        return all(word in cls.DISMISSAL_WORDS or word in cls.FILLER_WORDS
                   for word in words)

    @classmethod
    def looks_like_agreement(cls, text: str) -> bool:
        """
        Whether a short answer to a yes/no question the assistant just asked was a
        yes.

        Deliberately looser than looks_like_dismissal. That one has to guard
        against swallowing a real request, so it insists every word be an
        acknowledgement or filler; here the assistant has just asked a question, so
        a stray content word ("yeah go on", "yes turn it off") doesn't make it any
        less of an answer. Refusals are checked first, since "no" is in both.

        Args:
            text: What the user said.

        Returns:
            True if it should be read as "yes".
        """
        words = re.findall(r"[a-z']+", text.lower())
        if not words or len(words) > 6:
            return False
        return bool(set(words) & cls.DISMISSAL_WORDS)

    @classmethod
    def looks_like_refusal(cls, text: str) -> bool:
        """
        Whether a short answer was turning down an offer the assistant just made,
        e.g. "no", "leave it", "not now".

        Only meaningful straight after the assistant has asked a yes/no question,
        and checked before looks_like_dismissal in that case - the two overlap on
        "no", which means opposite things depending on whether it's answering a
        question or being said to something that's ringing.

        Args:
            text: What the user said.

        Returns:
            True if it should be read as "no".
        """
        words = re.findall(r"[a-z']+", text.lower())
        if not words or len(words) > 6:
            return False
        return bool(set(words) & cls.REFUSAL_WORDS)

    def dismiss(self) -> Dict:
        """
        Acknowledges anything currently going off, stopping it repeating.

        For a repeating alarm this only silences today's occurrence - its next
        one was already scheduled when this one went off. Fully cancelling the
        repeat is a separate, explicit request (see cancel_alarm), never implied
        by just dismissing it.

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

        # An alarm has no label or duration to name it by, so it's described by its time instead.
        now = time.time()
        described = [f"{self._describe_alarm(t) if t.get('kind') == 'alarm' else self._describe(t)}, "
                     f"{format_duration(now - t['missed_at'])} ago" for t in missed]
        return {"success": True, "message": "You missed " + "; ".join(described) + "."}

    def _announce(self, message: str, sound: str = "announcement_sound.wav", boost: bool = False,
                  still_wanted: Optional[Callable[[], bool]] = None):
        """
        Passes a message to the callback which speaks it.

        Args:
            message: What to say.
            sound: Which sound effect should introduce the message.
            boost: True if it should be spoken at full volume regardless of the
                   configured alarm level - see _sound_off.
            still_wanted: Checked by the callback right before anything plays. An
                          announcement can be held back while the user talks to the
                          assistant, and they may dismiss it in that very conversation -
                          without this it would still play once it was finally let through.
        """
        logger.info(f"Timer finished: {message}")

        if self.on_timer_finished:
            try:
                self.on_timer_finished(message, sound, boost, still_wanted=still_wanted)
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
        Plain-text descriptions of every running timer, reminder and alarm - the
        same wording list_timers/list_alarms would speak, but as a list rather
        than one sentence.

        Used to give something else (the LLM) visibility of what's actually
        running, so it can be matched against even when speech-to-text mangles
        a label.
        """
        with self._lock:
            timers = sorted(self._timers.values(), key=lambda t: t["due_at"])
            alarms = sorted(self._alarms.values(), key=lambda a: a["due_at"])
        return [self._list_entry(timer) for timer in timers] + [self._describe_alarm(a) for a in alarms]

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
        spoken = spoken_clock_time(due.tm_hour, due.tm_min)
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

    @staticmethod
    def _timer_matches(timer: Dict, wanted: str) -> bool:
        """
        Whether a timer or reminder is the one the user described.

        Args:
            timer: The timer to check.
            wanted: What the user called it, lowercased - its label, its length, or
                    the clock time it's due.

        Returns:
            True if it matches.
        """
        timer_label = (timer["label"] or "").lower()
        wanted_units = _duration_units(wanted)
        wanted_times = _time_of_day_candidates(wanted)
        due_local = time.localtime(timer["due_at"])

        matches_label = bool(timer_label) and (wanted in timer_label or timer_label in wanted)
        # Unlabelled timers are matched on their length instead. Every number/unit pair
        # the user said has to match the timer's actual duration exactly, so "the 30
        # minute timer" can't also grab a 1 hour 30 minute timer just for sharing the
        # digit "30" and the word "minute".
        matches_duration = (not timer_label
                            and bool(wanted_units)
                            and wanted_units == _duration_units(format_duration(timer["duration"]).lower()))
        # A timer or reminder can also be identified by the clock time it's
        # due to go off, e.g. "cancel the 5:40 one".
        matches_time = (bool(wanted_times)
                        and (due_local.tm_hour, due_local.tm_min) in wanted_times)

        return matches_label or matches_duration or matches_time

    def _about_ringing(self, label: Optional[str], kind: str) -> bool:
        """
        Whether a cancel request is about something which is going off right now,
        rather than one still waiting to go off.

        Args:
            label: What the user called it. None ("cancel it", "turn that off") means
                   whatever of that kind is ringing.
            kind: "clock" for timers / reminders, "alarm" for alarms.

        Returns:
            True if something of that kind is ringing and the request describes it.
        """
        with self._lock:
            ringing = [entry["timer"] for entry in self._ringing if entry["kind"] == kind]
        if not ringing:
            return False
        if not label:
            return True

        wanted = label.strip().lower()
        if kind == "alarm":
            wanted_times = _time_of_day_candidates(wanted)
            return any((alarm["hour"], alarm["minute"]) in wanted_times for alarm in ringing)
        return any(self._timer_matches(timer, wanted) for timer in ringing)

    def _silence_kind(self, kind: str) -> int:
        """
        Stops everything of one kind which is going off, leaving the rest ringing.
        Used when the user cancels every timer, or every alarm, while one is sounding.

        Returns:
            How many were silenced.
        """
        with self._lock:
            silenced = [entry for entry in self._ringing if entry["kind"] == kind]
            self._ringing = [entry for entry in self._ringing if entry["kind"] != kind]
        return len(silenced)

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
        # no pending timer left to cancel by then. Only when the request is actually
        # about what's ringing, though - "cancel the pizza reminder" while some other
        # timer is going off means the pizza reminder, and dismissing the other one
        # instead would leave it set while the user thinks it's gone.
        silenced = self._silence_kind("clock") if cancel_all else 0
        if not cancel_all and self._about_ringing(label, "clock"):
            return self.dismiss()

        with self._lock:
            if not self._timers:
                if silenced:
                    return {"success": True, "message": "Okay.", "chime": True}
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

            # Every timer matching what was said, rather than stopping at the first
            # one - two 10 minute timers both match "the 10 minute timer" equally,
            # so which one gets cancelled must be asked rather than guessed.
            matches = [timer for timer in self._timers.values() if self._timer_matches(timer, wanted)]

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

    def _spoken_time_of_day(self, hour: int, minute: int) -> str:
        """Formats an hour/minute as a spoken clock time, e.g. "7:00 AM"."""
        return spoken_clock_time(hour, minute)

    def _describe_alarm(self, alarm: Dict) -> str:
        """Names an alarm for speech, e.g. "7:00 AM alarm, every weekday"."""
        base = f"{self._spoken_time_of_day(alarm['hour'], alarm['minute'])} alarm"
        repeat_desc = self._repeat_description(alarm["repeat"])
        return f"{base}, {repeat_desc}" if repeat_desc else base

    def set_alarm(self, hour: int, minute: int, repeat=None) -> Dict:
        """
        Set an alarm for a time of day, optionally repeating.

        Alarms never take a label - that's what a reminder is for. An alarm is
        identified by its time (and, if it repeats, that pattern) alone.

        Args:
            hour: 0-23.
            minute: 0-59.
            repeat: None for a one-off alarm, or a repeat pattern in any form
                    parse_repeat understands - "daily", "weekdays", "weekends",
                    or the days themselves.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        try:
            hour, minute = int(hour), int(minute)
        except (TypeError, ValueError):
            return {"success": False, "message": "Sorry, I didn't catch what time for."}

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return {"success": False, "message": "That's not a real time."}

        # Asked about rather than guessed at, since a wrong guess means an alarm
        # going off on days it shouldn't. Worded as a question so the answer is
        # listened for straight away.
        try:
            repeat = parse_repeat(repeat)
        except ValueError:
            logger.info(f"Couldn't read the repeat pattern {repeat!r}, asking")
            return {"success": False, "message": "Sorry, which days should that alarm repeat on?"}

        due_at = self._next_occurrence(hour, minute, repeat)

        with self._lock:
            # Same idea as a timer set for the same clock minute as another - two
            # alarms due at once would be indistinguishable once they're ringing.
            for existing in self._alarms.values():
                if existing["hour"] == hour and existing["minute"] == minute:
                    return {"success": False,
                            "message": f"You already have a {self._describe_alarm(existing)}."}

            alarm_id = self._next_alarm_id
            self._next_alarm_id += 1
            alarm = {"id": alarm_id, "hour": hour, "minute": minute, "repeat": repeat, "due_at": due_at}
            self._alarms[alarm_id] = alarm
            self._save_alarms()

        logger.info(f"Alarm set: {self._describe_alarm(alarm)}")
        return {"success": True, "message": f"{self._describe_alarm(alarm)}."}

    def list_alarms(self) -> Dict:
        """
        Report the alarms which are set.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._lock:
            alarms = sorted(self._alarms.values(), key=lambda a: a["due_at"])

        if not alarms:
            return {"success": True, "message": "You don't have any alarms set."}

        described = [self._describe_alarm(a) for a in alarms]
        if len(described) == 1:
            return {"success": True, "message": f"One alarm: {described[0]}."}
        return {"success": True, "message": f"{len(described)} alarms: " + ", ".join(described) + "."}

    def next_alarm(self) -> Dict:
        """
        Report how long until the next alarm.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._lock:
            alarms = sorted(self._alarms.values(), key=lambda a: a["due_at"])

        if not alarms:
            return {"success": True, "message": "You don't have any alarms set."}

        alarm = alarms[0]
        remaining = format_duration(alarm["due_at"] - time.time())
        return {"success": True,
                "message": f"Your next alarm is the {self._describe_alarm(alarm)}, in {remaining}."}

    def cancel_alarm(self, label: Optional[str] = None, cancel_all: bool = False,
                     scope: Optional[str] = None) -> Dict:
        """
        Cancel an alarm.

        An alarm is identified by its time of day, since it never has a label.
        Cancelling a repeating one needs to know whether that means just today's
        occurrence or the whole schedule - if the user didn't already say which,
        this asks rather than guessing (skipping today entirely is very different
        from losing a daily alarm for good).

        Args:
            label: A time of day ('7am'), or left out when there's only one alarm
                   or the user just means "my alarm".
            cancel_all: True to cancel every alarm outright, regardless of repeat.
            scope: 'today' to skip just the next occurrence of a repeating alarm,
                   'all' to remove the whole repeat. Leave out when the user
                   didn't specify - a repeating alarm then gets asked about
                   rather than guessed at; a one-off alarm has nothing to ask,
                   since there's no repeat to choose between.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        # An alarm currently ringing is dismissed rather than cancelled here -
        # dismissing it already does the right thing (silences today, and a
        # repeating one's next occurrence was scheduled when this one went off).
        # Only when the request is about the ringing alarm, though: with a timer
        # going off, "cancel my 7am alarm" still means the 7am alarm.
        silenced = self._silence_kind("alarm") if cancel_all else 0
        if not cancel_all and self._about_ringing(label, "alarm"):
            with self._lock:
                ringing_ids = [entry["timer"]["id"] for entry in self._ringing if entry["kind"] == "alarm"]
            result = self.dismiss()

            # "Stop it repeating" said to a ringing alarm means the whole schedule,
            # not just today's - which dismissing alone would leave in place.
            if scope == "all":
                with self._lock:
                    removed = [self._alarms.pop(alarm_id) for alarm_id in ringing_ids if alarm_id in self._alarms]
                    if removed:
                        self._save_alarms()
                if removed:
                    return {"success": True, "message": f"{self._describe_alarm(removed[0])} cancelled."}
            return result

        with self._lock:
            if not self._alarms:
                if silenced:
                    return {"success": True, "message": "Okay.", "chime": True}
                return {"success": False, "message": "There aren't any alarms to cancel."}

            if cancel_all:
                count = len(self._alarms)
                self._alarms.clear()
                self._save_alarms()
                return {"success": True,
                        "message": "Alarm cancelled." if count == 1 else f"All {count} alarms cancelled."}

            if not label:
                if len(self._alarms) == 1:
                    alarm = next(iter(self._alarms.values()))
                else:
                    options = ", ".join(self._describe_alarm(a) for a in self._alarms.values())
                    return {"success": False,
                            "message": f"You have {len(self._alarms)} alarms: {options}. Which one?"}
            else:
                wanted_times = _time_of_day_candidates(label.strip().lower())
                matches = [a for a in self._alarms.values()
                          if wanted_times and (a["hour"], a["minute"]) in wanted_times]

                # Nothing matched, but with a single alarm set there's nothing
                # else the user could have meant.
                if not matches and len(self._alarms) == 1:
                    matches = list(self._alarms.values())

                if not matches:
                    return {"success": False, "message": f"I couldn't find an alarm for {label}."}

                if len(matches) > 1:
                    options = ", ".join(self._describe_alarm(a) for a in matches)
                    return {"success": False,
                            "message": f"There are {len(matches)} alarms at that time: {options}. "
                                       f"Which would you like to cancel?"}
                alarm = matches[0]

            # A repeating alarm needs to know which was meant before anything
            # happens - the two outcomes are too different to guess between.
            if alarm["repeat"] and scope not in ("today", "all"):
                return {"success": False,
                        "message": f"Your {self._describe_alarm(alarm)} repeats - would you like to "
                                   f"skip it just for today, or stop it repeating altogether?"}

            if alarm["repeat"] and scope == "today":
                alarm["due_at"] = self._next_occurrence(alarm["hour"], alarm["minute"], alarm["repeat"],
                                                         after=alarm["due_at"] + 60)
                self._save_alarms()
                return {"success": True, "message": f"Skipped today's {self._describe_alarm(alarm)}."}

            self._alarms.pop(alarm["id"], None)
            self._save_alarms()
            return {"success": True, "message": f"{self._describe_alarm(alarm)} cancelled."}

    def stop(self):
        """Stop the background thread, used when the assistant shuts down."""
        self._running = False
        logger.info("Clock stopped")
