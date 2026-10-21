"""
Routines Test
Checks routines are kept only when they make sense, are found from what the user
said, and run by themselves once at their time - in a folder of the test's own.
"""

import json
from datetime import datetime

import pytest

from voice_assistant.routines import Routines

MORNING = {"name": "Good morning", "phrases": "Good morning!, morning", "at": "7:00", "repeat": "weekdays",
           "steps": "what's the weather\n\n  what's on today  \nplay my morning playlist"}


@pytest.fixture
def routines(tmp_path):
    return Routines(path=str(tmp_path / "routines.json"))


def test_a_routine_is_kept_as_the_page_sent_it_tidied(routines, tmp_path):
    result = routines.save(MORNING)
    assert result["success"]
    routine = result["routine"]
    assert {key: routine[key] for key in ("name", "phrases", "at", "repeat", "steps")} == {
        "name": "Good morning", "phrases": ["good morning", "morning"], "at": "07:00", "repeat": "weekdays",
        "steps": ["what's the weather", "what's on today", "play my morning playlist"]}
    # Still there for the next Nova.
    assert Routines(path=str(tmp_path / "routines.json")).all() == [routine]


def test_changing_a_routine_keeps_it_one_routine(routines):
    routine = routines.save(MORNING)["routine"]
    changed = routines.save({**routine, "steps": ["what's the weather"]})
    assert changed["success"] and changed["routine"]["id"] == routine["id"]
    assert [kept["steps"] for kept in routines.all()] == [["what's the weather"]]


@pytest.mark.parametrize("changes, said", [({"name": " "}, "needs a name"), ({"steps": "\n \n"}, "something to do"),
                                           ({"at": "25:00"}, "isn't a time"), ({"repeat": "fortnightly"}, "can run"),
                                           ({"steps": ["x"] * 11}, "10 steps")])
def test_a_routine_which_doesnt_make_sense_isnt_kept(routines, changes, said):
    result = routines.save({**MORNING, **changes})
    assert not result["success"] and said in result["message"]
    assert routines.all() == []


def test_two_routines_cant_be_started_the_same_way(routines):
    routines.save(MORNING)
    result = routines.save({"name": "Wake up", "phrases": "morning", "steps": "what time is it"})
    assert result == {"success": False, "message": "“morning” already starts Good morning."}
    assert not routines.save({**MORNING, "phrases": "rise and shine"})["success"]


@pytest.mark.parametrize("said, found", [("Good morning.", True), ("morning", True),
                                         ("Run my good morning routine", True), ("good morning routine", True),
                                         ("good morning to you", False), ("what's the weather", False)])
def test_a_routine_is_found_from_what_was_said(routines, said, found):
    routines.save(MORNING)
    assert (routines.called(said) is not None) is found


def test_a_routine_runs_by_itself_once_on_its_days(routines):
    routines.save(MORNING)
    tuesday = datetime(2026, 10, 20, 7, 0, 10)
    assert [routine["name"] for routine in routines.due(tuesday)] == ["Good morning"]
    # Asked again within the minute, it's already run.
    assert routines.due(tuesday.replace(second=40)) == []
    assert routines.due(datetime(2026, 10, 20, 7, 1)) == []
    # Not on a Saturday, as it's for weekdays.
    assert routines.due(datetime(2026, 10, 24, 7, 0)) == []
    assert len(routines.due(datetime(2026, 10, 21, 7, 0))) == 1


def test_a_routine_is_deleted(routines):
    routine = routines.save(MORNING)["routine"]
    assert routines.remove(routine["id"]) == {"success": True, "message": "Deleted Good morning."}
    assert routines.all() == [] and not routines.remove(routine["id"])["success"]


def test_one_routine_which_doesnt_make_sense_doesnt_lose_the_rest(tmp_path):
    path = tmp_path / "routines.json"
    path.write_text(json.dumps({"routines": [{"name": "", "steps": ["x"]}, {"name": "Bedtime", "steps": ["good night"]}]}))
    assert [routine["name"] for routine in Routines(path=str(path)).all()] == ["Bedtime"]
