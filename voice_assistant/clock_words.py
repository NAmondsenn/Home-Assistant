"""
Clock Words
The clock's words: how it says a duration or a time of day, how it reads one back, and
which spoken words mean stop, yes or no.

This is part of the Clock (see clock.py), which keeps the lock, the timers and alarms
and the saved files. Nothing here changes any of them, so everything in this file can
be tried out on its own.
"""

import re
import time
from datetime import datetime, timedelta
from typing import Dict, Optional


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
    Formats a time of day for speech on the 12-hour clock, e.g. "7:05 AM", or
    "midnight" and "noon" - "12 AM" is how a computer says midnight, not a person,
    and which of "12 AM" and "12 PM" is which isn't something everyone knows.

    Built by hand rather than with strftime's "%-I", which drops the leading zero
    but only exists on Linux and macOS - Windows rejects it outright, which would
    stop this module (and its tests) running anywhere but the Pi.
    """
    if minute == 0 and hour in (0, 12):
        return "midnight" if hour == 0 else "noon"
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


def seconds_until(at: str, now: Optional[datetime] = None) -> int:
    """
    How many seconds until a local clock time.

    Worked out from the local time on that day, so the clocks going forward or back
    in between is accounted for.

    Args:
        at: "HH:MM" for the next time it's that time - later today, or tomorrow if
            it's already gone - or "YYYY-MM-DD HH:MM" for a particular day, which
            can be in the past.
        now: The current local time - only given by the tests.

    Returns:
        The seconds until then, rounded. Zero or less if a given day has passed.

    Raises:
        ValueError: If at can't be read.
    """
    now = now or datetime.now()
    text = at.strip().replace("T", " ")
    if " " in text:
        target = datetime.fromisoformat(text)
    else:
        clock_time = datetime.strptime(text, "%H:%M").time()
        target = datetime.combine(now.date(), clock_time)
        if target <= now:
            target += timedelta(days=1)
    return round(target.timestamp() - now.timestamp())


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

    # As Nova says them itself - see spoken_clock_time.
    if re.search(r"\bmidnight\b", text):
        candidates.add((0, 0))
    if re.search(r"\b(noon|midday)\b", text):
        candidates.add((12, 0))

    return candidates


class ClockWords:
    """
    Part of Clock, which uses the lock and the stores clock.py sets up: the words it
    speaks timers and alarms in, and the words it listens for while something rings.
    """

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

    # Words which mean "make it stop" when something is going off. Kept deliberately
    # broad, since anything said to a nagging reminder is meant to silence it.
    DISMISSAL_WORDS = {
        "stop", "stopped", "cancel", "cancelled", "dismiss", "dismissed", "okay", "ok",
        "alright", "right", "fine", "yes", "yep", "yeah", "yep", "done", "finished",
        "enough", "quiet", "shush", "shut", "off", "thanks", "thank", "got", "heard",
        "understood", "silence", "mute", "no",
    }

    # Said last, these mean "stop" whatever was heard before them. "Stop" alone,
    # over a timer, has come back as "It's a little bus stop" - which still went to
    # Claude, and only stopped the timer by the roundabout route of being talked over.
    # The old Moonshine speech-to-text heard a lone "stop" as "soap", "sop", or just
    # "so" - which, said over a timer, went to Claude, who replied "Your message just
    # says 'So'" and asked what was wanted.
    STOP_WORDS = {"stop", "enough", "cancel", "dismiss", "quiet", "shush", "silence", "mute", "soap", "sop", "so"}

    # A plain answer to a yes/no question - see answer_to_offer. "Okay" and "right"
    # aren't here, since "okay, leave it" turns an offer down.
    YES_WORDS = {"yes", "yeah", "yep", "yup", "aye", "sure", "definitely"}
    NO_WORDS = {"no", "nope", "nah"}

    # Words which turn down an offer. "no" is a dismissal word too - said to a
    # ringing timer it means "make it stop" - so when the assistant has just asked
    # a yes/no question, refusals are weighed first, or answering "no" to "shall I
    # turn that reminder off?" would turn it off.
    REFUSAL_WORDS = {
        "no", "nope", "nah", "not", "dont", "don't", "leave", "keep", "later",
        "ignore", "nevermind", "never",
    }

    # Words which carry no request of their own, so they don't stop something being
    # a bare acknowledgement. Several of the dismissal words above are ordinary
    # English ("off", "no", "right", "thanks"), so a phrase is only treated as a
    # dismissal when everything in it is one of these two sets - otherwise "turn
    # off the lights" would silence a timer and never reach the lights.
    FILLER_WORDS = {
        "a", "all", "am", "an", "and", "are", "as", "at", "be", "been", "but",
        "can", "could", "did", "do", "for", "from", "have", "has", "had", "hey", "i",
        "im", "i'm", "is", "it", "it's", "its", "ive", "i've", "just", "know", "let",
        "me", "mm", "mhm", "my", "now", "of", "oh", "please", "so", "than",
        "that", "that's", "thats", "the", "then", "there", "this", "to", "too", "up",
        "uh", "um", "very", "was", "well", "will", "would", "you", "your",
    }

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
        assistant at all counts as having heard it (see session.py). It only decides
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
        # Ending in "stop" or the like, it's taken as that - unless it's "don't stop".
        if words[-1] in cls.STOP_WORDS and not set(words) & cls.REFUSAL_WORDS:
            return True
        # Has to actually acknowledge something - filler alone isn't a dismissal.
        if not set(words) & cls.DISMISSAL_WORDS:
            return False
        return all(word in cls.DISMISSAL_WORDS or word in cls.FILLER_WORDS
                   for word in words)

    @classmethod
    def answer_to_offer(cls, text: str) -> Optional[bool]:
        """
        Whether a short answer to a yes/no question the assistant just asked ("shall
        I turn that reminder off?") was a yes, a no, or neither.

        A plain "yes" or "no" decides it wherever it comes, so "yes, I don't need it
        any more" is a yes despite the "don't", and "no, turn it off" a no despite
        the "off". Saying both, as in "yeah, no", is taken as neither rather than
        guessed at. Only without one are the softer words weighed: a refusal ("leave
        it", "not now") first, since "okay, leave it" is turning the offer down, then
        an acknowledgement ("turn it off", "okay"). Deliberately looser than
        looks_like_dismissal - the assistant has just asked a question, so a stray
        content word ("yeah go on") doesn't make it any less of an answer.

        Args:
            text: What the user said.

        Returns:
            True for yes, False for no, or None when it isn't a clear answer - which
            is then handled as a fresh request.
        """
        words = re.findall(r"[a-z']+", text.lower())
        if not words:
            return None
        said = set(words)
        # "Why not?" means yes, though "not" on its own turns an offer down.
        if " ".join(words) in ("why not", "go on then", "go on"):
            return True
        yes, no = bool(said & cls.YES_WORDS), bool(said & cls.NO_WORDS)
        if yes != no and (len(words) <= 10 or words[0] in cls.YES_WORDS | cls.NO_WORDS):
            return yes
        if yes or len(words) > 6:
            return None
        if said & cls.REFUSAL_WORDS:
            return False
        if said & cls.DISMISSAL_WORDS:
            return True
        return None

    def _spoken_due(self, due_at: float) -> str:
        """
        Formats an absolute due time for speech, e.g. "5:40 PM", or "5:40 PM on
        Tuesday" if it's due on a different day to today.

        Midnight is named by the night it ends - "midnight tonight", "midnight on
        Tuesday night" - since "midnight on Wednesday", for the moment Wednesday
        begins, is just as often taken to mean the end of it.
        """
        due = time.localtime(due_at)
        now = time.localtime()
        spoken = spoken_clock_time(due.tm_hour, due.tm_min)
        if (due.tm_hour, due.tm_min) == (0, 0):
            night = time.localtime(due_at - 12 * 3600)
            if (night.tm_year, night.tm_yday) == (now.tm_year, now.tm_yday):
                return "midnight tonight"
            return time.strftime("midnight on %A night", night)
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

    def _spoken_time_of_day(self, hour: int, minute: int) -> str:
        """Formats an hour/minute as a spoken clock time, e.g. "7:00 AM"."""
        return spoken_clock_time(hour, minute)

    def _describe_alarm(self, alarm: Dict) -> str:
        """Names an alarm for speech, e.g. "7:00 AM alarm, every weekday"."""
        base = f"{self._spoken_time_of_day(alarm['hour'], alarm['minute'])} alarm"
        repeat_desc = self._repeat_description(alarm["repeat"])
        return f"{base}, {repeat_desc}" if repeat_desc else base
