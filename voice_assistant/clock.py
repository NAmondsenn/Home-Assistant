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
"""

import copy
import os
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Set

from voice_assistant import paths, storage
from voice_assistant.clock_words import (WEEKDAY_NAMES, ClockWords, _time_of_day_candidates, format_duration,
                                         parse_repeat, seconds_until)
from voice_assistant.clock_ringing import ALARM_SOUND, Ringing
# Where ringing now lives; named here too so the tests, and anything else which reached
# for these on the clock, still find them.
from voice_assistant.clock_ringing import ALARM_START_LEVEL  # noqa: F401
from voice_assistant.clock_words import spoken_clock_time  # noqa: F401
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Resolves paths relative to the project root, so saved timers live inside the project like everything else.
TIMERS_FILE = os.path.join(paths.DATA_DIR, "timers.json")
ALARMS_FILE = os.path.join(paths.DATA_DIR, "alarms.json")

# A timer which came due while the assistant was off is still announced if it was
# recent, anything older is ignored.
MISSED_TIMER_GRACE_SECONDS = 3600

# Waits until NTP corrects the time, so the clock is accurate on startup.
PLAUSIBLE_TIME_AFTER = 1735689600
# A tick which keeps failing the same way is logged again after this many seconds at
# most - it ticks twice a second, so one bad entry would otherwise flood the log.
FAILED_TICK_LOG_GAP = 60


class Clock(Ringing, ClockWords):
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
                      something the satellite rang itself. Called with the clock's
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
            alarm = {"id": alarm_id, "hour": hour, "minute": minute, "repeat": repeat, "due_at": due_at,
                     "satellite": str(satellite) if satellite else None}

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
                missed, waiting = [], []
                for item in self._missed:
                    ready = self.can_hear is None or self.can_hear(item["satellite"])
                    (missed if ready else waiting).append(item)
                self._missed = waiting
                # Let go of in the file before it's said, so a brain lost as it says it
                # never says it again.
                if missed:
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

    def set_timer(self, duration_seconds: Optional[int] = None, label: Optional[str] = None,
                  is_reminder: bool = False, at: Optional[str] = None,
                  satellite: Optional[str] = None) -> Dict:
        """
        Start a countdown timer, or a reminder if it carries a label.

        Args:
            duration_seconds: How long to count down for. Not needed when at is given.
            label: What to say when it goes off. Without one it's a plain timer,
                   with one it's a reminder.
            is_reminder: True when the user explicitly asked for a "reminder" (or
                         to be "reminded"), even if they haven't named it yet. A
                         reminder is identified by that name when it's cancelled
                         or announced, so one can't be created without one - if
                         this is True and no label was given, the user is asked
                         to name it instead of a nameless reminder being set.
            at: A clock time to count down to instead - see seconds_until.
            satellite: The satellite it was set from, which it rings on.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        # A clock time is turned into a duration here, rather than by the model. Left
        # to the model, "remind me at 4pm" meant clock arithmetic in seconds from a
        # time given only to the minute, and slips by a minute (or an hour) happened.
        if at:
            try:
                duration_seconds = seconds_until(at)
            except ValueError:
                return {"success": False, "message": "Sorry, I didn't catch what time for."}
            if duration_seconds <= 0:
                return {"success": False, "message": "That time has already passed."}

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
                self._pending_reminder = {"due_at": due_at, "duration": duration_seconds, "satellite": satellite}
            return {"success": False, "message": "What would you like to name this reminder?"}

        with self._changing():
            gone = self._gone(satellite)
            if gone:
                return gone
            clash = self._timer_clash(label, due_at)
            if clash:
                return {"success": False, "message": clash}

            timer_id = self._next_id
            self._next_id += 1
            self._timers[timer_id] = {
                "id": timer_id,
                "label": label,
                "duration": duration_seconds,
                "due_at": due_at,
                "satellite": satellite,
            }
            # Saved immediately, so the timer survives a restart moments later.
            self._save()

        spoken_duration = format_duration(duration_seconds)
        logger.info(f"Timer set for {spoken_duration}" + (f" ({label})" if label else ""), extra=TRANSCRIPT)

        if label:
            return {"success": True, "message": f"I'll remind you in {spoken_duration}."}
        return {"success": True, "message": f"Timer set for {spoken_duration}."}

    def _timer_clash(self, label: Optional[str], due_at: float) -> Optional[str]:
        """
        What to say if a timer would clash with one already set, else None. The caller
        is expected to already hold the lock.

        Two plain timers due in the same minute would be indistinguishable once
        they're both running, so this is caught up front rather than leaving the user
        with two timers they can't tell apart. A reminder is told apart by its name
        instead, so it only clashes with another of the same name - "remind me to
        take the pizza out in 10 minutes" isn't refused just because a 10 minute timer
        was set a moment earlier.

        Args:
            label: The timer's label, or None for a plain timer.
            due_at: When it's due, as an epoch time.
        """
        due_local = time.localtime(due_at)
        for existing in self._timers.values():
            if (label or "").strip().lower() != (existing["label"] or "").strip().lower():
                continue
            existing_local = time.localtime(existing["due_at"])
            if (due_local.tm_year, due_local.tm_yday, due_local.tm_hour, due_local.tm_min) == \
               (existing_local.tm_year, existing_local.tm_yday, existing_local.tm_hour, existing_local.tm_min):
                return f"You already have a {self._describe(existing)} for {self._spoken_due(existing['due_at'])}."
        return None

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

        return self.set_timer(remaining, label, is_reminder=True, satellite=pending["satellite"])

    def timer_count(self) -> int:
        """How many timers and reminders are counting down."""
        with self._lock:
            return len(self._timers)

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

        with self._changing():
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

            # Nothing matched. With a single timer running it's probably what was
            # meant, but deleting it unasked was too surprising - "cancel the pizza
            # reminder" quietly deleted an unrelated 10 minute timer - so it asks.
            # The answer comes back through the model, as "which one?" above does.
            if len(self._timers) == 1:
                timer = next(iter(self._timers.values()))
                return {"success": False,
                        "message": f"You only have the {self._describe(timer)} for "
                                   f"{self._spoken_due(timer['due_at'])}. Do you want me to cancel that?"}

            return {"success": False, "message": f"I couldn't find a timer for {label}."}

    def set_alarm(self, hour: int, minute: int, repeat=None, satellite: Optional[str] = None) -> Dict:
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
            satellite: The satellite it was set from, which it rings on.

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

        with self._changing():
            gone = self._gone(satellite)
            if gone:
                return gone
            clash = self._alarm_clash(hour, minute, repeat, due_at)
            if clash:
                return {"success": False, "message": clash}

            alarm_id = self._next_alarm_id
            self._next_alarm_id += 1
            alarm = {"id": alarm_id, "hour": hour, "minute": minute, "repeat": repeat, "due_at": due_at,
                     "satellite": satellite}
            self._alarms[alarm_id] = alarm
            self._save_alarms()

        logger.info(f"Alarm set: {self._describe_alarm(alarm)}")
        return {"success": True, "message": f"{self._describe_alarm(alarm)}."}

    def _gone(self, satellite: Optional[str]) -> Optional[Dict]:
        """
        What to say if something is being set for a satellite the brain no longer
        lets in, else None - see let_in. The caller is expected to already hold the lock.
        """
        if satellite and self.let_in is not None and not self.let_in(satellite):
            return {"success": False, "message": "That satellite's gone, so it can't ring there."}
        return None

    def _alarm_clash(self, hour: int, minute: int, repeat, due_at: float) -> Optional[str]:
        """
        What to say if an alarm would clash with one already set, else None. The
        caller is expected to already hold the lock.

        Same idea as a timer set for the same clock minute as another - two alarms
        due at once would be indistinguishable once they're ringing. Only alarms which
        would actually ring on the same day clash, though: a one-off 7am on Saturday
        is fine alongside a weekday 7am.

        Args:
            hour, minute: The time of day it's for.
            repeat: Its repeat pattern, as from parse_repeat.
            due_at: Its next occurrence.
        """
        for existing in self._alarms.values():
            if (existing["hour"] == hour and existing["minute"] == minute
                    and self._alarm_days_overlap(repeat, due_at, existing)):
                return f"You already have a {self._describe_alarm(existing)}."
        return None

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

    def overview(self, satellite: Optional[str] = None) -> Dict[str, List[Dict]]:
        """
        The timers, reminders and alarms set, for the settings page.

        Args:
            satellite: Only those which ring on this satellite, or None for all of them.

        Returns:
            Dict of "timers" and "alarms", each a list - soonest first - of their
            "id", "name" as Nova says it, and when they're next "due_at".
        """
        with self._lock:
            timers = sorted((timer for timer in self._timers.values()
                             if satellite is None or timer["satellite"] == satellite),
                            key=lambda timer: timer["due_at"])
            alarms = sorted((alarm for alarm in self._alarms.values()
                             if satellite is None or alarm["satellite"] == satellite),
                            key=lambda alarm: alarm["due_at"])
            return {"timers": [{"id": t["id"], "name": self._describe(t), "due_at": t["due_at"]} for t in timers],
                    "alarms": [{"id": a["id"], "name": self._describe_alarm(a), "due_at": a["due_at"]}
                               for a in alarms]}

    def remove(self, kind: str, item_id: int) -> Dict:
        """
        Removes one timer, reminder or alarm by its id - picked from a list on the
        settings page, where there's no question which one was meant.

        Args:
            kind: "alarm", or "timer" for a timer or reminder.
            item_id: Its id, from overview().

        Returns:
            Dict with 'success' and a 'message' - and once removed, 'removed', a copy
            of it as it was, for the page to keep so it can be put back (see put_back).
            The page keeps it to itself, never sending it on.
        """
        with self._changing():
            store = self._alarms if kind == "alarm" else self._timers
            item = store.pop(item_id, None)
            if item is None:
                return {"success": False, "message": "That's already gone."}
            if kind == "alarm":
                self._save_alarms()
            else:
                self._save()
        name = self._describe_alarm(item) if kind == "alarm" else self._describe(item)
        logger.info(f"Removed on the settings page: {name}", extra=TRANSCRIPT)
        return {"success": True, "message": f"Removed the {name}.", "removed": copy.deepcopy(item)}

    def put_back(self, kind: str, item: Dict, now: Optional[float] = None) -> Dict:
        """
        Puts back a timer, reminder or alarm just removed on the settings page, as it
        was - with its time, repeat, label and satellite, which is sent its copy again
        - unless that would ring straight away, or can't be told apart from one set
        since.

        Args:
            kind: "alarm", or "timer" for a timer or reminder.
            item: It, as remove() gave it.
            now: The time, as an epoch timestamp - the current time if None.

        Returns:
            Dict with 'success' and a 'message'.
        """
        item = copy.deepcopy(item)
        with self._changing():
            gone = self._gone(item.get("satellite"))
            if gone:
                return gone
            now = time.time() if now is None else now
            if item["due_at"] <= now:
                if kind != "alarm" or not item["repeat"]:
                    # A timer's done with, and so is a one-off alarm's only occurrence -
                    # put back, either would ring the moment the clock next ticked.
                    return {"success": False, "message": "Its time has passed, so it isn't put back."}
                # A repeating one carries on from its next occurrence, as one missed
                # while the brain was off does - see _load_alarms.
                item["due_at"] = self._next_occurrence(item["hour"], item["minute"], item["repeat"],
                                                       after=max(item["due_at"] + 60, now))

            if kind == "alarm":
                clash = self._alarm_clash(item["hour"], item["minute"], item["repeat"], item["due_at"])
                store = self._alarms
            else:
                clash = self._timer_clash(item["label"], item["due_at"])
                store = self._timers
            if clash:
                return {"success": False, "message": clash}

            # Ids are never given twice, so its own is free - the check is so nothing
            # is ever put over something else.
            if item["id"] in store:
                if kind == "alarm":
                    item["id"], self._next_alarm_id = self._next_alarm_id, self._next_alarm_id + 1
                else:
                    item["id"], self._next_id = self._next_id, self._next_id + 1
            store[item["id"]] = item
            if kind == "alarm":
                self._save_alarms()
            else:
                self._save()
        name = self._describe_alarm(item) if kind == "alarm" else self._describe(item)
        logger.info(f"Put back on the settings page: {name}", extra=TRANSCRIPT)
        return {"success": True, "message": f"Put back the {name}."}

    def move_satellite(self, from_satellite: Optional[str], to_satellite: Optional[str]) -> int:
        """
        Gives every timer and alarm which rings on one satellite to another - those
        set before there were satellites, when the Pi was all of Nova, to the
        default (from None), or a removed satellite's to the default, or to None
        with no satellite left, for the next one added. That takes in whatever's
        ringing, or waiting to be announced as missed, so it has somewhere to ring
        or be said.

        Args:
            from_satellite: The satellite they ring on now, or None for those on none.
            to_satellite: The satellite they should ring on, or None for none.

        Returns:
            How many it gave, counting what's ringing or missed - and those let go of
            as the other already had them.
        """
        def on(item: Dict, satellite: Optional[str]) -> bool:
            return (item["satellite"] or None) == satellite

        def same_alarm(item: Dict, other: Dict, occurrence: bool) -> bool:
            # The same time and days - alarms never take a label - and, for an
            # occurrence, the same one.
            same = ("hour", "minute", "repeat") + (("due_at",) if occurrence else ())
            return "hour" in item and "hour" in other and all(other[key] == item[key] for key in same)

        def doubled(item: Dict, theirs: List[Dict]) -> bool:
            # An occurrence the other satellite already has, so it would ring there twice.
            return any(on(other, to_satellite) and same_alarm(item, other, True) for other in theirs)

        def stored_twice(alarm: Dict) -> Optional[Dict]:
            # The other satellite's copy of an alarm in the store. A repeating one is
            # the same alarm whenever it's next due, as "skip just today" on one copy
            # only moves that copy on. Two one-offs at 7am, today's and tomorrow's,
            # are two alarms.
            occurrence = not alarm["repeat"]
            return next((other for other in self._alarms.values()
                         if other is not alarm and on(other, to_satellite) and same_alarm(alarm, other, occurrence)),
                        None)

        def answered(entry: Dict, dropped_ids: Set[int]) -> bool:
            # A ringing alarm is a copy made as it came due, and a repeating one's
            # stored copy moves on then - so the other satellite's stored copy past
            # this occurrence has had it already: rung, ringing or skipped. Rung
            # there, a one-off leaves no trace to match, but doubles only come from
            # older files, and only for the quarter of an hour it rings.
            alarm = entry["timer"]
            # Timers are numbered apart from alarms.
            if "hour" not in alarm:
                return False
            if alarm["id"] in dropped_ids:
                return True
            return bool(alarm["repeat"]) and any(
                on(other, to_satellite) and same_alarm(alarm, other, False) and other["due_at"] > alarm["due_at"]
                for other in self._alarms.values())

        with self._changing():
            timers = [timer for timer in self._timers.values() if on(timer, from_satellite)]
            alarms = [alarm for alarm in self._alarms.values() if on(alarm, from_satellite)]
            ringing = [entry for entry in self._ringing if on(entry["timer"], from_satellite)]
            missed = [item for item in self._missed if on(item, from_satellite)]

            # Each doubled one is let go, with what of it is ringing or missed - the
            # other's copy kept, as the later skip of the two.
            dropped = []
            for alarm in alarms:
                kept = stored_twice(alarm)
                if kept is not None:
                    kept["due_at"] = max(kept["due_at"], alarm["due_at"])
                    dropped.append(alarm)
            dropped_ids = {alarm["id"] for alarm in dropped}
            dropped_ringing = [entry for entry in ringing
                               if doubled(entry["timer"], [other["timer"] for other in self._ringing])
                               or answered(entry, dropped_ids)]
            dropped_missed = [item for item in missed if doubled(item, self._missed)]
            for alarm in dropped:
                del self._alarms[alarm["id"]]
            self._ringing = [entry for entry in self._ringing if not any(entry is gone for gone in dropped_ringing)]
            self._missed = [item for item in self._missed if not any(item is gone for gone in dropped_missed)]

            moved = (timers
                     + [alarm for alarm in alarms if not any(alarm is gone for gone in dropped)]
                     + [entry["timer"] for entry in ringing if not any(entry is gone for gone in dropped_ringing)]
                     + [item for item in missed if not any(item is gone for gone in dropped_missed)])
            for item in moved:
                item["satellite"] = to_satellite
            given = moved + dropped + dropped_ringing + dropped_missed
            # What's ringing, and what's missed, is saved with the timers - and what's
            # ringing is in each copy too.
            if timers or ringing or missed:
                self._save()
            if alarms:
                self._save_alarms()

        if given:
            logger.info(f"Gave {len(given)} timer(s) and alarm(s) from {from_satellite or 'no satellite'} "
                        f"to {to_satellite or 'no satellite, for the next one added'}"
                        + (f", letting go of {len(given) - len(moved)} it already had" if len(given) > len(moved) else ""))
        return len(given)

    def satellites(self) -> Set[Optional[str]]:
        """
        Returns:
            Each satellite something is set, ringing or missed on - None for those on none.
        """
        with self._lock:
            items = (list(self._timers.values()) + list(self._alarms.values())
                     + [entry["timer"] for entry in self._ringing] + self._missed)
            return {item["satellite"] or None for item in items}

    @staticmethod
    def _occurrence(item: Dict, due: Optional[float] = None) -> str:
        """
        The id of one occurrence of a timer or alarm, as a satellite knows it - see
        copy_for - e.g. "alarm-3@1792998000". A repeating alarm has one for each
        day it rings, so dismissing Monday's while away can't also dismiss
        Tuesday's. The kind is in it because timers and alarms are numbered
        separately, so timer 3 and alarm 3 are different things.

        Args:
            item: The timer or alarm, whether set, ringing or missed. Only an alarm
                  has a time of day, which is how the two are told apart.
            due: When the occurrence is due, if it isn't the item's own due_at.
        """
        kind = "alarm" if "hour" in item else "timer"
        return f"{kind}-{item['id']}@{int(item['due_at'] if due is None else due)}"

    def copy_for(self, satellite: str, within: float = 7 * 86400) -> List[Dict]:
        """
        What a satellite needs to ring its own timers and alarms while it can't
        reach the brain: each occurrence due within a while, with a repeating
        alarm's days spelt out, so all the satellite has to do is ring each one
        when it's due.

        Whatever's ringing on it now is in it too, due from when it began. The
        satellite only rings from its copy once it's lost the brain, so that's
        what lets it carry on ringing something it lost the brain part way through.

        Args:
            satellite: The satellite they ring on.
            within: How many seconds ahead to look.

        Returns:
            A list, soonest first, of each occurrence's "id" - which apply_away is
            given back - when it's "due" in epoch seconds, its "kind", "timer" or
            "alarm", and the "sound" it rings with.
        """
        with self._lock:
            until = time.time() + within
            copy = [{"id": self._occurrence(entry["timer"]), "due": entry["first_rung_at"],
                     "kind": "alarm" if entry["kind"] == "alarm" else "timer",
                     "sound": ALARM_SOUND if entry["kind"] == "alarm" else self._timer_sound(entry["timer"])}
                    for entry in self._ringing if entry["timer"]["satellite"] == satellite]
            copy += [{"id": self._occurrence(timer), "due": timer["due_at"], "kind": "timer",
                      "sound": self._timer_sound(timer)}
                     for timer in self._timers.values()
                     if timer["satellite"] == satellite and timer["due_at"] <= until]
            for alarm in self._alarms.values():
                if alarm["satellite"] != satellite:
                    continue
                due = alarm["due_at"]
                while due <= until:
                    copy.append({"id": self._occurrence(alarm, due), "due": due, "kind": "alarm",
                                 "sound": ALARM_SOUND})
                    if not alarm["repeat"]:
                        break
                    due = self._following(alarm, due)
        return sorted(copy, key=lambda entry: entry["due"])

    def apply_away(self, satellite: str, rang: List[str], dismissed: List[str], ringing: Iterable[str] = ()):
        """
        Catches up on what a satellite did while it couldn't reach the brain, which
        it reports by the ids copy_for gave it.

        What was dismissed there is dealt with as if it had been dismissed here: a
        timer goes, an alarm moves on to its next occurrence (or goes, if it doesn't
        repeat), and one which the brain is ringing as well is dismissed, as
        dismiss() would. What only rang is left to ring here, if the brain is ringing
        it too - nobody answered it, and the satellite can be reached again. One the
        brain missed, being off as it came due, isn't rung again if it had stopped
        ringing there - but one still ringing there, which the satellite stops as it
        reaches the brain, starts ringing here. None of them is said to have been
        missed afterwards, since each rang where it was meant to.

        Args:
            satellite: The satellite reporting.
            rang: The occurrences which rang there.
            dismissed: The occurrences dismissed there.
            ringing: The occurrences still ringing there when it reached the brain.

        Raises:
            OSError: What it was told couldn't be saved - so the satellite mustn't be
                told it has been, and the brain restarted would have lost it. Raised
                once all else is done, so what it took over still rings here.
        """
        rang, dismissed = set(rang), set(dismissed)
        ringing = set(ringing) - dismissed

        def reported(item, among):
            return item["satellite"] == satellite and self._occurrence(item) in among

        started = []
        with self._changing():
            timers = [timer for timer in self._timers.values() if reported(timer, dismissed)]
            for timer in timers:
                del self._timers[timer["id"]]

            moved = 0
            for alarm in list(self._alarms.values()):
                # Once for each occurrence dismissed in a row - two mornings away, say.
                while reported(alarm, dismissed):
                    moved += 1
                    if not alarm["repeat"]:
                        del self._alarms[alarm["id"]]
                        break
                    alarm["due_at"] = self._following(alarm, alarm["due_at"])

            # Stopped before it rings out, so no reminder of it goes to the phone
            # either (see _end_ringing).
            still_ringing = [entry for entry in self._ringing if not reported(entry["timer"], dismissed)]
            # What the brain missed while it was off, but the satellite was still
            # ringing, carries on ringing here - the brain restarted just as an alarm
            # came due, say. Left to be missed, it rang for the half a minute the
            # satellite took to reach the brain again, and was never even mentioned.
            now = time.time()
            for item in self._missed:
                if reported(item, ringing):
                    started.append(self._ringing_entry(item, "alarm" if item.get("kind") == "alarm" else "clock", now))
            still_ringing += started
            # Nor is "While I was off" said, once the brain's back, for something
            # which rang on the satellite meanwhile - or "You missed" for one which
            # rang out here but was dismissed there.
            missed = [item for item in self._missed if not reported(item, rang | dismissed | ringing)]
            unacknowledged = [item for item in self._unacknowledged if not reported(item, dismissed)]
            forgotten = (len(self._ringing) - len(still_ringing) + len(started) + len(self._missed) - len(missed)
                         + len(self._unacknowledged) - len(unacknowledged))
            self._ringing, self._missed, self._unacknowledged = still_ringing, missed, unacknowledged

            # A file an earlier save failed to write is written again too, or a report
            # told again once that changed nothing would be taken as saved. Only for a
            # report with something in it, though - one with nothing leaves the satellite
            # nothing to forget once it's settled, and with the disk still full, every
            # satellite reaching the brain would otherwise be hung up on.
            needed = bool(rang or dismissed or ringing)
            saved = True
            if timers or forgotten or (needed and self._timers_unsaved):
                saved = self._save()
            if moved or (needed and self._alarms_unsaved):
                saved = self._save_alarms() and saved

        for entry in started:
            self._run_round(self._first_round, entry)
        if rang or dismissed or ringing:
            logger.info(f"While {satellite} was away, {len(rang)} rang and {len(dismissed)} were dismissed; "
                        f"{len(started)} still ringing there ring on here")
        if not saved:
            raise OSError(f"Couldn't save what {satellite} said happened while it couldn't reach the brain")

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
                with self._changing():
                    removed = [self._alarms.pop(alarm_id) for alarm_id in ringing_ids if alarm_id in self._alarms]
                    if removed:
                        self._save_alarms()
                if removed:
                    return {"success": True, "message": f"{self._describe_alarm(removed[0])} cancelled."}
            return result

        with self._changing():
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

                # Nothing matched. With a single alarm set it's probably what was
                # meant, but cancelling it unasked is too surprising, so it asks.
                if not matches and len(self._alarms) == 1:
                    only = next(iter(self._alarms.values()))
                    return {"success": False,
                            "message": f"You only have the {self._describe_alarm(only)}. "
                                       f"Do you want me to cancel that?"}

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
                alarm["due_at"] = self._following(alarm, alarm["due_at"])
                self._save_alarms()
                return {"success": True, "message": f"Skipped today's {self._describe_alarm(alarm)}."}

            self._alarms.pop(alarm["id"], None)
            self._save_alarms()
            return {"success": True, "message": f"{self._describe_alarm(alarm)} cancelled."}

    def stop(self):
        """Stop the background thread, used when the assistant shuts down."""
        self._running = False
        logger.info("Clock stopped")
