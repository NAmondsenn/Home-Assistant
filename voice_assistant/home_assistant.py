"""
Home Assistant Module.

Turns the lights on and off, and reads the calendar, through Home Assistant.

Nova signs in as a Home Assistant user of its own, with a long-lived access token,
and asks for things in plain words through Home Assistant's own Assist - "turn off
the kitchen lights" - rather than naming devices itself. Assist only knows about
the devices exposed to it (Settings > Voice assistants > Expose), so that list is
what Nova can reach, enforced by Home Assistant rather than by anything Claude is
told. Nothing which unlocks or opens anything is passed on at all - see SENSITIVE.

The calendar is whichever calendars Home Assistant has, such as a Google Calendar
added through its Google Calendar integration.
"""

import logging
import re
from datetime import date, datetime, time as day_time, timedelta
from typing import Dict, List, Optional

import requests

from voice_assistant.clock import spoken_clock_time

# Logger setup
logger = logging.getLogger(__name__)

# Seconds to wait for Home Assistant before giving up. It's on the local network,
# so anything slower than this means it's down.
REQUEST_TIMEOUT = 5

# Home Assistant's own built-in Assist, which matches requests against the exposed
# devices by itself. Named, rather than left to the default, in case the default
# has been set to a language model - which would be slower, and less predictable.
DEFAULT_AGENT = "conversation.home_assistant"

# Requests which could let someone in are refused by voice however well they were
# heard - a misheard word, or a TV in the background, mustn't be able to unlock the
# front door. Checked here, in code, not left to Claude. "Turn on the front door
# light" is still fine; "open the front door" isn't.
SENSITIVE = re.compile(r"\b(?:un)?lock|\bgarage\b|\bgates?\b|\bdisarm|\barm the\b|\balarm system\b"
                       r"|\b(?:open|close|shut)\b.*\bdoors?\b")

# Most events said for one day before the rest are only counted.
MOST_EVENTS_SAID = 5


class HomeAssistant:
    """One Home Assistant, reached over its REST API."""

    def __init__(self, url: str, token: str, agent: str = DEFAULT_AGENT, calendars: Optional[List[str]] = None,
                 activities: Optional[Dict[str, List[str]]] = None):
        """
        Args:
            url: Its address, e.g. "http://homeassistant.local:8123".
            token: A long-lived access token for Nova's own Home Assistant user.
            agent: The Assist agent requests go to.
            calendars: The calendars to read, e.g. ["calendar.personal"], or None
                for all of them.
            activities: The regular things in the user's life and what they're called
                on the calendar - {"work": ["Shift"], "uni": ["CSC"]} - for "what time
                do I have work?". An event with a word in its name starting with one of
                them is that activity. Just work, called "work", if not given.
        """
        self.url = url.rstrip("/")
        self.agent = agent
        self.calendars = list(calendars or [])
        self.activities = {name.lower(): [called.lower() for called in calls]
                           for name, calls in (activities or {"work": ["work"]}).items()}
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    @classmethod
    def from_config(cls, config, token: Optional[str]) -> Optional["HomeAssistant"]:
        """
        The Home Assistant config.yaml points at, or None if it isn't set up.

        Args:
            config: Configs loaded from config.yaml - home_assistant.url and friends.
            token: HA_TOKEN from .env.

        Returns:
            A HomeAssistant, or None without both an address and a token.
        """
        settings = config.section("home_assistant") if config else {}
        url = (settings.get("url") or "").strip()
        if not url:
            return None
        if not token:
            logger.warning("home_assistant.url is set but HA_TOKEN isn't in .env, so Home Assistant is off")
            return None
        logger.info(f"Home Assistant at {url}")
        return cls(url, token, calendars=settings.get("calendars"), activities=settings.get("activities"))

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self._session.request(method, self.url + path, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response

    @staticmethod
    def _failure_message(error: Exception) -> str:
        """What to say when Home Assistant couldn't be asked, depending on why."""
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 401:
            return "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return "I can't reach Home Assistant right now."
        return "Home Assistant had a problem with that."

    def command(self, request: str) -> Dict:
        """
        Asks Home Assistant's Assist to do something, in plain words.

        Args:
            request: e.g. "turn off the kitchen lights".

        Returns:
            Dict with 'success' and 'message' - Assist's own reply - and 'chime' for
            something done, since the lights changing speaks for itself.
        """
        if SENSITIVE.search(request.lower()):
            logger.warning(f"Refused a sensitive Home Assistant request: '{request}'")
            return {"success": False, "message": "I don't lock, unlock or open anything by voice."}

        try:
            reply = self._request("POST", "/api/conversation/process",
                                  json={"text": request, "language": "en", "agent_id": self.agent}).json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}

        response = reply.get("response") or {}
        said = ((response.get("speech") or {}).get("plain") or {}).get("speech") or ""
        kind = response.get("response_type")
        logger.info(f"Home Assistant: {kind} - '{said}'")
        if kind == "action_done":
            return {"success": True, "message": said or "Done.", "chime": True}
        if kind == "query_answer":
            return {"success": True, "message": said}
        return {"success": False, "message": said or "Home Assistant didn't understand that."}

    def calendar_names(self) -> List[str]:
        """The calendars to read - those config.yaml names, or every one there is."""
        if self.calendars:
            return self.calendars
        return [calendar["entity_id"] for calendar in self._request("GET", "/api/calendars").json()]

    def events(self, first: date, days: int = 1) -> List[Dict]:
        """
        Everything on the calendars over some days.

        Args:
            first: The first day.
            days: How many days, from 1.

        Returns:
            The events, earliest first, each a dict with "summary", "start" and
            "all_day" - start a local datetime, or a date for an all-day event.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
        """
        start = datetime.combine(first, day_time()).astimezone()
        end = start + timedelta(days=days)
        found = []
        for calendar in self.calendar_names():
            for event in self._request("GET", f"/api/calendars/{calendar}",
                                       params={"start": start.isoformat(), "end": end.isoformat()}).json():
                found.append(_read_event(event))
        return sorted(found, key=lambda event: _sort_key(event["start"]))

    def agenda(self, first: date, days: int = 1, today: Optional[date] = None) -> Dict:
        """
        What's on the calendar, as something to say.

        Args:
            first: The first day asked about.
            days: How many days, e.g. 2 for a weekend.
            today: Today's date - only given by the tests.

        Returns:
            Dict with 'success' and the agenda as the 'message'.
        """
        try:
            events = self.events(first, days)
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error(f"Couldn't read the calendar: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": describe_agenda(events, first, days, today or date.today())}

    def activity(self, name: str, first: date, days: int = 1, today: Optional[date] = None,
                 count: Optional[int] = None, now: Optional[datetime] = None) -> Dict:
        """
        When the user has one of their activities - work, uni - from the events named as it.

        Args:
            name: The activity, e.g. "work".
            first: The first day asked about.
            days: How many days.
            today: Today's date - only given by the tests.
            count: Only say the next this many times, for "when's my next shift?".
            now: The time now - only given by the tests.

        Returns:
            Dict with 'success' and the times as the 'message'.
        """
        called = self.activities.get((name or "").strip().lower())
        if called is None:
            return {"success": False,
                    "message": f"I don't know what {name} is called on your calendar. It can be added to "
                               f"home_assistant.activities in my settings."}
        try:
            events = self.events(first, days)
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error(f"Couldn't read the calendar: {e}")
            return {"success": False, "message": self._failure_message(e)}
        # At the start of a word, so "CSC" finds "CSC203 Lecture" and "shift" finds
        # "Night shift", but neither turns up inside some other word.
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(word) for word in called) + ")", re.IGNORECASE)
        found = [event for event in events if pattern.search(event["summary"])]
        name = name.strip().lower()
        if count:
            return {"success": True, "message": describe_next(name, found, count, first,
                                                              now or datetime.now().astimezone())}
        return {"success": True, "message": describe_activity(name, found, first, days, today or date.today())}


def _by_day(events: List[Dict], first: date) -> Dict[date, List[Dict]]:
    """Events grouped by the day they start on - or the first day, for one already going."""
    by_day: Dict[date, List[Dict]] = {}
    for event in events:
        day = event["start"] if event["all_day"] else event["start"].date()
        by_day.setdefault(max(day, first), []).append(event)
    return by_day


def _stretches(events: List[Dict]) -> List[List]:
    """
    A day's timed events as stretches of time. Back-to-back sessions - uni's 2:30
    until 3:30, then 3:30 until 5:30 - are one stretch, 2:30 until 5:30, rather than
    read out one by one.
    """
    stretches = []
    for event in sorted((event for event in events if not event["all_day"]), key=lambda e: e["start"]):
        end = event.get("end") or event["start"]
        if stretches and event["start"] <= stretches[-1][1]:
            stretches[-1][1] = max(stretches[-1][1], end)
        else:
            stretches.append([event["start"], end])
    return stretches


def _span(start: datetime, end: datetime) -> str:
    """'from 2:30 PM until 5:30 PM', or 'at 2:30 PM' for something with no length."""
    said = spoken_clock_time(start.hour, start.minute)
    return f"from {said} until {spoken_clock_time(end.hour, end.minute)}" if end > start else f"at {said}"


def describe_activity(name: str, events: List[Dict], first: date, days: int, today: date) -> str:
    """
    Says when the user has an activity, a day at a time.

    Args:
        name: The activity, e.g. "work".
        events: Its events, from HomeAssistant.events, earliest first.
        first: The first day asked about.
        days: How many days.
        today: Today's date.

    Returns:
        e.g. "Tomorrow, you've got work from 9:00 AM until 5:30 PM." or "You haven't
        got uni tomorrow."
    """
    by_day = _by_day(events, first)
    sentences = []
    for day in sorted(by_day):
        times = ["all day"] if any(event["all_day"] for event in by_day[day]) else []
        times += [_span(start, end) for start, end in _stretches(by_day[day])]
        listed = times[0] if len(times) == 1 else ", ".join(times[:-1]) + ", and " + times[-1]
        on = _on(day, today)
        sentences.append(f"{on[0].upper()}{on[1:]}, you've got {name} {listed}.")

    if sentences:
        return " ".join(sentences)
    last = first + timedelta(days=days - 1)
    if days == 1:
        return f"You haven't got {name} {spoken_day(first, today)}."
    return f"You haven't got {name} between {spoken_day(first, today)} and {spoken_day(last, today)}."


def describe_next(name: str, events: List[Dict], count: int, first: date, now: datetime) -> str:
    """
    Says when the user next has an activity - once, or the next few times.

    Asked "when do I next have work?", every shift for the next fortnight used to be
    read out, when only the next one was wanted.

    Args:
        name: The activity, e.g. "work".
        events: Its events from the first day on, earliest first.
        count: How many times to say.
        first: The first day looked at - usually today.
        now: The time now, so one already over today isn't counted.

    Returns:
        e.g. "You're next at work tomorrow, from 3:30 PM until 11:30 PM." or "Your
        next 2 times at work: tomorrow, from ...; and on Saturday, from ...".
    """
    today = now.date()
    upcoming = []
    by_day = _by_day(events, first)
    for day in sorted(by_day):
        if any(event["all_day"] for event in by_day[day]) and day >= today:
            upcoming.append((day, "all day", None, None))
        upcoming += [(day, _span(start, end), start, end) for start, end in _stretches(by_day[day]) if end > now]
    upcoming = upcoming[:count]

    if not upcoming:
        return f"You haven't got {name} in the next two weeks."
    if len(upcoming) == 1:
        day, span, start, end = upcoming[0]
        if start is not None and start <= now:
            return f"You're at {name} now, until {spoken_clock_time(end.hour, end.minute)}."
        return f"You're next at {name} {_on(day, today)}, {span}."
    said = [f"{_on(day, today)}, {span}" for day, span, _, _ in upcoming]
    return f"Your next {len(upcoming)} times at {name}: {'; '.join(said[:-1])}; and {said[-1]}."


def _read_event(event: Dict) -> Dict:
    """One event from Home Assistant, with its start and end read into dates or local times."""
    start, end = event.get("start") or {}, event.get("end") or {}
    if "dateTime" in start:
        when, all_day = datetime.fromisoformat(start["dateTime"]).astimezone(), False
        until = datetime.fromisoformat(end["dateTime"]).astimezone() if "dateTime" in end else None
    else:
        when, all_day, until = date.fromisoformat(start["date"]), True, None
    return {"summary": (event.get("summary") or "something").strip(), "start": when, "end": until,
            "all_day": all_day}


def _sort_key(when) -> datetime:
    """Sorts all-day events before anything at a time on the same day."""
    return when if isinstance(when, datetime) else datetime.combine(when, day_time()).astimezone()


def spoken_day(day: date, today: date) -> str:
    """
    A day as it's said: "today", "tomorrow", "Thursday" within the week, or
    "Thursday the 30th of October" further off.
    """
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if today < day < today + timedelta(days=7):
        return day.strftime("%A")
    suffix = "th" if 11 <= day.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day.day % 10, "th")
    return f"{day.strftime('%A')} the {day.day}{suffix} of {day.strftime('%B')}"


def _on(day: date, today: date) -> str:
    """'today' and 'tomorrow' stand alone; any other day is 'on Thursday'."""
    said = spoken_day(day, today)
    return said if said in ("today", "tomorrow") else f"on {said}"


def describe_agenda(events: List[Dict], first: date, days: int, today: date) -> str:
    """
    Says what's on, a day at a time.

    Args:
        events: From HomeAssistant.events, earliest first.
        first: The first day asked about.
        days: How many days.
        today: Today's date.

    Returns:
        e.g. "On Thursday, you've got the dentist at 9:30 AM, and dinner with Sam at
        7:00 PM." or "You've got nothing on tomorrow."
    """
    by_day = {first + timedelta(days=offset): [] for offset in range(days)}
    for event in events:
        when = event["start"]
        day = when if event["all_day"] else when.date()
        # An event which started before the days asked about, and runs into them.
        by_day.setdefault(max(day, first), []).append(event)

    sentences = []
    for day, on_day in by_day.items():
        if not on_day:
            continue
        said = [event["summary"] + (", all day" if event["all_day"] else
                                    f" at {spoken_clock_time(event['start'].hour, event['start'].minute)}")
                for event in on_day[:MOST_EVENTS_SAID]]
        if len(on_day) > MOST_EVENTS_SAID:
            said.append(f"{len(on_day) - MOST_EVENTS_SAID} more")
        listed = said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", and " + said[-1]
        on = _on(day, today)
        sentences.append(f"{on[0].upper()}{on[1:]}, you've got {listed}.")

    if sentences:
        return " ".join(sentences)
    last = first + timedelta(days=days - 1)
    if days == 1:
        return f"You've got nothing on {spoken_day(first, today)}."
    if days == 2:
        return f"You've got nothing on {spoken_day(first, today)} or {spoken_day(last, today)}."
    return f"You've got nothing on from {spoken_day(first, today)} to {spoken_day(last, today)}."
