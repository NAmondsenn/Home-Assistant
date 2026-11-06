"""
Clock Module
Handles countdown timers, reminders and alarms.

All three run on a background thread, so they can go off while the assistant is
busy listening for the wake word. When one comes due, it calls the callback it was
given, which is what actually speaks the announcement.

Timers and reminders count down from a duration and are kept together; an alarm is
scheduled against the wall clock and can repeat, so it's kept separately (see
_alarms) and rings to its own, more insistent pattern.

The words it says and listens for - how a duration or a time of day is spoken, and
which spoken words mean stop, yes or no - are in clock_words.py (ClockWords), which
Clock builds on.

How it rings - the rounds of ringing, the alarm's rising volume, and what dismissing,
silencing and the missed list do - is in clock_ringing.py (Ringing), which Clock builds
on too.

What the satellites are sent of it - handing a satellite's timers and alarms to another,
the copy it rings from while it can't reach the brain, and applying what it says
happened meanwhile - is in clock_copies.py (SatelliteCopies), again built on by Clock.

Setting, listing and cancelling them - what is said to set one, the clashes it is checked
for, how they are described and counted, and removing one or putting it back - is in
clock_requests.py (ClockRequests), the last part Clock builds on.
"""

import os
import logging
import math
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from voice_assistant import paths, storage
from voice_assistant.clock_words import WEEKDAY_NAMES, ClockWords, format_duration, parse_repeat
from voice_assistant.clock_copies import SatelliteCopies
from voice_assistant.clock_requests import ClockRequests
from voice_assistant.clock_ringing import Ringing
# Where these now live; named here too so the tests, and anything else which reached
# for them on the clock, still find them.
from voice_assistant.clock_ringing import ALARM_SOUND, ALARM_START_LEVEL  # noqa: F401
from voice_assistant.clock_words import _time_of_day_candidates, seconds_until, spoken_clock_time  # noqa: F401

# Logger setup
logger = logging.getLogger(__name__)

# Resolves paths relative to the project root, so saved timers live inside the project like everything else.
TIMERS_FILE = os.path.join(paths.DATA_DIR, "timers.json")
ALARMS_FILE = os.path.join(paths.DATA_DIR, "alarms.json")

# A timer which came due while the assistant was off is still announced if it was
# recent, anything older is ignored - and one held past it for a satellite which
# couldn't hear it yet is kept for "what did I miss" instead (see _tick).
MISSED_TIMER_GRACE_SECONDS = 3600

# Waits until NTP corrects the time, so the clock is accurate on startup.
PLAUSIBLE_TIME_AFTER = 1735689600
# A tick which keeps failing the same way is logged again after this many seconds at
# most - it ticks twice a second, so one bad entry would otherwise flood the log.
FAILED_TICK_LOG_GAP = 60


class Clock(ClockRequests, Ringing, SatelliteCopies, ClockWords):
    """
    Runs countdown timers and reminders.

    A timer optionally carries a label, which turns it into a reminder: the label
    is spoken when it goes off, and can be used to refer to it later ("cancel the
    pizza timer").
    """

    def __init__(self, on_timer_finished: Optional[Callable[..., None]] = None,
                 check_interval: float = 0.5, config=None, start: bool = True,
                 on_unanswered: Optional[Callable[..., None]] = None,
                 on_change: Optional[Callable[[], None]] = None,
                 can_hear: Optional[Callable[[Optional[str]], bool]] = None,
                 let_in: Optional[Callable[[str], bool]] = None):
        """
        Args:
            on_timer_finished: Called with (message, sound, boost, still_wanted=...,
                               satellite=...), plus start, length and level for an
                               alarm's bursts, when a timer goes off. This is how the
                               assistant speaks it - the Clock itself has no audio -
                               and satellite is where it was set from, so it rings there.
            on_unanswered: Called with a timer, reminder or alarm which stopped
                           ringing without anyone answering it, which it was -
                           "clock" or "alarm" - and satellite=, where it rang, so a
                           reminder can be sent to the user's phone. Called on the
                           clock's own thread, so it mustn't wait on anything.
            on_change: Called with nothing once anything has been set, cancelled,
                       removed, dismissed or rung, and saved - so each satellite's
                       copy (see copy_for) can be brought up to date. Called on
                       whichever thread made the change, the clock's own among them,
                       so it mustn't wait on anything either.
            can_hear: Called with a satellite's name - or None, for something which
                      rings on no satellite - to ask whether it's there to be told
                      what came due while the brain was off: connected, and having
                      said what it rang from its copy meanwhile (see apply_away). Each
                      is kept until it is, rather than said to nobody - or said for
                      something the satellite rang itself - and one past the grace by
                      then is kept for "what did I miss" instead. Called with the clock's
                      lock held, so it mustn't wait on anything, nor call the clock.
                      None says them all at the first tick.
            let_in: Called with a satellite's name as something is set or put back
                    for it, to ask whether the brain still lets it in. One removed
                    meanwhile - by a turn still in flight on it, or the page - is
                    refused, as it would otherwise ring nowhere. Called with the
                    clock's lock held, so it mustn't call the clock. None lets in
                    every satellite.
            check_interval: How often the background thread checks for due timers.
            start: Whether to start the background thread now. The brain starts it
                   later, once it has given what rings on no satellite to the
                   default (see start), and the tests never do, so they can step
                   the clock along with _tick() instead.
        """
        self.on_timer_finished = on_timer_finished
        self.on_unanswered = on_unanswered
        self.on_change = on_change
        self.can_hear = can_hear
        self.let_in = let_in
        self.check_interval = check_interval

        self._timers: Dict[int, Dict] = {}
        self._next_id = 1
        # Guards the timers dict, since the background thread and the assistant's main loop both touch it.
        self._lock = threading.Lock()
        # How many times anything's been saved, which is how _changing knows
        # whether what it guarded actually changed anything.
        self._saves = 0
        # Whether the last save of each file failed, so the file isn't what's kept
        # here - which apply_away writes again before it says it has saved anything.
        self._timers_unsaved = self._alarms_unsaved = False

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
        # Within a cycle, an alarm rings in short bursts with a gap after each, long
        # enough to say "Hey Nova, stop" into, and builds up to full volume over
        # alarm_ramp seconds - see _ring_alarm.
        self.alarm_burst = max(0.5, float(timers_config.get("alarm_burst", 3)))
        self.alarm_burst_gap = max(0.0, float(timers_config.get("alarm_burst_gap", 1)))
        self.alarm_ramp = max(0.0, float(timers_config.get("alarm_ramp", 30)))

        self._alarms: Dict[int, Dict] = {}
        self._next_alarm_id = 1

        # Timers which have gone off and are waiting to be repeated or dismissed.
        self._ringing: List[Dict] = []
        # Timers which were never acknowledged, so the user can ask what they missed.
        self._unacknowledged: List[Dict] = []

        # What came due while the brain was off, until it's announced once its satellite
        # can hear it - kept in the timers file meanwhile, see _save.
        self._missed: List[Dict] = []
        # Restores timers saved before the last shutdown, so they survive restarts.
        self._load()
        self._load_alarms()

        self._running = True
        # The last error a tick raised, and when it was logged - see _tick_safely.
        self._tick_error: Optional[str] = None
        self._tick_error_logged = 0.0
        # Each round of ringing runs on a thread of its own when the clock has its
        # background thread - see _run_round. The tests step through without one,
        # so there a round simply runs in whatever called it.
        self._rounds_on_threads = False

        if start:
            self.start()

    def start(self):
        """Starts the background thread, which rings everything as it comes due."""
        self._rounds_on_threads = True
        # A daemon thread so it never keeps the assistant alive on shutdown.
        self._thread = threading.Thread(target=self._run, daemon=True, name="clock")
        self._thread.start()
        logger.info("Clock started")

    def _save(self) -> bool:
        """
        Writes the pending timers to the disk, along with any timer or alarm going off
        right now - so a restart while one is ringing reports it as missed (or, if the
        satellite is still ringing it, rings it again) rather than losing it - and
        whatever was missed and hasn't been dealt with yet, so a restart doesn't lose
        that either. An alarm's store has already moved on without them, so this is
        the only place they're kept.

        Saved atomically (see storage.py), so losing power mid-write leaves the
        previous file intact. The caller is expected to already hold the lock.

        Returns:
            Whether it was saved.
        """
        self._saves += 1
        try:
            storage.save_json(TIMERS_FILE, {
                "next_id": self._next_id,
                "timers": list(self._timers.values()),
                "ringing": [entry["timer"] for entry in self._ringing],
                "missed": self._missed,
            })
        except Exception as e:
            # Saving is best-effort: a failure here shouldn't stop a timer working
            # for the rest of this session.
            logger.error(f"Could not save timers: {e}")
            self._timers_unsaved = True
            return False
        self._timers_unsaved = False
        return True

    def _load(self):
        """
        Restores timers from disk.

        Timers which came due while the assistant was off are kept aside to be
        announced late, as long as they aren't older than the grace period - as is
        whatever was already missed, and not yet dealt with, before the last restart.
        """
        try:
            data = storage.load_json(TIMERS_FILE)
        except Exception as e:
            # A corrupt file shouldn't stop the assistant starting.
            logger.error(f"Could not read saved timers, ignoring them: {e}")
            return
        if data is None:
            return

        now = time.time()
        restored = missed = dropped = 0
        unnumbered = []

        # Anything which was going off when the assistant stopped is past its due
        # time, so it's treated the same as one which came due while it was off.
        saved = [(timer, False) for timer in data.get("timers", []) + data.get("ringing", [])]
        saved += [(timer, True) for timer in data.get("missed", [])]
        for timer, was_missed in saved:
            # Skips anything malformed rather than trusting the file's contents.
            if not isinstance(timer, dict) or "due_at" not in timer:
                continue

            try:
                timer["due_at"] = float(timer["due_at"])
                timer["duration"] = int(timer.get("duration", 0))
                timer["id"] = int(timer.get("id") or 0)
            except (TypeError, ValueError):
                continue

            label = timer.get("label")
            timer["label"] = str(label) if label else None
            # None for one set before there were satellites - see move_satellite.
            satellite = timer.get("satellite")
            timer["satellite"] = str(satellite) if satellite else None

            # An alarm which was ringing, already past its time - see _save. It joins the
            # missed ones below and never the timers, even if the clock has moved back,
            # as does anything which had already been missed.
            if timer["due_at"] > now and "hour" not in timer and not was_missed:
                if timer["id"]:
                    self._timers[timer["id"]] = timer
                else:
                    unnumbered.append(timer)
                restored += 1
            elif now - timer["due_at"] <= MISSED_TIMER_GRACE_SECONDS:
                # Recently missed, so it's still worth mentioning once started.
                if self._note_missed(timer):
                    missed += 1
            else:
                dropped += 1

        # Keeps IDs unique across restarts, so a restored timer can't be confused with a new one.
        self._next_id = max([int(data.get("next_id", 1))]
                            + [t["id"] + 1 for t in self._timers.values()] or [1])

        # A satellite's copy names each occurrence by its timer's id (see copy_for),
        # so one saved without an id is given one, and saved with it below so it
        # keeps it.
        for timer in unnumbered:
            timer["id"] = self._next_id
            self._next_id += 1
            self._timers[timer["id"]] = timer

        if restored or missed or dropped:
            logger.info(f"Restored {restored} timer(s), {missed} missed, {dropped} too old")

        # Saved straight away, with what came due while it was off kept as missed rather
        # than as timers, and whatever's too old gone. What's missed stays in the file
        # until it's dealt with - said (see _tick, which lets go of it before it's said)
        # or settled by its satellite (see apply_away) - so a crash loop neither loses
        # it before it's said nor says the same "while I was off" again after.
        if missed or dropped or unnumbered:
            self._save()

    def _note_missed(self, item: Dict) -> bool:
        """
        Keeps something which came due while the brain was off, to be announced once
        its satellite can hear it - unless it's kept already. A brain lost as it
        started may have noted an alarm as missed in the timers file without yet
        moving it on in the alarms file, so the next finds it in both.

        Args:
            item: The timer or alarm, as it was when it came due.

        Returns:
            Whether it wasn't kept already.
        """
        occurrence = self._occurrence(item)
        if any(self._occurrence(other) == occurrence for other in self._missed):
            return False
        self._missed.append(item)
        return True

    def _save_alarms(self) -> bool:
        """
        Writes the alarms to disk, the same atomic way _save writes timers. The
        caller is expected to already hold the lock.

        Returns:
            Whether it was saved.
        """
        self._saves += 1
        try:
            storage.save_json(ALARMS_FILE, {"next_id": self._next_alarm_id,
                                            "alarms": list(self._alarms.values())})
        except Exception as e:
            logger.error(f"Could not save alarms: {e}")
            self._alarms_unsaved = True
            return False
        self._alarms_unsaved = False
        return True

    @contextmanager
    def _changing(self):
        """
        Holds the lock for something which may change what's set, then tells
        on_change once it's let go - if anything was saved meanwhile, since that's
        what changing something always does. Told outside the lock, because the
        brain asks straight back for each satellite's copy_for, which needs it.
        """
        with self._lock:
            saves = self._saves
            yield
            changed = self._saves != saves
        if changed and self.on_change:
            try:
                self.on_change()
            except Exception as e:
                # Passing it on must never stop whatever made the change.
                logger.error(f"Couldn't pass on a change to the timers and alarms: {e}")

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
        try:
            data = storage.load_json(ALARMS_FILE)
        except Exception as e:
            logger.error(f"Could not read saved alarms, ignoring them: {e}")
            return
        if data is None:
            return

        now = time.time()
        restored = missed = overdue = 0
        unnumbered = []
        for alarm in data.get("alarms", []):
            if not isinstance(alarm, dict):
                continue
            try:
                hour, minute = int(alarm["hour"]), int(alarm["minute"])
                alarm_id = int(alarm.get("id") or 0)
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

            # None for one set before there were satellites - see move_satellite.
            satellite = alarm.get("satellite")
            moved_on_from = alarm.get("moved_on_from")
            alarm = {"id": alarm_id, "hour": hour, "minute": minute, "repeat": repeat, "due_at": due_at,
                     "satellite": str(satellite) if satellite else None}
            # The occurrence it last rang on its own satellite - see move_satellite. Left
            # out if it's unreadable - NaN included, which json reads - so the alarm
            # itself still loads. Moving it past one missed below doesn't change it, as
            # the brain didn't ring that one; apply_away notes it if the satellite did.
            if (isinstance(moved_on_from, (int, float)) and not isinstance(moved_on_from, bool)
                    and math.isfinite(moved_on_from)):
                alarm["moved_on_from"] = moved_on_from

            if due_at <= now:
                overdue += 1
                # Recently missed, so it's still worth mentioning once started.
                if now - due_at <= MISSED_TIMER_GRACE_SECONDS and self._note_missed({**alarm, "kind": "alarm"}):
                    missed += 1
                if not repeat:
                    continue
                alarm["due_at"] = self._next_occurrence(hour, minute, repeat, after=max(due_at + 60, now))

            if alarm_id:
                self._alarms[alarm_id] = alarm
            else:
                unnumbered.append(alarm)
            restored += 1

        self._next_alarm_id = max([int(data.get("next_id", 1))]
                                  + [a["id"] + 1 for a in self._alarms.values()] or [1])

        # Given an id if it was saved without one, as a timer is in _load.
        for alarm in unnumbered:
            alarm["id"] = self._next_alarm_id
            self._next_alarm_id += 1
            self._alarms[alarm["id"]] = alarm

        # What was missed goes into the timers file before this one lets go of it - so a
        # brain lost in between finds it in both, and notes it once (see _note_missed),
        # rather than in neither.
        if missed:
            self._save()
        # Then every overdue alarm is moved on, or gone if it doesn't repeat, so the
        # next start doesn't find the same occurrence due, and note it again after it
        # has been said.
        if overdue or unnumbered:
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

    def _following(self, alarm: Dict, due: float) -> float:
        """
        When a repeating alarm next rings after the occurrence due at due.

        Everything which moves an alarm on comes through here - ringing it,
        skipping it, a satellite dismissing it, and spelling out a satellite's copy
        - since an occurrence's id (see _occurrence) only means the same to the
        brain and the satellite while they all agree on when the next one is.

        Args:
            alarm: The alarm.
            due: The occurrence to move on from, as an epoch time.

        Returns:
            The next occurrence, as an epoch time.
        """
        return self._next_occurrence(alarm["hour"], alarm["minute"], alarm["repeat"], after=due + 60)

    def _alarm_days_overlap(self, repeat, due_at: float, existing: Dict) -> bool:
        """
        Whether a new alarm would ever ring on the same day as an existing one at the
        same time of day.

        Args:
            repeat: The new alarm's repeat pattern, as from parse_repeat.
            due_at: The new alarm's next occurrence.
            existing: The alarm already set.
        """
        new_days = self._repeat_weekdays(repeat)
        old_days = self._repeat_weekdays(existing["repeat"])
        if new_days and old_days:
            return bool(new_days & old_days)
        if new_days:
            return datetime.fromtimestamp(existing["due_at"]).weekday() in new_days
        if old_days:
            return datetime.fromtimestamp(due_at).weekday() in old_days
        return datetime.fromtimestamp(due_at).date() == datetime.fromtimestamp(existing["due_at"]).date()

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

    def _run(self):
        """Background loop which fires timers, reminders and alarms as they come due."""
        while self._running:
            self._tick_safely(time.time(), time.monotonic())
            time.sleep(self.check_interval)

    def _tick_safely(self, now: float, moment: float):
        """
        One tick, with anything unexpected it raises logged rather than left to end
        the clock's thread - which would take every timer and alarm with it, and
        with the satellite connected nothing would ring its copy either. The same
        error is logged once, then at most once every FAILED_TICK_LOG_GAP seconds.

        Args:
            now: The current time, as an epoch timestamp.
            moment: time.monotonic(), which setting the system clock doesn't move.
        """
        try:
            self._tick(now)
        except Exception as error:
            said = f"{type(error).__name__}: {error}"
            if said != self._tick_error or moment - self._tick_error_logged >= FAILED_TICK_LOG_GAP:
                logger.exception("The clock's tick failed, so trying again at the next one")
                self._tick_error, self._tick_error_logged = said, moment

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
        # not rung - by the time it's noticed, the moment's passed. Each waits until
        # its satellite can hear it - see can_hear.
        if self._missed:
            with self._lock:
                missed, waiting, too_late = [], [], []
                for item in self._missed:
                    if self.can_hear is not None and not self.can_hear(item["satellite"]):
                        waiting.append(item)
                    elif now - item["due_at"] > MISSED_TIMER_GRACE_SECONDS:
                        too_late.append(item)
                    else:
                        missed.append(item)
                self._missed = waiting
                # One whose satellite was away for hours after the brain came back isn't
                # said once it's back - "That was 5 hours ago" is no use to anyone. It's
                # kept for "what did I miss" instead, as anything else missed is, from when
                # it came due - and only the last ten, as _record_unacknowledged keeps.
                for item in too_late:
                    item["missed_at"] = item["due_at"]
                self._unacknowledged = (self._unacknowledged + too_late)[-10:]
                # Let go of in the file before it's said, so a brain lost as it says it
                # never says it again.
                if missed or too_late:
                    self._save()
            for item in missed:
                late = format_duration(now - item["due_at"])
                if item.get("kind") == "alarm":
                    base, sound = f"your {self._describe_alarm(item)}", "announcement_sound.wav"
                else:
                    base = item["label"] or f"your {format_duration(item['duration'])} timer"
                    sound = self._timer_sound(item)
                self._announce(f"While I was off: {base}. That was {late} ago.", sound,
                               satellite=item["satellite"])

        # Whatever comes due is registered as ringing under the same lock that takes
        # it out of the store, so the copy a satellite is sent when on_change hears
        # of it still holds it (see copy_for) - sent in between, it held neither,
        # and a satellite which lost the brain part way through went quiet.
        started = []
        with self._changing():
            for timer_id, timer in list(self._timers.items()):
                if timer["due_at"] <= now:
                    started.append(self._ringing_entry(self._timers.pop(timer_id), "clock", now))

            # What rings is a copy of the alarm as it was when it came due. A repeating
            # alarm stays in the store and is moved on to its next occurrence straight
            # away, rather than once it stops ringing - otherwise a restart while it was
            # ringing (up to a quarter of an hour) would lose it from the file for good.
            for alarm_id, alarm in list(self._alarms.items()):
                if alarm["due_at"] <= now:
                    started.append(self._ringing_entry({**alarm, "kind": "alarm"}, "alarm", now))
                    if alarm.get("repeat"):
                        alarm["moved_on_from"] = alarm["due_at"]
                        alarm["due_at"] = self._following(alarm, alarm["due_at"])
                    else:
                        del self._alarms[alarm_id]

            self._ringing.extend(started)
            # The timers file holds what's ringing too, alarms included, so a restart
            # while one rings reports it as missed rather than losing it.
            if started:
                self._save()
            if any(entry["kind"] == "alarm" for entry in started):
                self._save_alarms()

        for entry in started:
            self._run_round(self._first_round, entry)

        # Repeats anything which went off and hasn't been dismissed yet - unless its
        # last round is still going.
        repeat_now = []
        with self._lock:
            for entry in self._ringing:
                if not entry["busy"] and entry["next_at"] <= now:
                    entry["busy"] = True
                    repeat_now.append(entry)

        for entry in repeat_now:
            self._run_round(self._continue_ringing, entry)

    def stop(self):
        """Stop the background thread, used when the assistant shuts down."""
        self._running = False
        logger.info("Clock stopped")
