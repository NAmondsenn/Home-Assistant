"""
Home Assistant Test
Checks the lights and the calendar through Home Assistant, using a fake in place of
its API, so nothing is sent anywhere.
"""

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from voice_assistant import network
from voice_assistant.home_assistant import HomeAssistant, describe_agenda, describe_found


@pytest.fixture(autouse=True)
def london(monkeypatch):
    """
    Runs every test on UK time, whatever the machine's own time zone. Calendar times
    are said in the Pi's own time, and the events here are written in BST - so on
    GitHub, which runs on UTC, uni "from 11:30 AM" came out as "from 10:30 AM".
    """
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


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


def test_with_no_network_it_isnt_asked_at_all(make_home, monkeypatch):
    # With the Pi's cable out, "turn on the lamp" waited 20 seconds for Home
    # Assistant's name to fail to be looked up.
    monkeypatch.setattr(network, "online", lambda: False)
    home = make_home({})
    assert home.command("turn on the lamp")["message"] == "I can't reach Home Assistant right now."
    assert home._session.requests == []


def test_without_an_address_and_a_token_its_off(make_config):
    assert HomeAssistant.from_config(make_config(), "token") is None
    assert HomeAssistant.from_config(make_config(home_assistant={"url": "http://ha:8123"}), None) is None
    home = HomeAssistant.from_config(make_config(home_assistant={"url": "http://ha:8123"}), "token")
    assert home.url == "http://ha:8123" and home._session.headers["Authorization"] == "Bearer token"


# The settings page

STATES = [
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen", "brightness": 128, "supported_color_modes": ["color_temp"]}},
    {"entity_id": "light.porch", "state": "off", "attributes": {"friendly_name": "Porch", "supported_color_modes": ["onoff"]}},
    {"entity_id": "light.garage", "state": "unavailable", "attributes": {"friendly_name": "Garage light"}},
    {"entity_id": "script.movie_night", "state": "off", "attributes": {"friendly_name": "Movie night"}},
    {"entity_id": "scene.relax", "state": "2026-10-20T21:00:00", "attributes": {"friendly_name": "Relax"}},
    {"entity_id": "script.open_garage", "state": "off", "attributes": {"friendly_name": "Garage"}},
    {"entity_id": "script.leaving", "state": "off", "attributes": {"friendly_name": "Unlock the front door"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"}},
    {"entity_id": "switch.adguard_protection", "state": "on", "attributes": {"friendly_name": "AdGuard protection"}},
]


def _state(entity):
    return next(state for state in STATES if state["entity_id"] == entity)


def test_the_page_is_shown_the_lights_and_only_whats_safe_to_run(make_home):
    overview = make_home({"/api/states": STATES}).overview()

    assert [light["name"] for light in overview["lights"]] == ["Garage light", "Kitchen", "Porch"]
    assert overview["lights"][1] == {"id": "light.kitchen", "name": "Kitchen", "on": True, "reachable": True,
                                     "dimmable": True, "brightness": 50}
    assert not overview["lights"][2]["dimmable"] and not overview["lights"][0]["reachable"]
    # A light can't let anyone in, whatever it's called; a script or scene might, and
    # nothing else is offered at all.
    assert overview["routines"] == [{"id": "script.movie_night", "name": "Movie night", "kind": "script"},
                                    {"id": "scene.relax", "name": "Relax", "kind": "scene"}]


def test_the_page_is_told_when_home_assistant_cant_be_reached(make_home):
    assert make_home({"/api/states": requests.ConnectionError("down")}).overview() == {
        "success": False, "message": "I can't reach Home Assistant right now."}


def test_a_light_is_switched_and_dimmed_by_its_own_id(make_home):
    home = make_home({"/api/services/light/turn_on": [], "/api/services/light/turn_off": []})
    assert home.set_light("light.kitchen", on=False)["success"]
    assert home.set_light("light.kitchen", brightness=30)["success"]
    assert [(path, sent["json"]) for _, path, sent in home._session.requests] == [
        ("/api/services/light/turn_off", {"entity_id": "light.kitchen"}),
        ("/api/services/light/turn_on", {"entity_id": "light.kitchen", "brightness_pct": 30})]


@pytest.mark.parametrize("entity", ["lock.front_door", "switch.adguard_protection", "light.kitchen/../lock", ""])
def test_only_a_light_is_switched(make_home, entity):
    home = make_home({})
    assert not home.set_light(entity, on=True)["success"]
    assert home._session.requests == []


def test_a_script_is_run_and_a_scene_set(make_home):
    home = make_home({"/api/states/script.movie_night": _state("script.movie_night"), "/api/services/script/turn_on": [],
                      "/api/states/scene.relax": _state("scene.relax"), "/api/services/scene/turn_on": []})
    assert home.run("script.movie_night") == {"success": True, "message": "Started Movie night."}
    assert home.run("scene.relax") == {"success": True, "message": "Set Relax."}
    assert [(path, sent.get("json")) for _, path, sent in home._session.requests if "services" in path] == [
        ("/api/services/script/turn_on", {"entity_id": "script.movie_night"}),
        ("/api/services/scene/turn_on", {"entity_id": "scene.relax"})]


@pytest.mark.parametrize("entity", ["script.open_garage", "script.leaving"])
def test_a_script_which_could_let_anyone_in_isnt_run_from_the_page_either(make_home, entity):
    # Looked up by name again first - one could have been renamed since the page was drawn.
    home = make_home({f"/api/states/{entity}": _state(entity), "/api/services/script/turn_on": []})
    assert not home.run(entity)["success"]
    assert [path for _, path, _ in home._session.requests] == [f"/api/states/{entity}"]


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
    {"summary": "CSC2035/P01/01 <04-07>", "start": {"dateTime": "2026-10-20T11:30:00+01:00"},
     "end": {"dateTime": "2026-10-20T12:30:00+01:00"}},
    {"summary": "CSC2032/T01/01", "start": {"dateTime": "2026-10-20T14:30:00+01:00"},
     "end": {"dateTime": "2026-10-20T15:30:00+01:00"}},
    {"summary": "CSC2035/L01/01 <04-07>", "start": {"dateTime": "2026-10-20T15:30:00+01:00"},
     "end": {"dateTime": "2026-10-20T17:30:00+01:00"}},
    {"summary": "Discussion group", "start": {"dateTime": "2026-10-20T18:00:00+01:00"},
     "end": {"dateTime": "2026-10-20T19:00:00+01:00"}},
]


def test_a_day_of_uni_is_said_as_uni_not_session_by_session(make_home):
    # Each session was read out by its own name - "CSC2035/P01/01 <04-07> at 11:30
    # AM" - which took 25 seconds for one day, and meant little.
    home = make_home({"/api/calendars": [{"entity_id": "calendar.uni"}], "/api/calendars/calendar.uni": UNI_DAY})
    home.activities = {"uni": ["csc"]}
    assert home.agenda(TODAY, today=TODAY)["message"] == (
        "Today, you've got uni from 11:30 AM until 12:30 PM and from 2:30 PM until 5:30 PM, "
        "and Discussion group at 6:00 PM.")


def test_one_thing_is_looked_for_months_ahead(make_home):
    home = make_home({"/api/calendars": [{"entity_id": "calendar.personal"}],
                      "/api/calendars/calendar.personal": UNI_DAY + [
                          {"summary": "Car MOT", "start": {"dateTime": "2026-11-30T09:30:00+00:00"},
                           "end": {"dateTime": "2026-11-30T10:30:00+00:00"}}]})
    assert home.find("mot", TODAY, today=TODAY) == {
        "success": True, "message": "You've got Car MOT on Monday the 30th of November, at 9:30 AM."}
    method, path, sent = home._session.requests[1]
    assert (datetime.fromisoformat(sent["params"]["end"]) - datetime.fromisoformat(sent["params"]["start"])).days == 90


def test_looking_for_something_which_isnt_there_says_so(make_home):
    home = make_home({"/api/calendars": [{"entity_id": "calendar.uni"}], "/api/calendars/calendar.uni": UNI_DAY})
    assert home.find("MOT", TODAY, today=TODAY)["message"] == \
        "I can't see MOT on your calendar in the next three months."


def test_several_found_are_said_together_up_to_a_few():
    events = [_event("Football", datetime(2026, 10, 21, 10, 0)), _event("Sam's birthday", date(2026, 10, 22), True)]
    assert describe_found("football", events[:1] * 5, TODAY) == (
        "You've got Football tomorrow, at 10:00 AM; Football tomorrow, at 10:00 AM; and Football tomorrow, "
        "at 10:00 AM. And 2 more after that.")
    assert describe_found("sam", events[1:], TODAY) == "You've got Sam's birthday on Thursday, all day."


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


# The next time

# Made as the file is read, before the london fixture has set the time zone, so
# given it themselves.
LONDON = ZoneInfo("Europe/London")


def _shift(day, start_hour, end_hour):
    return {"summary": "Shift", "start": datetime(2026, 10, day, start_hour, 0, tzinfo=LONDON),
            "end": datetime(2026, 10, day, end_hour, 0, tzinfo=LONDON), "all_day": False}


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=LONDON)
SHIFTS = [_shift(20, 8, 11), _shift(21, 15, 23), _shift(24, 14, 22), _shift(25, 14, 21)]


def test_the_next_time_is_just_the_one():
    # Asked "when do I next have work?", every shift for a fortnight used to be read out.
    from voice_assistant.home_assistant import describe_next
    # This morning's shift is over, so it's tomorrow's.
    assert describe_next("work", SHIFTS, 1, date(2026, 10, 20), NOW) == \
        "You're next at work tomorrow, from 3:00 PM until 11:00 PM."


def test_the_next_few_times_are_said_together():
    from voice_assistant.home_assistant import describe_next
    assert describe_next("work", SHIFTS, 3, date(2026, 10, 20), NOW) == (
        "Your next 3 times at work: tomorrow, from 3:00 PM until 11:00 PM; on Saturday, from 2:00 PM until "
        "10:00 PM; and on Sunday, from 2:00 PM until 9:00 PM.")


def test_being_there_now_says_so():
    from voice_assistant.home_assistant import describe_next
    assert describe_next("work", SHIFTS, 1, date(2026, 10, 20), datetime(2026, 10, 20, 9, 30, tzinfo=LONDON)) == \
        "You're at work now, until 11:00 AM."


def test_nothing_coming_up_says_so():
    from voice_assistant.home_assistant import describe_next
    assert describe_next("uni", [], 1, date(2026, 10, 20), NOW) == "You haven't got uni in the next two weeks."
