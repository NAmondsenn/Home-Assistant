"""
Clock Requests
Setting, listing and cancelling timers, reminders and alarms: what is said to set one,
the clashes it is checked for, how they are described and counted, and removing one or
putting it back.

This is part of the Clock (see clock.py), which keeps the lock, the timers and alarms
and the saved files these read and change.
"""

import copy
import logging
import time
from typing import Dict, List, Optional

from voice_assistant.clock_words import _time_of_day_candidates, format_duration, parse_repeat, seconds_until
from voice_assistant.logging_setup import TRANSCRIPT

# So every line logged here keeps the name it had when this code was in clock.py.
logger = logging.getLogger("voice_assistant.clock")


class ClockRequests:
    """
    Part of Clock, which uses the lock and the stores clock.py sets up: setting, listing
    and cancelling timers, reminders and alarms.
    """

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
                alarm["moved_on_from"] = alarm["due_at"]
                alarm["due_at"] = self._following(alarm, alarm["due_at"])
                self._save_alarms()
                return {"success": True, "message": f"Skipped today's {self._describe_alarm(alarm)}."}

            self._alarms.pop(alarm["id"], None)
            self._save_alarms()
            return {"success": True, "message": f"{self._describe_alarm(alarm)} cancelled."}
