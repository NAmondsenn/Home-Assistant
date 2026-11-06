"""
Google Calendar Test
Checks Nova's own Google sign-in - that it asks for only what Nova uses, refuses an
address from another sign-in, keeps the sign-in readable by Nova's user only, and
keeps an access token until it nearly runs out - and the calendar read from Google
with it, and said. A fake stands in for Google, answering as its API reference
gives it, so nothing ever reaches Google. Every id, secret, code and token is made up.
"""

import base64
import hashlib
import inspect
import json
import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import pytest
import requests
import urllib3

from voice_assistant import google_calendar, storage
from voice_assistant.actions import Action, ActionExecutor
from voice_assistant.choices import ChoiceStore
from voice_assistant.google_calendar import (CANT_REACH_GOOGLE, NEEDS_SIGN_IN, ClientRefused, GoogleCalendar,
                                             GoogleSignIn, GoogleSignInError, SignInNeeded, describe_agenda,
                                             describe_found, named_by_address, spoken_name)
from voice_assistant.intents import LocalIntents

CLIENT_ID = "made-up-client.apps.example.com"
CLIENT_SECRET = "made-up-client-secret"
REFRESH_TOKEN = "made-up-refresh-token"
ACCESS_TOKEN = "made-up-access-token"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
        self.ok = status < 400

    def json(self):
        return self.body

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeGoogle:
    """
    Stands in for a requests.Session, answering each address from a list of answers
    and noting every request - an answer can be an exception, to be raised instead,
    and an answer that isn't a FakeResponse is a 200 with it as the body.
    """

    def __init__(self, **answers):
        self.answers = {address: list(each) for address, each in answers.items()}
        self.asked = []
        # What each GET was sent with besides its address - its params, headers and timeout.
        self.fetched = []
        # Each POST, as it was sent.
        self.posted = []

    def post(self, url, data=None, json=None, headers=None, timeout=None, **kwargs):
        # Nova only ever posts for a token or a new event - see test_nova_never_changes_or_deletes_an_event.
        if url != google_calendar.TOKEN and not url.endswith("/events"):
            raise AssertionError(f"Nothing should be posted to {url}")
        self.asked.append((url, dict(data or json or {})))
        self.posted.append({"url": url, "json": json, "headers": dict(headers or {}), "timeout": timeout})
        return self._answer(url)

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        if url not in self.answers:
            raise AssertionError(f"Nothing should be fetched from {url}")
        self.asked.append((url, dict(params or {})))
        self.fetched.append({"params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout})
        return self._answer(url)

    def _answer(self, url):
        answer = self.answers[url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, FakeResponse) else FakeResponse(200, answer)


def _token_answer(access=ACCESS_TOKEN, expires_in=3600, refresh=REFRESH_TOKEN):
    body = {"access_token": access, "expires_in": expires_in, "token_type": "Bearer",
            "scope": " ".join(google_calendar.SCOPES)}
    if refresh:
        body["refresh_token"] = refresh
    return FakeResponse(200, body)


def _query(address):
    return {name: values[0] for name, values in parse_qs(urlsplit(address).query).items()}


def _pasted(state, code="made-up-code"):
    """The address Google's page sends the browser to, as the user pastes it back."""
    fields = {"state": state, "scope": " ".join(google_calendar.SCOPES)}
    if code:
        fields["code"] = code
    return google_calendar.REDIRECT + "?" + urlencode(fields)


@pytest.fixture
def token_file(tmp_path):
    return str(tmp_path / "data" / "google_token.json")


def _signed_in(token_file):
    storage.save_json(token_file, {"refresh_token": REFRESH_TOKEN, "scope": " ".join(google_calendar.SCOPES)})


# Signing in

def test_the_sign_in_asks_for_only_what_nova_uses(token_file):
    address = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=FakeGoogle()).start()

    assert address.startswith(google_calendar.AUTHORISE + "?")
    fields = _query(address)
    assert fields["scope"].split(" ") == [
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.readonly",
        "https://www.googleapis.com/auth/calendar.events.owned"]
    assert fields["access_type"] == "offline" and fields["prompt"] == "consent"
    assert fields["code_challenge_method"] == "S256" and fields["code_challenge"]
    assert fields["state"]
    assert fields["client_id"] == CLIENT_ID and fields["redirect_uri"] == google_calendar.REDIRECT
    assert fields["response_type"] == "code"
    # The secret is only ever sent to Google's token address, never put in a link.
    assert CLIENT_SECRET not in address


@pytest.mark.parametrize("started", [True, False])
def test_a_sign_in_finished_from_another_address_is_refused(token_file, started):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    if started:
        sign_in.start()

    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted("another-sign-ins-state"))

    assert google.asked == [] and not os.path.exists(token_file)


def test_a_pasted_state_that_isnt_plain_ascii_is_refused_as_another_sign_ins(token_file):
    # Compared as it came, a pasted "state=%C3%A9" raised a TypeError rather than saying so.
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    sign_in.start()

    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted("\u00e9"))

    assert google.asked == [] and not os.path.exists(token_file)


def test_an_address_without_googles_code_is_refused(token_file):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    state = _query(sign_in.start())["state"]

    with pytest.raises(ValueError, match="That address hasn't got Google's code in it."):
        sign_in.finish(_pasted(state, code=None))

    assert google.asked == [] and not os.path.exists(token_file)
    # The state is used once, so the same address can't finish it afterwards either.
    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(state))


def test_finishing_saves_the_sign_in_for_novas_user_only(token_file):
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer()]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    fields = _query(sign_in.start())

    sign_in.finish(_pasted(fields["state"]))

    [(url, sent)] = google.asked
    assert url == google_calendar.TOKEN
    assert sent["code"] == "made-up-code" and sent["grant_type"] == "authorization_code"
    assert sent["redirect_uri"] == google_calendar.REDIRECT
    assert sent["client_id"] == CLIENT_ID and sent["client_secret"] == CLIENT_SECRET
    # The verifier sent is the one whose S256 was in the address.
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == fields["code_challenge"]

    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    with open(token_file) as file:
        saved = json.load(file)
    assert saved == {"refresh_token": REFRESH_TOKEN, "scope": " ".join(google_calendar.SCOPES)}
    assert sign_in.signed_in()
    # The state and verifier are used once.
    with pytest.raises(ValueError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(fields["state"]))


# Using it

def test_an_access_token_is_kept_until_it_nearly_runs_out(token_file, monkeypatch):
    _signed_in(token_file)
    now = [1000.0]
    monkeypatch.setattr(google_calendar.time, "monotonic", lambda: now[0])
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer("first-access", refresh=None),
                                                    _token_answer("second-access", refresh=None)]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    assert sign_in.access_token() == "first-access"
    now[0] += 3500
    assert sign_in.access_token() == "first-access"
    assert len(google.asked) == 1
    [(_, sent)] = google.asked
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == REFRESH_TOKEN

    # Within a minute of running out, it's refreshed.
    now[0] += 41
    assert sign_in.access_token() == "second-access"
    assert len(google.asked) == 2


def test_a_revoked_sign_in_says_it_needs_doing_again(token_file):
    _signed_in(token_file)
    google = FakeGoogle(**{google_calendar.TOKEN: [
        FakeResponse(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}),
        requests.ConnectionError("no network")]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    with pytest.raises(SignInNeeded):
        sign_in.access_token()
    with pytest.raises(requests.RequestException):
        sign_in.access_token()


@pytest.mark.parametrize("status", [400, 401])
def test_a_client_google_no_longer_knows_says_the_sign_in_needs_doing_again(token_file, status):
    # The client deleted in Google Cloud, or its secret made anew: taken for Google being
    # out of reach, "I can't reach Google's calendar" would never have gone away.
    _signed_in(token_file)
    google = FakeGoogle(**{google_calendar.TOKEN: [
        FakeResponse(status, {"error": "invalid_client", "error_description": "The OAuth client was not found."})]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    with pytest.raises(SignInNeeded):
        sign_in.access_token()


@pytest.mark.parametrize("status", [400, 401])
def test_a_client_google_wont_let_sign_in_says_the_sign_in_needs_doing_again(token_file, status):
    # A client of the wrong type - not a "Desktop app" - which trying again won't put right either.
    _signed_in(token_file)
    google = FakeGoogle(**{google_calendar.TOKEN: [
        FakeResponse(status, {"error": "unauthorized_client", "error_description": "Unauthorized"})]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    with pytest.raises(SignInNeeded):
        sign_in.access_token()


def test_without_a_sign_in_it_needs_doing(token_file):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    assert not sign_in.signed_in()
    with pytest.raises(SignInNeeded):
        sign_in.access_token()
    assert google.asked == []


# Signing in, when it goes wrong

def test_a_sign_in_without_every_permission_isnt_kept(token_file):
    # Google lets each box on its page be unticked. Kept, the sign-in said "Signed in to
    # Google." - and adding an event would only have failed later, by voice.
    answer = _token_answer()
    answer.body["scope"] = " ".join(google_calendar.SCOPES[:2])
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError) as refused:
        sign_in.finish(_pasted(state))

    said = str(refused.value)
    assert "add events to your own calendars" in said and "read your calendars' events" not in said
    assert "sign in again, with every box ticked" in said
    assert not os.path.exists(token_file)


def test_a_client_google_doesnt_know_at_the_sign_in_is_told_apart(token_file):
    # A mistyped GOOGLE_CLIENT_SECRET at first setup, which trying again never helps.
    google = FakeGoogle(**{google_calendar.TOKEN: [FakeResponse(401, {"error": "invalid_client",
                                                                      "error_description": "Unauthorized"})]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    state = _query(sign_in.start())["state"]

    with pytest.raises(ClientRefused):
        sign_in.finish(_pasted(state))

    assert not os.path.exists(token_file)


def test_a_google_sign_in_that_couldnt_be_saved_isnt_said_to_have_worked(token_file, monkeypatch, caplog):
    def full(path, data, indent=None):
        raise OSError(28, "No space left on device", path)

    monkeypatch.setattr(google_calendar.storage, "save_json", full)
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [_token_answer()]}))
    state = _query(sign_in.start())["state"]
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GoogleSignInError, match="Couldn't save the sign-in - check Nova's data folder."):
        sign_in.finish(_pasted(state))

    assert not sign_in.signed_in()
    # Not kept in memory either, as if it had worked until a restart.
    assert sign_in._access is None
    for secret in (REFRESH_TOKEN, ACCESS_TOKEN, "made-up-code", CLIENT_SECRET):
        assert secret not in caplog.text


def test_two_google_finishes_at_once_use_the_state_once(token_file):
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer() for _ in range(8)]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    address = _pasted(_query(sign_in.start())["state"])
    finished = []
    ready = threading.Barrier(8)

    def finish():
        ready.wait()
        try:
            sign_in.finish(address)
            finished.append(True)
        except ValueError:
            pass

    threads = [threading.Thread(target=finish) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert finished == [True] and len(google.asked) == 1


def test_urllib3_never_logs_googles_addresses_whatever_novas_level():
    # Its debug lines have each address asked for - an events address has a calendar's
    # id in it, and the user's own is their email address. Run alone, so nothing else
    # Nova imports has quietened it first.
    checked = subprocess.run(
        [sys.executable, "-c", "import logging, voice_assistant.google_calendar; "
                               "assert not logging.getLogger('urllib3.connectionpool').isEnabledFor(logging.INFO)"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_a_started_sign_in_runs_out(token_file, monkeypatch):
    # Kept for ever, a sign-in started and left would still take its address days later.
    now = [1000.0]
    monkeypatch.setattr(google_calendar.time, "monotonic", lambda: now[0])
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer()]})
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    state = _query(sign_in.start())["state"]
    now[0] += 601
    with pytest.raises(GoogleSignInError, match="That sign-in ran out - start again."):
        sign_in.finish(_pasted(state))
    assert google.asked == [] and not os.path.exists(token_file)

    # Just within the ten minutes, it's taken.
    state = _query(sign_in.start())["state"]
    now[0] += 599
    sign_in.finish(_pasted(state))
    assert sign_in.signed_in()


@pytest.mark.parametrize("error, logged", [("access_denied", "access_denied"),
                                           ("bad\nINFO forged line", "an unreadable error")])
def test_a_cancelled_google_sign_in_is_answered_as_spotifys_is(token_file, caplog, error, logged):
    # Cancel, on Google's page, sends the browser back with error= and the right state,
    # and no code - which isn't the user copying too little of the address.
    caplog.set_level(logging.INFO)
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError, match="Google didn't accept that sign-in - start again."):
        sign_in.finish(f"{google_calendar.REDIRECT}?{urlencode({'error': error, 'state': state})}")

    assert google.asked == [] and not os.path.exists(token_file)
    assert f"Google turned the sign-in down: {logged}" in caplog.text and "forged" not in caplog.text
    # Used up, as any other finish.
    with pytest.raises(GoogleSignInError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(state))


@pytest.mark.parametrize("answer, code", [
    (FakeResponse(400, {"error": "redirect_uri_mismatch", "error_description": "Not what the client lists"}),
     "redirect_uri_mismatch"),
    (FakeResponse(403, {"error": "access_denied", "error_description": "The project's off"}), "access_denied"),
    # Anything but a plain error code is only its status - the answer's own words never.
    (FakeResponse(400, {"error": "<b>made-up</b>", "error_description": "Words"}), "HTTP 400"),
    (FakeResponse(404, ["not", "an", "error"]), "HTTP 404"),
])
def test_google_refusing_the_sign_in_otherwise_names_its_error(token_file, caplog, answer, code):
    # Said as Google out of reach, a client set up wrongly sent the user the wrong way.
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError) as refused:
        sign_in.finish(_pasted(state))

    assert str(refused.value) == f"Google refused the sign-in ({code}) - check the client in Google Cloud."
    assert not os.path.exists(token_file)
    for words in ("Not what the client lists", "The project's off", "<b>", "Words"):
        assert words not in caplog.text


@pytest.mark.parametrize("answer", [
    FakeResponse(429, {"error": "rate_limit_exceeded", "error_description": "Words"}),
    FakeResponse(429, ["not", "an", "error"]),
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}),
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits",
                                                          "reason": "userRateLimitExceeded"}]}}),
])
def test_google_slowing_the_sign_in_down_says_to_try_again_not_to_check_the_client(token_file, caplog, answer):
    # A busy Google isn't a client set up wrongly, which "check the client in Google Cloud" says.
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError) as refused:
        sign_in.finish(_pasted(state))

    assert str(refused.value) == "Google is slowing Nova down - start the sign-in again in a minute."
    assert f"HTTP {answer.status_code}" in caplog.text and "Words" not in caplog.text
    assert not os.path.exists(token_file)
    # The sign-in is used up, so it is started afresh.
    with pytest.raises(GoogleSignInError, match="That address isn't from this sign-in - start again."):
        sign_in.finish(_pasted(state))


@pytest.mark.parametrize("changed", [
    {"access_token": None},
    {"access_token": ""},
    {"refresh_token": None},
    {"scope": ["https://www.googleapis.com/auth/calendar.events.owned"]},
    {"token_type": "mac"},
    {"token_type": None},
])
def test_an_answer_from_google_that_isnt_a_whole_sign_in_isnt_kept(token_file, changed):
    # Saved without an access token, the page said "Couldn't reach Google" for a sign-in it
    # had kept - and one with no scope as text didn't answer at all.
    answer = _token_answer()
    for name, value in changed.items():
        if value is None:
            answer.body.pop(name, None)
        else:
            answer.body[name] = value
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file,
                           session=FakeGoogle(**{google_calendar.TOKEN: [answer]}))
    state = _query(sign_in.start())["state"]

    with pytest.raises(GoogleSignInError, match="Google didn't give Nova a sign-in to keep - try again."):
        sign_in.finish(_pasted(state))

    assert not os.path.exists(token_file) and sign_in._access is None


class RunsAtOnce:
    """Stands in for threading.Thread, doing its work as it's started."""

    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


def test_a_sign_in_reads_the_list_again_at_once(make_calendar, monkeypatch):
    calendar = make_calendar()
    calendar.calendars()
    calendar._session.answers[LIST] = [_calendar_list(_entry("calendar-gym", "Gym"))]
    monkeypatch.setattr(google_calendar.threading, "Thread", RunsAtOnce)

    calendar.read_list_again()

    assert [each["id"] for each in calendar.known()] == ["calendar-gym"]
    assert "gym" in calendar.activity_words()


def test_a_list_read_under_way_cant_put_the_last_accounts_list_back(make_calendar, monkeypatch):
    # A read begun with the account before's sign-in, which answers once the new account's
    # read - started by its sign-in - has finished.
    calendar = make_calendar()
    google = calendar._session
    google.answers[LIST] = [_calendar_list(_entry("calendar-old", "Old", primary=True)),
                            _calendar_list(_entry("calendar-new", "New", primary=True))]
    monkeypatch.setattr(google_calendar.threading, "Thread", RunsAtOnce)
    asked = google.get

    def get(url, *args, **kwargs):
        answer = asked(url, *args, **kwargs)
        if url == LIST and len([each for each in google.asked if each[0] == LIST]) == 1:
            calendar.read_list_again()
        return answer

    monkeypatch.setattr(google, "get", get)
    calendar.calendars()

    assert [each["id"] for each in calendar.known()] == ["calendar-new"]


def test_nova_says_google_is_signed_in_to_on_the_settings_page():
    assert NEEDS_SIGN_IN == "I need signing in to Google again. That's done on the settings page."


def test_the_command_on_the_brain_is_gone():
    # The settings page signs in now, so there's no second way in to keep safe.
    assert not hasattr(google_calendar, "main")
    assert "__main__" not in inspect.getsource(google_calendar)


# Reading the calendars

LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"

# Made as the file is read, before the london fixture has set the time zone, so
# given it themselves.
LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def london(monkeypatch):
    """
    Runs every test on UK time, whatever the machine's own time zone. Calendar times are
    said in the brain's own time, and the events here are written in BST - so on
    GitHub, which runs on UTC, uni "from 11:30 AM" came out as "from 10:30 AM".
    """
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class FakeSignIn:
    """Stands in for GoogleSignIn, with an access token that's always good."""

    def access_token(self):
        return ACCESS_TOKEN

    def drop_access_token(self):
        pass


def _events_url(calendar_id):
    return f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"


def _entry(calendar_id, name, primary=False, access_role="owner", override=None):
    """One calendar in Google's calendarList, as its API reference gives it."""
    entry = {"kind": "calendar#calendarListEntry", "etag": '"1"', "id": calendar_id, "summary": name,
             "timeZone": "Europe/London", "accessRole": access_role, "selected": True}
    if primary:
        entry["primary"] = True
    if override:
        entry["summaryOverride"] = override
    return entry


def _calendar_list(*entries, next_page=None):
    page = {"kind": "calendar#calendarList", "etag": '"1"', "items": list(entries)}
    if next_page:
        page["nextPageToken"] = next_page
    return page


def _when(value):
    """An event's start or end as Google gives it - a date for all day, or a time."""
    return {"date": value} if len(value) == 10 else {"dateTime": value, "timeZone": "Europe/London"}


def _google_event(summary, start, end=None, **more):
    """One event, as Google's events.list gives it."""
    event = {"kind": "calendar#event", "id": f"made-up-{abs(hash((summary, start)))}", "status": "confirmed",
             "summary": summary, "start": _when(start)}
    if end:
        event["end"] = _when(end)
    event.update(more)
    return event


def _events_page(*events, next_page=None):
    page = {"kind": "calendar#events", "summary": "A calendar", "timeZone": "Europe/London", "items": list(events)}
    if next_page:
        page["nextPageToken"] = next_page
    return page


def _choices(tmp_path, activities=None, whats_on=None):
    """
    The calendars' choices, in calendars.json - which are activities, and which are out of
    what's on, by calendar id.
    """
    choices = ChoiceStore(str(tmp_path / "calendars.json"), google_calendar.CALENDAR_CHOICES)
    if activities:
        choices.change(lambda state: state.update(activities=activities))
    if whats_on:
        choices.change(lambda state: state.update(whats_on=whats_on))
    return choices


@pytest.fixture
def make_calendar(monkeypatch, tmp_path):
    """
    Makes GoogleCalendars reading from a FakeGoogle: one calendar, Personal - the user's
    own - with the events given, unless calendars gives each calendar's id and its events -
    each read reads times over. Each is named from its id: "calendar-uni-timetable" is
    "Uni timetable".
    """
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)

    def make(events=(), calendars=None, activities=None, reads=1, whats_on=None):
        calendars = calendars if calendars is not None else {"calendar-personal": list(events)}
        google = FakeGoogle(**{LIST: [_calendar_list(*[_entry(calendar_id, _named(calendar_id),
                                                              primary=calendar_id == "calendar-personal")
                                                       for calendar_id in calendars])],
                               **{_events_url(calendar_id): [_events_page(*its_events)] * reads
                                  for calendar_id, its_events in calendars.items()}})
        return GoogleCalendar(FakeSignIn(), _choices(tmp_path, activities, whats_on), session=google)

    return make


def _named(calendar_id):
    return calendar_id.split("-", 1)[1].replace("-", " ").capitalize()


def _read(calendar):
    """The events requests made, each as (calendar id, params)."""
    prefix = "https://www.googleapis.com/calendar/v3/calendars/"
    return [(url[len(prefix):-len("/events")], params) for url, params in calendar._session.asked
            if url.startswith(prefix)]


def test_every_calendar_in_googles_list_is_read(monkeypatch, tmp_path):
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)
    google = FakeGoogle(**{
        LIST: [_calendar_list(_entry("calendar-personal", "Personal", primary=True),
                              _entry("calendar-uni", "My timetable", access_role="reader", override="Uni"),
                              next_page="page-2"),
               _calendar_list(_entry("calendar-work", "Work", access_role="writer"))],
        _events_url("calendar-personal"): [_events_page(
            _google_event("Dentist", "2026-10-22T09:30:00+01:00", "2026-10-22T10:00:00+01:00"))],
        _events_url("calendar-uni"): [_events_page(
            _google_event("CSC2035 Lecture", "2026-10-22T11:30:00+01:00", "2026-10-22T12:30:00+01:00"))],
        _events_url("calendar-work"): [_events_page(
            _google_event("Shift", "2026-10-22T07:00:00+01:00", "2026-10-22T09:00:00+01:00"))],
    })
    calendar = GoogleCalendar(FakeSignIn(), _choices(tmp_path), session=google)

    events = calendar.events(date(2026, 10, 22), 1)

    assert [(event["summary"], event["calendar"]) for event in events] == [
        ("Shift", "Work"), ("Dentist", "Personal"), ("CSC2035 Lecture", "Uni")]
    # Both pages of the list, the second asked for by Google's token for it.
    assert [params.get("pageToken") for url, params in google.asked if url == LIST] == [None, "page-2"]
    assert calendar.known() == [
        {"id": "calendar-personal", "name": "Personal", "primary": True, "writable": True},
        {"id": "calendar-uni", "name": "Uni", "primary": False, "writable": False},
        {"id": "calendar-work", "name": "Work", "primary": False, "writable": False}]
    for calendar_id, params in _read(calendar):
        assert params["singleEvents"] == "true" and params["orderBy"] == "startTime"
        assert params["maxResults"] == 250
        assert params["timeMin"] == "2026-10-22T00:00:00+01:00" and params["timeMax"] == "2026-10-23T00:00:00+01:00"
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal", "calendar-uni",
                                                                    "calendar-work"]
    # Each with Nova's access token, and a time limit.
    assert all(fetched["headers"] == {"Authorization": f"Bearer {ACCESS_TOKEN}"} and fetched["timeout"]
               for fetched in google.fetched)

    # The list is kept for a while, and only the calendars given are read when some are.
    google.answers[_events_url("calendar-work")].append(_events_page())
    calendar.events(date(2026, 10, 22), 1, calendar_ids=["calendar-work"])
    assert [url for url, _ in google.asked].count(LIST) == 2
    assert [calendar_id for calendar_id, _ in _read(calendar)][-1] == "calendar-work"
    assert len(_read(calendar)) == 4


def test_every_page_of_events_is_read(make_calendar):
    calendar = make_calendar()
    calendar._session.answers[_events_url("calendar-personal")] = [
        _events_page(_google_event("Dentist", "2026-10-22T09:30:00+01:00"), next_page="events-2"),
        _events_page(_google_event("Dinner with Sam", "2026-10-22T19:00:00+01:00"), next_page="events-3"),
        _events_page(_google_event("Bin day", "2026-10-22", "2026-10-23"))]

    events = calendar.events(date(2026, 10, 22), 1)

    assert [event["summary"] for event in events] == ["Bin day", "Dentist", "Dinner with Sam"]
    pages = [params for _, params in _read(calendar)]
    assert [params.get("pageToken") for params in pages] == [None, "events-2", "events-3"]
    # Every page is for the same days.
    assert len({(params["timeMin"], params["timeMax"]) for params in pages}) == 1


def test_twenty_pages_of_events_are_read_and_no_more(make_calendar):
    # A token which always leads to another page - Google looping - would otherwise never stop.
    calendar = make_calendar()
    endless = _events_page(_google_event("Dentist", "2026-10-22T09:30:00+01:00"), next_page="again")
    calendar._session.answers[_events_url("calendar-personal")] = [endless] * 30

    with pytest.raises(ValueError, match="Google gave more than 20 pages"):
        calendar.events(date(2026, 10, 22), 1)

    assert len(_read(calendar)) == 20
    # Said as Google out of reach, as any answer of the wrong shape is.
    calendar._session.answers[_events_url("calendar-personal")] = [endless] * 30
    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 21)) == {
        "success": False, "message": CANT_REACH_GOOGLE}

    # The twentieth page being the last is no trouble.
    last = _events_page(_google_event("Bin day", "2026-10-22", "2026-10-23"))
    calendar._session.answers[_events_url("calendar-personal")] = [endless] * 19 + [last]
    assert len(calendar.events(date(2026, 10, 22), 1)) == 20
    assert len(_read(calendar)) == 60


def test_a_repeating_event_is_on_each_of_its_days(make_calendar):
    # singleEvents=true has Google give each time it's on, as Home Assistant did.
    lectures = [_google_event("CSC2035 Lecture", f"2026-10-{day}T11:00:00+01:00", f"2026-10-{day}T12:00:00+01:00",
                              recurringEventId="made-up-weekly-lecture",
                              originalStartTime={"dateTime": f"2026-10-{day}T11:00:00+01:00",
                                                 "timeZone": "Europe/London"})
                for day in (20, 22)]
    calendar = make_calendar(lectures)

    assert calendar.find("lecture", date(2026, 10, 20), today=date(2026, 10, 20))["message"] == (
        "You've got CSC2035 Lecture today, at 11:00 AM; and CSC2035 Lecture on Thursday, at 11:00 AM.")


def test_an_all_day_event_is_said_on_its_day(make_calendar):
    calendar = make_calendar([_google_event("Sam's birthday", "2026-10-30", "2026-10-31")], reads=2)

    assert calendar.agenda(date(2026, 10, 30), 1, today=date(2026, 10, 20))["message"] == \
        "On Friday the 30th of October, you've got Sam's birthday, all day."
    # Google's end is the day after - the 30th is the only day it's on.
    [event] = calendar.events(date(2026, 10, 30), 1)
    assert (event["start"], event["end"], event["all_day"]) == (date(2026, 10, 30), None, True)


def test_a_cancelled_event_is_left_out(make_calendar):
    # A time taken off a repeating event comes as its own entry, cancelled.
    calendar = make_calendar([
        _google_event("CSC2035 Lecture", "2026-10-22T11:00:00+01:00", "2026-10-22T12:00:00+01:00"),
        {"kind": "calendar#event", "id": "made-up-cancelled", "status": "cancelled",
         "recurringEventId": "made-up-weekly-lecture",
         "originalStartTime": {"dateTime": "2026-10-22T15:00:00+01:00"}},
        _google_event("Football", "2026-10-22T18:00:00+01:00", "2026-10-22T19:00:00+01:00", status="cancelled")])

    assert [event["summary"] for event in calendar.events(date(2026, 10, 22), 1)] == ["CSC2035 Lecture"]


def test_an_event_after_the_clocks_go_back_is_said_at_its_own_time(make_calendar):
    # The clocks went back on Sunday the 25th of October 2026. Google gives a winter time
    # in London as UTC, with a Z.
    calendar = make_calendar([_google_event("Lecture", "2026-10-24T09:00:00+01:00", "2026-10-24T10:00:00+01:00"),
                              _google_event("Lecture", "2026-10-26T09:00:00Z", "2026-10-26T10:00:00Z")])

    assert calendar.find("lecture", date(2026, 10, 24), today=date(2026, 10, 24))["message"] == (
        "You've got Lecture today, at 9:00 AM; and Lecture on Monday, at 9:00 AM.")

    # Three days over the change are each a whole day, from local midnight to local midnight.
    calendar = make_calendar()
    calendar.events(date(2026, 10, 24), 3)
    [(_, params)] = _read(calendar)
    assert params["timeMin"] == "2026-10-24T00:00:00+01:00" and params["timeMax"] == "2026-10-27T00:00:00+00:00"


@pytest.mark.parametrize("down", ["no network", "not answering", "a server error", "a wrong answer"])
def test_without_the_internet_it_says_it_cant_reach_google(make_calendar, monkeypatch, caplog, down):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    if down == "no network":
        monkeypatch.setattr(google_calendar.network, "online", lambda: False)

    for asked in (lambda: calendar.agenda(date(2026, 10, 22), 1), lambda: calendar.find("MOT", date(2026, 10, 22)),
                  lambda: calendar.activity("work", date(2026, 10, 22))):
        for calendar_id in ("calendar-personal", "calendar-work"):
            url = _events_url(calendar_id)
            # Each error names the address, with the calendar's id in it, as requests' own do.
            calendar._session.answers[url] = [{
                "no network": _events_page(),
                "not answering": requests.ConnectionError(f"Max retries exceeded with url: {url}"),
                "a server error": requests.HTTPError(f"503 Server Error: Backend Error for url: {url}",
                                                     response=FakeResponse(503, {"error": {"code": 503}})),
                "a wrong answer": {"items": "nothing"}}[down]]
        with caplog.at_level(logging.INFO):
            assert asked() == {"success": False, "message": CANT_REACH_GOOGLE}
    if down == "no network":
        # Nothing is even tried - not the sign-in either.
        assert calendar._session.asked == []
    # The calendar's id - for the primary, the user's email address - is never logged.
    assert caplog.records and not any("calendar-" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("status", [403, 404])
def test_a_calendar_google_wont_give_is_left_out_and_the_rest_read(make_calendar, caplog, status):
    # Deleted while the list was kept, or no longer shared: one calendar said "I can't
    # reach Google's calendar" for every one of them.
    calendar = make_calendar(calendars={"calendar-personal": [
        _google_event("Dentist", "2026-10-22T09:30:00+01:00", "2026-10-22T10:00:00+01:00")], "calendar-gone": []})
    # Answered as Google does, and raised by _get's own raise_for_status - past the 401 retry.
    calendar._session.answers[_events_url("calendar-gone")] = [FakeResponse(status, {"error": {
        "code": status, "errors": [{"domain": "global", "reason": "notFound" if status == 404 else "forbidden"}]}})]

    with caplog.at_level(logging.INFO):
        assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20)) == {
            "success": True, "message": "On Thursday, you've got Dentist at 9:30 AM."}
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert not any("calendar-" in record.getMessage() for record in caplog.records)
    # Google's list is asked for afresh next time, so a deleted calendar is gone from it.
    calendar._session.answers[LIST].append(_calendar_list(_entry("calendar-personal", "Personal", primary=True)))
    calendar._session.answers[_events_url("calendar-personal")].append(_events_page())
    calendar.agenda(date(2026, 10, 22), 1)
    assert [url for url, _ in calendar._session.asked].count(LIST) == 2


@pytest.mark.parametrize("answer", [
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}),
    FakeResponse(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits",
                                                          "reason": "userRateLimitExceeded"}]}}),
    FakeResponse(429, {"error": {"code": 429, "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}),
])
def test_google_slowing_nova_down_is_google_out_of_reach_not_a_calendar_gone(make_calendar, answer):
    # Left out as if gone, the calendar refused for a moment would have been "nothing on".
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar._session.answers[_events_url("calendar-work")] = [answer]
    assert calendar.agenda(date(2026, 10, 22), 1) == {"success": False, "message": CANT_REACH_GOOGLE}
    assert calendar._known_at is not None


@pytest.mark.parametrize("status", [403, 404])
def test_an_activity_whose_calendar_google_wont_give_says_it_cant_reach_google(make_calendar, status):
    # Never "You haven't got gym tomorrow" when the Gym calendar wasn't read at all.
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-gym": []})
    calendar._session.answers[_events_url("calendar-gym")] = [FakeResponse(status, {"error": {"code": status}})]
    assert calendar.activity("gym", date(2026, 10, 21)) == {"success": False, "message": CANT_REACH_GOOGLE}


DENTIST = _google_event("Dentist", "2026-10-22T09:30:00+01:00", "2026-10-22T10:00:00+01:00")
SHIFT = _google_event("Shift", "2026-10-22T07:00:00+01:00", "2026-10-22T09:00:00+01:00")


def test_a_calendar_out_of_whats_on_isnt_read_for_it(make_calendar):
    # Chosen out on the page before a restart: nothing's known of Google's list yet, so
    # it's left out of the calendars read once that's been read, not before.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT]},
                             whats_on={"calendar-work": False}, reads=2)
    assert calendar.known() == [] and calendar.left_out() == {"calendar-work"}

    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20)) == {
        "success": True, "message": "On Thursday, you've got Dentist at 9:30 AM."}
    assert calendar.find("shift", date(2026, 10, 22), today=date(2026, 10, 20))["message"] == \
        "I can't see shift on your calendar in the next three months."
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal"] * 2


def test_a_calendar_gone_doesnt_stop_whats_on_with_one_left_out(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT], "calendar-gone": []},
                             whats_on={"calendar-work": False})
    calendar._session.answers[_events_url("calendar-gone")] = [FakeResponse(404, {"error": {"code": 404}})]

    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20)) == {
        "success": True, "message": "On Thursday, you've got Dentist at 9:30 AM."}
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal", "calendar-gone"]
    # One chosen out that's gone from Google's list is no trouble either.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST]}, whats_on={"calendar-deleted": False})
    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20))["success"]


def test_an_activity_out_of_whats_on_is_still_read_for_itself(make_calendar):
    # Out of what's on, so a day isn't full of shifts - but "am I working on Thursday?" still knows.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT]},
                             whats_on={"calendar-work": False})
    assert calendar.activity("work", date(2026, 10, 22), today=date(2026, 10, 20)) == {
        "success": True, "message": "On Thursday, you've got work from 7:00 AM until 9:00 AM."}
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-work"]


def test_an_activity_chosen_on_the_page_is_heard_at_the_next_request(make_calendar, tmp_path):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar.calendars()
    intents = LocalIntents(activities=calendar.activity_words)
    assert "personal" not in calendar.activity_words()

    calendar.choose("calendar-personal", activity=True)
    calendar.choose("calendar-work", activity=False)
    assert "personal" in calendar.activity_words() and "work" not in calendar.activity_words()
    [action] = intents.match("When's personal tomorrow?")["actions"]
    assert action.params["activity"] == "personal"
    # Kept for after a restart, and what's on is left as it was.
    assert ChoiceStore(str(tmp_path / "calendars.json"), google_calendar.CALENDAR_CHOICES).state == {
        "activities": {"calendar-personal": True, "calendar-work": False}, "whats_on": {}, "new_events": None}

    calendar.choose("calendar-work", whats_on=False)
    calendar.choose("calendar-personal", whats_on=False)
    calendar.choose("calendar-personal", whats_on=True)
    assert calendar.left_out() == {"calendar-work"}
    # Only a calendar Google's showing.
    with pytest.raises(ValueError, match="^Google isn't showing that calendar just now.$"):
        calendar.choose("calendar-made-up", activity=True)
    assert "calendar-made-up" not in calendar.choices.get("activities")


def test_a_calendar_out_of_whats_on_is_out_of_a_briefing_too(make_calendar):
    # A routine's briefing, and Claude, ask through get_calendar - which is the agenda.
    calendar = make_calendar(calendars={"calendar-personal": [DENTIST], "calendar-work": [SHIFT]},
                             whats_on={"calendar-work": False})
    result = ActionExecutor(calendar=calendar).execute(Action("get_calendar", {"start": "2026-10-22", "days": 1}))
    assert "Dentist" in result.say and "work" not in result.say.lower()
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-personal"]


def test_an_activity_switch_is_never_on_for_a_calendar_that_cant_be_one(make_calendar):
    # One with no name, or a name another calendar listed first has, is never heard or
    # told to Claude - so the page's switch for it isn't "on", whatever's chosen.
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []},
                             activities={"calendar-blank": True, "calendar-work-too": True})
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", "Personal", primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-work-too", "Work"), _entry("calendar-blank", ""), _entry("calendar-gym", "Gym"))]
    listed = {each["id"]: each for each in calendar.calendars()}
    assert {calendar_id: calendar.is_activity(each) for calendar_id, each in listed.items()} == {
        "calendar-personal": False, "calendar-work": True, "calendar-work-too": None, "calendar-blank": None,
        "calendar-gym": True}
    # With the first switched off, the second of the name is the one heard.
    calendar.choose("calendar-work", activity=False)
    assert calendar.is_activity(listed["calendar-work"]) is False
    assert calendar.is_activity(listed["calendar-work-too"]) is True


def test_an_event_without_a_name_in_words_is_still_said(make_calendar):
    calendar = make_calendar([_google_event(42, "2026-10-22T09:30:00+01:00"),
                              _google_event(None, "2026-10-22T11:00:00+01:00")])
    assert calendar.agenda(date(2026, 10, 22), 1, today=date(2026, 10, 20))["message"] == \
        "On Thursday, you've got something at 9:30 AM, and something at 11:00 AM."


def test_a_sign_in_that_needs_doing_again_says_so(make_calendar, token_file):
    calendar = make_calendar()
    # Not signed in at all, so nothing is asked of Google.
    calendar.sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=FakeGoogle())

    assert calendar.agenda(date(2026, 10, 22), 1) == {"success": False, "message": NEEDS_SIGN_IN}
    assert calendar._session.asked == []


def test_an_access_token_google_turns_down_is_got_afresh_once(make_calendar, token_file):
    # A sign-in removed in the Google account turns its access token down straight away.
    # Kept until it ran out, that would have been "I can't reach Google" for an hour.
    _signed_in(token_file)
    calendar = make_calendar(reads=2)
    url = _events_url("calendar-personal")
    google = calendar._session
    google.answers[google_calendar.TOKEN] = [_token_answer("first-access", refresh=None),
                                             _token_answer("second-access", refresh=None)]
    google.answers[url] = [FakeResponse(401, {"error": {"code": 401}}), _events_page()]
    calendar.sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    assert calendar.agenda(date(2026, 10, 22), 1)["success"] is True
    assert [fetched["headers"]["Authorization"] for fetched in google.fetched if fetched["params"].get("timeMin")] \
        == ["Bearer first-access", "Bearer second-access"]

    # Turned down afresh, it's the sign-in that needs doing again.
    google.answers[google_calendar.TOKEN] = [_token_answer("third-access", refresh=None)]
    google.answers[url] = [FakeResponse(401, {"error": {"code": 401}}), FakeResponse(401, {"error": {"code": 401}})]
    assert calendar.agenda(date(2026, 10, 22), 1) == {"success": False, "message": NEEDS_SIGN_IN}


def test_the_calendar_is_off_without_googles_keys(monkeypatch, make_config, caplog, tmp_path):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    with caplog.at_level("INFO"):
        assert GoogleCalendar.from_env(make_config()) is None
    assert len([record for record in caplog.records if "GOOGLE_CLIENT_ID" in record.getMessage()]) == 1

    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(google_calendar.paths, "GOOGLE_TOKEN_FILE", "/made/up/google_token.json")
    monkeypatch.setattr(google_calendar.paths, "DATA_DIR", str(tmp_path))
    calendar = GoogleCalendar.from_env(make_config())
    assert calendar.sign_in.client_id == CLIENT_ID and calendar.sign_in.path == "/made/up/google_token.json"
    # Every calendar in what's on, and new events to the primary, to begin with.
    assert calendar.choices.path == str(tmp_path / "calendars.json")
    assert calendar.choices.state == {"activities": {}, "whats_on": {}, "new_events": None}


# The calendar's words, moved from Home Assistant's

def test_the_calendar_is_read_for_the_days_asked_about(make_calendar):
    thursday = date(2026, 10, 22)
    calendar = make_calendar([
        _google_event("Dinner with Sam", "2026-10-22T19:00:00+01:00"),
        _google_event("Dentist", "2026-10-22T09:30:00+01:00"),
        _google_event("Bin day", "2026-10-22", "2026-10-23"),
    ])
    events = calendar.events(thursday, 1)

    assert [event["summary"] for event in events] == ["Bin day", "Dentist", "Dinner with Sam"]
    [(_, sent)] = _read(calendar)
    start = datetime.fromisoformat(sent["timeMin"])
    assert start.date() == thursday and datetime.fromisoformat(sent["timeMax"]) - start == timedelta(days=1)


def _event(summary, when, all_day=False):
    return {"summary": summary, "start": when, "all_day": all_day}


TODAY = date(2026, 10, 20)


def test_a_day_is_said_event_by_event():
    events = [_event("Bin day", date(2026, 10, 22), all_day=True),
              _event("Dentist", datetime(2026, 10, 22, 9, 30))]
    assert describe_agenda(events, date(2026, 10, 22), 1, TODAY) == \
        "On Thursday, you've got Bin day, all day, and Dentist at 9:30 AM."


def test_a_free_day_says_so():
    assert describe_agenda([], TODAY + timedelta(days=1), 1, TODAY) == "You've got nothing on tomorrow."
    assert describe_agenda([], date(2026, 10, 24), 2, TODAY) == "You've got nothing on Saturday or Sunday."


def test_several_days_only_say_the_ones_with_something_on():
    events = [_event("Football", datetime(2026, 10, 25, 10, 0))]
    assert describe_agenda(events, date(2026, 10, 24), 2, TODAY) == "On Sunday, you've got Football at 10:00 AM."


def test_a_busy_day_is_cut_short():
    events = [_event(f"Meeting {n}", datetime(2026, 10, 20, 9 + n, 0)) for n in range(7)]
    assert describe_agenda(events, TODAY, 1, TODAY).endswith("Meeting 4 at 1:00 PM, and 2 more.")


def test_a_day_further_off_is_said_in_full():
    assert describe_agenda([], date(2026, 10, 30), 1, TODAY) == "You've got nothing on Friday the 30th of October."


def _activity(name, day):
    """An activity's day, as _as_activities gives it."""
    start = datetime.combine(day, datetime.min.time()).replace(hour=9)
    return {"summary": name, "start": start, "all_day": False, "spans": [[start, start + timedelta(hours=4)]],
            "activity": True}


def test_a_week_is_an_overview_without_the_times():
    # Every time of every day, "uni from 11:30 AM until 1 PM and from 2:30 PM until
    # 4:30 PM", took 52 seconds for a week.
    events = sorted([_activity("uni", TODAY), _activity("uni", date(2026, 10, 22)), _activity("uni", date(2026, 10, 26)),
                     _activity("work", date(2026, 10, 21)), _activity("work", date(2026, 10, 24)),
                     _event("Payday", datetime(2026, 10, 21, 16)), _event("Haircut", datetime(2026, 10, 23, 12, 20))],
                    key=lambda event: event["start"])
    assert describe_agenda(events, TODAY, 7, TODAY) == (
        "Over the next week, you've got uni today, Thursday and Monday, and work tomorrow and Saturday. "
        "There's also Payday tomorrow, and Haircut on Friday.")


def test_a_fortnight_counts_the_days_and_names_only_a_few_things():
    # Read out whole, a fortnight once took three and a half minutes.
    events = ([_activity("work", TODAY + timedelta(days=n)) for n in range(14)]
              + [_activity("uni", TODAY + timedelta(days=n)) for n in (0, 2, 7)]
              + [_event(f"Meeting {n}", datetime(2026, 10, 21, 10) + timedelta(days=n)) for n in range(7)])
    events.sort(key=lambda event: event["start"])
    said = describe_agenda(events, TODAY, 14, TODAY)
    assert said.startswith("Over the next two weeks, you've got work every day, and uni on 3 days. There's also "
                           "Meeting 0 tomorrow, Meeting 1 on Thursday,")
    assert said.count("Meeting") == 5 and said.endswith(", and 2 more.")


def test_an_overview_of_another_week_says_which_one():
    events = [_activity("uni", date(2026, 10, 27)), _event("Dentist", datetime(2026, 10, 29, 9, 30))]
    assert describe_agenda(events, date(2026, 10, 26), 7, TODAY) == (
        "From Monday to Sunday the 1st of November, you've got uni on Tuesday. "
        "There's also Dentist on Thursday the 29th of October.")
    assert describe_agenda([], date(2026, 10, 26), 7, TODAY) == \
        "You've got nothing on from Monday to Sunday the 1st of November."


UNI_DAY = [
    _google_event("CSC2035/P01/01 <04-07>", "2026-10-20T11:30:00+01:00", "2026-10-20T12:30:00+01:00"),
    _google_event("CSC2032/T01/01", "2026-10-20T14:30:00+01:00", "2026-10-20T15:30:00+01:00"),
    _google_event("CSC2035/L01/01 <04-07>", "2026-10-20T15:30:00+01:00", "2026-10-20T17:30:00+01:00"),
    _google_event("Discussion group", "2026-10-20T18:00:00+01:00", "2026-10-20T19:00:00+01:00"),
]


def test_whats_on_says_a_day_of_uni_as_uni(make_calendar):
    # Each session was read out by its own name - "CSC2035/P01/01 <04-07> at 11:30
    # AM" - which took 25 seconds for one day, and meant little. The Uni calendar's
    # sessions are uni, whatever each is called; the user's own calendar's are as they are.
    calendar = make_calendar(calendars={"calendar-personal": UNI_DAY[3:], "calendar-uni": UNI_DAY[:3]})
    assert calendar.agenda(TODAY, today=TODAY)["message"] == (
        "Today, you've got uni from 11:30 AM until 12:30 PM and from 2:30 PM until 5:30 PM, "
        "and Discussion group at 6:00 PM.")


def test_one_thing_is_looked_for_months_ahead(make_calendar):
    calendar = make_calendar(UNI_DAY + [
        _google_event("Car MOT", "2026-11-30T09:30:00+00:00", "2026-11-30T10:30:00+00:00")])
    assert calendar.find("mot", TODAY, today=TODAY) == {
        "success": True, "message": "You've got Car MOT on Monday the 30th of November, at 9:30 AM."}
    [(_, sent)] = _read(calendar)
    assert (datetime.fromisoformat(sent["timeMax"]).date() - datetime.fromisoformat(sent["timeMin"]).date()).days == 90


def test_looking_for_something_which_isnt_there_says_so(make_calendar):
    calendar = make_calendar(UNI_DAY)
    assert calendar.find("MOT", TODAY, today=TODAY)["message"] == \
        "I can't see MOT on your calendar in the next three months."


def test_several_found_are_said_together_up_to_a_few():
    events = [_event("Football", datetime(2026, 10, 21, 10, 0)), _event("Sam's birthday", date(2026, 10, 22), True)]
    assert describe_found("football", events[:1] * 5, TODAY) == (
        "You've got Football tomorrow, at 10:00 AM; Football tomorrow, at 10:00 AM; and Football tomorrow, "
        "at 10:00 AM. And 2 more after that.")
    assert describe_found("sam", events[1:], TODAY) == "You've got Sam's birthday on Thursday, all day."


# The activities - each calendar but the user's own, by its name

def test_every_event_on_the_work_calendar_is_work(make_calendar):
    calendar = make_calendar(calendars={
        "calendar-personal": [_google_event("Work drinks", "2026-10-21T19:00:00+01:00", "2026-10-21T21:00:00+01:00")],
        "calendar-work": [_google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00"),
                          _google_event("Cover", "2026-10-21T08:00:00+01:00", "2026-10-21T12:00:00+01:00")]})

    assert calendar.activity("work", date(2026, 10, 21), today=date(2026, 10, 20)) == {
        "success": True,
        "message": "Tomorrow, you've got work from 8:00 AM until noon, and from 3:30 PM until 10:00 PM."}
    # Only the Work calendar is read - "Work drinks", on the user's own, isn't work.
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-work"]


def test_a_calendar_added_later_is_an_activity_by_itself(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": [], "calendar-gym": [
        _google_event("Legs", "2026-10-21T18:00:00+01:00", "2026-10-21T19:00:00+01:00")]})
    google = calendar._session
    google.answers[LIST] = [_calendar_list(_entry("calendar-personal", "Personal", primary=True),
                                           _entry("calendar-work", "Work")),
                            _calendar_list(_entry("calendar-personal", "Personal", primary=True),
                                           _entry("calendar-work", "Work"), _entry("calendar-gym", "Gym"))]
    intents = LocalIntents(activities=calendar.activity_words)
    calendar.calendars()
    # Until Google lists it, "gym" is Claude's.
    assert intents.match("When's gym tomorrow?") is None

    calendar.calendars(max_age=0)
    tomorrow = date.today() + timedelta(days=1)
    [action] = intents.match("When's gym tomorrow?")["actions"]
    assert (action.tool, action.params) == ("get_activity", {"activity": "gym", "start": tomorrow.isoformat(),
                                                             "days": 1})
    assert calendar.activity("gym", date(2026, 10, 21), today=date(2026, 10, 20))["message"] == \
        "Tomorrow, you've got gym from 6:00 PM until 7:00 PM."
    assert [calendar_id for calendar_id, _ in _read(calendar)] == ["calendar-gym"]


def test_the_primary_calendar_isnt_an_activity(make_calendar, tmp_path):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar.calendars()
    assert calendar.activity_calendars() == {
        "work": {"id": "calendar-work", "name": "Work", "primary": False, "writable": True}}

    # Until it's switched on in calendars.json - and Work, switched off there, isn't one.
    calendar.choices = _choices(tmp_path, {"calendar-personal": True, "calendar-work": False})
    assert list(calendar.activity_calendars()) == ["personal"]
    assert calendar.activity("work", date(2026, 10, 21)) == {
        "success": False, "message": "I haven't got a calendar called work."}


def test_an_activity_nova_hasnt_got_says_so(make_calendar):
    # It used to say to add it to home_assistant.activities - now there's nothing to edit.
    calendar = make_calendar()
    assert calendar.activity("gym", date(2026, 10, 21)) == {
        "success": False, "message": "I haven't got a calendar called gym."}
    assert calendar.activity("Gym ", date(2026, 10, 21))["message"] == "I haven't got a calendar called Gym."
    assert calendar.activity("", date(2026, 10, 21)) == {
        "success": False, "message": "I didn't catch which of your calendars you meant."}
    assert _read(calendar) == []


def test_uni_sessions_back_to_back_are_said_as_one_stretch(make_calendar):
    # From the user's calendar: a practical, a tutorial, then a lecture straight after it.
    calendar = make_calendar(calendars={"calendar-personal": UNI_DAY[3:], "calendar-uni": UNI_DAY[:3]})
    assert calendar.activity("uni", date(2026, 10, 20), today=date(2026, 10, 20))["message"] == \
        "Today, you've got uni from 11:30 AM until 12:30 PM, and from 2:30 PM until 5:30 PM."


def test_a_day_off_says_so(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []}, reads=2)
    assert calendar.activity("work", date(2026, 10, 24), 2, today=date(2026, 10, 20))["message"] == \
        "You haven't got work between Saturday and Sunday."
    assert calendar.activity("work", date(2026, 10, 21), today=date(2026, 10, 20))["message"] == \
        "You haven't got work tomorrow."


def test_split_shifts_and_all_day_work_are_both_said():
    from voice_assistant.google_calendar import describe_activity
    events = [{"summary": "Shift", "start": datetime(2026, 10, 22, 9, 0), "end": datetime(2026, 10, 22, 13, 0),
               "all_day": False},
              {"summary": "Shift", "start": datetime(2026, 10, 22, 14, 0), "end": datetime(2026, 10, 22, 18, 0),
               "all_day": False},
              {"summary": "Shift", "start": date(2026, 10, 23), "end": None, "all_day": True}]
    assert describe_activity("work", events, date(2026, 10, 22), 2, TODAY) == (
        "On Thursday, you've got work from 9:00 AM until 1:00 PM, and from 2:00 PM until 6:00 PM. "
        "On Friday, you've got work all day.")


# Google's own calendars - a country's holidays, the contacts' birthdays - with the ids it gives them.
HOLIDAYS = "en.uk#holiday@group.v.calendar.google.com"
BIRTHDAYS = "addressbook#contacts@group.v.calendar.google.com"


def test_googles_own_calendars_arent_activities(make_calendar, tmp_path):
    # Every non-primary calendar was an activity, so "Christmas Day" was said as
    # "holidays in united kingdom", and everyone's birthday as "birthdays".
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", "Personal", primary=True), _entry("calendar-work", "Work"),
        _entry(HOLIDAYS, "Holidays in United Kingdom", access_role="reader"),
        _entry(BIRTHDAYS, "Birthdays", access_role="reader"))]
    calendar._session.answers[_events_url(HOLIDAYS)] = [_events_page(
        _google_event("Christmas Day", "2026-12-25", "2026-12-26"))]
    calendar._session.answers[_events_url(BIRTHDAYS)] = [_events_page(
        _google_event("Sam's birthday", "2026-12-25", "2026-12-26"))]
    calendar._session.answers[_events_url("calendar-personal")] = [_events_page()]
    calendar._session.answers[_events_url("calendar-work")] = [_events_page()]

    assert calendar.agenda(date(2026, 12, 25), 1, today=date(2026, 12, 24))["message"] == \
        "Tomorrow, you've got Christmas Day, all day, and Sam's birthday, all day."
    assert list(calendar.activity_calendars()) == ["work"]
    # Unless switched on in calendars.json.
    calendar.choices = _choices(tmp_path, {HOLIDAYS: True})
    assert list(calendar.activity_calendars()) == ["work", "holidays in united kingdom"]


# Calendars named by an email address - the user's own primary, as Google names it, and
# another person's shared with them. Both addresses are made up.
OWN_ADDRESS = "me@example.com"
THEIR_ADDRESS = "someone@example.com"


def _named_by_addresses(make_calendar, tmp_path, activities=None):
    """A calendar whose primary and a shared one are named by addresses, beside Work - each with a day on."""
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []},
                             activities=activities)
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", OWN_ADDRESS, primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-theirs", THEIR_ADDRESS, access_role="reader"))]
    calendar._session.answers[_events_url("calendar-personal")] = [_events_page(
        _google_event("Dentist", "2026-10-21T08:00:00+01:00", "2026-10-21T08:30:00+01:00"))]
    calendar._session.answers[_events_url("calendar-work")] = [_events_page(
        _google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00"))]
    calendar._session.answers[_events_url("calendar-theirs")] = [_events_page(
        _google_event("Office", "2026-10-21T09:00:00+01:00", "2026-10-21T17:00:00+01:00"))]
    return calendar


@pytest.mark.parametrize("name", [
    OWN_ADDRESS, "ME@EXAMPLE.COM", "first.last+cal@sub.example.co.uk", "a_b-c@x-y.io", "user@xn--p1ai.xn--p1ai",
    "\u540d\u524d@\u4f8b\u3048.\u65e5\u672c", " someone@example.com ", "Alice's calendar (alice@example.com)",
])
def test_a_name_with_an_email_address_in_it_is_named_by_an_address(name):
    # The privacy rule: a name holding a real address is never said, or told to Claude.
    assert named_by_address({"name": name})
    assert spoken_name({"name": name, "primary": True}) == "your calendar"
    assert spoken_name({"name": name, "primary": False}) == "a shared calendar"


@pytest.mark.parametrize("name", ["Gym @ 6", "Gym @6", "Gym@6", "Gym@6.30", "Meet @ 6.30pm", "Dinner @ Mario's.",
                                  "@", "Work @ Tesco", "", "Gym @ 6, me@"])
def test_a_name_with_an_at_sign_but_no_address_is_said_as_it_is(name):
    assert not named_by_address({"name": name})
    assert spoken_name({"name": name, "primary": False}) == name


def test_a_calendar_called_gym_at_six_is_an_activity_said_by_its_name(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-gym": []},
                             activities={"calendar-gym": True}, reads=2)
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", OWN_ADDRESS, primary=True), _entry("calendar-gym", "Gym @ 6"))]
    calendar._session.answers[_events_url("calendar-gym")] = [_events_page(
        _google_event("Class", "2026-10-21T18:00:00+01:00", "2026-10-21T19:00:00+01:00"))] * 2
    assert [event["calendar"] for event in calendar.events(date(2026, 10, 21), 1)] == ["Gym @ 6"]
    assert list(calendar.activity_calendars()) == ["gym @ 6"]
    assert "gym 6" in calendar.activity_words()
    assert calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"] == (
        "Tomorrow, you've got gym @ 6 from 6:00 PM until 7:00 PM.")


def test_a_calendar_named_by_an_address_isnt_an_activity(make_calendar, tmp_path, caplog):
    # Another person's calendar shared with the user is named by their email address. As
    # an activity it went to Claude in get_activity's description, and was said aloud.
    calendar = _named_by_addresses(make_calendar, tmp_path)
    with caplog.at_level(logging.INFO):
        message = calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"]
    assert message == ("Tomorrow, you've got Dentist at 8:00 AM, Office at 9:00 AM, and work from 3:30 PM "
                       "until 10:00 PM.")
    assert list(calendar.activity_calendars()) == ["work"]
    assert not any("example" in word for word in calendar.activity_words())
    assert not any("example" in record.getMessage() for record in caplog.records)


def test_a_calendar_named_by_an_address_switched_on_is_said_without_it(make_calendar, tmp_path, caplog):
    calendar = _named_by_addresses(make_calendar, tmp_path,
                                   activities={"calendar-personal": True, "calendar-theirs": True})
    with caplog.at_level(logging.INFO):
        message = calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"]
    assert message == ("Tomorrow, you've got your calendar from 8:00 AM until 8:30 AM, a shared calendar from "
                       "9:00 AM until 5:00 PM, and work from 3:30 PM until 10:00 PM.")
    assert list(calendar.activity_calendars()) == ["your calendar", "work", "a shared calendar"]
    # Nor can it be asked about by name - "your" and "a" would be heard as activities.
    assert not any("example" in word or word.split()[0] in ("your", "a") for word in calendar.activity_words())
    assert not any("example" in record.getMessage() for record in caplog.records)


def test_an_events_calendar_is_never_given_by_an_address(make_calendar, tmp_path):
    calendar = _named_by_addresses(make_calendar, tmp_path)
    found = calendar.events(date(2026, 10, 21), 1)
    assert {event["calendar_id"]: event["calendar"] for event in found} == {
        "calendar-personal": "your calendar", "calendar-work": "Work", "calendar-theirs": "a shared calendar"}


def test_an_all_day_event_on_an_activity_calendar_is_said_by_its_own_name(make_calendar):
    # An activity is a stretch of time - a training day on the Work calendar isn't "work, all day".
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": [
        _google_event("Training day", "2026-10-21", "2026-10-22"),
        _google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00")]})
    assert calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"] == \
        "Tomorrow, you've got Training day, all day, and work from 3:30 PM until 10:00 PM."


def test_the_agenda_takes_an_activity_by_its_calendar_not_its_name(make_calendar, tmp_path):
    # The user's own calendar, called Work too, isn't the Work activity.
    calendar = make_calendar(calendars={"calendar-personal": [], "calendar-work": []})
    calendar._session.answers[LIST] = [_calendar_list(_entry("calendar-personal", "Work", primary=True),
                                                      _entry("calendar-work", "Work"))]
    calendar._session.answers[_events_url("calendar-personal")] = [_events_page(
        _google_event("Dentist", "2026-10-21T09:00:00+01:00", "2026-10-21T10:00:00+01:00"))]
    calendar._session.answers[_events_url("calendar-work")] = [_events_page(
        _google_event("Late", "2026-10-21T15:30:00+01:00", "2026-10-21T22:00:00+01:00"))]
    assert calendar.agenda(date(2026, 10, 21), 1, today=date(2026, 10, 20))["message"] == \
        "Tomorrow, you've got Dentist at 9:00 AM, and work from 3:30 PM until 10:00 PM."


def test_shifts_and_working_are_work_even_beside_a_calendar_starting_with_them(make_calendar):
    calendar, _ = _heard(make_calendar, "Work", "Working group", "Shift swaps")
    words = calendar.activity_words()
    assert words["working"] == "work" and words["shift"] == "work" and words["shifts"] == "work"
    assert words["working group"] == "working group" and words["shift swaps"] == "shift swaps"


def test_two_names_that_tidy_alike_are_the_first_listed(make_calendar):
    calendar = make_calendar(calendars={"calendar-personal": []})
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", "Personal", primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-work-again", "Work!"))]
    calendar.calendars()
    assert calendar.activity_words()["work"] == "work"


def _heard(make_calendar, *names):
    """LocalIntents hearing the activities of calendars with these names, beside the user's own."""
    calendar = make_calendar(calendars={"calendar-personal": [], **{
        f"calendar-{name.lower().replace(' ', '-')}": [] for name in names}})
    calendar.calendars()
    return calendar, LocalIntents(activities=calendar.activity_words)


def test_the_same_words_still_mean_work_and_uni(make_calendar):
    calendar, intents = _heard(make_calendar, "Uni", "Work")
    today = date.today()

    def asked(text):
        [action] = intents.match(text)["actions"]
        return action.tool, action.params

    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    assert asked("Am I working on Saturday?") == ("get_activity", {"activity": "work", "start": saturday.isoformat(),
                                                                   "days": 1})
    assert asked("When's my next lecture?") == ("get_activity", {"activity": "uni", "start": today.isoformat(),
                                                                 "count": 1})
    assert asked("What are my next three shifts?") == ("get_activity", {"activity": "work",
                                                                        "start": today.isoformat(), "count": 3})
    assert calendar.activity_words()["classes"] == "uni" and calendar.activity_words()["works"] == "work"


@pytest.mark.parametrize("text, heard", [
    # A first word alone, in a bare shape, is an everyday question - plan 1's sunset and home.
    ("When is sunset?", None), ("When's sunset?", None), ("Am I at home?", None), ("Am I home?", None),
    # With a day, or the whole name, or a shape that plainly names an activity, it's the calendar.
    ("When's sunset tomorrow?", "sunset yoga"), ("Am I at home on Saturday?", "home group"),
    ("When is sunset yoga?", "sunset yoga"), ("What time do I have sunset?", "sunset yoga"),
    ("When's my next sunset?", "sunset yoga"),
])
def test_a_first_word_alone_isnt_heard_in_a_bare_question(make_calendar, text, heard):
    _, intents = _heard(make_calendar, "Sunset yoga", "Home group")
    result = intents.match(text)
    assert (result["actions"][0].params["activity"] if result else None) == heard


@pytest.mark.parametrize("name, text, heard", [
    ("C++ club", "What time do I have c club tomorrow?", "c++ club"),
    ("C++ club", "What's the time?", None),
    ("C++ club", "Have I got a cold?", None),
    ("A", "What time do I have a tomorrow?", "a"),
    ("A", "Have I got a meeting today?", None),
    ("A", "Set a timer for ten minutes.", None),
    ("A", "Am I at a loss?", None),
])
def test_a_calendar_named_oddly_is_still_heard_strictly(make_calendar, name, text, heard):
    _, intents = _heard(make_calendar, name)
    result = intents.match(text)
    activities = [action.params["activity"] for action in (result or {}).get("actions", [])
                  if action.tool == "get_activity"]
    assert activities == ([heard] if heard else [])


def test_a_calendar_of_two_words_is_heard_by_its_first(make_calendar):
    calendar, intents = _heard(make_calendar, "Uni timetable")
    assert calendar.activity_words() == {
        "uni timetable": "uni timetable", "uni timetables": "uni timetable", "uni": "uni timetable",
        "university": "uni timetable", "lecture": "uni timetable", "lectures": "uni timetable",
        "class": "uni timetable", "classes": "uni timetable"}
    [action] = intents.match("What time do I have uni tomorrow?")["actions"]
    assert action.params["activity"] == "uni timetable"

    # Not once "Uni clubs" is there too - which of them would "uni" be?
    calendar, intents = _heard(make_calendar, "Uni timetable", "Uni clubs")
    assert "uni" not in calendar.activity_words() and "lecture" not in calendar.activity_words()
    assert intents.match("What time do I have uni tomorrow?") is None
    [action] = intents.match("What time do I have uni clubs tomorrow?")["actions"]
    assert action.params["activity"] == "uni clubs"


# The next time

def _shift(day, start_hour, end_hour):
    return {"summary": "Shift", "start": datetime(2026, 10, day, start_hour, 0, tzinfo=LONDON),
            "end": datetime(2026, 10, day, end_hour, 0, tzinfo=LONDON), "all_day": False}


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LONDON)
SHIFTS = [_shift(20, 8, 11), _shift(21, 15, 23), _shift(24, 14, 22), _shift(25, 14, 21)]


def test_the_next_time_is_just_the_one():
    # Asked "when do I next have work?", every shift for a fortnight used to be read out.
    from voice_assistant.google_calendar import describe_next
    # This morning's shift is over, so it's tomorrow's.
    assert describe_next("work", SHIFTS, 1, date(2026, 10, 20), NOW) == \
        "You're next at work tomorrow, from 3:00 PM until 11:00 PM."


def test_the_next_few_times_are_said_together():
    from voice_assistant.google_calendar import describe_next
    assert describe_next("work", SHIFTS, 3, date(2026, 10, 20), NOW) == (
        "Your next 3 times at work: tomorrow, from 3:00 PM until 11:00 PM; on Saturday, from 2:00 PM until "
        "10:00 PM; and on Sunday, from 2:00 PM until 9:00 PM.")


def test_being_there_now_says_so():
    from voice_assistant.google_calendar import describe_next
    assert describe_next("work", SHIFTS, 1, date(2026, 10, 20), datetime(2026, 10, 20, 9, 30, tzinfo=LONDON)) == \
        "You're at work now, until 11:00 AM."


def test_nothing_coming_up_says_so():
    from voice_assistant.google_calendar import describe_next
    assert describe_next("uni", [], 1, date(2026, 10, 20), NOW) == "You haven't got uni in the next two weeks."


# Adding to the calendar

SATURDAY = date(2026, 10, 24)


@pytest.fixture
def for_adding(monkeypatch, tmp_path):
    """
    Makes GoogleCalendars to add to: Personal, the user's own and primary, and Work, which
    they own too - and Uni, shared with them, which Google won't let Nova add to. Each
    calendar takes one event, unless answers gives what Google answers.
    """
    monkeypatch.setattr(google_calendar.network, "online", lambda: True)

    def make(new_events=None, answers=None, primary="Personal"):
        entries = [_entry("calendar-personal", primary, primary=True), _entry("calendar-work", "Work"),
                   _entry("calendar-uni", "Uni", access_role="reader")]
        google = FakeGoogle(**{LIST: [_calendar_list(*entries)],
                               **{_events_url(entry["id"]): list(answers or [_inserted()]) for entry in entries}})
        choices = _choices(tmp_path)
        if new_events:
            choices.change(lambda state: state.update(new_events=new_events))
        return GoogleCalendar(FakeSignIn(), choices, session=google)

    return make


def _inserted():
    """Google's answer to an event added - the event, as events.insert gives it back."""
    return {"kind": "calendar#event", "id": "made-up-event", "status": "confirmed"}


def _added(calendar):
    """Each event sent to be added, as (calendar id, what was sent)."""
    prefix = "https://www.googleapis.com/calendar/v3/calendars/"
    return [(posted["url"][len(prefix):-len("/events")], posted["json"]) for posted in calendar._session.posted]


def test_a_new_event_goes_to_the_primary_calendar_and_lasts_an_hour(for_adding):
    calendar = for_adding()

    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY) == {
        "success": True, "message": "Added Dentist, Thursday the 29th at 3 PM."}
    # The clocks went back on the 25th, so the 29th is on GMT.
    assert _added(calendar) == [("calendar-personal", {"summary": "Dentist",
                                                       "start": {"dateTime": "2026-10-29T15:00:00+00:00"},
                                                       "end": {"dateTime": "2026-10-29T16:00:00+00:00"}})]
    [posted] = calendar._session.posted
    assert posted["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert posted["timeout"] == google_calendar.REQUEST_TIMEOUT
    assert google_calendar.DEFAULT_LENGTH == timedelta(hours=1)

    # Before they went back, it's on BST, and an end given is kept.
    calendar = for_adding()
    assert calendar.add("Coffee with Sam", "2026-10-24 10:30", end="2026-10-24 11:15", today=SATURDAY)["message"] \
        == "Added Coffee with Sam, Saturday the 24th at 10:30 AM."
    assert _added(calendar)[0][1]["start"] == {"dateTime": "2026-10-24T10:30:00+01:00"}
    assert _added(calendar)[0][1]["end"] == {"dateTime": "2026-10-24T11:15:00+01:00"}


def test_with_no_time_its_all_day(for_adding):
    calendar = for_adding()
    assert calendar.add("Holiday", "2026-10-30", today=SATURDAY) == {
        "success": True, "message": "Added Holiday, Friday the 30th, all day."}
    assert _added(calendar)[0][1] == {"summary": "Holiday", "start": {"date": "2026-10-30"},
                                      "end": {"date": "2026-10-31"}}

    # all_day makes a time all day too, and an end day is included.
    calendar = for_adding()
    calendar.add("Holiday", "2026-10-30 09:00", end="2026-11-01", all_day=True, today=SATURDAY)
    assert _added(calendar)[0][1] == {"summary": "Holiday", "start": {"date": "2026-10-30"},
                                      "end": {"date": "2026-11-02"}}


def test_a_time_that_cant_be_read_or_ends_first_isnt_added(for_adding):
    calendar = for_adding()
    # Only exactly "YYYY-MM-DD HH:MM" or "YYYY-MM-DD" - "20261029" was a time, at midnight.
    for start, end in (("Thursday at 3", None), ("2026-10-29 15:00", "about four"), ("", None), ("20261029", None),
                       ("2026-10-29T15:00", None), ("2026-10-29 15:00+01:00", None), ("2026-W44-4", None),
                       ("2026-10-29 15", None), ("2026-10-29 3:00", None), (None, None)):
        assert calendar.add("Dentist", start, end=end, today=SATURDAY) == {
            "success": False, "message": "I didn't catch when that is."}
    for start, end in (("2026-10-29 15:00", "2026-10-29 14:00"), ("2026-10-30", "2026-10-29"),
                       ("2026-10-29 15:00", "2026-10-29 15:00")):
        assert calendar.add("Dentist", start, end=end, today=SATURDAY) == {
            "success": False, "message": "That ends before it starts."}
    assert calendar.add("  ", "2026-10-29 15:00", today=SATURDAY) == {
        "success": False, "message": "I didn't catch what to add."}
    assert calendar._session.posted == []


def test_a_calendar_named_is_used(for_adding):
    for named in ("work", "Work", "shifts"):
        calendar = for_adding()
        assert calendar.add("Shift", "2026-10-31 09:00", calendar=named, today=SATURDAY) == {
            "success": True, "message": "Added Shift to Work, Saturday the 31st at 9 AM."}, named
        assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-work"]

    # The one new events go to anyway isn't named.
    calendar = for_adding()
    assert calendar.add("Dentist", "2026-10-29 15:00", calendar="personal", today=SATURDAY)["message"] == \
        "Added Dentist, Thursday the 29th at 3 PM."
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"]

    # The user's own, whatever Google calls it.
    for named in ("my calendar", "primary", "personal", "my own calendar", "Nathaniel"):
        calendar = for_adding(primary="Nathaniel")
        assert calendar.add("Dentist", "2026-10-29 15:00", calendar=named, today=SATURDAY)["message"] == \
            "Added Dentist, Thursday the 29th at 3 PM.", named
        assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"], named
    # "My work calendar" is still Work.
    calendar = for_adding()
    calendar.add("Shift", "2026-10-31 09:00", calendar="my work calendar", today=SATURDAY)
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-work"]


def test_a_calendar_whose_name_is_no_words_is_never_the_one_named(for_adding):
    # "My calendar" is "" once "my" and "calendar" are taken off, as is a name of emoji
    # alone - so "add it to my calendar" went to the party calendar, and said "to 🎉".
    for named in ("my calendar", "calendar", "the calendar"):
        calendar = for_adding()
        calendar._session.answers[LIST] = [_calendar_list(
            _entry("calendar-personal", "Personal", primary=True), _entry("calendar-party", "🎉"))]
        assert calendar.add("Dentist", "2026-10-29 15:00", calendar=named, today=SATURDAY)["message"] == \
            "Added Dentist, Thursday the 29th at 3 PM.", named
        assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"], named


def test_the_chosen_calendar_is_used_until_its_gone(for_adding):
    calendar = for_adding(new_events="calendar-work")
    assert calendar.add("Shift", "2026-10-31 09:00", today=SATURDAY)["message"] == \
        "Added Shift, Saturday the 31st at 9 AM."
    # The primary, named, is now the one that isn't where new events go.
    assert calendar.add("Dentist", "2026-10-29 15:00", calendar="personal", today=SATURDAY)["message"] == \
        "Added Dentist to Personal, Thursday the 29th at 3 PM."
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-work", "calendar-personal"]

    # Deleted in Google, new events go to the primary again.
    calendar._known_at = None
    calendar._session.answers[LIST].append(_calendar_list(_entry("calendar-personal", "Personal", primary=True)))
    calendar._session.answers[_events_url("calendar-personal")].append(_inserted())
    assert calendar.add("Shift", "2026-11-07 09:00", today=SATURDAY)["message"] == \
        "Added Shift, Saturday the 7th of November at 9 AM."
    assert _added(calendar)[-1][0] == "calendar-personal"

    # And one chosen that Nova can't add to is never tried.
    calendar = for_adding(new_events="calendar-uni")
    calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"]


def test_new_events_only_go_to_a_calendar_nova_can_add_to(for_adding):
    calendar = for_adding()
    calendar.calendars()
    with pytest.raises(ValueError, match="^Nova can only add to your own calendars.$"):
        calendar.set_new_events("calendar-uni")
    with pytest.raises(ValueError, match="^Google isn't showing that calendar just now.$"):
        calendar.set_new_events("calendar-made-up")
    assert calendar.choices.get("new_events") is None


def test_new_events_go_to_the_primary_until_another_is_chosen(for_adding):
    calendar = for_adding()
    calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY)
    # Used from the next one, with no restart.
    calendar.set_new_events("calendar-work")
    assert calendar.add("Shift", "2026-10-31 09:00", today=SATURDAY)["message"] == \
        "Added Shift, Saturday the 31st at 9 AM."
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal", "calendar-work"]
    assert calendar.choices.get("new_events") == "calendar-work"


def test_a_calendar_nova_cant_add_to_says_so(for_adding):
    calendar = for_adding()
    assert calendar.add("Lecture", "2026-10-29 15:00", calendar="uni", today=SATURDAY) == {
        "success": False, "message": "I can't add to Uni - Google only lets me add to your own calendars."}
    assert calendar.add("Gym", "2026-10-29 15:00", calendar="gym", today=SATURDAY) == {
        "success": False, "message": "I haven't got a calendar called that."}
    assert calendar._session.posted == []


def test_a_calendar_named_by_an_address_is_never_said_when_adding(for_adding, caplog):
    # Google names the user's own primary by their email address.
    calendar = for_adding(new_events="calendar-work", primary=OWN_ADDRESS, answers=[_inserted()] * 3)
    calendar._session.answers[LIST] = [_calendar_list(
        _entry("calendar-personal", OWN_ADDRESS, primary=True), _entry("calendar-work", "Work"),
        _entry("calendar-theirs", THEIR_ADDRESS, access_role="reader"),
        _entry("calendar-mine-too", "other@example.com"))]
    calendar._session.answers[_events_url("calendar-mine-too")] = [_inserted()]
    with caplog.at_level(logging.INFO):
        for named in ("my calendar", "personal", OWN_ADDRESS):
            assert calendar.add("Dentist", "2026-10-29 15:00", calendar=named, today=SATURDAY)["message"] == \
                "Added Dentist to your calendar, Thursday the 29th at 3 PM.", named
        assert calendar.add("Dentist", "2026-10-29 15:00", calendar="other@example.com", today=SATURDAY)["message"] \
            == "Added Dentist to a shared calendar, Thursday the 29th at 3 PM."
        assert calendar.add("Lecture", "2026-10-29 15:00", calendar=THEIR_ADDRESS, today=SATURDAY) == {
            "success": False, "message": "I can't add to a shared calendar - Google only lets me add to your own "
                                         "calendars."}
    assert [calendar_id for calendar_id, _ in _added(calendar)] == ["calendar-personal"] * 3 + ["calendar-mine-too"]
    assert not any("example" in record.getMessage() for record in caplog.records)


def test_an_event_next_month_says_which_month(for_adding):
    calendar = for_adding(answers=[_inserted()] * 5)
    assert calendar.add("Dentist", "2026-11-02 10:30", today=date(2026, 10, 30))["message"] == \
        "Added Dentist, Monday the 2nd of November at 10:30 AM."
    assert calendar.add("Dentist", "2026-11-02", today=date(2026, 11, 1))["message"] == \
        "Added Dentist, Monday the 2nd, all day."
    # The year too, once it's not within the coming twelve months - a year misheard is heard.
    assert calendar.add("Dentist", "2027-10-23 15:00", today=SATURDAY)["message"] == \
        "Added Dentist, Saturday the 23rd of October at 3 PM."
    assert calendar.add("Dentist", "2027-10-24 15:00", today=SATURDAY)["message"] == \
        "Added Dentist, Sunday the 24th of October 2027 at 3 PM."
    assert calendar.add("Dentist", "2028-01-03", today=SATURDAY)["message"] == \
        "Added Dentist, Monday the 3rd of January 2028, all day."


def test_a_day_already_gone_isnt_added(for_adding):
    # Claude getting the week or the year wrong is the likeliest mishearing.
    calendar = for_adding()
    for start in ("2026-10-23 15:00", "2026-10-23", "2025-10-29 15:00"):
        assert calendar.add("Dentist", start, today=SATURDAY) == {
            "success": False, "message": "That day's already gone - which day did you mean?"}, start
    assert calendar._session.posted == []
    # Earlier today is added - something just done, logged.
    assert calendar.add("Run", "2026-10-24 07:00", today=SATURDAY)["message"] == "Added Run, Saturday the 24th at 7 AM."


@pytest.mark.parametrize("zone, start, end", [
    # 1:30 comes twice in London on the 25th - first on BST - and an hour on, it's 1:30 on GMT.
    ("Europe/London", "2026-10-25T01:30:00+01:00", "2026-10-25T01:30:00+00:00"),
    ("UTC", "2026-10-25T01:30:00+00:00", "2026-10-25T02:30:00+00:00"),
])
def test_an_event_as_the_clocks_go_back_lasts_an_hour(for_adding, monkeypatch, zone, start, end):
    # Set here, whatever the run's own zone, so each is checked in both runs.
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    calendar = for_adding()
    assert calendar.add("Night out", "2026-10-25 01:30", today=SATURDAY)["message"] == \
        "Added Night out, Sunday the 25th at 1:30 AM."
    assert _added(calendar)[0][1]["start"] == {"dateTime": start} and _added(calendar)[0][1]["end"] == {"dateTime": end}


def _refused(status, reason="forbidden"):
    return FakeResponse(status, {"error": {"code": status, "errors": [{"domain": "global", "reason": reason}]}})


LOST = "I lost Google partway through, so I can't tell whether Dentist was added."
TROUBLE = "Google had trouble, so I can't tell whether Dentist was added."


@pytest.mark.parametrize("answer, said, posts", [
    # Refused.
    (_refused(403), "Google wouldn't add that.", 1),
    (_refused(400, "invalid"), "Google wouldn't add that.", 1),
    # Google slowing Nova down - as when reading.
    (_refused(403, "rateLimitExceeded"), CANT_REACH_GOOGLE, 1),
    (_refused(403, "userRateLimitExceeded"), CANT_REACH_GOOGLE, 1),
    (_refused(429, "rateLimitExceeded"), CANT_REACH_GOOGLE, 1),
    # Sent, but unanswered - so it may well have been added, and isn't sent again.
    (requests.ReadTimeout("Read timed out."), "Google didn't answer in time, so I can't tell whether Dentist was "
                                              "added.", 1),
    (FakeResponse(200, "not an event"), "Google's answer didn't say, so I can't tell whether Dentist was added.", 1),
    # Google's own trouble - a front end that got no answer from behind it, or a backendError,
    # which Google says to retry - may have come after it was added.
    (_refused(500, "backendError"), TROUBLE, 1),
    (_refused(502, "backendError"), TROUBLE, 1),
    (_refused(503, "backendError"), TROUBLE, 1),
    (FakeResponse(504, "Gateway Timeout"), TROUBLE, 1),
    (requests.ConnectionError(urllib3.exceptions.ProtocolError(
        "Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))), LOST, 1),
    (requests.ConnectionError("Connection aborted."), LOST, 1),
    (requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead"), LOST, 1),
    (requests.exceptions.ContentDecodingError("Received response with content-encoding: gzip"), LOST, 1),
    # Never sent, so certainly not added.
    (requests.ConnectTimeout("Connection timed out."), CANT_REACH_GOOGLE, 1),
    (requests.ConnectionError(urllib3.exceptions.MaxRetryError(None, "/calendar/v3", reason=(
        urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection")))), CANT_REACH_GOOGLE, 1),
    # Turned down before it ran out, the access token is got afresh once - Google added nothing.
    (_refused(401, "authError"), NEEDS_SIGN_IN, 2),
])
def test_nothing_is_said_added_unless_google_took_it(for_adding, caplog, answer, said, posts):
    calendar = for_adding(answers=[answer, answer])
    with caplog.at_level(logging.INFO):
        assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY) == {"success": False, "message": said}
    assert len(calendar._session.posted) == posts
    # Neither the calendar's id - for the primary, the user's email address - nor the event is logged.
    assert caplog.records and not any("calendar-" in record.getMessage() or "Dentist" in record.getMessage()
                                      for record in caplog.records)


def test_adding_without_google_says_why(for_adding, monkeypatch, token_file):
    calendar = for_adding()
    monkeypatch.setattr(google_calendar.network, "online", lambda: False)
    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY) == {
        "success": False, "message": CANT_REACH_GOOGLE}
    assert calendar._session.asked == []

    monkeypatch.setattr(google_calendar.network, "online", lambda: True)
    calendar.sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=FakeGoogle())
    assert calendar.add("Dentist", "2026-10-29 15:00", today=SATURDAY) == {"success": False, "message": NEEDS_SIGN_IN}
    assert calendar._session.posted == []


def test_nova_never_changes_or_deletes_an_event():
    # FakeGoogle can only GET, and POST to the token address or an events list, so no test
    # in this file has Nova do anything else to Google.
    assert {name for name in vars(FakeGoogle) if not name.startswith("_")} == {"get", "post"}
    source = inspect.getsource(google_calendar)
    assert not re.search(r"\b(PATCH|PUT|DELETE)\b", source)
    assert not re.search(r"\.(patch|put|delete|request)\(", source)
    # The token, and an event added - nothing else is posted.
    assert source.count(".post(") == 2
