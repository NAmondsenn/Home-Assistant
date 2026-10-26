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

After all of those, a plain request for something of Home Assistant's - "pause the
TV", "run goodnight" - goes straight to its Assist, so Nova's own commands always
come first.
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from voice_assistant.actions import Action
from voice_assistant.clock import spoken_clock_time
from voice_assistant.lists import names_list
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
# Where the activity rules have the activity - "what time do I have work?". Each of the
# user's calendars but their own is one, by its name, so the words are Google's, put in
# here at each match - see LocalIntents._activity_rule. Until then, any words will do.
ACTIVITY = r"(?P<activity>[a-z0-9' ]+?)"
# The bare shapes before an activity - "when's sunset?", "am I at home?" - in which an
# activity's first word alone is an everyday question, not "Sunset yoga" or "Home group",
# unless a day follows.
BARE_BEFORE_ACTIVITY = r"(?:when is|when's|am i)(?: at| in)? "

# The devices a battery is asked about by - the kind words batteries.find knows. A
# device with a name of its own ("the S25 Ultra") is left to Claude.
BATTERY_DEVICE = "mac|phone|tablet|laptop|computer"
# And the devices found or rung by - those the Home Assistant app is on.
FOUND_DEVICE = "phone|tablet|mac|laptop"
# The PC, as ha_guard.PC_LOCK has it - see pc.py.
PC = r"(?:my |the )?(?:pc|computer)"

# A thing of Home Assistant's for Assist - "the TV", "AdGuard protection", "movie
# night" - of one to four words, none of them "and", "then" or "plus", so "turn off
# the TV and the lamp" is left to Claude rather than sent as one thing. "The" or "my"
# is never the name itself, so "set my volume to 40" isn't the volume of something
# called "my".
THING_WORD = r"(?!(?:and|then|plus)\b)[a-z0-9']+"
THING = rf"(?:all the |the |my )?(?P<thing>(?!(?:the|my)\b){THING_WORD}(?: {THING_WORD}){{0,3}})"

# Words for Nova's own things, which Assist mustn't be given in their place - "turn
# off the timer" is Nova's timer, not a Home Assistant helper of that name. Left to
# Claude, which knows Nova's tools.
NOVAS_OWN = {"timer", "timers", "alarm", "alarms", "reminder", "reminders", "music", "song", "songs", "track",
             "tracks", "playlist", "volume", "notification", "notifications", "microphone", "mic", "mute",
             "shuffle", "repeat", "spotify", "nova", "weather", "forecast", "calendar", "playback", "navidrome",
             "chime", "wake", "quiet", "battery", "warnings"}

# What only the conversation so far could say the meaning of - "turn it off", "turn
# the other one off", "open the first one" - which Assist, with no idea what was
# meant, can't be given.
PRONOUNS = {"it", "this", "that", "them", "these", "those", "one", "ones", "both", "other", "others", "rest",
            "same", "again", "first", "last"}

# When, rather than what - "pause the TV in five minutes" is a delay, which only Claude
# can set. A time is a unit anywhere, or one of TIME_LEADS straight before a number, a
# unit, "a", "an" or "half" - so "the TV in the kitchen" and "fan 2" are still things.
TIME_UNITS = {"second", "seconds", "minute", "minutes", "hour", "hours", "bit", "while", "later", "tonight",
              "tomorrow", "o'clock"}
TIME_LEADS = {"in", "for", "at", "until", "after", "before", "by"}
# Who for, rather than what: "run me a bath" isn't a script called "me a bath".
NOT_A_THING = {"me", "us"}

# A list, as it's said - "my shopping list", "notes", "the uni-work list". Which list it is
# is up to lists.py, which finds it by the user's own words.
LISTWORDS = r"(?:my |the )?[a-z0-9' -]+?"

# The words for Nova's own things and the devices, which a list's words mustn't have -
# "add ten minutes to the timer", "put it on my phone" - left to the rules after, or Claude.
NOT_A_LIST = {"timer", "alarm", "reminder", "calendar", "schedule", "diary", "agenda", "phone", "tablet", "mac",
              "laptop", "ipad"}
# A connector before the list - the last one, so "a trip to Ikea" is one thing - but never
# the "to" of "to do", which is a list's name.
LIST_LEAD = r"(?:to|on|onto) (?!do\b)"


# The point-back words that never name a thing, wherever they are - "add that cake to my
# list" is the cake just talked about. The rest of PRONOUNS are only when they're all the
# thing is, so "one egg" and "the last of the bread" are things, but "the other one" isn't.
ALWAYS_POINTS_BACK = {"it", "this", "that", "them", "these", "those"}


def points_back(item: str) -> bool:
    """
    Whether a thing said for a list only points back, to what the conversation so far says.

    Args:
        item: The thing as it was said, e.g. "the other one".
    """
    words = item.split()
    rest = [word for word in words if word not in ("the", "my", "some")]
    return bool(ALWAYS_POINTS_BACK & set(words)) or (bool(rest) and all(word in PRONOUNS for word in rest))


def not_a_list(said: Optional[str]) -> bool:
    """
    Whether a list's words name one of Nova's own things or a device, e.g. "my phone's notes".

    Args:
        said: The list as it was said, or None.
    """
    # Singular, so "my reminders" is a reminder too - after any "'s", so "phone's" is a phone.
    return any(re.sub(r"s$", "", re.sub(r"'s$", "", word)) in NOT_A_LIST for word in (said or "").split())


# A day or a time in what was said for a list - "put the dentist on Thursday at 3" is
# the calendar, Claude's, not a list called "Thursday at 3". A list named after a day is
# still one with "list" said: "my Monday jobs list".
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
HOURS_SAID = "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midnight"
WHEN_WORDS = re.compile(rf"\b(?:today|tonight|tomorrow|next week|(?:{'|'.join(WEEKDAYS)})s?|{'|'.join(MONTHS)}"
                        rf"|at (?:\d+|{HOURS_SAID})|\d+(?: \d+)? ?(?:am|pm|a m|p m))\b")


def names_a_day(said: Optional[str]) -> bool:
    """
    Whether a list's words hold a day or a time - and not the word "list" - e.g.
    "thursday at 3" in "put the dentist on Thursday at 3".

    Args:
        said: The list as it was said, or None.
    """
    return bool(said) and not names_list(said) and WHEN_WORDS.search(said) is not None


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


def _says_a_time(words: List[str]) -> bool:
    """Whether a thing's words hold a time - "the TV in five minutes", "the oven at six"."""
    for word, after in zip(words, words[1:] + [""]):
        if word in TIME_UNITS:
            return True
        if word in TIME_LEADS and (after.isdigit() or after in NUMBER_WORDS or after in TIME_UNITS or after == "half"):
            return True
    return False


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


def _tidied(text: str) -> str:
    """Words as the rules see them: lower case, letters, digits and apostrophes, single-spaced."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", text.lower())).strip()


def _ordinal(day: int) -> str:
    """A day of the month as it's said, e.g. 4 -> "4th"."""
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


class LocalIntents:
    """Recognises the everyday commands handled without Claude."""

    def __init__(self, clock=None, ntfy=None, home=None, activities: Optional[Callable[[], Dict[str, str]]] = None):
        """
        Args:
            clock: The Clock, to check there's exactly one timer before cancelling
                "the timer". Without it, that's left to Claude.
            ntfy: The user's notifications - see ntfy.py. Without it, "any
                notifications?" and the like are left to Claude.
            home: Home Assistant - see home_assistant.py. Without it, nothing is taken
                for one of Assist's own sentences, so "is the post office open?" is
                Claude's rather than "Home Assistant isn't set up yet".
            activities: Gives every word an activity is heard by, to the activity - see
                GoogleCalendar.activity_words. Asked at each match, so a calendar added
                in Google is heard from then on. Without it, the activity rules don't
                match, and Claude has them.
        """
        self.clock = clock
        self.ntfy = ntfy
        self.home = home
        self.activities = activities
        # The request as it was said, punctuation and all, while match looks at it - a
        # session's requests are matched one at a time. "Eggs, milk, bread" has commas,
        # which the rules never see.
        self._said = ""
        # Each pattern has to match the whole request, after politeness is taken off.
        self._rules = [
            # "Set of one minute timer" is how Moonshine heard "set a one minute timer"
            # with the network cable out - so it went to Claude, who couldn't be asked.
            (r"(?:(?:set|start) (?:me )?(?:a |an |of )?)?timer for (?P<amount>[a-z0-9 ]+?) (?P<unit>second|minute|hour)s?",
             self._timer),
            (r"(?:(?:set|start) (?:me )?(?:a |an |of )?)?(?P<amount>[a-z0-9 ]+?) (?P<unit>second|minute|hour) timer",
             self._timer),
            # "Soap" or "so", "scap" and "quiter" are how Moonshine heard "stop", "skip"
            # and "quieter" said on their own - see speech_to_text.py.
            (r"(?:pause|stop|soap|so)(?: the)?(?: music| song| spotify)?", self._playback("pause")),
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
            # Dimming has no sentence of Assist's own, so Nova steps the brightness
            # itself, from the lights' names in Home Assistant.
            (rf"(?P<direction>dim|brighten) (?P<words>{LIGHT})", self._dim),
            # The lights go straight to Home Assistant, which works out which ones
            # from the words itself - and still works with the internet down. One rule
            # for each shape, so each can name its parts for Nova's own lights, which
            # switch them should the guard refuse the sentence. "Turn the lamp on"
            # comes before "lamp on", which would take "turn" for part of the name.
            (rf"(?:turn|switch) (?P<state>on|off) (?P<words>{LIGHT})", self._lights),
            (rf"(?:turn|switch) (?P<words>{LIGHT}) (?P<state>on|off)", self._lights),
            # Not "is the lamp on?" or "are the lights on?", whose "is" and "are" aren't part
            # of the name - Nova's own lights would switch them on if the guard refused it.
            # They're questions, the first for Assist's shape below.
            (rf"(?!(?:is|are)\b)(?P<words>{LIGHT}) (?P<state>on|off)", self._lights),
            (rf"set (?P<words>{LIGHT}) to (?P<brightness>\d{{1,3}})(?: percent)?", self._lights),
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
            # "When's gym tomorrow?" too.
            (rf"(?:(?:what time|when) (?:do i|am i|is)(?: have| start| finish| have to be at| got)?|when's) "
             rf"(?:at |in )?{ACTIVITY}(?:(?: on| for)? {DAY})?", self._activity_rule(self._activity)),
            (rf"(?:am i|do i have|have i got) (?:at |in )?{ACTIVITY}(?:(?: on| for)? {DAY})?",
             self._activity_rule(self._activity)),
            # When it's next - "when's my next shift?" is just the one, "what are my
            # next three shifts?" three.
            (rf"when (?:do i|am i) next (?:have |at |in |got )?{ACTIVITY}", self._activity_rule(self._next_activity)),
            (rf"(?:when|what)(?:'s| is) my next {ACTIVITY}", self._activity_rule(self._next_activity)),
            (rf"(?:when|what)(?:'re| are) my next (?P<count>[a-z0-9]+) {ACTIVITY}",
             self._activity_rule(self._next_activity)),
            # Notifications - see ntfy.py.
            (r"(?:are there |have i got |do i have )?any (?:new )?notifications|what(?:'s| are) my notifications"
             r"|read (?:me )?my notifications", self._notifications),
            (r"tell me more|what was that(?: about)?", self._tell_more),
            # The batteries - see batteries.py. A device is only the one word, so "what's on my
            # phone" or "how's my phone's screen" are never taken for a battery.
            (rf"(?:how(?:'s| is)|what(?:'s| is)) (?:the |my )(?P<device>{BATTERY_DEVICE})(?:'s)? battery"
             rf"(?: level| percentage)?|what(?:'s| is) (?:the |my )(?P<device2>{BATTERY_DEVICE}) (?:on|at)",
             self._batteries),
            (r"(?:what(?:'s| are| is) (?:the |my )?)?battery levels?|how are (?:all )?my batteries(?: doing)?",
             self._batteries),
            # Where the phone is, and ringing it - see devices.py. Only the one word, as for a
            # battery, so "where's my phone charger?" is Claude's.
            (rf"where(?:'s| is) (?:my |the )(?P<device>{FOUND_DEVICE})", self._find_device(False)),
            (rf"(?:find|ring) (?:my |the )(?P<device>{FOUND_DEVICE})", self._find_device(True)),
            (r"(?:send|text) (?:that|it|this) to (?:my phone|me)|text me (?:that|it)"
             r"|(?:send|put) (?:that|it|this) on my phone", self._send_to_phone),
            # The Mac or the tablet, through the Home Assistant app - only the device itself, said
            # plainly, so "put it on my iPad's list" is left alone.
            (r"(?:send|text|put) (?:that|it|this) (?:to|on) (?:my |the )(?P<device>mac|macbook|laptop|tablet|ipad)",
             self._send_to_device),
            # The PC - see pc.py. Before the lists, whose "put X to Y" would take "put my PC to
            # sleep" for adding to a list called sleep, and before Assist's shapes, so "lock my
            # PC" is never Assist's and "is my PC on?" is answered from HASS.Agent's entities.
            (rf"(?:turn|switch) on {PC}|(?:turn|switch) {PC} on|wake(?: up)? {PC}", self._pc("on")),
            (rf"(?:turn|switch) off {PC}|(?:turn|switch) {PC} off|shut down {PC}|shut {PC} down", self._pc("off")),
            (rf"(?:restart|reboot) {PC}", self._pc("restart")),
            (rf"put {PC} to sleep", self._pc("sleep")),
            (rf"turn off {PC}(?:'s)? (?:screen|monitor)", self._pc("screen_off")),
            (rf"lock {PC}", self._pc("lock")),
            (rf"is {PC} on", self._pc("status")),
            # The to-do lists - see lists.py. Assist is asked first for the adding and
            # ticking off, but only with its own list intents (see ActionExecutor), so
            # "take milk off my shopping list", which Assist reads as removing it, is only
            # ever ticked off.
            (rf"(?:add|put) (?P<item>.+) {LIST_LEAD}(?P<list>{LISTWORDS})", self._add_to_list),
            (rf"what(?:'s| is) on (?P<list>{LISTWORDS})", self._read_list),
            (rf"read (?:me )?(?P<list>{LISTWORDS} list)", self._read_list),
            (rf"tick (?!off )(?P<item>.+) off (?P<list>{LISTWORDS})", self._tick_off_list),
            (rf"tick (?:off )?(?P<item>.+?)(?: off)?(?: (?:on|from) (?P<list>{LISTWORDS}))?", self._tick_off_list),
            (rf"(?:take|cross) (?P<item>.+?) off (?P<list>{LISTWORDS})", self._take_off_list),
            # "I've got the milk" - and "I've got the flu", which only ticks off "flu" itself,
            # so a sentence about the world reaches Claude. Assist has no sentence like it.
            (r"i(?:'ve| have) got (?:the |some )?(?P<item>.+)", self._got),
            # Anything else of Home Assistant's - switches, media players, scripts and
            # scenes - goes to Assist, last, so that each of Nova's own commands is
            # matched first: "pause" is Nova's music, "pause the TV" Assist's. It's only
            # sent on the wider rule (see HomeAssistant.command), after the guard.
            (rf"(?:turn|switch) (?:on|off) {THING}", self._assist),
            (rf"(?:turn|switch) {THING} (?:on|off)", self._assist),
            (rf"(?:pause|resume|unpause) {THING}", self._assist),
            (rf"(?:next|previous) (?:track|song) on {THING}", self._assist),
            (rf"set (?:the )?{THING} volume to \d{{1,3}}(?: percent)?", self._assist),
            (rf"is {THING} (?:on|off|open|closed|playing|paused)", self._ask),
            (rf"(?:activate|run) {THING}", self._assist),
            (rf"(?:open|close) {THING}", self._assist),
        ]

    def match(self, text: str, only: Optional[Sequence[str]] = None) -> Optional[Dict]:
        """
        Handles a request locally, if it's one of the everyday commands.

        Args:
            text: What the user said.
            only: The tools a rule may give, or None for any - so a rule giving any other
                is passed over, e.g. ["find_device"] while Nova listens for an answer.

        Returns:
            A result shaped like LLMHandler.process_query's - a "response" and a list
            of "actions" - or None to leave the request to Claude.
        """
        request = _tidied(text)
        request = re.sub(POLITE_END, "", re.sub(POLITE_START, "", request)).strip()
        self._said = text

        for pattern, handle in self._rules:
            found = re.fullmatch(pattern, request)
            if not found:
                continue
            result = handle(found)
            if result is not None and only is not None and not (
                    result["actions"] and all(action.tool in only for action in result["actions"])):
                continue
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
        # Setting a brightness switches a light on - bar 0%, which Home Assistant takes
        # as off, so Nova's own lights don't switch it on at 1% instead.
        brightness = found.groupdict().get("brightness")
        level = int(brightness) if brightness is not None else None
        on = found["state"] == "on" if level is None else level > 0
        return self._result("Okay.", [Action("control_home", {
            "command": found.string, "on": on, "words": found["words"], "brightness": level})])

    def _dim(self, found) -> Dict:
        direction = "down" if found["direction"] == "dim" else "up"
        return self._result("Okay.", [Action("dim_lights", {"direction": direction, "words": found["words"]})])

    def _assist(self, found, question: bool = False) -> Optional[Dict]:
        # A pronoun anywhere, not only alone, as in "pause it in five minutes" - which
        # isn't a thing called "it in five minutes".
        words = found["thing"].split()
        if self.home is None or _says_a_time(words) or any(word in NOVAS_OWN or word in PRONOUNS
                                                           or word in NOT_A_THING for word in words):
            return None
        asked = {"command": found.string, "wider": True, **({"question": True} if question else {})}
        return self._result("Okay.", [Action("control_home", asked)])

    def _ask(self, found) -> Optional[Dict]:
        # Said to be a question, so that one the guard refuses - "is the garage door
        # open?" - goes to Claude, which can read it, rather than being refused.
        return self._assist(found, question=True)

    def _listed(self, found, tool: str, item: Optional[str] = None, **params) -> Optional[Dict]:
        """
        A list's action - None without Home Assistant, for a thing which only points back, or
        for list words naming one of Nova's own things or a device, a day or a time, or
        joining two requests.
        """
        said = found.groupdict().get("list")
        words = (said or "").split()
        if (self.home is None or not_a_list(said) or names_a_day(said) or {"and", "then", "plus"} & set(words)
                or (item is not None and points_back(item))):
            return None
        if item is None:
            params = {"list": said}
        else:
            # The request, for Assist to be asked first - unless the rule says otherwise.
            params = {"item": item, "list": said, **(params or {"command": found.string})}
        return self._result("Okay.", [Action(tool, params)])

    def _add_to_list(self, found) -> Optional[Dict]:
        # Several things at once - "eggs, milk and bread" - are Claude's, which splits
        # them, as is "salt and vinegar crisps", which it keeps whole.
        words = found["item"].split()
        # The thing as said, to see the commas between its words - but not one before
        # "please", as in "add milk to my list, please".
        as_said = re.search("[^a-z0-9']+".join(map(re.escape, words)), self._said.lower())
        if {"and", "plus"} & set(words) or " as well as " in f" {found['item']} " or as_said is None \
                or "," in as_said.group():
            return None
        return self._listed(found, "add_to_list", found["item"])

    def _read_list(self, found) -> Optional[Dict]:
        return self._listed(found, "read_list")

    def _tick_off_list(self, found, **params) -> Optional[Dict]:
        # "Tick milk off my calendar" is the shape before, which named no list - not a
        # thing called "milk off my calendar".
        if "off" in found["item"].split():
            return None
        return self._listed(found, "tick_off_list", found["item"], **params)

    def _take_off_list(self, found) -> Optional[Dict]:
        # "Take the day off work" is as everyday as "I've got the flu", so unless the words say
        # it's a list, only the thing itself is ticked off - never "Day rota" on a Work list.
        return self._tick_off_list(found, command=found.string, exact=not names_list(found["list"]))

    def _got(self, found) -> Optional[Dict]:
        return self._listed(found, "tick_off_list", found["item"], list=None, exact=True)

    def _calendar(self, found) -> Dict:
        first, days = calendar_days(found["day"], date.today())
        return self._result("Okay.", [Action("get_calendar", {"start": first.isoformat(), "days": days})])

    def _activity_rule(self, handle: Callable) -> Callable:
        """
        An activity rule's handler, which matches the request again with the activities'
        words in the rule's ACTIVITY - the request having already matched with any words
        there, so a request of another shape never has the calendar's names asked for.
        It's None, for Claude, without the calendar, or when none of its activities is there.
        """
        def matched(found) -> Optional[Dict]:
            words = self._activity_words()
            if not words:
                return None
            pattern = found.re.pattern.replace(ACTIVITY, "(?P<activity>" + "|".join(
                re.escape(word) for word in sorted(words, key=len, reverse=True)) + ")")
            again = re.fullmatch(pattern, found.string)
            if not again:
                return None
            name = _tidied(words[again["activity"]]).split()
            first_word_alone = len(name) > 1 and again["activity"] == name[0]
            if (first_word_alone and not again.groupdict().get("day")
                    and re.fullmatch(BARE_BEFORE_ACTIVITY, found.string[:again.start("activity")])):
                return None
            return handle(again, words)
        return matched

    def _activity_words(self) -> Dict[str, str]:
        """
        The words the activities are heard by, without those for Nova's own things - a
        calendar called Timer or Music is Claude's to ask about, as Claude has both, so
        it never takes over "when's my next timer?". See ACTIVITY.
        """
        if self.activities is None:
            return {}
        own = NOVAS_OWN | NOT_A_LIST
        return {word: activity for word, activity in self.activities().items()
                if word and not any(part in own or part.rstrip("s") in own for part in word.split())}

    def _activity(self, found, words: Dict[str, str]) -> Dict:
        first, days = calendar_days(found["day"], date.today())
        return self._result("Okay.", [Action("get_activity", {"activity": words[found["activity"]],
                                                              "start": first.isoformat(), "days": days})])

    def _next_activity(self, found, words: Dict[str, str]) -> Optional[Dict]:
        count = _number(found.groupdict().get("count") or "one")
        if not count:
            return None
        return self._result("Okay.", [Action("get_activity", {"activity": words[found["activity"]],
                                                              "start": date.today().isoformat(), "count": count})])

    def _weather(self, found) -> Dict:
        day = found["day"] or found["day2"] or "today"
        return self._result("Okay.", [Action("get_weather", {"day": day})])

    def _cancel_only_timer(self, found) -> Optional[Dict]:
        # Only when there's exactly one, so there's no question which was meant.
        if not self.clock or self.clock.timer_count() != 1:
            return None
        return self._result("Okay.", [Action("cancel_timer")])

    def _batteries(self, found) -> Dict:
        device = found.groupdict().get("device") or found.groupdict().get("device2")
        return self._result("Okay.", [Action("get_batteries", {"device": device} if device else {})])

    def _find_device(self, ring: bool):
        return lambda found: self._result("Okay.", [Action("find_device", {"device": found["device"], "ring": ring})])

    def _notifications(self, found) -> Optional[Dict]:
        return self._result("Okay.", [Action("read_notifications")]) if self.ntfy else None

    def _tell_more(self, found) -> Optional[Dict]:
        # Only about an alert said a moment ago. Otherwise it's a follow-up to whatever
        # was being talked about - "tell me more" about the weather - which is Claude's.
        if not self.ntfy or not self.ntfy.just_said():
            return None
        return self._result("Okay.", [Action("tell_me_more")])

    def _send_to_device(self, found) -> Optional[Dict]:
        return self._result("Okay.", [Action("send_to_phone", {"device": found["device"]})]) if self.home else None

    def _pc(self, action: str):
        return lambda found: self._result("Okay.", [Action("pc_power", {"action": action})])

    def _send_to_phone(self, found) -> Optional[Dict]:
        return self._result("Okay.", [Action("send_to_phone")]) if self.ntfy else None
