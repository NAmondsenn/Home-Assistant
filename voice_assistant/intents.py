"""
Local Intents Module.

Handles the handful of everyday commands which don't need a language model - "set a
timer for ten minutes", "pause", "skip", "turn it up", "what time is it" - without
going to Claude at all.

Every request used to go to Claude first, so even these paid for a round trip to
the cloud, and when the internet dropped, timers and the music controls stopped
working altogether. The patterns here are deliberately strict: only a whole request
which is exactly one of these is handled locally. Anything else - "set a timer for
ten minutes and play Drake", "pause it in a bit" - still goes to Claude as before.
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from voice_assistant.actions import Action
from voice_assistant.clock import spoken_clock_time
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90,
}

UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}

# Said around a request without changing it - "could you pause the music please".
POLITE_START = r"^(?:please |can you |could you |would you |will you )+"
POLITE_END = r"(?: please| thanks| thank you)+$"

# A light's name - "kitchen", "living room" - of up to three words, none of them
# "and", so "turn off the timer and the lights" is left to Claude rather than all
# sent to Home Assistant as if it were one light.
LIGHT = r"(?:all )?(?:the |my )?(?:(?!and\b)[a-z']+ ){0,3}(?:lights?|lamps?)"

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
# The regular things asked about on the calendar - "what time do I have work?". What
# each is called on the calendar is up to Home Assistant's settings; anything else is
# left to Claude.
ACTIVITIES = {"work": "work", "working": "work", "shift": "work", "shifts": "work", "uni": "uni",
              "university": "uni", "college": "college", "school": "school", "lecture": "uni", "lectures": "uni",
              "class": "uni", "classes": "uni"}
ACTIVITY = r"(?P<activity>" + "|".join(ACTIVITIES) + ")"

# A day asked about by name - anything less plain, like "next Thursday" (this week's
# or next week's?), is left to Claude.
DAY = r"(?P<day>today|tonight|tomorrow|this weekend|the weekend|" + "|".join(WEEKDAYS) + ")"


def _number(words: str) -> Optional[int]:
    """
    A number said as digits or words, e.g. "10", "ten", "twenty five".

    Returns:
        The number, or None if it isn't one - which keeps the request away from the
        local path rather than guessing at it.
    """
    if words.isdigit():
        return int(words)
    parts = words.split()
    if len(parts) == 1:
        return NUMBER_WORDS.get(parts[0])
    if len(parts) == 2 and parts[0] in ("twenty", "thirty", "forty", "fifty") \
            and NUMBER_WORDS.get(parts[1], 10) < 10:
        return NUMBER_WORDS[parts[0]] + NUMBER_WORDS[parts[1]]
    return None


def calendar_days(day: Optional[str], today: date) -> Tuple[date, int]:
    """
    The days a plainly named day means.

    Args:
        day: e.g. "tomorrow", "thursday", "this weekend", or None for today.
        today: Today's date.

    Returns:
        (first day, how many days) - a weekday is the next one, or today if it's
        that day now, and the weekend is what's left of it.
    """
    if day in (None, "today", "tonight"):
        return today, 1
    if day == "tomorrow":
        return today + timedelta(days=1), 1
    if day in ("this weekend", "the weekend"):
        if today.weekday() == 6:
            return today, 1
        if today.weekday() == 5:
            return today, 2
        return today + timedelta(days=5 - today.weekday()), 2
    return today + timedelta(days=(WEEKDAYS.index(day) - today.weekday()) % 7), 1


def _ordinal(day: int) -> str:
    """A day of the month as it's said, e.g. 4 -> "4th"."""
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


class LocalIntents:
    """Recognises the everyday commands handled without Claude."""

    def __init__(self, clock=None):
        """
        Args:
            clock: The Clock, to check there's exactly one timer before cancelling
                "the timer". Without it, that's left to Claude.
        """
        self.clock = clock
        # Each pattern has to match the whole request, after politeness is taken off.
        self._rules = [
            (r"(?:(?:set|start) (?:me )?(?:a |an )?)?timer for (?P<amount>[a-z0-9 ]+?) (?P<unit>second|minute|hour)s?",
             self._timer),
            (r"(?:(?:set|start) (?:me )?(?:a |an )?)?(?P<amount>[a-z0-9 ]+?) (?P<unit>second|minute|hour) timer",
             self._timer),
            # "Soap", "scap" and "quiter" are how Moonshine heard "stop", "skip" and
            # "quieter" said on their own - see speech_to_text.py.
            (r"(?:pause|stop|soap)(?: the)?(?: music| song| spotify)?", self._playback("pause")),
            (r"(?:resume|unpause|play)(?: the)?(?: music| spotify)?|carry on playing", self._resume),
            (r"(?:skip|next|scap)(?: (?:this|the))?(?: song| track| one)?", self._playback("skip")),
            (r"(?:previous|last) (?:song|track)|go back(?: a (?:song|track))?", self._playback("previous")),
            (r"turn (?:it|the volume|the music) (?P<direction>up|down)|volume (?P<direction2>up|down)",
             self._volume_step),
            (r"louder", lambda match: self._volume({"direction": "up"})),
            (r"quieter|softer|quiter", lambda match: self._volume({"direction": "down"})),
            (r"(?:set (?:the )?)?volume (?:to )?(?P<level>\d{1,3})(?: percent)?", self._volume_level),
            (r"what(?:'s| is) the time|what time is it(?: now)?|tell me the time", self._time),
            (r"what(?:'s| is) (?:the date|today's date)(?: today)?|what day is it(?: today)?", self._date),
            (r"(?:cancel|stop|delete) (?:the|my) timer", self._cancel_only_timer),
            (r"what(?:'s| is| will) the weather(?: (?:going to )?be)?(?: like)?(?: (?P<day>today|tomorrow))?"
             r"|what(?:'s| is) the forecast(?: for)?(?: (?P<day2>today|tomorrow))?", self._weather),
            # The lights go straight to Home Assistant, which works out which ones
            # from the words itself - and still works with the internet down.
            (rf"(?:turn|switch) (?:on|off) {LIGHT}|(?:turn|switch) {LIGHT} (?:on|off)|{LIGHT} (?:on|off)"
             rf"|(?:dim|brighten) {LIGHT}|set {LIGHT} to \d{{1,3}}(?: percent)?", self._lights),
            # The calendar, for a day asked about by name. One rule each, since a
            # pattern can only name the day once.
            (rf"what(?:'s| is) (?:on )?(?:my )?(?:calendar|schedule|diary|agenda)(?:(?: (?:on|for))? {DAY})?",
             self._calendar),
            (rf"what(?:'s| is) (?:on|planned|happening)(?: (?:on|for))? {DAY}", self._calendar),
            (rf"what (?:have i|do i have)(?: got)?(?: on| planned| going on| happening)?(?: (?:on|for))? {DAY}",
             self._calendar),
            (rf"(?:am i|are we) (?:busy|free)(?: (?:on|for))? {DAY}", self._calendar),
            (rf"do i have anything(?: on| planned)?(?: (?:on|for))? {DAY}", self._calendar),
            # Work, uni and the like, from the calendar - "what time do I have work
            # tomorrow?". Without a day, it's today.
            (rf"(?:what time|when) (?:do i|am i|is)(?: have| start| finish| have to be at| got)? (?:at |in )?"
             rf"{ACTIVITY}(?:(?: on| for)? {DAY})?", self._activity),
            (rf"(?:am i|do i have|have i got) (?:at |in )?{ACTIVITY}(?:(?: on| for)? {DAY})?", self._activity),
            # When it's next - "when's my next shift?" is just the one, "what are my
            # next three shifts?" three.
            (rf"when (?:do i|am i) next (?:have |at |in |got )?{ACTIVITY}", self._next_activity),
            (rf"(?:when|what)(?:'s| is) my next {ACTIVITY}", self._next_activity),
            (rf"(?:when|what)(?:'re| are) my next (?P<count>[a-z0-9]+) {ACTIVITY}", self._next_activity),
        ]

    def match(self, text: str) -> Optional[Dict]:
        """
        Handles a request locally, if it's one of the everyday commands.

        Args:
            text: What the user said.

        Returns:
            A result shaped like LLMHandler.process_query's - a "response" and a list
            of "actions" - or None to leave the request to Claude.
        """
        request = re.sub(r"[^a-z0-9' ]", " ", text.lower())
        request = re.sub(r"\s+", " ", request).strip()
        request = re.sub(POLITE_END, "", re.sub(POLITE_START, "", request)).strip()

        for pattern, handle in self._rules:
            found = re.fullmatch(pattern, request)
            if not found:
                continue
            result = handle(found)
            if result is not None:
                logger.info(f"Handled locally: '{text}'" + (f" (actions: {result['actions']})"
                                                            if result["actions"] else ""), extra=TRANSCRIPT)
                return result
        return None

    @staticmethod
    def _result(response: str, actions: Optional[List[Action]] = None) -> Dict:
        return {"response": response, "actions": actions or [], "success": True}

    def _timer(self, found) -> Optional[Dict]:
        amount = found["amount"]
        # "Half an hour" is the one fraction said often enough to be worth knowing.
        if amount in ("half an", "half a") and found["unit"] == "hour":
            seconds = 1800
        else:
            number = _number(amount)
            if number is None:
                return None
            seconds = number * UNIT_SECONDS[found["unit"]]
        return self._result("Okay.", [Action("set_timer", {"duration_seconds": seconds})])

    def _playback(self, command: str):
        return lambda found: self._result("Okay.", [Action("control_playback", {"action": command})])

    def _resume(self, found) -> Dict:
        # A play with nothing named carries on with whatever was playing.
        return self._result("Okay.", [Action("play_music")])

    def _volume(self, change: Dict) -> Dict:
        # Which volume is left to the executor, which turns the music down if it's
        # playing and the assistant's voice down if not.
        return self._result("Okay.", [Action("set_volume", change)])

    def _volume_step(self, found) -> Dict:
        return self._volume({"direction": found["direction"] or found["direction2"]})

    def _volume_level(self, found) -> Optional[Dict]:
        level = int(found["level"])
        return self._volume({"level": level}) if level <= 100 else None

    def _time(self, found) -> Dict:
        now = datetime.now()
        return self._result(f"It's {spoken_clock_time(now.hour, now.minute)}.")

    def _date(self, found) -> Dict:
        now = datetime.now()
        return self._result(f"It's {now.strftime('%A')} the {_ordinal(now.day)} of {now.strftime('%B')}.")

    def _lights(self, found) -> Dict:
        return self._result("Okay.", [Action("control_home", {"command": found.string})])

    def _calendar(self, found) -> Dict:
        first, days = calendar_days(found["day"], date.today())
        return self._result("Okay.", [Action("get_calendar", {"start": first.isoformat(), "days": days})])

    def _activity(self, found) -> Dict:
        first, days = calendar_days(found["day"], date.today())
        return self._result("Okay.", [Action("get_activity", {"activity": ACTIVITIES[found["activity"]],
                                                              "start": first.isoformat(), "days": days})])

    def _next_activity(self, found) -> Optional[Dict]:
        count = _number(found.groupdict().get("count") or "one")
        if not count:
            return None
        return self._result("Okay.", [Action("get_activity", {"activity": ACTIVITIES[found["activity"]],
                                                              "start": date.today().isoformat(), "count": count})])

    def _weather(self, found) -> Dict:
        day = found["day"] or found["day2"] or "today"
        return self._result("Okay.", [Action("get_weather", {"day": day})])

    def _cancel_only_timer(self, found) -> Optional[Dict]:
        # Only when there's exactly one, so there's no question which was meant.
        if not self.clock or self.clock.timer_count() != 1:
            return None
        return self._result("Okay.", [Action("cancel_timer")])
