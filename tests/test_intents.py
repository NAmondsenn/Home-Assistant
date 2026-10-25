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


# Stands in for Home Assistant, which the Assist shapes need to be there at all.
HOME = object()


def _actions(text, clock=None, home=HOME):
    """The actions a request is handled with locally, or None if it's left to Claude."""
    result = LocalIntents(clock=clock, home=home).match(text)
    return None if result is None else result["actions"]


@pytest.mark.parametrize("text, seconds", [
    ("Set a timer for ten minutes.", 600),
    ("set a timer for 10 minutes", 600),
    ("Timer for 30 seconds", 30),
    ("Set a 5 minute timer.", 300), ("Set of one minute timer,", 60),
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


@pytest.mark.parametrize("text, command, on, words, brightness", [
    ("Turn off the kitchen lights.", "turn off the kitchen lights", False, "the kitchen lights", None),
    ("Switch the lamp on.", "switch the lamp on", True, "the lamp", None),
    ("Living room lights on.", "living room lights on", True, "living room lights", None),
    ("Lights off, please.", "lights off", False, "lights", None),
    ("Set the bedroom lamp to 30 percent.", "set the bedroom lamp to 30 percent", True, "the bedroom lamp", 30),
    # Off, as Home Assistant takes 0% - so Nova's own lights don't switch it on at 1%.
    ("Set the garage light to 0 percent.", "set the garage light to 0 percent", False, "the garage light", 0),
])
def test_the_lights_go_straight_to_home_assistant(text, command, on, words, brightness):
    # With what Nova's own lights need, should the guard refuse the sentence.
    assert _actions(text) == [Action("control_home", {"command": command, "on": on, "words": words,
                                                      "brightness": brightness})]


@pytest.mark.parametrize("text, direction, words", [
    ("Dim the lights.", "down", "the lights"),
    ("Brighten the lamp.", "up", "the lamp"),
    ("Dim the bedroom lights.", "down", "the bedroom lights"),
    ("Dim the main light.", "down", "the main light"),
    ("brighten all the lights, please", "up", "all the lights"),
])
def test_dimming_is_done_without_claude(text, direction, words):
    assert _actions(text) == [Action("dim_lights", {"direction": direction, "words": words})]


@pytest.mark.parametrize("text", ["Dim the timer and the lights.", "Dim the screen.", "Dim the lights by half.",
                                  "Brighten my day.", "Brighten up the lights a bit.", "Dim."])
def test_dimming_leaves_what_isnt_a_light_to_claude(text):
    assert _actions(text) is None


def test_setting_a_light_to_a_level_still_goes_to_assist():
    assert _actions("Set the lamp to 30 percent.") == [Action("control_home", {
        "command": "set the lamp to 30 percent", "on": True, "words": "the lamp", "brightness": 30})]


def test_the_lights_with_something_else_are_left_to_claude():
    # Sent whole, Home Assistant would take "the timer and the" for a light's name.
    assert _actions("Turn off the timer and the lights.") is None


@pytest.mark.parametrize("text", [
    "Pause the TV.", "Turn off AdGuard protection.", "Switch the fan on.", "Next track on the TV.",
    "Set the TV volume to 40.", "Is the fan on?", "Run goodnight.", "Activate movie night.", "Close the blinds.",
    # The light rule doesn't take "is" for part of a light's name.
    "Is the lamp on?",
])
def test_assists_own_sentences_go_to_assist(text):
    # Sent whole, on the wider rule - see HomeAssistant.command.
    request = text.lower().rstrip(".?")
    assert _actions(text) == [Action("control_home", {"command": request, "wider": True})]


@pytest.mark.parametrize("text", [
    "Turn off the microphone.", "Turn off my alarm.", "Turn off the timer.", "Turn on shuffle.", "Turn on repeat.",
    "Turn it off.", "Pause it.", "Is it on?", "Turn off the music.", "Turn off notifications.",
    "Turn off the TV and the lamp.", "Turn up the heating.",
    # "My" isn't a name, so this isn't "the volume" of something called "my".
    "Set my volume to 40.",
    # Nova's own, by more of their words.
    "Pause playback.", "Pause Navidrome.", "Turn off the chime.", "Turn off the wake word.", "Turn off quiet hours.",
    "Turn on battery alerts.", "Turn off the warnings.",
    # Words that only point back, to something the conversation so far says.
    "Turn the other one off.", "Turn the same one on.", "Turn both off.", "Switch the rest off.", "Run again.",
    "Run the last one.", "Open the first one.",
    # Two things at once, however they're joined.
    "Turn off the TV plus the fan.", "Turn off the TV then the fan.",
    # When, rather than what - a delay is Claude's.
    "Pause the TV in five minutes.", "Pause the TV for a bit.", "Turn off the TV in ten minutes.",
    "Turn off the oven at six.", "Turn on the heating until ten.", "Turn off the light in five minutes.",
    "Turn off the TV after the film.", "Run the dishwasher tonight.", "Turn on the heating by 7.",
    # For the user, not a thing of Home Assistant's.
    "Run me a bath.", "Open us a window.",
    # The shapes with nothing above yet for what they mustn't catch.
    "Previous track on Spotify.", "Run the timer.", "Activate it.", "Open it.", "Close that.",
])
def test_what_the_assist_rule_mustnt_catch_is_left_alone(text):
    # 0003: local rules stay strict. Nova's own things, words Assist can't know the
    # meaning of, a delay, or two things at once, are Claude's.
    assert _actions(text) is None


@pytest.mark.parametrize("text", [
    "Pause the TV.", "Is the post office open?", "Is Tesco open?", "Is the pharmacy closed?", "Is the football on?",
    "Run through my day.", "Open YouTube.", "Open the settings.", "Turn on the news.",
])
def test_without_home_assistant_nothing_is_taken_for_assist(text):
    # Otherwise "is the post office open?" would get "Home Assistant isn't set up yet",
    # and never reach Claude.
    assert _actions(text, home=None) is None


@pytest.mark.parametrize("text, action", [
    ("Pause.", Action("control_playback", {"action": "pause"})),
    ("Next track.", Action("control_playback", {"action": "skip"})),
    ("Set the volume to 40.", Action("set_volume", {"level": 40})),
])
def test_novas_own_commands_still_come_before_assist(text, action):
    assert _actions(text) == [action]


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


# Notifications

class FakeNtfy:
    """Stands in for ntfy, saying whether an alert was just said out loud."""

    def __init__(self, just_said=False):
        self.just = just_said

    def just_said(self):
        return self.just


@pytest.mark.parametrize("said, tool", [
    ("Any notifications?", "read_notifications"),
    ("Have I got any new notifications?", "read_notifications"),
    ("What are my notifications?", "read_notifications"),
    ("Send that to my phone.", "send_to_phone"),
    ("Can you send it to my phone please?", "send_to_phone"),
    ("Text me that.", "send_to_phone"),
])
def test_notification_commands_are_handled_on_the_pi(said, tool):
    assert [action.tool for action in LocalIntents(ntfy=FakeNtfy()).match(said)["actions"]] == [tool]


def test_tell_me_more_is_only_about_an_alert_just_said():
    assert LocalIntents(ntfy=FakeNtfy(just_said=True)).match("Tell me more.")["actions"][0].tool == "tell_me_more"
    # Otherwise it's a follow-up to whatever was being talked about, which is Claude's.
    assert LocalIntents(ntfy=FakeNtfy(just_said=False)).match("Tell me more.") is None


def test_without_ntfy_notification_commands_go_to_claude():
    for said in ("Any notifications?", "Send that to my phone.", "Tell me more."):
        assert LocalIntents().match(said) is None


@pytest.mark.parametrize("text, params", [
    ("How's my Mac's battery?", {"device": "mac"}),
    ("what's my phone on", {"device": "phone"}),
    ("What's the tablet's battery?", {"device": "tablet"}),
    ("Battery levels.", {}),
    ("How are my batteries?", {}),
])
def test_battery_questions_are_answered_locally(text, params):
    assert _actions(text) == [Action("get_batteries", params)]


@pytest.mark.parametrize("text", [
    "What's on my phone",
    "What's on tonight",
    "Play battery by Metallica",
    "Set an alarm for 7",
    "How's my battery",
    "My phone's battery is flat",
    "What's the battery of the car",
    "Battery levels and the weather",
    "Turn off my phone",
])
def test_what_the_battery_rules_mustnt_catch_is_left_alone(text):
    # 0003: local rules stay strict. "What's on tonight" is the calendar's, and the rest isn't a battery question.
    actions = _actions(text)
    assert not actions or all(action.tool != "get_batteries" for action in actions)
