"""
Google Calendar Test
Checks Nova's own Google sign-in - that it asks for only what Nova uses, refuses an
address from another sign-in, keeps the sign-in readable by Nova's user only, and
keeps an access token until it nearly runs out - with a fake of Google's token
address, so nothing ever reaches Google. Every id, secret, code and token is made up.
"""

import base64
import hashlib
import json
import os
import stat
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests

from voice_assistant import google_calendar, storage
from voice_assistant.google_calendar import GoogleSignIn, SignInNeeded

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
            raise requests.HTTPError(f"{self.status_code} error")


class FakeGoogle:
    """
    Stands in for a requests.Session, answering each address from a list of answers
    and noting every request - an answer can be an exception, to be raised instead.
    """

    def __init__(self, **answers):
        self.answers = {address: list(each) for address, each in answers.items()}
        self.asked = []

    def post(self, url, data=None, timeout=None, **kwargs):
        self.asked.append((url, dict(data or {})))
        answer = self.answers[url].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url, **kwargs):
        raise AssertionError(f"Nothing should be fetched from {url}")


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

    with pytest.raises(ValueError, match="That address isn't from this sign-in."):
        sign_in.finish(_pasted("another-sign-ins-state"))

    assert google.asked == [] and not os.path.exists(token_file)


def test_an_address_without_googles_code_is_refused(token_file):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)
    state = _query(sign_in.start())["state"]

    with pytest.raises(ValueError, match="That address hasn't got Google's code in it."):
        sign_in.finish(_pasted(state, code=None))

    assert google.asked == [] and not os.path.exists(token_file)
    # The state is used once, so the same address can't finish it afterwards either.
    with pytest.raises(ValueError, match="That address isn't from this sign-in."):
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
    with pytest.raises(ValueError, match="That address isn't from this sign-in."):
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


def test_without_a_sign_in_it_needs_doing(token_file):
    google = FakeGoogle()
    sign_in = GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=google)

    assert not sign_in.signed_in()
    with pytest.raises(SignInNeeded):
        sign_in.access_token()
    assert google.asked == []


# The command

def test_the_sign_in_command_never_prints_a_secret(token_file, monkeypatch, capsys):
    monkeypatch.setattr(google_calendar.paths, "load_secrets", lambda: None)
    monkeypatch.setattr(google_calendar.paths, "GOOGLE_TOKEN_FILE", token_file)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    google = FakeGoogle(**{google_calendar.TOKEN: [_token_answer()]})
    monkeypatch.setattr(google_calendar.requests, "Session", lambda: google)
    printed = []

    def paste(prompt=""):
        # What the user sees before pasting - the address to open, with its state.
        before = capsys.readouterr().out + prompt
        printed.append(before)
        address = next(word for word in before.split() if word.startswith(google_calendar.AUTHORISE))
        return _pasted(_query(address)["state"])

    monkeypatch.setattr("builtins.input", paste)

    google_calendar.main([])

    printed.append(capsys.readouterr().out)
    shown = "".join(printed)
    assert "The page won't load - that's expected. Copy its address from the address bar and paste it here:" in shown
    assert "Signed in to Google." in shown
    for secret in (CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN, "made-up-code"):
        assert secret not in shown
    assert GoogleSignIn(CLIENT_ID, CLIENT_SECRET, token_file, session=FakeGoogle()).signed_in()

    # Already signed in, it says so and asks nothing, unless told to sign in again.
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("Nothing should be asked"))
    google_calendar.main([])
    assert "Already signed in to Google" in capsys.readouterr().out
