"""
Local Intents Test
Checks which everyday commands are handled without Claude, and - just as much -
which are left to it.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from voice_assistant import intents as intents_module
from voice_assistant.intents import LocalIntents


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
    assert _actions(text) == [{"type": "clock", "command": "set_timer", "duration_seconds": seconds,
                               "at": None, "label": None, "is_reminder": False}]


@pytest.mark.parametrize("text, command", [
    # A bare "stop" is the music too - anything ringing has already been dismissed by then.
    ("Pause.", "pause"), ("Pause the music.", "pause"), ("Stop the music.", "pause"), ("Stop.", "pause"),
    ("Skip.", "skip"), ("Next song.", "skip"), ("Skip this song.", "skip"),
    ("Previous song.", "previous"), ("Go back.", "previous"),
])
def test_playback_controls_are_handled_without_claude(text, command):
    assert _actions(text) == [{"type": "spotify", "command": command}]


@pytest.mark.parametrize("text", ["Resume.", "Play.", "Play the music.", "Unpause."])
def test_resuming_plays_whatever_was_playing(text):
    assert _actions(text) == [{"type": "spotify", "command": "play", "query": None, "search_type": None}]


@pytest.mark.parametrize("text, change", [
    ("Turn it up.", {"direction": "up"}), ("Turn it down.", {"direction": "down"}),
    ("Volume up.", {"direction": "up"}), ("Louder.", {"direction": "up"}), ("Quieter.", {"direction": "down"}),
    ("Volume 40.", {"level": 40}), ("Set the volume to 70 percent.", {"level": 70}),
])
def test_volume_is_changed_without_claude(text, change):
    action = {"type": "volume", "category": None, "level": None, "direction": None, "amount": None}
    action.update(change)
    assert _actions(text) == [action]


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
