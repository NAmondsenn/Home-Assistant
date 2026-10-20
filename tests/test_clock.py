"""
Clock Test
Tests timers, reminders and alarms without the background thread or any audio.

The Clock is created with start=False and stepped along by calling _tick() with a
chosen time, and its saved files are redirected into a temporary folder, so nothing
here touches the real data/ folder or waits on a real clock.
"""

import json
import threading
import time
from datetime import datetime, timedelta

import pytest

from voice_assistant import clock as clock_module
from voice_assistant.clock import (Clock, format_duration, parse_repeat, seconds_until, spoken_clock_time,
                                   _time_of_day_candidates)


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


# Counting down to a clock time

@pytest.fixture
def london(monkeypatch):
    """Runs a test on UK time, whatever the machine's own time zone."""
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("at, now, seconds", [
    ("16:00", datetime(2026, 10, 19, 14, 30), 5400),
    # Already gone today, so it's tomorrow's.
    ("09:00", datetime(2026, 10, 19, 14, 30), 66600),
    ("2026-11-04 09:00", datetime(2026, 11, 3, 9, 0), 86400),
    # The clocks go back an hour overnight, so it's 25 hours away, not 24.
    ("2026-10-25 12:00", datetime(2026, 10, 24, 12, 0), 90000),
    ("2026-10-18 09:00", datetime(2026, 10, 19, 9, 0), -86400),
])
def test_seconds_until_a_clock_time(london, at, now, seconds):
    # The model used to do this arithmetic itself, from a time given only to the
    # minute and with no idea when the clocks change.
    assert seconds_until(at, now=now) == seconds


def test_a_reminder_can_be_set_for_a_clock_time(make_clock):
    clock = make_clock()
    target = (datetime.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
    result = clock.set_timer(label="call mum", at=target.strftime("%H:%M"))

    assert result["success"]
    assert abs(next(iter(clock._timers.values()))["due_at"] - target.timestamp()) < 1


@pytest.mark.parametrize("at, message", [
    ("teatime", "Sorry, I didn't catch what time for."),
    ("2020-01-01 09:00", "That time has already passed."),
])
def test_a_time_which_cant_be_used_is_refused(make_clock, at, message):
    assert make_clock().set_timer(label="call mum", at=at)["message"] == message


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


def test_cancelling_something_that_doesnt_exist_asks_about_the_only_timer(make_clock):
    # "Cancel the pizza reminder" with only a 10 minute timer running used to
    # delete the timer without asking.
    clock = make_clock()
    clock.set_timer(600)
    result = clock.cancel_timer(label="pizza")

    assert result["message"].startswith("You only have the 10 minute timer")
    assert result["message"].endswith("Do you want me to cancel that?")
    assert len(clock._timers) == 1


def test_cancelling_an_alarm_that_doesnt_exist_asks_about_the_only_one(make_clock):
    clock = make_clock()
    clock.set_alarm(7, 0)
    result = clock.cancel_alarm(label="9pm")

    assert result["message"] == "You only have the 7:00 AM alarm. Do you want me to cancel that?"
    assert len(clock._alarms) == 1


def test_two_plain_timers_due_in_the_same_minute_are_refused(make_clock, monkeypatch):
    # Set at the start of a minute, since at 59 seconds past, 600 and 601 seconds
    # from now fall in different minutes - which failed this one time in sixty.
    start_of_minute = time.time() // 60 * 60
    monkeypatch.setattr(clock_module.time, "time", lambda: start_of_minute)
    clock = make_clock()
    assert clock.set_timer(600)["success"]
    assert not clock.set_timer(601)["success"]


def test_a_reminder_isnt_refused_because_of_a_timer_due_the_same_minute(make_clock):
    # A reminder is told apart by its name, so "remind me to take the pizza out in
    # 10 minutes" shouldn't be refused because of a 10 minute timer.
    clock = make_clock()
    assert clock.set_timer(600)["success"]
    assert clock.set_timer(600, label="take the pizza out")["success"]
    assert clock.set_timer(600, label="feed the cat")["success"]
    assert not clock.set_timer(600, label="Feed the cat")["success"]


@pytest.mark.parametrize("first, second, clash", [
    ("weekdays", "weekends", False),
    ("weekdays", "daily", True),
    (["monday"], ["tuesday"], False),
])
def test_alarms_at_the_same_time_only_clash_on_the_same_day(make_clock, first, second, clash):
    clock = make_clock()
    assert clock.set_alarm(7, 0, repeat=first)["success"]
    assert clock.set_alarm(7, 0, repeat=second)["success"] is not clash


def test_a_one_off_alarm_only_clashes_with_a_repeating_one_on_its_days(make_clock):
    # A one-off 7am on a Saturday is fine alongside a weekday 7am.
    clock = make_clock()
    clock.set_alarm(7, 0, repeat="weekdays")
    weekday = datetime.fromtimestamp(clock._next_occurrence(7, 0, None)).weekday()
    assert clock.set_alarm(7, 0)["success"] is (weekday >= 5)


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


def test_talking_only_silences_what_was_ringing_at_the_wake_word(make_clock):
    # A timer which comes due while the assistant is replying hasn't been heard yet,
    # since announcements wait for the conversation to end - so it mustn't be
    # silenced along with the one the user was answering.
    clock = make_clock()
    clock.set_timer(60)
    clock.set_timer(180)
    now = time.time()
    clock._tick(now + 61)
    heard = clock.ringing_state()["insistent_entries"]
    clock._tick(now + 181)

    assert clock.silence_insistent(heard)
    assert [entry["timer"]["duration"] for entry in clock._ringing] == [180]


def test_a_missed_timer_is_only_announced_once_however_often_it_restarts(make_clock, tmp_path):
    # The file kept missed timers until something else changed, so a crash loop
    # announced the same "while I was off" on every start.
    due = time.time() - 60
    (tmp_path / "timers.json").write_text(json.dumps(
        {"next_id": 2, "timers": [{"id": 1, "due_at": due, "duration": 600, "label": None}]}))

    assert len(make_clock()._missed) == 1
    assert make_clock()._missed == []


def test_a_timer_ringing_when_the_assistant_stops_is_reported_as_missed(make_clock, tmp_path):
    # It had already been taken out of the saved file when it started ringing, so a
    # restart mid-ring lost it without a word.
    clock = make_clock()
    clock.set_timer(60)
    clock._tick(time.time() + 61)
    saved = json.loads((tmp_path / "timers.json").read_text())
    assert [timer["duration"] for timer in saved["ringing"]] == [60]

    # Restarting a minute after it went off.
    saved["ringing"][0]["due_at"] = time.time() - 60
    (tmp_path / "timers.json").write_text(json.dumps(saved))
    assert [timer["duration"] for timer in make_clock()._missed] == [60]


def test_one_ringing_item_doesnt_hold_up_another(make_clock):
    # The clock used to play each round itself, so while one item was ringing -
    # an alarm cycle lasts two and a half minutes - nothing else was even checked.
    first_finished = threading.Event()
    seen = {}

    def on_announce(clock, announcement):
        if "first" in announcement["message"]:
            time.sleep(0.5)
            first_finished.set()
        else:
            seen["first_still_ringing"] = not first_finished.is_set()

    clock = make_clock(on_announce=on_announce)
    clock._rounds_on_threads = True
    clock.set_timer(60, label="first")
    clock.set_timer(120, label="second")
    now = time.time()
    clock._tick(now + 61)
    clock._tick(now + 121)

    first_finished.wait(2)
    assert seen == {"first_still_ringing": True}


def test_timers_are_held_until_the_system_clock_is_set(make_clock):
    clock = make_clock()
    clock.set_timer(60)
    clock._tick(clock_module.PLAUSIBLE_TIME_AFTER - 1)
    assert not clock.is_ringing() and clock.announced == []


def test_timer_announcement_can_tell_once_its_been_dismissed(make_clock):
    clock = make_clock()
    clock.set_timer(60)
    clock._tick(time.time() + 61)
    still_wanted = clock.announced[0]["still_wanted"]
    assert still_wanted()
    clock.dismiss()
    # What the assistant checks after waiting for a conversation to finish.
    assert not still_wanted()


def test_alarm_announcement_can_tell_once_its_been_dismissed(make_clock):
    checks = []

    def dismiss_after_first_sound(clock, announcement):
        checks.append(announcement["still_wanted"]())
        clock.dismiss()
        checks.append(announcement["still_wanted"]())

    clock = make_clock(on_announce=dismiss_after_first_sound)
    clock.set_alarm(7, 0)
    for alarm in clock._alarms.values():
        alarm["due_at"] = time.time() - 5
    clock._tick(time.time())
    assert checks == [True, False]
    # Nothing more was played after the dismissal.
    assert len(clock.announced) == 1


def test_an_alarm_rings_in_bursts_which_carry_on_through_its_sound(make_clock):
    # It used to play without a break, at full volume from the first second, which
    # right by the microphone could drown out "Hey Nova, stop".
    def dismiss_after_four(clock, announcement):
        if len(clock.announced) == 4:
            clock.dismiss()

    clock = make_clock(on_announce=dismiss_after_four, alarm_burst=3, alarm_burst_gap=0)
    clock.set_alarm(7, 0)
    for alarm in clock._alarms.values():
        alarm["due_at"] = time.time() - 5
    clock._tick(time.time())

    bursts = clock.announced
    assert bursts[0]["message"].endswith("Time to get up.")
    assert [b["message"] for b in bursts[1:]] == [None, None, None]
    assert [(b["start"], b["length"]) for b in bursts] == [(0.0, 3.0), (3.0, 3.0), (6.0, 3.0), (9.0, 3.0)]
    assert all(b["boost"] for b in bursts)
    assert bursts[0]["level"] == pytest.approx(clock_module.ALARM_START_LEVEL)


@pytest.mark.parametrize("elapsed, level", [(0, 0.3), (15, 0.65), (30, 1.0), (90, 1.0)])
def test_an_alarm_builds_up_to_full_volume(make_clock, elapsed, level):
    assert make_clock(alarm_ramp=30)._alarm_level(elapsed) == pytest.approx(level)


def _ring_a_timer(clock, seconds=60, label=None):
    """Sets a timer and steps the clock past it, leaving it ringing."""
    clock.set_timer(seconds, label=label)
    clock._tick(time.time() + seconds + 1)
    assert clock.is_ringing()


def _ring_an_alarm(clock, repeat=None):
    """Sets a 7am alarm and makes it go off now, leaving it ringing."""
    clock.alarm_ring_duration = 0
    clock.set_alarm(7, 0, repeat)
    for alarm in clock._alarms.values():
        alarm["due_at"] = time.time() - 5
    clock._tick(time.time())
    assert clock.is_ringing()


def test_cancelling_an_alarm_while_a_timer_rings_cancels_the_alarm(make_clock):
    clock = make_clock()
    clock.set_alarm(7, 0)
    _ring_a_timer(clock)
    result = clock.cancel_alarm("7am")
    assert result["success"] and result["message"] == "7:00 AM alarm cancelled."
    assert clock.list_alarms()["message"] == "You don't have any alarms set."
    # The timer it had nothing to do with is still going.
    assert clock.is_ringing()


def test_cancelling_a_named_reminder_while_another_timer_rings(make_clock):
    clock = make_clock()
    clock.set_timer(3600, label="pizza")
    _ring_a_timer(clock)
    assert clock.cancel_timer("the pizza reminder")["message"].startswith("Reminder for ")
    assert clock.list_timers()["message"] == "You don't have any timers running."
    assert clock.is_ringing()


def test_cancel_with_no_name_still_dismisses_whats_ringing(make_clock):
    clock = make_clock()
    clock.set_timer(3600, label="pizza")
    _ring_a_timer(clock)
    assert clock.cancel_timer()["message"] == "Okay."
    assert not clock.is_ringing()
    assert len(clock.describe_all()) == 1


def test_cancelling_the_ringing_timer_by_name_dismisses_it(make_clock):
    clock = make_clock()
    _ring_a_timer(clock, label="pasta")
    assert clock.cancel_timer("pasta")["message"] == "Okay."
    assert not clock.is_ringing()


def test_cancel_all_timers_while_one_rings_stops_everything(make_clock):
    clock = make_clock()
    clock.set_timer(3600, label="pizza")
    _ring_a_timer(clock)
    assert clock.cancel_timer(cancel_all=True)["success"]
    assert not clock.is_ringing()
    assert clock.describe_all() == []


def test_dismissing_a_ringing_repeating_alarm_keeps_tomorrows(make_clock):
    clock = make_clock()
    _ring_an_alarm(clock, "daily")
    assert clock.cancel_alarm()["message"] == "Okay."
    assert not clock.is_ringing()
    assert clock.list_alarms()["message"] == "One alarm: 7:00 AM alarm, every day."


def test_stopping_a_ringing_alarm_repeating_removes_the_schedule(make_clock):
    clock = make_clock()
    _ring_an_alarm(clock, "daily")
    assert clock.cancel_alarm(scope="all")["message"] == "7:00 AM alarm, every day cancelled."
    assert not clock.is_ringing()
    assert clock.list_alarms()["message"] == "You don't have any alarms set."


# Saying "stop" and friends

@pytest.mark.parametrize("text, dismissal", [
    ("stop", True), ("okay thanks", True), ("Alright, alright.", True), ("yes I heard it", True),
    ("turn off the lights", False), ("no music please", False), ("play Spotify", False),
    ("um", False), ("", False),
    # "Stop" over a ringing timer, as Whisper heard it on the Pi.
    ("It's a little bus stop.", True), ("Okay, that's enough.", True),
    ("Don't stop.", False), ("Stop the music and play something else instead", False),
    # How Moonshine heard "stop" said on its own.
    ("Soap.", True),
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


@pytest.mark.parametrize("given, stored", [
    (None, None), ("", None),
    ("daily", "daily"), ("every day", "daily"), ("everyday", "daily"),
    ("weekdays", "weekdays"), ("weekends", "weekends"),
    ("mon, wed", ["monday", "wednesday"]),
    ("mondays and wednesdays", ["monday", "wednesday"]),
    ("Tues & Thurs", ["tuesday", "thursday"]),
    (["friday", "monday"], ["monday", "friday"]),
    (["monday", "tuesday", "wednesday", "thursday", "friday"], "weekdays"),
    (["saturday", "sunday"], "weekends"),
    ("every saturday", ["saturday"]),
])
def test_parse_repeat(given, stored):
    assert parse_repeat(given) == stored


@pytest.mark.parametrize("given", ["funday", "custom", "mon, someday", 7])
def test_parse_repeat_rejects_what_it_cant_read(given):
    with pytest.raises(ValueError):
        parse_repeat(given)


def test_set_alarm_reads_spoken_repeat_patterns(make_clock):
    clock = make_clock()
    assert clock.set_alarm(7, 0, "every day")["message"] == "7:00 AM alarm, every day."
    assert clock.set_alarm(6, 30, "mon, wed")["message"] == "6:30 AM alarm, every Monday and Wednesday."
    assert clock._alarms[2]["repeat"] == ["monday", "wednesday"]


def test_set_alarm_asks_when_it_cant_read_the_days(make_clock):
    clock = make_clock()
    result = clock.set_alarm(7, 0, "custom")
    assert not result["success"] and result["message"].endswith("?")
    assert clock.list_alarms()["message"] == "You don't have any alarms set."


def test_custom_days_alarm_skips_other_days(make_clock):
    tuesday = datetime(2026, 10, 20, 12, 0).timestamp()
    due = datetime.fromtimestamp(make_clock()._next_occurrence(7, 0, ["monday", "wednesday"], after=tuesday))
    assert due.strftime("%A") == "Wednesday"


def test_alarm_saved_with_an_old_style_repeat_still_repeats(make_clock, tmp_path):
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": ["every day"], "due_at": time.time() + 3600}]}))
    assert make_clock().list_alarms()["message"] == "One alarm: 7:00 AM alarm, every day."


def _stop_on_first_sound(clock, announcement):
    """An announcer which acts like the user saying "stop" as soon as anything sounds."""
    clock.dismiss()


def _make_due(clock, seconds_ago=5):
    """Moves every alarm's due time into the recent past, as if it had just come round."""
    for alarm in clock._alarms.values():
        alarm["due_at"] = time.time() - seconds_ago
    with clock._lock:
        clock._save_alarms()


def test_repeating_alarm_survives_a_restart_while_ringing(make_clock, tmp_path):
    saved_while_ringing = []

    def restart_mid_ring(clock, announcement):
        # Reads the file while it's ringing - exactly what a restart at this moment would load.
        saved_while_ringing.append(json.loads((tmp_path / "alarms.json").read_text())["alarms"])
        clock.dismiss()

    clock = make_clock(on_announce=restart_mid_ring)
    clock.set_alarm(7, 0, "daily")
    _make_due(clock)
    clock._tick(time.time())

    # Already moved on to tomorrow in the file, before anything was dismissed or rang out.
    saved = saved_while_ringing[0]
    assert len(saved) == 1 and saved[0]["due_at"] > time.time()

    restarted = make_clock()
    assert restarted.list_alarms()["message"] == "One alarm: 7:00 AM alarm, every day."


def test_one_off_alarm_is_removed_once_it_goes_off(make_clock):
    clock = make_clock(on_announce=_stop_on_first_sound)
    clock.set_alarm(7, 0)
    _make_due(clock)
    clock._tick(time.time())
    assert clock.list_alarms()["message"] == "You don't have any alarms set."


def test_one_off_alarm_missed_while_off_is_reported_not_moved_to_tomorrow(make_clock):
    clock = make_clock()
    clock.set_alarm(7, 0)
    _make_due(clock, seconds_ago=600)

    restarted = make_clock()
    assert restarted.list_alarms()["message"] == "You don't have any alarms set."
    restarted._tick(time.time())
    assert restarted.announced[0]["message"] == "While I was off: your 7:00 AM alarm. That was 10 minutes ago."

    # Only mentioned the once, even after another restart.
    again = make_clock()
    again._tick(time.time())
    assert again.announced == []


def test_repeating_alarm_missed_while_off_carries_on(make_clock):
    clock = make_clock()
    clock.set_alarm(7, 0, "daily")
    _make_due(clock, seconds_ago=600)

    restarted = make_clock()
    assert next(iter(restarted._alarms.values()))["due_at"] > time.time()
    restarted._tick(time.time())
    assert restarted.announced[0]["message"].startswith("While I was off: your 7:00 AM alarm, every day.")


def test_unanswered_alarm_shows_up_as_missed(make_clock):
    clock = make_clock()
    clock.alarm_ring_duration = 0
    clock.alarm_max_cycles = 1
    clock.set_alarm(7, 0)
    _make_due(clock)
    clock._tick(time.time())
    assert not clock.is_ringing()
    assert clock.missed()["message"].startswith("You missed 7:00 AM alarm, ")
