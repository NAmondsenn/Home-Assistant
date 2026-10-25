"""
Google Calendar Module.

Nova's own sign-in to Google, for the calendar. See docs/decisions/0008.

The calendar comes straight from Google, not through Home Assistant - the user's
answer 2 - so Nova signs in to Google itself (decision 10). It asks only for what it
uses: the list of calendars, and reading and adding events. The sign-in is kept on
the brain, readable by Nova's user only, never on a satellite. Until the settings
page can do it, it's done once with a command on the brain, the way Spotify's is:

    runuser -u nova -- venv/bin/python -m voice_assistant.google_calendar

It's plain HTTPS with requests - an authorisation code with PKCE, from a "Desktop
app" client of the user's own - rather than a Google library, which would be one more
dependency for three requests.
"""

import argparse
import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from voice_assistant import paths, storage

logger = logging.getLogger(__name__)

# The least that list the calendars, read every calendar's events, and add to the
# user's own. events.owned could change an event too - Google has no scope that only
# adds - so it's Nova's own code that keeps it to adding.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
]

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


class SignInNeeded(Exception):
    """There's no sign-in to Google, or Google no longer accepts it."""


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
                or it hasn't got Google's code in it - nothing is asked or saved.
            requests.RequestException: If Google couldn't be reached, or refused the code.
        """
        state, verifier = self._state, self._verifier
        self._state = self._verifier = None
        fields = {name: values[0] for name, values in parse_qs(urlsplit(pasted.strip()).query).items()}
        # Compared in constant time, so how much of it matched can't be timed.
        if state is None or not secrets.compare_digest(fields.get("state", ""), state):
            raise ValueError("That address isn't from this sign-in.")
        if not fields.get("code"):
            raise ValueError("That address hasn't got Google's code in it.")

        answer = self._ask_for_token({"grant_type": "authorization_code", "code": fields["code"],
                                      "code_verifier": verifier, "redirect_uri": REDIRECT})
        if not answer.get("refresh_token"):
            raise ValueError("Google didn't give Nova a sign-in to keep - try again.")
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

    def _ask_for_token(self, fields: Dict[str, str]) -> Dict:
        """
        Asks Google's token address for a token.

        Args:
            fields: What's asked, besides the client's id and secret.

        Returns:
            Google's answer.

        Raises:
            SignInNeeded: If Google says the grant is no good - revoked, or run out.
            requests.RequestException: If Google couldn't be reached, or refused.
        """
        response = self._session.post(TOKEN, data={"client_id": self.client_id,
                                                   "client_secret": self.client_secret, **fields},
                                      timeout=REQUEST_TIMEOUT)
        if response.status_code == 400 and self._error(response) == "invalid_grant":
            # A sign-in revoked in the Google account, or one that's run out.
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
    except SignInNeeded:
        raise SystemExit("Google didn't accept that sign-in - try again.")
    except requests.RequestException:
        # Not the error itself, which could quote what Google answered.
        raise SystemExit("Couldn't reach Google, or it refused the sign-in - try again.")
    print("Signed in to Google.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
