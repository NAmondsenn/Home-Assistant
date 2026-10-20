"""
Home Assistant Test
Checks the lights and the calendar through Home Assistant, using a fake in place of
its API, so nothing is sent anywhere.
"""

from datetime import date, datetime, timedelta

import pytest
import requests

from voice_assistant.home_assistant import HomeAssistant, describe_agenda


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    """Stands in for Home Assistant's API, answering by path and noting what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.headers = {}
        self.requests = []

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("8123", 1)[1]
        self.requests.append((method, path, kwargs))
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)


def _assist(kind, speech):
    return {"response": {"response_type": kind, "speech": {"plain": {"speech": speech}}}}


@pytest.fixture
def make_home():
    def factory(answers, calendars=None):
        home = HomeAssistant("http://homeassistant.local:8123/", "token", calendars=calendars)
        home._session = FakeSession(answers)
        return home
    return factory


# The lights

def test_a_light_command_goes_to_assist_and_is_chimed(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Turned off the lights")})
    result = home.command("turn off the kitchen lights")

    assert result == {"success": True, "message": "Turned off the lights", "chime": True}
    method, path, sent = home._session.requests[0]
    assert sent["json"] == {"text": "turn off the kitchen lights", "language": "en",
                            "agent_id": "conversation.home_assistant"}


def test_a_question_is_answered_in_assists_words(make_home):
    home = make_home({"/api/conversation/process": _assist("query_answer", "The hall light is on")})
    assert home.command("is the hall light on") == {"success": True, "message": "The hall light is on"}


def test_something_assist_couldnt_do_says_why(make_home):
    home = make_home({"/api/conversation/process": _assist("error", "Sorry, I am not aware of any area called attic")})
    assert home.command("turn on the attic lights") == {
        "success": False, "message": "Sorry, I am not aware of any area called attic"}


@pytest.mark.parametrize("request_text", ["unlock the front door", "open the garage", "open the back door",
                                          "disarm the alarm", "lock the door"])
def test_nothing_which_lets_anyone_in_is_done_by_voice(make_home, request_text):
    home = make_home({"/api/conversation/process": _assist("action_done", "Done")})
    assert home.command(request_text)["success"] is False
    assert home._session.requests == []


def test_a_light_named_after_a_door_is_fine(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Turned on the light")})
    assert home.command("turn on the front door light")["success"]


@pytest.mark.parametrize("failure, said", [
    (requests.ConnectionError("refused"), "I can't reach Home Assistant right now."),
    (FakeResponse({}, status=401), "HA_TOKEN in the .env file needs checking"),
])
def test_a_failed_request_says_what_went_wrong(make_home, failure, said):
    home = make_home({"/api/conversation/process": failure})
    result = home.command("turn on the lights")
    assert result["success"] is False and said in result["message"]


def test_without_an_address_and_a_token_its_off(make_config):
    assert HomeAssistant.from_config(make_config(), "token") is None
    assert HomeAssistant.from_config(make_config(home_assistant={"url": "http://ha:8123"}), None) is None
    home = HomeAssistant.from_config(make_config(home_assistant={"url": "http://ha:8123"}), "token")
    assert home.url == "http://ha:8123" and home._session.headers["Authorization"] == "Bearer token"


# The calendar

def test_the_calendar_is_read_for_the_days_asked_about(make_home):
    thursday = date(2026, 10, 22)
    home = make_home({
        "/api/calendars": [{"entity_id": "calendar.personal"}],
        "/api/calendars/calendar.personal": [
            {"summary": "Dinner with Sam", "start": {"dateTime": "2026-10-22T19:00:00+01:00"}},
            {"summary": "Dentist", "start": {"dateTime": "2026-10-22T09:30:00+01:00"}},
            {"summary": "Bin day", "start": {"date": "2026-10-22"}},
        ],
    })
    events = home.events(thursday)

    assert [event["summary"] for event in events] == ["Bin day", "Dentist", "Dinner with Sam"]
    method, path, sent = home._session.requests[1]
    start = datetime.fromisoformat(sent["params"]["start"])
    assert start.date() == thursday and datetime.fromisoformat(sent["params"]["end"]) - start == timedelta(days=1)


def test_only_the_calendars_named_are_read(make_home):
    home = make_home({"/api/calendars/calendar.work": []}, calendars=["calendar.work"])
    home.events(date(2026, 10, 22))
    assert [path for _, path, _ in home._session.requests] == ["/api/calendars/calendar.work"]


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


# Work, uni and the like

def test_an_activity_is_found_by_its_name_and_said_with_its_hours(make_home):
    home = make_home({
        "/api/calendars": [{"entity_id": "calendar.work"}],
        "/api/calendars/calendar.work": [
            {"summary": "Dentist", "start": {"dateTime": "2026-10-21T08:00:00+01:00"},
             "end": {"dateTime": "2026-10-21T08:30:00+01:00"}},
            {"summary": "Shift", "start": {"dateTime": "2026-10-21T15:30:00+01:00"},
             "end": {"dateTime": "2026-10-21T22:00:00+01:00"}},
        ],
    })
    home.activities = {"work": ["shift"]}
    result = home.activity("work", date(2026, 10, 21), today=date(2026, 10, 20))
    assert result == {"success": True, "message": "Tomorrow, you've got work from 3:30 PM until 10:00 PM."}


def test_uni_sessions_back_to_back_are_said_as_one_stretch(make_home):
    # From the user's calendar: a practical, a tutorial, then a lecture straight after it.
    home = make_home({
        "/api/calendars": [{"entity_id": "calendar.uni"}],
        "/api/calendars/calendar.uni": [
            {"summary": "CSC2035/P01/01 <04-07>", "start": {"dateTime": "2026-10-20T11:30:00+01:00"},
             "end": {"dateTime": "2026-10-20T12:30:00+01:00"}},
            {"summary": "CSC2032/T01/01", "start": {"dateTime": "2026-10-20T14:30:00+01:00"},
             "end": {"dateTime": "2026-10-20T15:30:00+01:00"}},
            {"summary": "CSC2035/L01/01 <04-07>", "start": {"dateTime": "2026-10-20T15:30:00+01:00"},
             "end": {"dateTime": "2026-10-20T17:30:00+01:00"}},
            {"summary": "Discussion group", "start": {"dateTime": "2026-10-20T18:00:00+01:00"},
             "end": {"dateTime": "2026-10-20T19:00:00+01:00"}},
        ],
    })
    home.activities = {"uni": ["csc"]}
    assert home.activity("uni", date(2026, 10, 20), today=date(2026, 10, 20))["message"] == \
        "Today, you've got uni from 11:30 AM until 12:30 PM, and from 2:30 PM until 5:30 PM."


def test_a_day_off_says_so(make_home):
    home = make_home({"/api/calendars": [{"entity_id": "calendar.personal"}],
                      "/api/calendars/calendar.personal": [
                          {"summary": "Football", "start": {"dateTime": "2026-10-24T10:00:00+01:00"},
                           "end": {"dateTime": "2026-10-24T11:00:00+01:00"}}]})
    assert home.activity("work", date(2026, 10, 24), 2, today=date(2026, 10, 20))["message"] == \
        "You haven't got work between Saturday and Sunday."
    assert home.activity("work", date(2026, 10, 21), today=date(2026, 10, 20))["message"] == \
        "You haven't got work tomorrow."


def test_an_activity_it_doesnt_know_says_where_to_add_it(make_home):
    result = make_home({}).activity("gym", date(2026, 10, 21))
    assert result["success"] is False and "home_assistant.activities" in result["message"]


def test_split_shifts_and_all_day_work_are_both_said():
    from voice_assistant.home_assistant import describe_activity
    events = [{"summary": "Shift", "start": datetime(2026, 10, 22, 9, 0), "end": datetime(2026, 10, 22, 13, 0),
               "all_day": False},
              {"summary": "Shift", "start": datetime(2026, 10, 22, 14, 0), "end": datetime(2026, 10, 22, 18, 0),
               "all_day": False},
              {"summary": "Shift", "start": date(2026, 10, 23), "end": None, "all_day": True}]
    assert describe_activity("work", events, date(2026, 10, 22), 2, TODAY) == (
        "On Thursday, you've got work from 9:00 AM until 1:00 PM, and from 2:00 PM until 6:00 PM. "
        "On Friday, you've got work all day.")


def test_just_work_called_work_unless_set(make_config):
    home = HomeAssistant.from_config(make_config(home_assistant={"url": "http://ha:8123"}), "token")
    assert home.activities == {"work": ["work"]}
    home = HomeAssistant.from_config(make_config(home_assistant={
        "url": "http://ha:8123", "activities": {"Work": ["Shift"], "uni": ["CSC"]}}), "token")
    assert home.activities == {"work": ["shift"], "uni": ["csc"]}
