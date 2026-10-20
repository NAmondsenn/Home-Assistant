"""
Local Intents Test
Checks which everyday commands are handled without Claude, and - just as much -
which are left to it.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from voice_assistant import intents as intents_module
from voice_assistant.actions import Action
from voice_assistant.intents import LocalIntents, calendar_days


def _actions(text, clock=None):
    """The actions a request is handled with locally, or None if it's left to Claude."""
    result = LocalIntents(clock=clock).match(text)
    return None if result is None else result["actions"]


@pytest.mark.parametrize("text, seconds", [
    ("Set a timer for ten minutes.", 600),
    ("set a timer for 10 minutes", 600),
    ("Timer for 30 seconds", 30),
    ("Set a 5 minute timer.", 300),
    ("Start a timer for twenty five minutes, please.", 1500),
    ("Set a timer for an hour.", 3600),
    ("Set a timer for half an hour.", 1800),
    ("Could you set a timer for 2 hours?", 7200),
])
def test_timers_are_set_without_claude(text, seconds):
    # Every request used to go to Claude first, so a timer couldn't even be set
    # with the internet down.
    assert _actions(text) == [Action("set_timer", {"duration_seconds": seconds})]


@pytest.mark.parametrize("text, command", [
    # A bare "stop" is the music too - anything ringing has already been dismissed by then.
    ("Pause.", "pause"), ("Pause the music.", "pause"), ("Stop the music.", "pause"), ("Stop.", "pause"),
    ("Skip.", "skip"), ("Next song.", "skip"), ("Skip this song.", "skip"),
    ("Previous song.", "previous"), ("Go back.", "previous"),
    # How Moonshine heard "stop" and "skip" said on their own.
    ("Soap.", "pause"), ("So.", "pause"), ("Scap.", "skip"),
])
def test_playback_controls_are_handled_without_claude(text, command):
    assert _actions(text) == [Action("control_playback", {"action": command})]


@pytest.mark.parametrize("text", ["Resume.", "Play.", "Play the music.", "Unpause."])
def test_resuming_plays_whatever_was_playing(text):
    assert _actions(text) == [Action("play_music")]


@pytest.mark.parametrize("text, change", [
    ("Turn it up.", {"direction": "up"}), ("Turn it down.", {"direction": "down"}),
    ("Volume up.", {"direction": "up"}), ("Louder.", {"direction": "up"}), ("Quieter.", {"direction": "down"}),
    ("Volume 40.", {"level": 40}), ("Set the volume to 70 percent.", {"level": 70}),
])
def test_volume_is_changed_without_claude(text, change):
    assert _actions(text) == [Action("set_volume", change)]


@pytest.mark.parametrize("text", [
    # More than one thing, or something extra - Claude runs both or works it out.
    "Set a timer for ten minutes and play Drake.",
    "Pause it in five minutes.",
    "Play Drake.",
    "Turn up the heating.",
    "What's the time in Tokyo?",
    "Set a timer.",
    "Set a timer for a few minutes.",
    "Volume 400.",
    "Remind me to check the oven in ten minutes.",
])
def test_anything_else_is_left_to_claude(text):
    assert _actions(text) is None


def test_the_time_and_date_are_answered_without_claude(monkeypatch):
    class Evening(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 19, 19, 36)

    monkeypatch.setattr(intents_module, "datetime", Evening)
    assert LocalIntents().match("What time is it?") == {"response": "It's 7:36 PM.", "actions": [],
                                                        "success": True}
    assert LocalIntents().match("What's the date today?")["response"] == "It's Monday the 19th of October."


@pytest.mark.parametrize("timers, handled", [(1, True), (0, False), (2, False)])
def test_the_timer_is_only_cancelled_locally_when_theres_just_one(timers, handled):
    clock = SimpleNamespace(timer_count=lambda: timers)
    assert (_actions("Cancel the timer.", clock=clock) is not None) is handled


@pytest.mark.parametrize("text, day", [
    ("What's the weather?", "today"), ("What's the weather like tomorrow?", "tomorrow"),
    ("What will the weather be like today?", "today"), ("What's the forecast for tomorrow?", "tomorrow"),
])
def test_the_weather_is_asked_for_without_claude(text, day):
    assert _actions(text) == [Action("get_weather", {"day": day})]


@pytest.mark.parametrize("text, command", [
    ("Turn off the kitchen lights.", "turn off the kitchen lights"),
    ("Living room lights on.", "living room lights on"),
    ("Lights off, please.", "lights off"),
    ("Set the bedroom lamp to 30 percent.", "set the bedroom lamp to 30 percent"),
])
def test_the_lights_go_straight_to_home_assistant(text, command):
    assert _actions(text) == [Action("control_home", {"command": command})]


def test_the_lights_with_something_else_are_left_to_claude():
    # Sent whole, Home Assistant would take "the timer and the" for a light's name.
    assert _actions("Turn off the timer and the lights.") is None


@pytest.mark.parametrize("text", ["What have I got on Thursday?", "What's on my calendar for Thursday?",
                                  "Am I busy on Thursday?", "What do I have planned for Thursday?",
                                  "Do I have anything on Thursday?"])
def test_the_calendar_is_read_without_claude(text, monkeypatch):
    class Tuesday(intents_module.date):
        @classmethod
        def today(cls):
            return intents_module.date(2026, 10, 20)

    monkeypatch.setattr(intents_module, "date", Tuesday)
    assert _actions(text) == [Action("get_calendar", {"start": "2026-10-22", "days": 1})]


@pytest.mark.parametrize("day, today, expected", [
    (None, "2026-10-20", ("2026-10-20", 1)),
    ("tomorrow", "2026-10-20", ("2026-10-21", 1)),
    ("tuesday", "2026-10-20", ("2026-10-20", 1)),
    ("monday", "2026-10-20", ("2026-10-26", 1)),
    ("this weekend", "2026-10-21", ("2026-10-24", 2)),
    ("the weekend", "2026-10-24", ("2026-10-24", 2)),
    ("this weekend", "2026-10-25", ("2026-10-25", 1)),
])
def test_a_named_day_is_the_next_one(day, today, expected):
    from datetime import date
    first, days = calendar_days(day, date.fromisoformat(today))
    assert (first.isoformat(), days) == expected


@pytest.mark.parametrize("text, activity, start", [
    ("What time do I have work tomorrow?", "work", "2026-10-21"),
    ("When do I finish work today?", "work", "2026-10-20"),
    ("Am I working on Saturday?", "work", "2026-10-24"),
    ("What time is work?", "work", "2026-10-20"),
    ("What time do I have uni tomorrow?", "uni", "2026-10-21"),
    ("When do I finish uni today?", "uni", "2026-10-20"),
    ("Have I got lectures on Friday?", "uni", "2026-10-23"),
])
def test_work_and_uni_are_read_from_the_calendar_without_claude(text, activity, start, monkeypatch):
    class Tuesday(intents_module.date):
        @classmethod
        def today(cls):
            return intents_module.date(2026, 10, 20)

    monkeypatch.setattr(intents_module, "date", Tuesday)
    assert _actions(text) == [Action("get_activity", {"activity": activity, "start": start, "days": 1})]


@pytest.mark.parametrize("text, activity, count", [
    ("When do I next have work?", "work", 1),
    ("When's my next shift?", "work", 1),
    ("What are my next three shifts?", "work", 3),
    ("When is my next lecture?", "uni", 1),
])
def test_the_next_time_is_asked_for_without_claude(text, activity, count, monkeypatch):
    class Tuesday(intents_module.date):
        @classmethod
        def today(cls):
            return intents_module.date(2026, 10, 20)

    monkeypatch.setattr(intents_module, "date", Tuesday)
    assert _actions(text) == [Action("get_activity", {"activity": activity, "start": "2026-10-20", "count": count})]
