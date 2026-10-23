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

    def factory(on_announce=None, on_unanswered=None, on_change=None, can_hear=None, **timers_config):
        # One announcement per round and no pauses, so ringing never sleeps in a test.
        settings = {"announcements": 1, "repeat_gap": 0, **timers_config}
        announced = []

        def announce(message, sound, boost, **kwargs):
            announced.append({"message": message, "sound": sound, "boost": boost, **kwargs})
            if on_announce:
                on_announce(made, announced[-1])

        made = Clock(on_timer_finished=announce, config=make_config(timers=settings), start=False,
                     on_unanswered=on_unanswered, on_change=on_change, can_hear=can_hear)
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
    (0, 0, "midnight"), (12, 0, "noon"),
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
    # As Nova says them itself.
    assert _time_of_day_candidates("cancel the midnight one") == {(0, 0)}
    assert _time_of_day_candidates("the noon reminder") == _time_of_day_candidates("midday") == {(12, 0)}


def test_midnight_is_named_by_the_night_it_ends(make_clock):
    # "Midnight on Wednesday", for the moment Wednesday begins, is as often taken
    # to mean the end of it.
    clock = make_clock()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tonight, tomorrow_night = today + timedelta(days=1), today + timedelta(days=2)
    assert clock._spoken_due(tonight.timestamp()) == "midnight tonight"
    assert clock._spoken_due(tomorrow_night.timestamp()) == \
        f"midnight on {(today + timedelta(days=1)).strftime('%A')} night"


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


def test_whats_ringing_is_heard_and_dismissed_only_where_it_rings(make_clock):
    # With a second satellite, the kitchen saying "stop" dismissed the bedroom's alarm,
    # and talking to it at all silenced the bedroom's timer - neither of which could
    # be heard there.
    clock = make_clock()
    clock.set_timer(60, satellite="bedroom")
    clock.set_timer(120, label="pizza", satellite="kitchen")
    clock._tick(time.time() + 121)
    assert clock.is_ringing() and clock.is_ringing("bedroom") and clock.is_ringing("kitchen")
    assert not clock.is_ringing("hall")

    kitchen = clock.ringing_state("kitchen")
    assert kitchen["insistent_entries"] == [] and kitchen["reminder"] == "pizza"
    assert [entry["timer"]["satellite"] for entry in clock.ringing_state("bedroom")["insistent_entries"]] == ["bedroom"]

    assert clock.dismiss(satellite="kitchen")["success"]
    assert not clock.is_ringing("kitchen") and clock.is_ringing("bedroom")
    # Anywhere, when nothing says where.
    clock.dismiss()
    assert not clock.is_ringing()


def test_dismissing_only_clears_what_was_missed_where_it_was_dismissed(make_clock):
    clock = make_clock()
    clock.set_timer(60, satellite="bedroom")
    clock.set_timer(120, satellite="kitchen")
    now = time.time()
    clock._tick(now + 121)
    # As if both had rung out, with nobody there.
    for entry in clock._ringing:
        entry["give_up_at"] = 0
    clock._tick(now + 1000)

    assert clock.dismiss(satellite="kitchen")["success"]
    assert [timer["satellite"] for timer in clock._unacknowledged] == ["bedroom"]


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
    # announced the same "while I was off" on every start. Then they were forgotten as
    # soon as they'd been read, so a crash loop lost them before they were said at all.
    due = time.time() - 60
    (tmp_path / "timers.json").write_text(json.dumps(
        {"next_id": 2, "timers": [{"id": 1, "due_at": due, "duration": 600, "label": None}]}))

    # Kept through restarts until it's said...
    assert len(make_clock()._missed) == 1
    clock = make_clock()
    assert len(clock._missed) == 1
    clock._tick(time.time())
    assert len(clock.announced) == 1

    # ...and never said again.
    again = make_clock()
    again._tick(time.time())
    assert again._missed == [] and again.announced == []


def test_a_brain_lost_while_saying_what_it_missed_doesnt_say_it_again(make_clock, tmp_path):
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 2, "timers": [
        {"id": 1, "due_at": time.time() - 60, "duration": 600, "label": None}]}))
    restarted = []
    # Restarted the moment it's said - so it must have been let go of before.
    clock = make_clock(on_announce=lambda clock, announcement: restarted.append(make_clock()))

    clock._tick(time.time())

    assert len(clock.announced) == 1
    assert restarted[0]._missed == []


def test_whats_missed_and_then_settled_by_its_satellite_doesnt_come_back(make_clock, tmp_path):
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 1, "due_at": now - 60, "duration": 600, "label": None, "satellite": "bedroom"},
        {"id": 2, "due_at": now - 90, "duration": 900, "label": None, "satellite": "bedroom"}]}))
    clock = make_clock()

    clock.apply_away("bedroom", rang=[f"timer-1@{int(now - 60)}"], dismissed=[f"timer-2@{int(now - 90)}"])

    restarted = make_clock()
    restarted._tick(time.time())
    assert restarted._missed == [] and restarted.announced == []


def test_three_restarts_in_a_row_note_whatever_was_missed_once(make_clock, tmp_path):
    # A timer and two alarms came due while the brain was off, and another timer was
    # ringing as it went - and it restarts three times before any satellite is back.
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 1, "due_at": now - 60, "duration": 600, "label": None, "satellite": "bedroom"}], "ringing": [
        {"id": 2, "due_at": now - 30, "duration": 300, "label": None, "satellite": "bedroom"}]}))
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 3, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": "daily", "due_at": now - 120, "satellite": "bedroom"},
        {"id": 2, "hour": 8, "minute": 0, "repeat": None, "due_at": now - 180, "satellite": "bedroom"}]}))

    for _ in range(3):
        clock = make_clock()

    assert sorted(clock._occurrence(item) for item in clock._missed) == sorted([
        f"timer-1@{int(now - 60)}", f"timer-2@{int(now - 30)}", f"alarm-1@{int(now - 120)}",
        f"alarm-2@{int(now - 180)}"])
    # The repeating alarm is set for its next occurrence, the once, and the one-off is gone.
    assert list(clock._alarms) == [1] and now < clock._alarms[1]["due_at"] <= now + 25 * 3600
    assert clock._timers == {}
    clock._tick(time.time())
    assert len(clock.announced) == 4


def test_a_repeating_alarm_missed_by_a_brain_lost_as_it_started_is_noted_and_moved_on_once(make_clock,
                                                                                         tmp_path):
    # Lost having noted the alarm as missed in the timers file, but before it moved it
    # on in the alarms file - so the next brain finds it in both.
    now = time.time()
    alarms = json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": "daily", "due_at": now - 120, "satellite": "bedroom"}]})
    (tmp_path / "alarms.json").write_text(alarms)
    make_clock()
    (tmp_path / "alarms.json").write_text(alarms)

    for _ in range(2):
        clock = make_clock()

    assert [clock._occurrence(item) for item in clock._missed] == [f"alarm-1@{int(now - 120)}"]
    assert now < clock._alarms[1]["due_at"] <= now + 25 * 3600
    clock._tick(time.time())
    assert len(clock.announced) == 1
    # Said, and moved on in the alarms file too, so it's never noted again.
    again = make_clock()
    again._tick(time.time())
    assert again._missed == [] and again.announced == []


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
    ("Soap.", True), ("So.", True), ("So what's the weather?", False),
])
def test_looks_like_dismissal(text, dismissal):
    assert Clock.looks_like_dismissal(text) is dismissal


# Answers to "shall I turn that reminder off?" - True to turn it off, False to leave
# it, None for anything which isn't a clear answer.
@pytest.mark.parametrize("text, answer", [
    ("Yes.", True), ("yes please", True), ("Yeah, go on.", True), ("Yep.", True), ("Sure.", True),
    ("Okay.", True), ("Turn it off.", True), ("Alright, thanks.", True), ("Go on then.", True),
    # A plain "yes" wins over a "don't" or "not" elsewhere in the answer.
    ("Yes, I don't need it any more.", True), ("Yeah, I'm not going to forget now.", True),
    ("Why not?", True), ("Yes, don't bother keeping it going.", True),
    ("No.", False), ("Nope.", False), ("Nah, keep it.", False), ("No thanks.", False),
    ("Leave it.", False), ("Not now.", False), ("Later.", False), ("Don't turn it off.", False),
    ("Okay, leave it.", False), ("No, turn it off later.", False), ("Keep it going.", False),
    ("Never mind.", False), ("No, I'm still doing it.", False),
    # Both at once, or neither, isn't an answer - it's handled as a fresh request.
    ("Yeah, no.", None), ("What's the weather?", None), ("Play some music.", None), ("", None),
])
def test_answer_to_offer(text, answer):
    assert Clock.answer_to_offer(text) is answer


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


def test_a_reminder_nobody_answers_is_passed_on_once_it_stops_ringing(make_clock):
    # So it can be sent to the user's phone - see the brain's _unanswered.
    unanswered = []
    clock = make_clock(on_unanswered=lambda timer, kind, satellite: unanswered.append((timer["label"], kind)))
    clock.set_timer(60, label="take the bins out")
    now = time.time()
    clock._tick(now + 61)
    assert clock.is_ringing() and unanswered == []

    # As if it had rung for the five minutes a reminder's allowed.
    clock._ringing[0]["give_up_at"] = 0
    clock._tick(now + 1000)

    assert not clock.is_ringing()
    assert unanswered == [("take the bins out", "clock")]


def test_a_dismissed_reminder_isnt_passed_on(make_clock):
    unanswered = []
    clock = make_clock(on_unanswered=lambda timer, kind, satellite: unanswered.append(timer))
    clock.set_timer(60, label="take the bins out")
    clock._tick(time.time() + 61)
    clock.dismiss()
    assert unanswered == []


def test_a_reminder_which_rings_out_is_taken_out_of_its_satellites_copy(make_clock):
    # It used to stop ringing without on_change hearing, so the satellite's copy kept
    # it - and a satellite which lost the brain in the next few minutes rang it again.
    copies = []
    clock = make_clock(on_change=lambda: copies.append(clock.copy_for("bedroom")))
    clock.set_timer(60, label="take the bins out", satellite="bedroom")
    now = time.time()
    clock._tick(now + 61)
    assert [entry["kind"] for entry in copies[-1]] == ["timer"]

    # As if it had rung for the five minutes a reminder's allowed.
    clock._ringing[0]["give_up_at"] = 0
    clock._tick(now + 1000)
    assert not clock.is_ringing()
    assert copies[-1] == []


# The settings page

def test_whats_set_is_listed_soonest_first_by_the_names_nova_uses(make_clock):
    clock = make_clock()
    clock.set_timer(600, label="take the bins out")
    clock.set_timer(60)
    clock.set_alarm(7, 0, repeat="weekdays")
    overview = clock.overview()

    assert [timer["name"] for timer in overview["timers"]] == ["1 minute timer", "take the bins out reminder"]
    assert overview["alarms"][0]["name"] == "7:00 AM alarm, every weekday"


def test_one_can_be_removed_by_its_id(make_clock):
    clock = make_clock()
    clock.set_timer(600, label="take the bins out")
    clock.set_alarm(7, 0)
    overview = clock.overview()

    assert clock.remove("timer", overview["timers"][0]["id"])["message"] == "Removed the take the bins out reminder."
    assert clock.remove("alarm", overview["alarms"][0]["id"])["success"]
    assert clock.overview() == {"timers": [], "alarms": []}
    # Twice is once too many.
    assert clock.remove("alarm", overview["alarms"][0]["id"])["success"] is False


# Satellites

def test_a_timer_rings_on_the_satellite_it_was_set_from(make_clock):
    unanswered = []
    clock = make_clock(on_unanswered=lambda timer, kind, satellite: unanswered.append(satellite))
    clock.set_timer(60, satellite="bedroom")
    now = time.time()
    clock._tick(now + 61)
    assert clock.announced[0]["satellite"] == "bedroom"

    # As if it had rung for the ten minutes a timer's allowed, with nobody there.
    clock._ringing[0]["give_up_at"] = 0
    clock._tick(now + 1000)
    assert unanswered == ["bedroom"]


def test_an_alarm_rings_on_the_satellite_it_was_set_from(make_clock):
    clock = make_clock(on_announce=_stop_on_first_sound)
    clock.set_alarm(7, 0, satellite="bedroom")
    _make_due(clock)
    clock._tick(time.time())
    assert [announcement["satellite"] for announcement in clock.announced] == ["bedroom"]


def test_a_reminder_named_afterwards_rings_where_it_was_asked_for(make_clock):
    clock = make_clock()
    clock.set_timer(1200, is_reminder=True, satellite="bedroom")
    clock.name_pending_reminder("walk the dogs")
    assert [timer["satellite"] for timer in clock._timers.values()] == ["bedroom"]


def test_the_copy_holds_each_occurrence_within_a_week(make_clock, monkeypatch):
    # A Wednesday noon in June, well away from the clocks changing, so the week
    # ahead is seven whole days.
    wednesday = datetime(2026, 6, 10, 12, 0).timestamp()
    monkeypatch.setattr(clock_module.time, "time", lambda: wednesday)
    clock = make_clock()
    clock.set_alarm(7, 0, "weekdays", satellite="bedroom")
    clock.set_timer(600, satellite="bedroom")
    clock.set_timer(1200, label="pizza", satellite="bedroom")
    # One rings somewhere else, and the other after the week's out.
    clock.set_timer(900, satellite="kitchen")
    clock.set_timer(8 * 86400, satellite="bedroom")

    copy = clock.copy_for("bedroom")
    alarms = [entry for entry in copy if entry["kind"] == "alarm"]
    assert [(datetime.fromtimestamp(alarm["due"]).weekday(), datetime.fromtimestamp(alarm["due"]).hour)
            for alarm in alarms] == [(3, 7), (4, 7), (0, 7), (1, 7), (2, 7)]
    assert all(alarm["id"] == f"alarm-1@{int(alarm['due'])}" and alarm["sound"] == "alarm_sound.wav"
               for alarm in alarms)
    # Timer 1 isn't alarm 1, though they're numbered the same.
    assert [entry for entry in copy if entry["kind"] == "timer"] == [
        {"id": f"timer-1@{int(wednesday) + 600}", "due": wednesday + 600, "kind": "timer", "sound": "timer_beep.wav"},
        {"id": f"timer-2@{int(wednesday) + 1200}", "due": wednesday + 1200, "kind": "timer",
         "sound": "announcement_sound.wav"}]
    assert [entry["due"] for entry in copy] == sorted(entry["due"] for entry in copy)
    assert [entry["due"] for entry in clock.copy_for("kitchen")] == [wednesday + 900]


def test_what_was_dismissed_while_away_doesnt_ring_again(make_clock):
    clock = make_clock()
    clock.set_timer(60, label="pizza", satellite="bedroom")
    clock.set_timer(120, label="bins", satellite="bedroom")
    clock.set_alarm(7, 0, "daily", satellite="bedroom")
    copy = clock.copy_for("bedroom")
    pizza, bins = [entry["id"] for entry in copy if entry["kind"] == "timer"]
    first, second = [entry for entry in copy if entry["kind"] == "alarm"][:2]

    # Another satellite's report has nothing to do with these.
    clock.apply_away("kitchen", rang=[pizza], dismissed=[pizza])
    assert len(clock._timers) == 2

    # The pizza timer and the alarm were dismissed there. The bins reminder only
    # rang, with nobody there to answer it, so it's left to ring here.
    clock.apply_away("bedroom", rang=[pizza, bins, first["id"]], dismissed=[pizza, first["id"]])

    assert [timer["label"] for timer in clock._timers.values()] == ["bins"]
    assert clock._alarms[1]["due_at"] == second["due"]
    clock._tick(time.time() + 121)
    assert {announcement["message"] for announcement in clock.announced} == {"This is a reminder: bins"}


def test_what_was_dismissed_while_away_stops_ringing_here_too(make_clock):
    # The brain rings them as well while the satellite can't be reached, so they're
    # still going when it comes back and says they were dismissed.
    clock = make_clock()
    clock.alarm_ring_duration = 0
    clock.set_timer(60, satellite="bedroom")
    clock.set_alarm(7, 0, "daily", satellite="bedroom")
    _make_due(clock)
    rang = [entry["id"] for entry in clock.copy_for("bedroom")][:2]
    clock._tick(time.time() + 61)
    assert len(clock._ringing) == 2

    clock.apply_away("bedroom", rang=rang, dismissed=rang)
    assert not clock.is_ringing()


def test_changes_are_told_to_on_change(make_clock, tmp_path):
    told = []

    def on_change():
        # Only once the change is saved, and with the lock let go - the brain asks
        # straight back for each satellite's copy.
        saved = json.loads((tmp_path / "timers.json").read_text())
        told.append((len(saved["timers"]), clock._lock.locked()))

    clock = make_clock(on_change=on_change)
    clock.set_timer(60, satellite="bedroom")
    assert told == [(1, False)]

    clock.set_timer(600, satellite="bedroom")
    clock.set_alarm(7, 0, satellite="bedroom")
    clock.remove("timer", 2)
    clock.cancel_alarm("7am")
    clock._tick(time.time() + 61)
    clock.dismiss()
    clock.set_timer(600)
    clock.assign_unowned("bedroom")
    clock.apply_away("bedroom", rang=[], dismissed=[clock.copy_for("bedroom")[0]["id"]])
    assert len(told) == 10

    # Nothing changed, so there's nothing to tell.
    clock.set_alarm(25, 0)
    clock.cancel_timer()
    clock.dismiss()
    clock.assign_unowned("bedroom")
    clock.apply_away("bedroom", rang=[], dismissed=["timer-1@0"])
    assert len(told) == 10
    assert not any(locked for _, locked in told)


def test_entries_from_before_satellites_are_given_to_the_default(make_clock, tmp_path):
    # Saved before there were satellites, one of them without an id at all.
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 2, "due_at": time.time() + 600, "duration": 600, "label": None},
        {"due_at": time.time() + 1200, "duration": 1200, "label": "pizza"}]}))
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": "daily", "due_at": _at(7, 0, days_ahead=1)}]}))
    clock = make_clock()
    # The id it was given is saved, so its copy's ids still mean it after a restart.
    assert [timer.get("id") for timer in json.loads((tmp_path / "timers.json").read_text())["timers"]] == [2, 3]

    clock.set_timer(1800, satellite="kitchen")
    assert clock.assign_unowned("pi") == 3
    assert clock.assign_unowned("pi") == 0

    restarted = make_clock()
    assert [timer["name"] for timer in restarted.overview("pi")["timers"]] == ["10 minute timer", "pizza reminder"]
    assert [timer["name"] for timer in restarted.overview("kitchen")["timers"]] == ["30 minute timer"]
    assert len(restarted.overview("pi")["alarms"]) == 1 and restarted.overview("kitchen")["alarms"] == []
    assert sorted(timer["id"] for timer in restarted.overview()["timers"]) == [2, 3, 4]


def test_an_occurrence_id_survives_the_alarm_moving_on(make_clock, monkeypatch):
    # A minute before a daily 7am alarm, on a June morning well away from the clocks changing.
    before = datetime(2026, 6, 10, 6, 59).timestamp()
    monkeypatch.setattr(clock_module.time, "time", lambda: before)
    clock = make_clock()
    clock.alarm_ring_duration = 0
    clock.set_alarm(7, 0, "daily", satellite="bedroom")
    first, second, third = [entry["id"] for entry in clock.copy_for("bedroom")][:3]

    # It rings, and is moved on to tomorrow straight away, but the copy still
    # names both by the same ids.
    clock._tick(before + 60)
    assert [entry["id"] for entry in clock.copy_for("bedroom")][:2] == [first, second]

    # So the satellite dismissing today's stops what's ringing, and dismissing
    # tomorrow's moves the alarm on to the day after.
    clock.apply_away("bedroom", rang=[first], dismissed=[first])
    assert not clock.is_ringing()
    clock.apply_away("bedroom", rang=[second], dismissed=[second])
    assert clock.copy_for("bedroom")[0]["id"] == third


def test_what_rings_now_stays_in_the_copy(make_clock):
    # The brain sends a fresh copy the moment something starts ringing. If the
    # link drops part way through, the satellite carries on from that copy.
    copies = []
    clock = make_clock(on_change=lambda: copies.append(clock.copy_for("bedroom")))
    clock.set_timer(60, label="pizza", satellite="bedroom")
    occurrence = copies[-1][0]["id"]
    now = time.time()
    clock._tick(now + 61)
    assert copies[-1] == [{"id": occurrence, "due": now + 61, "kind": "timer", "sound": "announcement_sound.wav"}]

    clock.dismiss()
    assert copies[-1] == []


def test_a_dismissal_reported_late_stops_the_ringing_and_the_reminder(make_clock):
    # Both rang on the satellite and were dismissed there, while the brain, unable
    # to reach it, rang them too - and one has rung out here, unanswered, by the
    # time the satellite reports back.
    unanswered = []
    clock = make_clock(on_unanswered=lambda timer, kind, satellite: unanswered.append(timer["label"]))
    clock.set_timer(60, label="take the bins out", satellite="bedroom")
    clock.set_timer(120, label="feed the cat", satellite="bedroom")
    bins, cat = [entry["id"] for entry in clock.copy_for("bedroom")]
    now = time.time()
    clock._tick(now + 121)
    next(entry for entry in clock._ringing if entry["timer"]["label"] == "feed the cat")["give_up_at"] = 0
    clock._tick(now + 200)
    assert unanswered == ["feed the cat"]

    clock.apply_away("bedroom", rang=[bins, cat], dismissed=[bins, cat])

    assert not clock.is_ringing()
    clock._tick(now + 10000)
    # The bins reminder never rang out, so it never went to the phone.
    assert unanswered == ["feed the cat"]
    assert clock.missed()["message"] == "You haven't missed anything."


def test_what_rang_while_away_isnt_announced_as_missed(make_clock, tmp_path):
    # The brain was off while all three came due. The satellite rang two of them
    # from its copy - which had stopped by the time it was back, unanswered - so only
    # the third is worth a "While I was off".
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 1, "due_at": now - 60, "duration": 600, "label": None, "satellite": "bedroom"},
        {"id": 2, "due_at": now - 90, "duration": 900, "label": None, "satellite": "bedroom"}]}))
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": "daily", "due_at": now - 120, "satellite": "bedroom"}]}))
    clock = make_clock()
    assert len(clock._missed) == 3

    clock.apply_away("bedroom", rang=[f"timer-1@{int(now - 60)}", f"alarm-1@{int(now - 120)}"], dismissed=[],
                     ringing=[])
    clock._tick(time.time())
    assert len(clock.announced) == 1
    assert clock.announced[0]["message"].startswith("While I was off: your 15 minutes timer.")
    assert not clock.is_ringing()


def test_what_was_still_ringing_while_away_rings_on_here(make_clock, tmp_path):
    # The brain restarted as they came due, so missed them, while the satellite rang
    # them from its copy - and they were still ringing as it reached the brain again.
    # It stops ringing them then, for the brain to take over, which only rang what it
    # was ringing itself: nothing, so the alarm went quiet half a minute in.
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 1, "due_at": now - 30, "duration": 600, "label": None, "satellite": "bedroom"},
        {"id": 2, "due_at": now - 40, "duration": 900, "label": None, "satellite": "bedroom"}]}))
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": None, "due_at": now - 30, "satellite": "bedroom"}]}))
    clock = make_clock()
    clock.alarm_ring_duration = 0
    timer, dismissed, alarm = f"timer-1@{int(now - 30)}", f"timer-2@{int(now - 40)}", f"alarm-1@{int(now - 30)}"

    # Another satellite's report has nothing to do with them.
    clock.apply_away("kitchen", rang=[timer], dismissed=[], ringing=[timer])
    assert not clock.is_ringing()
    # One dismissed there isn't rung here, whatever else is said of it.
    clock.apply_away("bedroom", rang=[timer, dismissed, alarm], dismissed=[dismissed],
                     ringing=[timer, dismissed, alarm])

    assert clock._missed == []
    assert sorted(entry["timer"]["id"] for entry in clock._ringing) == [1, 1]
    messages = [announcement["message"] for announcement in clock.announced]
    assert messages[0] == "Timer for 10 minutes." and messages[1].endswith("Time to get up.")
    assert len(messages) == 2 and {announcement["satellite"] for announcement in clock.announced} == {"bedroom"}
    # Nor is either said to have been missed.
    clock._tick(time.time())
    assert len(clock.announced) == 2
    # Both are in the satellite's copy, for if it loses the brain again.
    assert {timer, alarm} <= {entry["id"] for entry in clock.copy_for("bedroom")}


def test_whats_missed_waits_until_its_satellite_can_hear_it(make_clock, tmp_path):
    # The brain was off as both came due, and neither satellite is back to say what it
    # rang from its copy meanwhile - so neither can be told yet what it missed.
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 1, "due_at": now - 60, "duration": 600, "label": None, "satellite": "bedroom"},
        {"id": 2, "due_at": now - 90, "duration": 900, "label": None, "satellite": "kitchen"}]}))
    back = set()
    clock = make_clock(can_hear=lambda satellite: satellite in back)
    clock._tick(time.time())
    assert clock.announced == []

    back.add("bedroom")
    clock._tick(time.time())
    clock._tick(time.time())
    assert [entry["satellite"] for entry in clock.announced] == ["bedroom"]
    # The kitchen's is still kept for it.
    clock.apply_away("kitchen", rang=[f"timer-2@{int(now - 90)}"], dismissed=[])
    back.add("kitchen")
    clock._tick(time.time())
    assert len(clock.announced) == 1


# A satellite's report, applied twice - as one is when the brain has saved it, but the
# connection drops before the satellite hears so, and it says it all again.

def test_a_timer_dismissed_while_away_and_told_twice_is_just_gone(make_clock):
    clock = make_clock()
    clock.set_timer(60, label="pizza", satellite="bedroom")
    clock.set_timer(120, label="bins", satellite="bedroom")
    pizza = clock.copy_for("bedroom")[0]["id"]

    for _ in range(2):
        clock.apply_away("bedroom", rang=[pizza], dismissed=[pizza])

    assert [timer["label"] for timer in clock._timers.values()] == ["bins"]


def test_a_one_off_alarm_dismissed_while_away_and_told_twice_is_just_gone(make_clock):
    clock = make_clock()
    clock.set_alarm(7, 0, satellite="bedroom")
    clock.set_alarm(8, 0, satellite="bedroom")
    seven = next(entry["id"] for entry in clock.copy_for("bedroom") if entry["id"].startswith("alarm-1@"))

    for _ in range(2):
        clock.apply_away("bedroom", rang=[seven], dismissed=[seven])

    assert [alarm["hour"] for alarm in clock._alarms.values()] == [8]


def test_a_repeating_alarm_dismissed_while_away_and_told_twice_moves_on_once(make_clock):
    clock = make_clock()
    clock.set_alarm(7, 0, "daily", satellite="bedroom")
    first, second = [entry["id"] for entry in clock.copy_for("bedroom")][:2]

    for _ in range(2):
        clock.apply_away("bedroom", rang=[first], dismissed=[first])

    assert clock.copy_for("bedroom")[0]["id"] == second


def test_what_rang_while_away_and_is_told_twice_still_isnt_announced_as_missed(make_clock, tmp_path):
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 1, "due_at": now - 60, "duration": 600, "label": None, "satellite": "bedroom"},
        {"id": 2, "due_at": now - 90, "duration": 900, "label": None, "satellite": "bedroom"}]}))
    clock = make_clock()

    for _ in range(2):
        clock.apply_away("bedroom", rang=[f"timer-1@{int(now - 60)}"], dismissed=[], ringing=[])
    clock._tick(time.time())

    assert len(clock.announced) == 1
    assert clock.announced[0]["message"].startswith("While I was off: your 15 minutes timer.")


def _still_ringing_as_the_brain_came_back(make_clock, tmp_path):
    """
    A clock restarted as an alarm came due, which its satellite was still ringing as it
    reached the brain again.

    Returns:
        The clock, and the id the satellite knows the alarm by.
    """
    now = time.time()
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": None, "due_at": now - 30, "satellite": "bedroom"}]}))
    clock = make_clock()
    clock.alarm_ring_duration = 0
    return clock, f"alarm-1@{int(now - 30)}"


def test_what_was_still_ringing_while_away_and_is_told_twice_rings_once(make_clock, tmp_path):
    clock, alarm = _still_ringing_as_the_brain_came_back(make_clock, tmp_path)

    for _ in range(2):
        clock.apply_away("bedroom", rang=[alarm], dismissed=[], ringing=[alarm])

    assert len(clock._ringing) == 1
    assert len(clock.announced) == 1 and clock.announced[0]["message"].endswith("Time to get up.")


def test_what_was_still_ringing_while_away_rings_once_on_a_brain_restarted_before_saying_it_has_it(
        make_clock, tmp_path):
    # Saved as ringing by the first, so the restarted brain rings it - once.
    clock, alarm = _still_ringing_as_the_brain_came_back(make_clock, tmp_path)
    clock.apply_away("bedroom", rang=[alarm], dismissed=[], ringing=[alarm])

    restarted = make_clock()
    restarted.alarm_ring_duration = 0
    restarted.apply_away("bedroom", rang=[alarm], dismissed=[], ringing=[alarm])
    restarted._tick(time.time())

    assert len(restarted._ringing) == 1
    assert len(restarted.announced) == 1 and restarted.announced[0]["message"].endswith("Time to get up.")


def test_a_clock_made_without_its_thread_can_be_started_later(make_clock):
    # The brain makes its clock without, so it can give what rings on no satellite to
    # the default before anything rings.
    clock = make_clock()
    clock.check_interval = 0.01
    clock.set_timer(60)
    for timer in clock._timers.values():
        timer["due_at"] = time.time() - 1
    clock.start()
    try:
        deadline = time.monotonic() + 5
        while not clock.announced and time.monotonic() < deadline:
            time.sleep(0.01)
        assert clock.announced[0]["message"] == "Timer for 1 minute."
    finally:
        clock.stop()


def test_an_alarm_saved_without_an_id_gets_one(make_clock, tmp_path):
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 4, "alarms": [
        {"id": 3, "hour": 7, "minute": 0, "repeat": "daily", "due_at": _at(7, 0, days_ahead=1)},
        {"hour": 8, "minute": 30, "repeat": None, "due_at": _at(8, 30, days_ahead=1)}]}))
    make_clock()

    # Saved with it straight away, so it keeps it.
    saved = json.loads((tmp_path / "alarms.json").read_text())
    assert [alarm["id"] for alarm in saved["alarms"]] == [3, 4] and saved["next_id"] == 5
    assert sorted(make_clock()._alarms) == [3, 4]


def test_silencing_while_ringing_saves_and_tells_on_change(make_clock, tmp_path):
    # Cancelling every timer while the only one rang left it in the file as
    # ringing, so a restart said it had been missed - and nothing was told.
    told = []
    clock = make_clock(on_change=lambda: told.append(True))
    _ring_a_timer(clock)
    told.clear()
    assert clock.cancel_timer(cancel_all=True)["message"] == "Okay."
    assert told == [True]
    assert json.loads((tmp_path / "timers.json").read_text())["ringing"] == []

    _ring_an_alarm(clock)
    told.clear()
    assert clock.cancel_alarm(cancel_all=True)["message"] == "Okay."
    assert told == [True]


def test_whats_ringing_or_missed_is_given_to_the_default_too(make_clock, tmp_path):
    clock = make_clock()
    _ring_a_timer(clock)
    assert clock.assign_unowned("pi") == 1
    assert [entry["kind"] for entry in clock.copy_for("pi")] == ["timer"]

    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 2, "timers": [
        {"id": 1, "due_at": time.time() - 60, "duration": 600, "label": None}]}))
    restarted = make_clock()
    assert restarted.assign_unowned("pi") == 1
    # Still given, through a restart before it's said.
    again = make_clock()
    again._tick(time.time())
    assert again.announced[0]["satellite"] == "pi"


def _ring_alarm_then_restart(make_clock, repeat):
    """
    An alarm for the bedroom satellite, ringing on the brain's clock as it restarts.

    Returns:
        The restarted clock, and the id the satellite knows the ringing occurrence by.
    """
    clock = make_clock()
    # One round only, so it's still ringing when the tick returns.
    clock.alarm_ring_duration = 0
    clock.set_alarm(7, 0, repeat, satellite="bedroom")
    _make_due(clock)
    occurrence = clock.copy_for("bedroom")[0]["id"]
    clock._tick(time.time())
    assert clock.is_ringing("bedroom")
    return make_clock(), occurrence


@pytest.mark.parametrize("repeat", [None, "daily"])
def test_an_alarm_ringing_when_the_brain_restarts_rings_again_if_still_ringing_there(make_clock, repeat):
    restarted, occurrence = _ring_alarm_then_restart(make_clock, repeat)
    restarted.alarm_ring_duration = 0

    restarted.apply_away("bedroom", rang=[occurrence], dismissed=[], ringing=[occurrence])

    assert restarted._missed == []
    assert restarted.is_ringing("bedroom")
    assert restarted.announced[0]["satellite"] == "bedroom"
    assert restarted.announced[0]["message"].endswith("Time to get up.")
    # Nothing said of it as missed afterwards.
    restarted._tick(time.time())
    assert len(restarted.announced) == 1


@pytest.mark.parametrize("repeat", [None, "daily"])
def test_an_alarm_ringing_when_the_brain_restarts_stays_quiet_if_dismissed_there(make_clock, repeat):
    restarted, occurrence = _ring_alarm_then_restart(make_clock, repeat)

    restarted.apply_away("bedroom", rang=[occurrence], dismissed=[occurrence], ringing=[])
    restarted._tick(time.time())

    assert restarted._missed == []
    assert not restarted.is_ringing()
    assert restarted.announced == []


@pytest.mark.parametrize("repeat", [None, "daily"])
def test_an_alarm_ringing_when_the_brain_restarts_is_mentioned_if_the_satellite_says_nothing(make_clock, repeat):
    # As a timer is, when the satellite restarted too.
    restarted, occurrence = _ring_alarm_then_restart(make_clock, repeat)

    restarted._tick(time.time())

    assert len(restarted.announced) == 1
    assert restarted.announced[0]["message"].startswith("While I was off: your 7:00 AM alarm")
    assert restarted.announced[0]["satellite"] == "bedroom"


def test_a_repeating_alarm_ringing_when_the_brain_restarts_is_still_set_once(make_clock):
    restarted, occurrence = _ring_alarm_then_restart(make_clock, "daily")
    restarted.alarm_ring_duration = 0
    restarted.apply_away("bedroom", rang=[occurrence], dismissed=[], ringing=[occurrence])

    assert restarted.list_alarms()["message"] == "One alarm: 7:00 AM alarm, every day."
    tomorrow = [entry["id"] for entry in restarted.copy_for("bedroom") if entry["due"] > time.time()]
    assert len(tomorrow) == len(set(tomorrow)) and len(tomorrow) >= 1
    assert occurrence not in tomorrow


def test_a_one_off_alarm_ringing_when_the_brain_restarts_isnt_set_again(make_clock):
    restarted, occurrence = _ring_alarm_then_restart(make_clock, None)
    restarted.alarm_ring_duration = 0
    restarted.apply_away("bedroom", rang=[occurrence], dismissed=[], ringing=[occurrence])

    assert restarted.list_alarms()["message"] == "You don't have any alarms set."


def test_dismissing_a_ringing_alarm_means_a_restart_doesnt_report_it(make_clock):
    clock = make_clock()
    clock.alarm_ring_duration = 0
    clock.set_alarm(7, 0, None)
    _make_due(clock)
    clock._tick(time.time())
    assert clock.is_ringing()
    clock.dismiss()

    assert make_clock()._missed == []
