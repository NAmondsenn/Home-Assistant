"""
ntfy Module.

Nova and the user's own ntfy server, both ways.

Alerts posted to the alert topics - the homelab's, say - are said out loud as they
arrive, pausing the music the way a timer does, and "tell me more" reads out the
whole of the last one. Between quiet_from and quiet_until only the most urgent are
said, and the rest wait for "any notifications?" - as do any which arrived while
Nova couldn't reach the server, rather than all being read out at once when it can
again.

And Nova sends things to the user's phone: what it just said, when asked to; a
reminder nobody answered; and Nova having had to restart itself after a problem.
What it sends is tagged "nova", so it's never read back out as an alert itself.

It all runs on the Pi, through the server's own API, with an access token from
NTFY_TOKEN in ~/.config/nova/.env.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence

import requests

from voice_assistant import network
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Seconds to wait for the server to take a message, or to start listening.
REQUEST_TIMEOUT = 5

# The server sends something at least every 45 seconds while it's being listened to,
# so a connection quiet for longer than this has gone, and is made afresh.
LISTEN_TIMEOUT = 90

# The longest wait between tries at reaching the server again, in seconds.
MOST_WAIT = 60

# An alert sent longer ago than this when it arrives - because Nova couldn't reach
# the server at the time - isn't said out loud, only kept for "any notifications?".
STALE_SECONDS = 600

# ntfy's top priority, the only one said out loud at night. An alert with no
# priority of its own is ntfy's default, 3.
URGENT = 5
DEFAULT_PRIORITY = 3

# The tag on everything Nova sends.
OWN_TAG = "nova"

# How many recent alerts are kept, for "any notifications?" and "tell me more".
KEPT = 20

# The longest an alert's headline may be when it's said out loud, in characters.
HEADLINE_CHARS = 120

# How long after an alert "tell me more" means more about it, in seconds.
TELL_MORE_WITHIN = 120


@dataclass
class Alert:
    """One message from an alert topic."""
    topic: str
    title: str
    message: str
    priority: int = DEFAULT_PRIORITY
    heard: bool = False

    @property
    def headline(self) -> str:
        """What's said when it arrives - its title, or else its first sentence."""
        text = self.title or re.split(r"(?<=[.!?])\s+", self.message, maxsplit=1)[0]
        if len(text) > HEADLINE_CHARS:
            text = text[:HEADLINE_CHARS].rsplit(" ", 1)[0] + "..."
        return text

    @property
    def details(self) -> str:
        """The whole of it, for "tell me more"."""
        parts = [part.rstrip(".") for part in (self.title, self.message) if part]
        return ". ".join(parts) + "." if parts else ""


class Ntfy:
    """The user's ntfy server: the alert topics listened to, and the topics sent to."""

    def __init__(self, url: str, token: str = "", alerts: Sequence[str] = (), phone: str = "",
                 reminders: str = "", restarts: str = "", quiet_from: str = "23:00",
                 quiet_until: str = "07:00", on_alert: Optional[Callable[[Alert], None]] = None,
                 session=None, clock: Callable[[], float] = time.time,
                 now: Callable[[], datetime] = datetime.now):
        """
        Args:
            url: The server, e.g. "https://ntfy.example.com".
            token: An access token for it, or nothing for a server anyone can use.
            alerts: Topics whose messages are said out loud as they arrive.
            phone: Where "send that to my phone" goes.
            reminders: Where a reminder nobody answered goes.
            restarts: Where Nova having to restart itself goes.
            quiet_from: When night starts, as "23:00".
            quiet_until: When it ends, as "07:00".
            on_alert: Says an alert out loud.
            session: What makes the requests - only replaced by the tests.
            clock: The time now, in seconds - only replaced by the tests.
            now: The date and time now - only replaced by the tests.
        """
        self.url = url.rstrip("/")
        self.alerts = [topic for topic in alerts if topic]
        self.phone, self.reminders, self.restarts = phone, reminders, restarts
        self.quiet_from = quiet_from if re.fullmatch(r"\d\d:\d\d", quiet_from or "") else "23:00"
        self.quiet_until = quiet_until if re.fullmatch(r"\d\d:\d\d", quiet_until or "") else "07:00"
        self.on_alert = on_alert
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._session = session or requests.Session()
        self._clock = clock
        self._now = now

        self._lock = threading.Lock()
        self._kept: List[Alert] = []
        self._last_said: Optional[Alert] = None
        self._said_at = 0.0
        # The last message heard, so listening again after a dropped connection
        # carries on from it rather than missing what came in meanwhile.
        self._last_id: Optional[str] = None
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._response = None

    @classmethod
    def from_config(cls, config, token: Optional[str], on_alert: Optional[Callable[[Alert], None]] = None):
        """
        The server ntfy.url in config.yaml names, or None if it doesn't name one.

        Args:
            config: Configs loaded from config.yaml.
            token: NTFY_TOKEN.
            on_alert: Says an alert out loud.
        """
        settings = config.section("ntfy")
        url = (settings.get("url") or "").strip()
        if not url:
            return None
        return cls(url, token or "", alerts=settings.get("alerts") or [], phone=settings.get("phone") or "",
                   reminders=settings.get("reminders") or "", restarts=settings.get("restarts") or "",
                   quiet_from=settings.get("quiet_from", "23:00"), quiet_until=settings.get("quiet_until", "07:00"),
                   on_alert=on_alert)

    # Sending

    def send(self, topic: str, message: str, title: str = "", priority: int = 0) -> bool:
        """
        Sends a message to one of the user's topics, tagged as Nova's.

        Args:
            topic: e.g. the phone topic.
            message: What it says.
            title: A few words saying what it is.
            priority: ntfy's 1 to 5, or 0 for its default.

        Returns:
            Whether the server took it.
        """
        if not topic:
            return False
        if not network.online():
            logger.warning(f"No network, so nothing was sent to {topic}")
            return False
        body = {"topic": topic, "message": message, "tags": [OWN_TAG]}
        if title:
            body["title"] = title
        if priority:
            body["priority"] = priority
        try:
            response = self._session.post(self.url, json=body, headers=self._headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"Couldn't send to {topic}: {e}")
            return False
        logger.info(f"Sent to {topic}: '{title or message}'", extra=TRANSCRIPT)
        return True

    def send_soon(self, topic: str, message: str, title: str = "", priority: int = 0):
        """Sends a message on a thread of its own, for something which mustn't wait on the server."""
        threading.Thread(target=self.send, args=(topic, message, title, priority), name="ntfy-send",
                         daemon=True).start()

    # Listening

    def start(self):
        """Starts listening to the alert topics, on a thread of its own."""
        if self.alerts and self._thread is None:
            self._thread = threading.Thread(target=self._listen, name="ntfy", daemon=True)
            self._thread.start()

    def stop(self):
        """Stops listening."""
        self._stopping.set()
        response = self._response
        if response is not None:
            response.close()

    def quiet(self) -> bool:
        """Whether it's night, when only the most urgent alerts are said out loud."""
        now = self._now().strftime("%H:%M")
        if self.quiet_from <= self.quiet_until:
            return self.quiet_from <= now < self.quiet_until
        return now >= self.quiet_from or now < self.quiet_until

    def _listen(self):
        """
        Listens to the alert topics until stopped, listening again whenever the
        connection drops - soon after one which worked, and less and less often while
        the server can't be reached at all.
        """
        wait = 1
        while not self._stopping.is_set():
            try:
                if not network.online():
                    raise requests.ConnectionError("no network")
                self._listen_once()
                wait = 1
            except Exception as e:
                # Closing the connection is how listening is stopped part way.
                if self._stopping.is_set():
                    return
                # Said once each time it's lost, rather than at every try.
                if wait == 1:
                    logger.warning(f"Lost the connection to ntfy ({e}), so trying again")
                wait = min(wait * 2, MOST_WAIT)
            self._stopping.wait(wait)

    def _listen_once(self):
        """One connection's worth of listening, until it drops or listening stops."""
        params = {"since": self._last_id} if self._last_id else {}
        with self._session.get(f"{self.url}/{','.join(self.alerts)}/json", params=params, headers=self._headers,
                               stream=True, timeout=(REQUEST_TIMEOUT, LISTEN_TIMEOUT)) as response:
            response.raise_for_status()
            self._response = response
            for line in response.iter_lines():
                if self._stopping.is_set():
                    return
                if line:
                    self._received(json.loads(line))

    def _received(self, event: Dict):
        """
        One event from the server - said out loud if it's an alert which should be,
        kept either way.
        """
        if event.get("event") != "message":
            return
        self._last_id = event.get("id") or self._last_id
        if OWN_TAG in (event.get("tags") or []):
            return
        alert = Alert(topic=event.get("topic", ""), title=(event.get("title") or "").strip(),
                      message=(event.get("message") or "").strip(),
                      priority=int(event.get("priority") or DEFAULT_PRIORITY))
        with self._lock:
            self._kept = (self._kept + [alert])[-KEPT:]

        if self._clock() - (event.get("time") or self._clock()) > STALE_SECONDS:
            logger.info(f"Kept, but not said - it came while ntfy couldn't be reached: '{alert.headline}'",
                        extra=TRANSCRIPT)
            return
        if alert.priority < URGENT and self.quiet():
            logger.info(f"Kept, but not said - it's night: '{alert.headline}'", extra=TRANSCRIPT)
            return
        self._said(alert)
        logger.info(f"Alert: '{alert.headline}'", extra=TRANSCRIPT)
        if self.on_alert:
            try:
                self.on_alert(alert)
            except Exception as e:
                logger.error(f"Couldn't say the alert: {e}")

    def _said(self, *alerts: Alert):
        with self._lock:
            for alert in alerts:
                alert.heard = True
            self._last_said = alerts[-1]
            self._said_at = self._clock()

    # Asked about

    def just_said(self) -> bool:
        """Whether an alert was said out loud a moment ago - so "tell me more" is about it."""
        return self._last_said is not None and self._clock() - self._said_at < TELL_MORE_WITHIN

    def tell_more(self) -> Dict:
        """
        The whole of the last alert said out loud.

        Returns:
            Dict with 'success' and the 'message' to say.
        """
        alert = self._last_said
        if alert is None:
            return {"success": True, "message": "There haven't been any notifications."}
        return {"success": True, "message": alert.details}

    def unheard(self) -> Dict:
        """
        The alerts which weren't said out loud when they came in, for "any notifications?".

        Returns:
            Dict with 'success' and the 'message' to say.
        """
        with self._lock:
            waiting = [alert for alert in self._kept if not alert.heard]
        if not waiting:
            return {"success": True, "message": "No new notifications."}
        self._said(*waiting)
        if len(waiting) == 1:
            return {"success": True, "message": f"One notification. {waiting[0].details}"}
        headlines = "; ".join(alert.headline.rstrip(".") for alert in waiting)
        return {"success": True, "message": f"{len(waiting)} notifications: {headlines}."}
