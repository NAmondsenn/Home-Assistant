"""
Google Calendar Module.

Reads the user's calendars from Google, and says what's on them, with Nova's own
sign-in to Google. See docs/decisions/0008.

The calendar comes straight from Google, not through Home Assistant - the user's
answer 2 - so Nova signs in to Google itself (decision 10). It asks only for what it
uses: the list of calendars, and reading and adding events. The sign-in is kept on
the brain, readable by Nova's user only, never on a satellite. Until the settings
page can do it, it's done once with a command on the brain, the way Spotify's is:

    runuser -u nova -- venv/bin/python -m voice_assistant.google_calendar

It's plain HTTPS with requests - an authorisation code with PKCE, from a "Desktop
app" client of the user's own - rather than a Google library, which would be one more
dependency for three requests.

Every calendar in Google's own list of the user's is read, so one added later turns up
by itself. With Google out of reach there's no calendar - no copy is kept. Each but the
user's own is an activity, by its name - "when's gym tomorrow?" works as soon as there's
a Gym calendar.
"""

import argparse
import base64
import hashlib
import logging
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, time as day_time, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests

from voice_assistant import network, paths, storage
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import spoken_clock_time

logger = logging.getLogger(__name__)

# The least that list the calendars, read every calendar's events, and add to the
# user's own. events.owned could change an event too - Google has no scope that only
# adds - so it's Nova's own code that keeps it to adding.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
]

# What each scope lets Nova do, as said when the user's sign-in left it out.
SCOPES_SAID = {
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly": "see the list of your calendars",
    "https://www.googleapis.com/auth/calendar.events.readonly": "read your calendars' events",
    "https://www.googleapis.com/auth/calendar.events.owned": "add events to your own calendars",
}

# The OAuth errors from Google's token address that only signing in again can put right:
# a sign-in revoked or run out, or a client deleted, or with a secret made anew - which
# would otherwise be heard as Google being out of reach, for good.
SIGN_IN_ERRORS = ("invalid_grant", "invalid_client")

AUTHORISE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
# Nothing listens here, so the page Google sends the browser to doesn't load, and the
# user pastes its address back - as with Spotify. The brain has no browser of its own.
REDIRECT = "http://127.0.0.1:8085/"

# How long a request to Google may take, in seconds.
REQUEST_TIMEOUT = 10

# An access token is refreshed this many seconds before Google says it runs out, so
# one that's nearly done isn't sent with a request which then arrives too late.
REFRESH_EARLY = 60

CALENDARS = "https://www.googleapis.com/calendar/v3/calendars"
CALENDAR_LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"

# The most events Google gives on each page - its own most.
EVENTS_PAGE = 250

# What Google answers for a calendar it won't give the events of - deleted since its list
# was read, or no longer shared with the user. That one is left out, not the whole read.
CALENDAR_GONE = (403, 404)

# The few English words for the same things as an activity's name - so "am I working on
# Saturday?" is the Work calendar, and "when's my next lecture?" the Uni one. They're
# words, not calendars: one called Uni gets uni's.
SAME_AS = {"work": ("working", "shift", "shifts"), "uni": ("university", "lecture", "lectures", "class", "classes")}

CANT_REACH_GOOGLE = "I can't reach Google's calendar right now."
NEEDS_SIGN_IN = "I need signing in to Google again. That's done with a command on the brain."

# Most events said for one day before the rest are only counted.
MOST_EVENTS_SAID = 5

# From this many days, what's on is given as an overview - which days there's uni
# or work, and the rest by name - rather than every time of every day. A week said
# in full, as "uni from 11:30 AM until 1 PM and from 2:30 PM until 4:30 PM" a day
# at a time, took 52 seconds; as an overview, about 18. A fortnight, read out
# whole, once took three and a half minutes. "What about Friday?" still gets that
# day in full.
OVERVIEW_FROM_DAYS = 3

# Most things other than the activities named in an overview.
MOST_OTHERS_IN_OVERVIEW = 5

# How far ahead to look for one thing on the calendar - "when's my MOT?" - and the
# most matches said.
SEARCH_DAYS = 90
MOST_FOUND = 3


class SignInNeeded(Exception):
    """There's no sign-in to Google, or Google no longer accepts it."""


class ClientRefused(SignInNeeded):
    """Google doesn't know Nova's OAuth client, or not with that secret."""


def _challenge(verifier: str) -> str:
    """The S256 code challenge for a PKCE verifier, as RFC 7636 has it."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class GoogleSignIn:
    """
    Nova's sign-in to Google: signs in once, keeps the refresh token, and gives an
    access token for each request.

    A refresh token is all that's saved. The access token is kept in memory only, so
    a copy of the data folder never holds one that works on its own.
    """

    def __init__(self, client_id: str, client_secret: str, path: str = paths.GOOGLE_TOKEN_FILE,
                 session: Optional[requests.Session] = None):
        """
        Args:
            client_id: The OAuth client's id, GOOGLE_CLIENT_ID.
            client_secret: The OAuth client's secret, GOOGLE_CLIENT_SECRET.
            path: Where the sign-in is kept.
            session: What requests go through - a fake, in the tests.
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.path = path
        self._session = session or requests.Session()
        # The sign-in under way, kept in memory only and used once.
        self._state: Optional[str] = None
        self._verifier: Optional[str] = None
        self._access: Optional[str] = None
        self._access_until = 0.0
        # Several requests at once refresh the access token only once between them.
        self._lock = threading.Lock()

    def start(self) -> str:
        """
        Starts a sign-in.

        Returns:
            The address to open in a browser, to sign in to Google there.
        """
        self._state = secrets.token_urlsafe()
        self._verifier = secrets.token_urlsafe(64)
        fields = {
            "client_id": self.client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            # Offline gives a refresh token, and consent makes sure it's given even
            # when this account has signed Nova in before.
            "access_type": "offline",
            "prompt": "consent",
            "state": self._state,
            "code_challenge": _challenge(self._verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTHORISE}?{urlencode(fields)}"

    def finish(self, pasted: str):
        """
        Finishes the sign-in started, from the address Google sent the browser to.

        The state and verifier are used once, whether it works or not, so an address
        can't be tried again.

        Args:
            pasted: The address, pasted from the browser's address bar.

        Raises:
            ValueError: If the address isn't from the sign-in started, or none was,
                or it hasn't got Google's code in it - nothing is asked or saved - or
                if Google wasn't given everything Nova asks for, or gave no sign-in to
                keep - nothing is saved.
            SignInNeeded: If Google turned the code or the client down.
            requests.RequestException: If Google couldn't be reached, or refused the code.
        """
        state, verifier = self._state, self._verifier
        self._state = self._verifier = None
        fields = {name: values[0] for name, values in parse_qs(urlsplit(pasted.strip()).query).items()}
        # Compared in constant time, so how much of it matched can't be timed - as bytes,
        # since compare_digest won't take a string with "é" in it, and a pasted address
        # could have anything.
        if state is None or not secrets.compare_digest(fields.get("state", "").encode("utf-8"),
                                                       state.encode("ascii")):
            raise ValueError("That address isn't from this sign-in.")
        if not fields.get("code"):
            raise ValueError("That address hasn't got Google's code in it.")

        answer = self._ask_for_token({"grant_type": "authorization_code", "code": fields["code"],
                                      "code_verifier": verifier, "redirect_uri": REDIRECT})
        if not answer.get("refresh_token"):
            raise ValueError("Google didn't give Nova a sign-in to keep - try again.")
        # Google lets each box on its page be unticked. A sign-in without adding would only
        # have failed later, by voice.
        granted = answer.get("scope", " ".join(SCOPES)).split()
        missing = [SCOPES_SAID[scope] for scope in SCOPES if scope not in granted]
        if missing:
            raise ValueError(f"Google didn't let Nova {' or '.join(missing)} - sign in again, "
                             f"with every box ticked.")
        storage.save_json(self.path, {"refresh_token": answer["refresh_token"],
                                      "scope": answer.get("scope", " ".join(SCOPES))})
        with self._lock:
            self._keep(answer)
        logger.info("Signed in to Google")

    def signed_in(self) -> bool:
        """
        Returns:
            Whether there's a saved sign-in.
        """
        return self._refresh_token() is not None

    def access_token(self) -> str:
        """
        An access token for a request to Google, refreshed when it's nearly run out.

        Returns:
            The access token.

        Raises:
            SignInNeeded: If there's no saved sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached.
        """
        with self._lock:
            if self._access and time.monotonic() < self._access_until - REFRESH_EARLY:
                return self._access
            refresh_token = self._refresh_token()
            if refresh_token is None:
                raise SignInNeeded("Nova isn't signed in to Google.")
            self._keep(self._ask_for_token({"grant_type": "refresh_token", "refresh_token": refresh_token}))
            return self._access

    def drop_access_token(self):
        """
        Forgets the access token kept, so the next request gets one afresh - for one
        Google has turned down before it ran out, as it does once the sign-in is removed
        in the Google account.
        """
        with self._lock:
            self._access = None

    def _ask_for_token(self, fields: Dict[str, str]) -> Dict:
        """
        Asks Google's token address for a token.

        Args:
            fields: What's asked, besides the client's id and secret.

        Returns:
            Google's answer.

        Raises:
            SignInNeeded: If Google says the grant is no good - revoked, or run out - or
                ClientRefused, if it doesn't know the client.
            requests.RequestException: If Google couldn't be reached, or refused.
        """
        response = self._session.post(TOKEN, data={"client_id": self.client_id,
                                                   "client_secret": self.client_secret, **fields},
                                      timeout=REQUEST_TIMEOUT)
        error = self._error(response) if response.status_code in (400, 401) else None
        if error == "invalid_client":
            raise ClientRefused("Google doesn't know Nova's OAuth client.")
        if error in SIGN_IN_ERRORS:
            raise SignInNeeded("Google no longer accepts Nova's sign-in.")
        response.raise_for_status()
        try:
            answer = response.json()
        except ValueError as e:
            raise requests.RequestException("Google's answer wasn't JSON") from e
        if not isinstance(answer, dict):
            raise requests.RequestException("Google's answer wasn't a token")
        return answer

    @staticmethod
    def _error(response) -> Optional[str]:
        """The OAuth error Google answered with, such as invalid_grant."""
        try:
            body = response.json()
        except ValueError:
            return None
        return body.get("error") if isinstance(body, dict) else None

    def _keep(self, answer: Dict):
        """
        Keeps an access token from Google's answer in memory, until it runs out.

        Raises:
            requests.RequestException: If the answer hasn't got one.
        """
        if not answer.get("access_token"):
            raise requests.RequestException("Google's answer had no access token")
        self._access = answer["access_token"]
        try:
            lasts = float(answer.get("expires_in", 0))
        except (TypeError, ValueError):
            lasts = 0.0
        self._access_until = time.monotonic() + lasts

    def _refresh_token(self) -> Optional[str]:
        """The saved refresh token, or None if there isn't one."""
        try:
            saved = storage.load_json(self.path)
        except (OSError, ValueError) as e:
            logger.warning(f"Couldn't read the Google sign-in, so it needs doing again: {type(e).__name__}")
            return None
        if not isinstance(saved, dict) or not isinstance(saved.get("refresh_token"), str):
            return None
        return saved["refresh_token"] or None


class GoogleCalendar:
    """The user's calendars, read from Google itself with Nova's own sign-in."""

    def __init__(self, sign_in: GoogleSignIn, choices: ChoiceStore, session: Optional[requests.Session] = None):
        """
        Args:
            sign_in: Nova's sign-in to Google, which gives each request its access token.
            choices: calendars.json - "activities", which calendars are activities by id,
                for one that isn't by itself, or is but shouldn't be.
            session: What requests go through - a fake, in the tests.
        """
        self.sign_in = sign_in
        self.choices = choices
        self._session = session or requests.Session()
        # Google's list of the user's calendars, and when it was read, by time.monotonic().
        self._known: List[Dict] = []
        self._known_at: Optional[float] = None

    @classmethod
    def from_env(cls, config) -> Optional["GoogleCalendar"]:
        """
        The calendar, once Google's keys are set - GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
        in .env, or under Keys on the settings page.

        Args:
            config: Configs loaded from config.yaml - none of which the calendar needs.

        Returns:
            A GoogleCalendar, or None without both keys.
        """
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            logger.info("The calendar is off until GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set - see the "
                        "README's Google Calendar")
            return None
        # The paths are looked up now, not when this module was imported, as paths.DATA_DIR can be moved first.
        return cls(GoogleSignIn(client_id, client_secret, paths.GOOGLE_TOKEN_FILE),
                   ChoiceStore(os.path.join(paths.DATA_DIR, "calendars.json"), {"activities": {}, "new_events": None}))

    def _get(self, url: str, params: Dict) -> Dict:
        """
        Asks Google's calendar for something, with Nova's access token.

        Args:
            url: The address.
            params: What's asked, in its query.

        Returns:
            Google's answer.

        Raises:
            SignInNeeded: If there's no sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached, or refused.
            ValueError: If its answer wasn't a JSON object.
        """
        # With no network at all, nothing's tried - neither the sign-in nor the calendar,
        # each of which would wait out its timeout to say so.
        if not network.online():
            raise requests.ConnectionError("No network connection")
        for _ in range(2):
            response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT,
                                         headers={"Authorization": f"Bearer {self.sign_in.access_token()}"})
            if response.status_code != 401:
                break
            # Turned down before it ran out - the sign-in removed in the Google account, say.
            # Got afresh, the sign-in says whether it still works; turned down again, it doesn't.
            self.sign_in.drop_access_token()
        if response.status_code == 401:
            raise SignInNeeded("Google turned Nova's access token down.")
        response.raise_for_status()
        answer = response.json()
        if not isinstance(answer, dict):
            raise ValueError("Google's answer wasn't an object")
        return answer

    def _pages(self, url: str, params: Dict) -> List[Dict]:
        """
        Every item Google lists, over as many pages as it gives them in.

        Raises:
            The same as _get, and ValueError if a page's items aren't a list of objects.
        """
        items, page = [], None
        while True:
            answer = self._get(url, {**params, "pageToken": page} if page else params)
            listed = answer.get("items", [])
            if not isinstance(listed, list) or not all(isinstance(item, dict) for item in listed):
                raise ValueError("Google's items aren't a list")
            items += listed
            page = answer.get("nextPageToken")
            if not page:
                return items

    def calendars(self, max_age: float = 300) -> List[Dict]:
        """
        Google's list of the user's calendars, read afresh once it's older than max_age -
        so one added in Google turns up by itself.

        Args:
            max_age: Seconds the list read last is good for.

        Returns:
            Each calendar as {"id", "name", "primary", "writable"} - writable for the user's
            own, as calendar.events.owned only adds to those.

        Raises:
            SignInNeeded: If there's no sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached, or refused.
            ValueError: If its answer wasn't a list of calendars.
        """
        if self._known_at is not None and time.monotonic() - self._known_at < max_age:
            return list(self._known)
        listed = []
        for entry in self._pages(CALENDAR_LIST, {}):
            if not isinstance(entry.get("id"), str):
                raise ValueError("A calendar in Google's list has no id")
            listed.append({"id": entry["id"], "name": entry.get("summaryOverride") or entry.get("summary") or "",
                           "primary": entry.get("primary") is True, "writable": entry.get("accessRole") == "owner"})
        self._known, self._known_at = listed, time.monotonic()
        return list(listed)

    def known(self) -> List[Dict]:
        """The calendars Google listed last, without asking it - none until it's been asked."""
        return list(self._known)

    def activity_calendars(self) -> Dict[str, Dict]:
        """
        The activities, from the calendars Google listed last, without asking it: every
        calendar but the user's own, unless calendars.json says otherwise for it.

        Returns:
            Each activity's word - its calendar's name, in lower case - to its calendar.
        """
        chosen = self.choices.get("activities")
        chosen = chosen if isinstance(chosen, dict) else {}
        found: Dict[str, Dict] = {}
        for calendar in self.known():
            choice = chosen.get(calendar["id"])
            word = calendar["name"].strip().lower()
            if word and (choice if isinstance(choice, bool) else not calendar["primary"]):
                # Of two calendars with one name, the first in Google's list is the one heard.
                found.setdefault(word, calendar)
        return found

    def activity_words(self) -> Dict[str, str]:
        """
        Every word an activity is heard by locally, to the activity: its name, its plural,
        and SAME_AS's words for it. A name of two or more words is heard by its first word
        too - "uni" for "Uni timetable" - unless another activity's name starts with it.
        Each is as the request is, once intents.py has tidied it - "Work (Tesco)" is
        "work tesco".

        Returns:
            e.g. {"work": "work", "works": "work", "shift": "work", ...}.
        """
        names = {_as_said(word): word for word in self.activity_calendars()}
        names.pop("", None)
        firsts: Dict[str, List[str]] = {}
        for said in names:
            firsts.setdefault(said.split()[0], []).append(said)
        heard: List[Tuple[str, str]] = [(said, word) for said, word in names.items()]
        heard += [(said if said.endswith("s") else said + "s", word) for said, word in names.items()]
        heard += [(first, names[saids[0]]) for first, saids in firsts.items() if len(saids) == 1 and first != saids[0]]
        heard += [(same, word) for said, word in list(heard) for same in SAME_AS.get(said, ())]
        words: Dict[str, str] = {}
        # A name is heard as that activity before it's another's plural, first word or SAME_AS.
        for said, word in heard:
            words.setdefault(said, word)
        return words

    def events(self, first: date, days: int, calendar_ids: Optional[List[str]] = None) -> List[Dict]:
        """
        Everything on the calendars over some days.

        Args:
            first: The first day.
            days: How many days, from 1.
            calendar_ids: The calendars to read, or None for every one in Google's list.

        Returns:
            The events, earliest first, each a dict with "summary", "start", "end",
            "all_day" and "calendar", its calendar's name - start a local datetime, or
            a date for an all-day event.

        Raises:
            SignInNeeded: If there's no sign-in, or Google no longer accepts it.
            requests.RequestException: If Google couldn't be reached, or refused.
            ValueError, KeyError, TypeError: If its answer wasn't the shape of events.

        A calendar Google won't give the events of - deleted, or no longer shared - is left
        out, with a warning, and the rest are read.
        """
        names = {calendar["id"]: calendar["name"] for calendar in self.calendars()}
        # Each end made from its own day, not the start plus so many hours, so a day the
        # clocks change on is still from midnight to midnight.
        window = {"timeMin": datetime.combine(first, day_time()).astimezone().isoformat(),
                  "timeMax": datetime.combine(first + timedelta(days=days), day_time()).astimezone().isoformat(),
                  # Each time a repeating event is on, as an event of its own - as Home Assistant gave them.
                  "singleEvents": "true", "orderBy": "startTime", "maxResults": EVENTS_PAGE}
        found = []
        for calendar_id in (list(names) if calendar_ids is None else calendar_ids):
            try:
                events = self._pages(f"{CALENDARS}/{quote(calendar_id, safe='')}/events", window)
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status not in CALENDAR_GONE:
                    raise
                # Not its id, or the error, which has the address with the id in it.
                logger.warning(f"Left a calendar out, as Google wouldn't give its events: {status}")
                # Its list is read afresh next time, so a calendar since deleted is gone from it.
                self._known_at = None
                continue
            for event in events:
                if event.get("status") == "cancelled":
                    continue
                # A calendar not in Google's list has no name to give - and its id, which
                # for the primary is the user's email address, is never said in its place.
                found.append({**_read_event(event), "calendar": names.get(calendar_id, "")})
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
        except READ_FAILURES as e:
            return _failed(e)
        events = _as_activities(events, self.activity_calendars(), first)
        return {"success": True, "message": describe_agenda(events, first, days, today or date.today())}

    def find(self, wanted: str, first: date, today: Optional[date] = None) -> Dict:
        """
        When one thing is on the calendar, for "when's my MOT?". Reading out whole
        days instead, Claude once read a fortnight of them looking for it.

        Args:
            wanted: A word or two from its name, e.g. "MOT".
            first: The first day to look from.
            today: Today's date - only given by the tests.

        Returns:
            Dict with 'success' and when it is as the 'message'.
        """
        words = [word.strip(".,!?'\"") for word in (wanted or "").split()]
        patterns = [_called([word]) for word in words if word]
        if not patterns:
            return {"success": False, "message": "I didn't catch what to look for."}
        try:
            events = self.events(first, SEARCH_DAYS)
        except READ_FAILURES as e:
            return _failed(e)
        found = [event for event in events if all(pattern.search(event["summary"]) for pattern in patterns)]
        return {"success": True, "message": describe_found(" ".join(words), found, today or date.today())}

    def activity(self, name: str, first: date, days: int = 1, today: Optional[date] = None,
                 count: Optional[int] = None, now: Optional[datetime] = None) -> Dict:
        """
        When the user has one of their activities - work, uni, the gym - from its calendar
        alone. Every event on it counts, whatever it's called.

        Args:
            name: The activity, or a word it's heard by, e.g. "work" or "shifts".
            first: The first day asked about.
            days: How many days.
            today: Today's date - only given by the tests.
            count: Only say the next this many times, for "when's my next shift?".
            now: The time now - only given by the tests.

        Returns:
            Dict with 'success' and the times as the 'message'.
        """
        said = (name or "").strip()
        if not said:
            return {"success": False, "message": "I didn't catch which of your calendars you meant."}
        try:
            # Google's list is read, if it's old, before it's looked in.
            self.calendars()
            activities = self.activity_calendars()
            word = said.lower() if said.lower() in activities else self.activity_words().get(_as_said(said))
            if word is None:
                return {"success": False, "message": f"I haven't got a calendar called {said}."}
            events = self.events(first, days, calendar_ids=[activities[word]["id"]])
        except READ_FAILURES as e:
            return _failed(e)
        if count:
            return {"success": True, "message": describe_next(word, events, count, first,
                                                              now or datetime.now().astimezone())}
        return {"success": True, "message": describe_activity(word, events, first, days, today or date.today())}


# What reading the calendar can fail with: no sign-in, Google out of reach or refusing,
# or an answer of the wrong shape.
READ_FAILURES = (SignInNeeded, requests.RequestException, ValueError, KeyError, TypeError)


def _failed(error: Exception) -> Dict:
    """
    What's said when the calendar couldn't be read, depending on why. Only the kind of
    failure is logged: a requests error names the address, with a calendar's id in it -
    for the primary, the user's email address.
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    logger.error(f"Couldn't read the calendar: {type(error).__name__}{f' {status}' if status else ''}")
    if isinstance(error, SignInNeeded):
        return {"success": False, "message": NEEDS_SIGN_IN}
    return {"success": False, "message": CANT_REACH_GOOGLE}


def _as_said(words: str) -> str:
    """Words as a request is matched by intents.py: lower case, letters, digits and apostrophes only."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", words.lower())).strip()


def _called(words: List[str]) -> re.Pattern:
    """
    Finds an event's name with a word in it starting with one of these - so "CSC"
    finds "CSC203 Lecture" and "shift" finds "Night shift", but neither turns up
    inside some other word.
    """
    return re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + ")", re.IGNORECASE)


def _as_activities(events: List[Dict], activities: Dict[str, Dict], first: date) -> List[Dict]:
    """
    The user's activities - work, uni - as one stretch of time each, named after the
    activity. A day of uni was read out session by session, by each one's own name -
    "CSC2035/P01/01 <04-08> at 11:30 AM" - which took 25 seconds and meant little.
    Now it's "uni from 9 AM until 1 PM". Everything else is left as it is.

    Args:
        events: From GoogleCalendar.events, earliest first - each with its "calendar".
        activities: The activity calendars, by each activity's word - see
            GoogleCalendar.activity_calendars. Every event on one is that activity.
        first: The first day asked about.

    Returns:
        The events, earliest first, with each activity once a day. Its stretches of
        time that day are in "spans", to be said with their ends as well as their
        starts.
    """
    kept, theirs = [], {name: [] for name in activities}
    for event in events:
        name = event["calendar"].strip().lower()
        (theirs[name] if name in theirs else kept).append(event)
    for name, its_events in theirs.items():
        for day, on_day in _by_day(its_events, first).items():
            if any(event["all_day"] for event in on_day):
                kept.append({"summary": name, "start": day, "end": None, "all_day": True, "activity": True})
            spans = _stretches(on_day)
            if spans:
                kept.append({"summary": name, "start": spans[0][0], "end": None, "all_day": False, "spans": spans,
                             "activity": True})
    return sorted(kept, key=lambda event: _sort_key(event["start"]))


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
        events: Its events, from GoogleCalendar.events, earliest first.
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
    """One event from Google, with its start and end read into dates or local times."""
    start, end = event.get("start") or {}, event.get("end") or {}
    if "dateTime" in start:
        when, all_day = datetime.fromisoformat(start["dateTime"]).astimezone(), False
        until = datetime.fromisoformat(end["dateTime"]).astimezone() if "dateTime" in end else None
    else:
        when, all_day, until = date.fromisoformat(start["date"]), True, None
    # Google's is always a string, but one that isn't mustn't raise out of the agenda.
    summary = event.get("summary") if isinstance(event.get("summary"), str) else None
    return {"summary": (summary or "something").strip(), "start": when, "end": until, "all_day": all_day}


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
    Says what's on - a day at a time for a day or two, or as an overview for more.

    Args:
        events: From GoogleCalendar.events, earliest first - with the activities
            joined up, see _as_activities.
        first: The first day asked about.
        days: How many days.
        today: Today's date.

    Returns:
        e.g. "On Thursday, you've got the dentist at 9:30 AM, and dinner with Sam at
        7:00 PM.", "Over the next week, you've got uni today and Thursday. There's
        also the dentist on Friday." or "You've got nothing on tomorrow."
    """
    if not events:
        return _nothing_on(first, days, today)
    if days >= OVERVIEW_FROM_DAYS:
        return _overview(events, first, days, today)
    return _day_by_day(events, first, days, today)


def _nothing_on(first: date, days: int, today: date) -> str:
    """What's said when there's nothing on - "You've got nothing on tomorrow", say."""
    last = first + timedelta(days=days - 1)
    if days == 1:
        return f"You've got nothing on {spoken_day(first, today)}."
    if days == 2:
        return f"You've got nothing on {spoken_day(first, today)} or {spoken_day(last, today)}."
    return f"You've got nothing on from {spoken_day(first, today)} to {spoken_day(last, today)}."


def _day_by_day(events: List[Dict], first: date, days: int, today: date) -> str:
    """What's on, each day in full - see describe_agenda."""
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
        shown = on_day[:MOST_EVENTS_SAID]
        said = [_event_said(event) for event in shown]
        if len(on_day) > len(shown):
            said.append(f"{len(on_day) - len(shown)} more")
        listed = said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", and " + said[-1]
        on = _on(day, today)
        sentences.append(f"{on[0].upper()}{on[1:]}, you've got {listed}.")

    return " ".join(sentences) if sentences else _nothing_on(first, days, today)


def _overview(events: List[Dict], first: date, days: int, today: date) -> str:
    """
    What's on over several days, without the times - see describe_agenda. Each
    activity is said with the days it's on - by name within a week, or as a count
    over longer - and then the other things, each with its day.
    """
    last = first + timedelta(days=days - 1)
    if first == today:
        over = {7: "Over the next week", 14: "Over the next two weeks"}.get(days, f"Over the next {days} days")
    else:
        over = f"From {spoken_day(first, today)} to {spoken_day(last, today)}"

    activity_days: Dict[str, List[date]] = {}
    others = []
    for event in events:
        day = max(event["start"] if event["all_day"] else event["start"].date(), first)
        if event.get("activity"):
            if day not in activity_days.setdefault(event["summary"], []):
                activity_days[event["summary"]].append(day)
        else:
            others.append((event, day))

    activities = []
    for name, on in activity_days.items():
        if len(on) == days:
            activities.append(f"{name} every day")
        elif days > 7:
            activities.append(f"{name} on {len(on)} day{'s' if len(on) != 1 else ''}")
        else:
            # Within a week a day's name is enough, even one further off than
            # spoken_day would name alone - the opening has already said which week.
            names = [spoken_day(day, today) if day <= today + timedelta(days=1) else day.strftime("%A")
                     for day in sorted(on)]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            activities.append(f"{name} {'' if names[0] in ('today', 'tomorrow') else 'on '}{listed}")

    named = [f"{event['summary']} {_on(day, today)}" for event, day in others[:MOST_OTHERS_IN_OVERVIEW]]
    if len(others) > len(named):
        named.append(f"{len(others) - len(named)} more")

    def joined(items: List[str]) -> str:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]

    if not activities:
        return f"{over}, you've got {joined(named)}."
    said = f"{over}, you've got {joined(activities)}."
    return f"{said} There's also {joined(named)}." if named else said


def _event_said(event: Dict) -> str:
    """One event, as it's said: "the dentist at 9:30 AM", "uni from 9 AM until 1 PM", "Sam's birthday, all day"."""
    if event["all_day"]:
        return f"{event['summary']}, all day"
    if event.get("spans"):
        return f"{event['summary']} " + " and ".join(_span(start, end) for start, end in event["spans"])
    return f"{event['summary']} at {spoken_clock_time(event['start'].hour, event['start'].minute)}"


def describe_found(wanted: str, events: List[Dict], today: date) -> str:
    """
    Says when something looked for on the calendar is on.

    Args:
        wanted: What was looked for, e.g. "MOT".
        events: The events found, earliest first.
        today: Today's date.

    Returns:
        e.g. "You've got Car MOT on Thursday the 30th of October, at 9:30 AM." or
        "I can't see MOT on your calendar in the next three months."
    """
    if not events:
        return f"I can't see {wanted} on your calendar in the next three months."
    said = []
    for event in events[:MOST_FOUND]:
        day = event["start"] if event["all_day"] else event["start"].date()
        when = "all day" if event["all_day"] else f"at {spoken_clock_time(event['start'].hour, event['start'].minute)}"
        said.append(f"{event['summary']} {_on(day, today)}, {when}")
    listed = said[0] if len(said) == 1 else "; ".join(said[:-1]) + "; and " + said[-1]
    more = f" And {len(events) - MOST_FOUND} more after that." if len(events) > MOST_FOUND else ""
    return f"You've got {listed}.{more}"


def main(argv: Optional[List[str]] = None):
    """
    Signs Nova in to Google, once, on the brain, as Nova's user:
    venv/bin/python -m voice_assistant.google_calendar

    It never prints a token, a code or a calendar's id.

    Args:
        argv: The command's arguments - sys.argv's, if None.

    Raises:
        SystemExit: With what went wrong, if the sign-in couldn't be done.
    """
    parser = argparse.ArgumentParser(prog="python -m voice_assistant.google_calendar",
                                     description="Signs Nova in to Google, for the calendar.")
    parser.add_argument("--again", action="store_true", help="sign in again, even if Nova already is")
    arguments = parser.parse_args(argv)

    paths.load_secrets()
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are needed first - in "
                         f"{paths.SECRETS_FILE}, or under Keys on the settings page.")

    sign_in = GoogleSignIn(client_id, client_secret, paths.GOOGLE_TOKEN_FILE)
    if sign_in.signed_in() and not arguments.again:
        print("Already signed in to Google - nothing to do. Give --again to sign in again.")
        return

    print("\nOpen this address in a browser, and sign in to Google there:")
    print(f"{sign_in.start()}\n")
    pasted = input("The page won't load - that's expected. Copy its address from the address bar "
                   "and paste it here: ")
    try:
        sign_in.finish(pasted)
    except ValueError as e:
        raise SystemExit(str(e))
    except ClientRefused:
        # Mistyped at first setup, or the client deleted or its secret made anew - trying again won't help.
        raise SystemExit("Google didn't accept GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET - check both in "
                         f"{paths.SECRETS_FILE}, or under Keys on the settings page.")
    except SignInNeeded:
        raise SystemExit("Google didn't accept that sign-in - try again.")
    except requests.RequestException:
        # Not the error itself, which could quote what Google answered.
        raise SystemExit("Couldn't reach Google, or it refused the sign-in - try again.")
    print("Signed in to Google.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
