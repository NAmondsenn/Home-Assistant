"""
Clock Ringing
How the Clock rings: the rounds of ringing a timer, reminder or alarm goes through until
someone answers it, the alarm's rising volume, and what dismissing, silencing and the
missed list do.

This is part of the Clock (see clock.py), which keeps the lock, the timers and alarms
and the saved files, and whose tick decides what has come due and starts the ringing.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from voice_assistant.clock_words import _time_of_day_candidates, format_duration, spoken_clock_time
from voice_assistant.logging_setup import TRANSCRIPT

# So every line logged here keeps the name it had when this code was in clock.py.
logger = logging.getLogger("voice_assistant.clock")

# How loud an alarm's first burst plays, as a share of full volume - see _alarm_level.
ALARM_START_LEVEL = 0.3
# What an alarm rings with, here and on a satellite ringing from its copy (see copy_for).
ALARM_SOUND = "alarm_sound.wav"


class Ringing:
    """
    Part of Clock, which uses the lock and the stores clock.py sets up: the rounds of
    ringing, and how they are answered, silenced or missed.
    """

    def _run_round(self, round_function: Callable[[Dict], None], entry: Dict):
        """
        Runs one round of ringing, on a thread of its own so the clock carries on
        checking everything else meanwhile.

        A round can take minutes - an alarm cycle is two and a half minutes of
        sound, and any round can wait up to a minute for a conversation to finish.
        Run on the clock's own thread, that held up every other timer, so one which
        came due during an alarm went off minutes late.

        Args:
            round_function: _first_round or _continue_ringing.
            entry: The ringing entry, already marked busy.
        """
        if self._rounds_on_threads:
            threading.Thread(target=round_function, args=(entry,), daemon=True,
                             name=f"ringing-{entry['kind']}").start()
        else:
            round_function(entry)

    def _ringing_entry(self, item: Dict, kind: str, now: float) -> Dict:
        """
        The ringing entry for something which has just come due - a timer,
        reminder, or alarm. _tick registers it as ringing before it speaks, so
        dismissing it part way through stops it immediately.

        Args:
            item: The timer or alarm which finished.
            kind: "clock" for a timer/reminder, "alarm" for an alarm - decides
                  which rules and which sound apply.
            now: The moment it came due.

        Returns:
            The entry, ready to go on _ringing.
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
                "sounding": False,
                # True while one of its rounds is playing, so _tick doesn't start
                # another on top of it - see _run_round.
                "busy": True}
        return entry

    def _first_round(self, entry: Dict):
        """
        Rings the first round for something which has just come due - see
        _ringing_entry.
        """
        still_ringing = self._ring_once(entry)

        # Timed from when it actually finished speaking, not from when it was
        # due. An announcement held back while the user was talking to the
        # assistant would otherwise be followed immediately by the next one,
        # bunching them together.
        entry["next_at"] = time.time() + entry["gap"]

        # left is None for a timer or reminder, which are bounded by give_up_at
        # instead - see _repeat_settings. An alarm can end on the very first round
        # if it only has one, which the current default doesn't, but is possible.
        if not still_ringing or (entry["left"] is not None and entry["left"] <= 0):
            self._end_ringing(entry)
        entry["busy"] = False

    def _continue_ringing(self, entry: Dict):
        """
        Rings the next round for something already ringing, once _tick finds it's
        due again (see _run_round). Ends it - dismissed or not - once its rounds
        run out, or once it's been ringing longer than it's allowed to.
        """
        # A timer or reminder is stopped by the clock rather than by a round count.
        # Checked before it speaks rather than after, so the last thing the user
        # hears is a full round and not one cut off part way through.
        if entry["give_up_at"] is not None and time.time() >= entry["give_up_at"]:
            self._end_ringing(entry)
            entry["busy"] = False
            return

        if entry["left"] is not None:
            entry["left"] -= 1
        # left of None means it isn't counting rounds at all - give_up_at above is
        # what ends it instead.
        still_going = entry["left"] is None or entry["left"] > 0

        still_ringing = self._ring_once(entry)

        if still_going and still_ringing:
            # Timed from when it finished speaking, so a round delayed by a
            # conversation doesn't run straight into the next one.
            entry["next_at"] = time.time() + entry["gap"]
        else:
            self._end_ringing(entry)
        entry["busy"] = False

    def _end_ringing(self, entry: Dict):
        """
        Ends one ringing entry which ran out without being acknowledged, noting it
        so the user can ask what they missed. Nothing needs rescheduling here -
        everything was already dealt with in its store when it started ringing.
        """
        # As any other change, so it goes from each satellite's copy too.
        with self._changing():
            finished = entry in self._ringing
            if finished:
                self._ringing.remove(entry)
                self._save()
        if not finished:
            # Already removed - dismiss() got there first and handled this itself.
            return

        self._record_unacknowledged(entry["timer"])
        if self.on_unanswered:
            try:
                self.on_unanswered(entry["timer"], entry["kind"], satellite=entry["timer"]["satellite"])
            except Exception as e:
                # Passing it on must never stop the clock's thread.
                logger.error(f"Couldn't pass on an unanswered {entry['kind']}: {e}")

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

    def _ring_once(self, entry: Dict) -> bool:
        """
        Plays one round for whatever's ringing - a timer/reminder announcement, or
        one alarm cycle. A later round is worded the same as the first: the user
        hasn't heard it yet, or they'd have dismissed it.

        Args:
            entry: The ringing entry (see _ringing_entry).

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
            self._sound_off(item, boost=boost)
            return self._is_ringing(item)
        finally:
            entry["sounding"] = False

    def _sound_off(self, timer: Dict, boost: bool = False):
        """
        Announces a timer or reminder which has come due.

        Announced twice in a row - sound, message, brief pause, then the same again
        - so a moment's inattention doesn't mean missing it entirely.

        Args:
            timer: The timer which finished.
            boost: True once a timer has been ringing unanswered long enough that
                   it should jump to full volume, regardless of the configured
                   alarm level. Never set for a reminder - see _repeat_settings.
        """
        # A labelled timer is a reminder, so the label is the message. It's
        # introduced rather than spoken bare, since a phrase out of nowhere isn't
        # obviously a reminder to someone across the room.
        if timer["label"]:
            message = f"This is a reminder: {timer['label']}"
        else:
            message = f"Timer for {format_duration(timer['duration'])}."
        sound = self._timer_sound(timer)

        for announcement in range(self.announcements):
            # Stops mid-round if the user dismisses it, rather than talking over them.
            if announcement and not self._is_ringing(timer):
                return
            self._announce(message, sound, boost, still_wanted=lambda: self._is_ringing(timer),
                           satellite=timer["satellite"])
            if announcement < self.announcements - 1:
                time.sleep(self.repeat_gap)

    @staticmethod
    def _timer_sound(timer: Dict) -> str:
        """
        The sound a timer or reminder rings with. A plain timer uses its own more
        insistent sound rather than the general announcement chime, since it's
        something to act on right away rather than hear about eventually.
        """
        return "announcement_sound.wav" if timer["label"] else "timer_beep.wav"

    def _ring_alarm(self, alarm: Dict) -> bool:
        """
        Rings an alarm for one cycle of alarm_ring_duration, stopping early if
        dismissed. Each cycle opens with a spoken cut-in (so it's clear what's
        actually going off, and what time it is, even for a later cycle), then
        carries on through alarm_sound.wav in bursts, so nothing after the cut-in
        is waiting on speech synthesis while it's trying to be loud.

        It used to play the sound without a break, at full volume from the first
        second - and right next to the microphone, that could drown out "Hey Nova,
        stop" altogether. Now each burst is followed by a short gap to say it into,
        and each cycle starts quieter and builds up to full volume.

        Args:
            alarm: The alarm which finished.

        Returns:
            True if it's still ringing (not dismissed) by the end of the cycle.
        """
        def still_wanted():
            return self._is_ringing(alarm)

        started = time.time()
        now = datetime.now()
        cutin = f"It's {spoken_clock_time(now.hour, now.minute)}. Time to get up."
        self._announce(cutin, ALARM_SOUND, boost=True, still_wanted=still_wanted, satellite=alarm["satellite"],
                       start=0.0, length=self.alarm_burst, level=self._alarm_level(0.0))
        if not self._is_ringing(alarm):
            return False

        # Where the next burst picks up in the sound, so the bursts carry on through
        # it rather than repeating its first few seconds over and over.
        position = self.alarm_burst
        end_at = started + self.alarm_ring_duration
        while time.time() < end_at:
            if not self._wait_while_ringing(alarm, self.alarm_burst_gap):
                return False
            self._announce(None, ALARM_SOUND, boost=True, still_wanted=still_wanted, satellite=alarm["satellite"],
                           start=position, length=self.alarm_burst,
                           level=self._alarm_level(time.time() - started))
            position += self.alarm_burst

        return self._is_ringing(alarm)

    def _alarm_level(self, elapsed: float) -> float:
        """
        How loud an alarm burst plays, as a share of full volume: ALARM_START_LEVEL
        at the start of a cycle, building up to full over alarm_ramp seconds.

        Args:
            elapsed: Seconds since the cycle started.
        """
        if self.alarm_ramp <= 0:
            return 1.0
        return min(1.0, ALARM_START_LEVEL + (1.0 - ALARM_START_LEVEL) * elapsed / self.alarm_ramp)

    def _wait_while_ringing(self, alarm: Dict, seconds: float) -> bool:
        """
        Waits through the gap after an alarm burst, finishing early if it's dismissed.

        Returns:
            True if it's still ringing at the end of the gap.
        """
        end = time.time() + seconds
        while time.time() < end:
            if not self._is_ringing(alarm):
                return False
            time.sleep(min(0.1, max(0.0, end - time.time())))
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
            self._keep_unacknowledged([timer], time.time())

    def _keep_unacknowledged(self, items: List[Dict], missed_at: Optional[float] = None):
        """
        Keeps what went without the user hearing it, for "what did I miss". Called with
        the lock held.

        Args:
            items: The timers, reminders and alarms missed.
            missed_at: When they were missed, as an epoch timestamp - or None for when
                each came due.
        """
        for item in items:
            item["missed_at"] = item["due_at"] if missed_at is None else missed_at
        # Only the recent ones are worth keeping, so this can't grow forever.
        self._unacknowledged = (self._unacknowledged + items)[-10:]

    def is_ringing(self, satellite: Optional[str] = None, unowned: bool = False) -> bool:
        """
        Whether a timer or reminder is going off and waiting to be acknowledged.

        Args:
            satellite: Only what rings on this satellite - what can be heard where the
                user is talking - or None for anywhere.
            unowned: Whether what rings on no satellite counts as ringing on this one,
                for the default, where it is heard.
        """
        with self._lock:
            return any(self._rings_on(entry, satellite, unowned) for entry in self._ringing)

    @staticmethod
    def _rings_on(entry: Dict, satellite: Optional[str], unowned: bool = False) -> bool:
        """
        Whether a ringing entry rings on a satellite - any, for None. One set before
        there were satellites rings on none, so isn't on any by name, but is heard on the
        default - see Brain.session_for - so counts as on it with unowned.
        """
        rings_on = entry["timer"]["satellite"]
        return satellite is None or rings_on == satellite or (unowned and rings_on is None)

    @staticmethod
    def _is_insistent(entry: Dict) -> bool:
        """
        Whether a ringing entry is one which keeps demanding attention - a timer or
        an alarm - rather than a reminder, which says its piece and waits.
        """
        return entry["kind"] == "alarm" or not entry["timer"]["label"]

    def ringing_state(self, satellite: Optional[str] = None) -> Dict:
        """
        What's going off right now, split by how it should be treated when the user
        starts talking. Meant to be read at the moment the wake word fires, since
        what matters is what the user could actually hear when they spoke.

        Args:
            satellite: Only what rings on the satellite the user is talking to - all
                they could hear - or None for anywhere.

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
            entries = [entry for entry in self._ringing if self._rings_on(entry, satellite)]

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
        right (see session.py) - and the missed list is left alone too, since this
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
        with self._changing():
            keep, silenced = [], []
            for entry in self._ringing:
                wanted = self._is_insistent(entry) and (heard is None or any(entry is h for h in heard))
                (silenced if wanted else keep).append(entry)
            self._ringing = keep
            if silenced:
                self._save()

        if silenced:
            logger.info(f"Silenced {len(silenced)} timer(s)/alarm(s) - the user was talking")
        return bool(silenced)

    def dismiss(self, satellite: Optional[str] = None, unowned: bool = False) -> Dict:
        """
        Acknowledges anything currently going off, stopping it repeating.

        For a repeating alarm this only silences today's occurrence - its next
        one was already scheduled when this one went off. Fully cancelling the
        repeat is a separate, explicit request (see cancel_alarm), never implied
        by just dismissing it.

        Args:
            satellite: Only what rings on the satellite the user is talking to - a
                "stop" in the kitchen mustn't silence the bedroom's alarm, which
                can't be heard there - or None for anywhere.
            unowned: Whether what rings on no satellite is dismissed too, for the
                default, where it is heard - see is_ringing.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        with self._changing():
            ringing = [entry for entry in self._ringing if self._rings_on(entry, satellite, unowned)]
            self._ringing = [entry for entry in self._ringing if not self._rings_on(entry, satellite, unowned)]
            # Dismissing also clears what was missed, since the user has now heard it.
            heard = [timer for timer in self._unacknowledged if satellite is None or timer["satellite"] == satellite]
            missed = len(heard)
            self._unacknowledged = [timer for timer in self._unacknowledged
                                    if not any(timer is other for other in heard)]
            if ringing:
                self._save()

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
                  still_wanted: Optional[Callable[[], bool]] = None, satellite: Optional[str] = None,
                  **playback):
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
            satellite: The satellite it rings on - the one it was set from.
            playback: Which part of the sound to play, and how loud - start and length
                      in seconds, and level as a share of the volume. Only an alarm's
                      bursts give these - see _ring_alarm.
        """
        # An alarm's bursts have no message, and come every few seconds, so they
        # aren't logged one by one.
        if message:
            logger.info(f"Timer finished: {message}", extra=TRANSCRIPT)

        if self.on_timer_finished:
            try:
                self.on_timer_finished(message, sound, boost, still_wanted=still_wanted, satellite=satellite,
                                       **playback)
            except Exception as e:
                # A failed announcement must not kill the timer thread.
                logger.error(f"Timer announcement failed: {e}")

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
        with self._changing():
            silenced = [entry for entry in self._ringing if entry["kind"] == kind]
            self._ringing = [entry for entry in self._ringing if entry["kind"] != kind]
            # Saved as dismiss() saves, so a restart doesn't report a timer silenced
            # here as missed - and so on_change hears of it.
            if silenced:
                self._save()
        return len(silenced)
