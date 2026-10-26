"""
Clock Copies
What the satellites are sent of the clock: handing a satellite's timers and alarms to
another, the copy a satellite rings from while it can't reach the brain, and what it
says happened meanwhile when it comes back.

This is part of the Clock (see clock.py), which keeps the lock, the timers and alarms
and the saved files, and whose ringing (see clock_ringing.py) the copies hand over to.
"""

import logging
import time
from typing import Dict, Iterable, List, Optional, Set

from voice_assistant.clock_ringing import ALARM_SOUND

# So every line logged here keeps the name it had when this code was in clock.py.
logger = logging.getLogger("voice_assistant.clock")


class SatelliteCopies:
    """
    Part of Clock, which uses the lock and the stores clock.py sets up: handing timers
    and alarms between satellites, the copy a satellite is sent, and applying what it
    reports back.
    """

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

        def answered(entry: Dict) -> bool:
            # The other satellite's own copy of a repeating alarm notes the occurrence
            # it last moved on from there - rung, dismissed or skipped - so this one
            # has been had there already. Only being due past it isn't enough: the
            # same alarm set there just after this one came due is too, and never rang.
            # A ringing one-off has already left the store, and two ringing at once are
            # caught by doubled.
            #
            # Accepted: after a brain restart, a satellite removed while its copy rings,
            # before the other has said it rang its own, rings once more there. That's
            # the safe side.
            alarm = entry["timer"]
            # Timers are numbered apart from alarms.
            if "hour" not in alarm:
                return False
            return bool(alarm["repeat"]) and any(
                on(other, to_satellite) and same_alarm(alarm, other, False)
                and int(other.get("moved_on_from", 0)) == int(alarm["due_at"])
                for other in self._alarms.values())

        with self._changing():
            timers = [timer for timer in self._timers.values() if on(timer, from_satellite)]
            alarms = [alarm for alarm in self._alarms.values() if on(alarm, from_satellite)]
            ringing = [entry for entry in self._ringing if on(entry["timer"], from_satellite)]
            missed = [item for item in self._missed if on(item, from_satellite)]

            # Worked out before the stored copies are merged below, which would change
            # what answered sees.
            dropped_ringing = [entry for entry in ringing
                               if doubled(entry["timer"], [other["timer"] for other in self._ringing])
                               or answered(entry)]

            # Each doubled one is let go, with what of it is ringing or missed - the
            # other's copy kept, as the later skip of the two, and with it the
            # occurrence that one moved on from.
            dropped = []
            for alarm in alarms:
                kept = stored_twice(alarm)
                if kept is not None:
                    if alarm["due_at"] > kept["due_at"] and "moved_on_from" in alarm:
                        kept["moved_on_from"] = alarm["moved_on_from"]
                    kept["due_at"] = max(kept["due_at"], alarm["due_at"])
                    dropped.append(alarm)
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
                    alarm["moved_on_from"] = alarm["due_at"]
                    alarm["due_at"] = self._following(alarm, alarm["due_at"])

            # One the brain missed, being off as it came due, was moved on by
            # _load_alarms, so the loop above never sees it - but it rang there, from
            # the satellite's copy, so it's noted as answered there all the same.
            for item in self._missed:
                # Timers are numbered apart from alarms.
                alarm = self._alarms.get(item["id"]) if "hour" in item else None
                if (alarm is not None and alarm["repeat"] and alarm["due_at"] > item["due_at"]
                        and reported(item, rang | dismissed | ringing)):
                    alarm["moved_on_from"] = item["due_at"]
                    moved += 1

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
