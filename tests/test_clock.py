"""
Clock Test
Tests timers, reminders and alarms without the background thread or any audio.

The Clock is created with start=False and stepped along by calling _tick() with a
chosen time, and its saved files are redirected into a temporary folder, so nothing
here touches the real data/ folder or waits on a real clock.
"""

import json
import time
from datetime import datetime, timedelta

import pytest

from voice_assistant import clock as clock_module
from voice_assistant.clock import Clock, format_duration, spoken_clock_time, _time_of_day_candidates


@pytest.fixture
def make_clock(tmp_path, monkeypatch, make_config):
    """
    Builds Clocks that save into a temporary folder and record their announcements
    instead of speaking them. Calling it again builds a fresh Clock over the same
    files, which is how a restart is simulated.
    """
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(tmp_path / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(tmp_path / "alarms.json"))

    def factory(on_announce=None, **timers_config):
        # One announcement per round and no pauses, so ringing never sleeps in a test.
        settings = {"announcements": 1, "repeat_gap": 0, **timers_config}
        announced = []

        def announce(message, sound, boost, **kwargs):
            announced.append({"message": message, "sound": sound, "boost": boost, **kwargs})
            if on_announce:
                on_announce(made, announced[-1])

        made = Clock(on_timer_finished=announce, config=make_config(timers=settings), start=False)
        made.announced = announced
        return made

    return factory


def _at(hour, minute, days_ahead=0):
    """Epoch time for a local clock time, today or a number of days ahead."""
    moment = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (moment + timedelta(days=days_ahead)).timestamp()


# Formatting

@pytest.mark.parametrize("seconds, spoken", [
    (0, "no time"), (1, "1 second"), (90, "1 minute and 30 seconds"),
    (3600, "1 hour"), (3725, "1 hour and 2 minutes"), (90000, "1 day and 1 hour"),
])
def test_format_duration(seconds, spoken):
    assert format_duration(seconds) == spoken


@pytest.mark.parametrize("hour, minute, spoken", [
    (0, 5, "12:05 AM"), (7, 0, "7:00 AM"), (12, 30, "12:30 PM"), (23, 59, "11:59 PM"),
])
def test_spoken_clock_time(hour, minute, spoken):
    assert spoken_clock_time(hour, minute) == spoken


def test_time_of_day_candidates():
    assert _time_of_day_candidates("the 5:40 pm one") == {(17, 40)}
    # Without am / pm both readings are possible.
    assert _time_of_day_candidates("the 5:40 one") == {(5, 40), (17, 40)}
    assert _time_of_day_candidates("cancel the 7am alarm") == {(7, 0)}
    # A bare number is a duration, not a time.
    assert _time_of_day_candidates("the 10 minute timer") == set()


# Timers and reminders

def test_set_and_list_timer(make_clock):
    clock = make_clock()
    result = clock.set_timer(600)
    assert result["success"] and result["message"] == "Timer set for 10 minutes."
    assert clock.list_timers()["message"].startswith("One timer: 10 minute timer for ")


def test_reminder_without_a_name_is_asked_for(make_clock):
    clock = make_clock()
    result = clock.set_timer(1200, is_reminder=True)
    assert not result["success"]
    assert result["message"] == "What would you like to name this reminder?"
    assert clock.name_pending_reminder("walk the dogs")["success"]
    assert clock.describe_all()[0].startswith("walk the dogs for ")


def test_cancel_by_label_and_by_length(make_clock):
    clock = make_clock()
    clock.set_timer(600)
    clock.set_timer(5400, label="pizza")
    assert clock.cancel_timer("the pizza reminder")["success"]
    assert clock.cancel_timer("the 10 minute timer")["success"]
    assert clock.list_timers()["message"] == "You don't have any timers running."


def test_cancel_by_length_doesnt_match_a_longer_timer(make_clock):
    clock = make_clock()
    clock.set_timer(1800)
    clock.set_timer(5400 + 60)
    clock.cancel_timer("the 30 minute timer")
    remaining = clock.describe_all()
    assert len(remaining) == 1 and remaining[0].startswith("1 hour and 31 minute timer")


def test_ambiguous_cancel_asks_which(make_clock):
    clock = make_clock()
    clock.set_timer(600)
    clock.set_timer(1200)
    result = clock.cancel_timer()
    assert not result["success"] and result["message"].endswith("Which one?")
    assert len(clock.describe_all()) == 2


def test_timers_survive_a_restart(make_clock):
    clock = make_clock()
    clock.set_timer(600, label="pizza")
    restarted = make_clock()
    assert restarted.describe_all()[0].startswith("pizza for ")


def test_a_timer_rings_when_due_and_dismisses(make_clock):
    clock = make_clock()
    clock.set_timer(60)
    clock._tick(time.time() + 61)
    assert clock.is_ringing()
    assert clock.announced[0]["message"] == "Timer for 1 minute."
    assert clock.dismiss()["success"]
    assert not clock.is_ringing()


def test_timers_are_held_until_the_system_clock_is_set(make_clock):
    clock = make_clock()
    clock.set_timer(60)
    clock._tick(clock_module.PLAUSIBLE_TIME_AFTER - 1)
    assert not clock.is_ringing() and clock.announced == []


# Saying "stop" and friends

@pytest.mark.parametrize("text, dismissal", [
    ("stop", True), ("okay thanks", True), ("Alright, alright.", True), ("yes I heard it", True),
    ("turn off the lights", False), ("no music please", False), ("play Spotify", False),
    ("um", False), ("", False),
])
def test_looks_like_dismissal(text, dismissal):
    assert Clock.looks_like_dismissal(text) is dismissal


# Alarms

def test_alarm_next_occurrence_respects_weekdays(make_clock):
    saturday = datetime(2026, 10, 17, 12, 0).timestamp()
    due = datetime.fromtimestamp(make_clock()._next_occurrence(7, 0, "weekdays", after=saturday))
    assert (due.weekday(), due.hour, due.minute) == (0, 7, 0)


def test_set_alarm_and_describe(make_clock):
    clock = make_clock()
    assert clock.set_alarm(7, 0, "weekdays")["message"] == "7:00 AM alarm, every weekday."
    assert clock.list_alarms()["message"] == "One alarm: 7:00 AM alarm, every weekday."


def test_alarms_are_saved(make_clock, tmp_path):
    clock = make_clock()
    clock.set_alarm(6, 30, "daily")
    saved = json.loads((tmp_path / "alarms.json").read_text())
    assert saved["alarms"][0]["hour"] == 6 and saved["alarms"][0]["repeat"] == "daily"
