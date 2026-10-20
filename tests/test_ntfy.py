"""
ntfy Test
Checks which alerts are said out loud and which are only kept, what's read out when
the user asks, and what's sent to their phone - with stand-ins for the server and
the time of day.
"""

import json
from datetime import datetime

import pytest
import requests

from voice_assistant import ntfy as ntfy_module
from voice_assistant.ntfy import Alert, Ntfy

NOW = 1_800_000_000.0


class FakeResponse:
    """Stands in for one of the server's answers - or, while listening, its stream of events."""

    def __init__(self, status=200, lines=()):
        self.status_code = status
        self.lines = [json.dumps(line).encode() if isinstance(line, dict) else line for line in lines]
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def iter_lines(self):
        yield from self.lines

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeSession:
    """Stands in for requests, keeping what was sent and giving back what the test lines up."""

    def __init__(self, posted_status=200, streams=()):
        self.posted = []
        self.posted_status = posted_status
        self.listened = []
        self.streams = list(streams)

    def post(self, url, json=None, headers=None, timeout=None):
        self.posted.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(self.posted_status)

    def get(self, url, params=None, headers=None, stream=False, timeout=None):
        self.listened.append({"url": url, "params": params, "headers": headers})
        return self.streams.pop(0)


@pytest.fixture(autouse=True)
def online(monkeypatch):
    monkeypatch.setattr(ntfy_module.network, "online", lambda: True)


def _ntfy(hour=12, minute=0, session=None, clock=lambda: NOW, **settings):
    said = []
    made = Ntfy("https://ntfy.example.com/", token="tk_secret", alerts=["homelab"], phone="clipboard",
                reminders="reminders", restarts="homelab", on_alert=said.append, session=session or FakeSession(),
                clock=clock, now=lambda: datetime(2026, 10, 20, hour, minute), **settings)
    return made, said


def _message(title="", message="Something happened.", priority=None, time=NOW, tags=None, id="abc123"):
    event = {"id": id, "time": time, "event": "message", "topic": "homelab", "message": message}
    if title:
        event["title"] = title
    if priority:
        event["priority"] = priority
    if tags:
        event["tags"] = tags
    return event


# Alerts coming in

def test_an_alert_is_said_by_its_title_and_the_rest_kept_for_tell_me_more():
    ntfy, said = _ntfy()
    ntfy._received(_message(title="Backup failed", message="Immich's nightly backup failed. Error 5."))

    assert [alert.headline for alert in said] == ["Backup failed"]
    assert ntfy.just_said()
    assert ntfy.tell_more()["message"] == "Backup failed. Immich's nightly backup failed. Error 5."


def test_without_a_title_its_first_sentence_is_said():
    ntfy, said = _ntfy()
    ntfy._received(_message(message="PVE2's disk is 95% full. Clear something soon."))
    assert said[0].headline == "PVE2's disk is 95% full."


def test_a_long_headline_is_cut_short_between_words():
    headline = Alert("homelab", title="word " * 40, message="").headline
    assert len(headline) <= ntfy_module.HEADLINE_CHARS + 3 and headline.endswith("word...")


@pytest.mark.parametrize("hour, minute, quiet", [(22, 59, False), (23, 0, True), (3, 0, True),
                                                 (6, 59, True), (7, 0, False), (12, 0, False)])
def test_night_runs_from_quiet_from_to_quiet_until(hour, minute, quiet):
    assert _ntfy(hour, minute)[0].quiet() is quiet


def test_quiet_hours_which_dont_cross_midnight_work_too():
    ntfy, _ = _ntfy(hour=14, quiet_from="13:00", quiet_until="15:00")
    assert ntfy.quiet()


def test_at_night_only_the_most_urgent_alerts_are_said():
    ntfy, said = _ntfy(hour=1)
    ntfy._received(_message(title="Update available", priority=3))
    ntfy._received(_message(title="UPS on battery", priority=5))

    assert [alert.headline for alert in said] == ["UPS on battery"]
    # The other's kept for the morning.
    assert ntfy.unheard()["message"] == "One notification. Update available. Something happened."


def test_an_alert_sent_while_nova_couldnt_reach_the_server_isnt_said():
    ntfy, said = _ntfy()
    ntfy._received(_message(title="Backup failed", time=NOW - 700))
    assert said == [] and "Backup failed" in ntfy.unheard()["message"]


def test_novas_own_messages_arent_read_back_out():
    ntfy, said = _ntfy()
    ntfy._received(_message(title="Nova restarted", tags=["nova"]))
    assert said == [] and ntfy.unheard()["message"] == "No new notifications."


def test_any_notifications_reads_out_whats_waiting_once():
    ntfy, _ = _ntfy(hour=2)
    ntfy._received(_message(title="Update available."))
    ntfy._received(_message(title="Certificate renewed"))

    assert ntfy.unheard()["message"] == "2 notifications: Update available; Certificate renewed."
    assert ntfy.unheard()["message"] == "No new notifications."
    # And "tell me more" is then about the last of them.
    assert ntfy.tell_more()["message"] == "Certificate renewed. Something happened."


def test_tell_me_more_is_only_about_an_alert_from_a_moment_ago():
    now = [NOW]
    ntfy, _ = _ntfy(clock=lambda: now[0])
    assert not ntfy.just_said()
    ntfy._received(_message(title="Backup failed", time=NOW))
    assert ntfy.just_said()
    now[0] += ntfy_module.TELL_MORE_WITHIN + 1
    assert not ntfy.just_said()


def test_tell_me_more_with_nothing_to_tell():
    assert _ntfy()[0].tell_more()["message"] == "There haven't been any notifications."


# Listening

def test_listening_carries_on_from_the_last_message_after_a_drop():
    session = FakeSession(streams=[FakeResponse(lines=[{"event": "open"}, {"event": "keepalive"},
                                                       _message(title="Backup failed", id="abc123")]),
                                   FakeResponse(lines=[])])
    ntfy, said = _ntfy(session=session)

    ntfy._listen_once()
    ntfy._listen_once()

    assert session.listened[0]["url"] == "https://ntfy.example.com/homelab/json"
    assert session.listened[0]["params"] == {} and session.listened[1]["params"] == {"since": "abc123"}
    assert session.listened[0]["headers"] == {"Authorization": "Bearer tk_secret"}
    # The server's own "open" and "keepalive" are just that.
    assert [alert.headline for alert in said] == ["Backup failed"]


def test_listening_stops_when_asked():
    ntfy, _ = _ntfy(session=FakeSession(streams=[]))
    ntfy.stop()
    ntfy._listen()


# Sending

def test_whats_sent_goes_to_the_topic_tagged_as_novas():
    session = FakeSession()
    ntfy, _ = _ntfy(session=session)

    assert ntfy.send("clipboard", "Mix the flour and the eggs...", title="Pancakes")
    assert session.posted == [{"url": "https://ntfy.example.com",
                               "json": {"topic": "clipboard", "message": "Mix the flour and the eggs...",
                                        "tags": ["nova"], "title": "Pancakes"},
                               "headers": {"Authorization": "Bearer tk_secret"}}]


def test_sending_says_when_it_didnt_work():
    ntfy, _ = _ntfy(session=FakeSession(posted_status=403))
    assert not ntfy.send("clipboard", "Hello")


def test_nothing_is_sent_with_no_network(monkeypatch):
    session = FakeSession()
    ntfy, _ = _ntfy(session=session)
    monkeypatch.setattr(ntfy_module.network, "online", lambda: False)
    assert not ntfy.send("clipboard", "Hello") and session.posted == []


def test_its_only_set_up_once_config_names_a_server(make_config):
    assert Ntfy.from_config(make_config(ntfy={"url": ""}), "tk_secret") is None
    made = Ntfy.from_config(make_config(ntfy={"url": "https://ntfy.example.com", "alerts": ["homelab"],
                                              "phone": "clipboard"}), "tk_secret")
    assert (made.url, made.alerts, made.phone, made.quiet_from) == ("https://ntfy.example.com", ["homelab"],
                                                                     "clipboard", "23:00")
