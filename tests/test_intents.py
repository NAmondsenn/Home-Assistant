"""
Local Intents Test
Checks which everyday commands are handled without Claude, and - just as much -
which are left to it.
"""

import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from voice_assistant import intents as intents_module
from voice_assistant.actions import Action
from voice_assistant.intents import LocalIntents, calendar_days


# Stands in for Home Assistant, which the Assist shapes need to be there at all.
HOME = object()


# The activities' words, as GoogleCalendar.activity_words gives them for a Work and a Uni calendar.
WORK_AND_UNI = {"work": "work", "works": "work", "working": "work", "shift": "work", "shifts": "work",
                "uni": "uni", "unis": "uni", "university": "uni", "lecture": "uni", "lectures": "uni",
                "class": "uni", "classes": "uni"}


def _actions(text, clock=None, home=HOME, activities=lambda: WORK_AND_UNI):
    """The actions a request is handled with locally, or None if it's left to Claude."""
    result = LocalIntents(clock=clock, home=home, activities=activities).match(text)
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
    # How Nemotron, hearing it again said twice, heard Piper's "Pause." on its own.
    ("Cause.", "pause"), ("Paws.", "pause"),
])
def test_playback_controls_are_handled_without_claude(text, command):
    assert _actions(text) == [Action("control_playback", {"action": command})]


def test_a_number_is_only_read_from_plain_digits():
    # "²" is a digit to str.isdigit, but not a number int() reads.
    assert intents_module._number("12") == 12 and intents_module._number("²") is None


@pytest.mark.parametrize("text, command", [
    # "Nova, pause." said in one breath, as Nemotron heard it.
    ("Novopause.", "pause"), ("Novapause.", "pause"), ("Novaskip.", "skip"), ("Novopause, please.", "pause"),
    # With the rest of the wake word before it.
    ("Hey Novopause.", "pause"), ("Hey, Novaskip.", "skip"),
])
def test_the_wake_word_run_into_a_command_is_the_command(text, command):
    assert _actions(text) == [Action("control_playback", {"action": command})]


@pytest.mark.parametrize("text", [
    # Words of their own which start the same way - Claude's, as ever.
    "Novel.", "Novacane.", "Novas.", "Nova.", "Novo.",
    # Only a lone word is tried, so a request run on from it is Claude's to work out.
    "Novopause the music.", "Hey Novopause the music.", "Hey Novel.", "Hey hey Novopause.",
])
def test_words_starting_like_the_wake_word_arent_taken_for_it(text):
    assert _actions(text) is None


def test_its_the_wake_word_set_which_is_run_into_a_command():
    jarvis = LocalIntents(wake_word="Hey Jarvis")
    assert jarvis.match("Jarvispause.")["actions"] == [Action("control_playback", {"action": "pause"})]
    assert jarvis.match("Hey Jarvispause.")["actions"] == [Action("control_playback", {"action": "pause"})]
    assert jarvis.match("Novopause.") is None and jarvis.match("Hey Novopause.") is None


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
    # With the word that often goes with each, which isn't a light's name.
    ("Brighten up the lights.", "up", "the lights"), ("Dim down the bedroom lamp.", "down", "the bedroom lamp"),
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
    "Set the TV volume to 40.", "Run goodnight.", "Activate movie night.", "Close the blinds.",
    # "In" and a number are only a delay together - "in five minutes", "at six".
    "Turn off the TV in the kitchen.", "Turn on fan 2.",
    # "One" is as often a name's number, and "second" an order, as either points back or
    # says when.
    "Turn on fan one.", "Turn on BBC One.", "Activate the second scene.", "Turn the second fan off.",
])
def test_assists_own_sentences_go_to_assist(text):
    # Sent whole, on the wider rule - see HomeAssistant.command.
    request = text.lower().rstrip(".?")
    assert _actions(text) == [Action("control_home", {"command": request, "wider": True})]


@pytest.mark.parametrize("text", [
    "Is the fan on?", "Is the garage door open?", "Is the patio closed?",
    # The light rule doesn't take "is" for part of a light's name.
    "Is the lamp on?",
])
def test_a_question_for_assist_says_its_a_question(text):
    # So a refused one goes to Claude, which can read it with home_status, rather than
    # being refused - see ActionExecutor.control_home.
    request = text.lower().rstrip(".?")
    assert _actions(text) == [Action("control_home", {"command": request, "wider": True, "question": True})]


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
    "Run the last one.", "Open the first one.", "Turn one off.", "Turn off the second one.", "Open the third one.",
    "Turn on the one in the kitchen.", "Turn off the fourth one.", "Turn on another one.", "Run the previous one.",
    "Turn off the second.",
    # Two things at once, however they're joined.
    "Turn off the TV plus the fan.", "Turn off the TV then the fan.",
    # When, rather than what - a delay is Claude's.
    "Pause the TV in five minutes.", "Pause the TV for a bit.", "Turn off the TV in ten minutes.",
    "Turn off the oven at six.", "Turn on the heating until ten.", "Turn off the light in five minutes.",
    "Run the dishwasher tonight.", "Turn on the heating by 7.", "Turn on the oven for half an hour.",
    "Pause the TV in a second.", "Turn off the fan in 30 seconds.", "Pause the TV a second.",
    "Pause the TV one second.", "Pause the TV in second.", "Turn off the fan for second.",
    # For the user, not a thing of Home Assistant's.
    "Run me a bath.", "Open us a window.",
    # The shapes with nothing above yet for what they mustn't catch.
    "Previous track on Spotify.", "Run the timer.", "Activate it.", "Open it.", "Close that.",
])
def test_what_the_assist_rule_mustnt_catch_is_left_alone(text):
    # 0003: local rules stay strict. Nova's own things, words Assist can't know the
    # meaning of, a delay, or two things at once, are Claude's.
    assert _actions(text) is None


def test_a_question_isnt_taken_for_a_light_sentence():
    # "Are the lights" isn't a light's name, which Nova's own lights would switch on
    # if the guard refused the sentence.
    assert _actions("Are the lights on?") is None


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


def test_without_the_calendar_work_and_uni_are_left_to_claude():
    for text in ("What time do I have work tomorrow?", "When's my next shift?", "Am I working on Saturday?"):
        assert _actions(text, activities=None) is None
        assert _actions(text, activities=dict) is None


# Calendars named like Nova's own things - Claude, which has both, is asked about them.
NOVAS_WORDS = {"timer": "timer", "timers": "timer", "music": "music", "musics": "music", "shopping": "shopping",
               "shoppings": "shopping", "notifications": "notifications", "phone": "phone", "phones": "phone"}


@pytest.mark.parametrize("text, action", [
    ("Set a timer for ten minutes.", Action("set_timer", {"duration_seconds": 600})),
    ("Pause the music.", Action("control_playback", {"action": "pause"})),
    ("Add milk to my shopping list.", Action("add_to_list", {"item": "milk", "list": "my shopping list",
                                                             "command": "add milk to my shopping list"})),
    ("What's on my shopping list?", Action("read_list", {"list": "my shopping list"})),
    ("Have I got any notifications?", Action("read_notifications")),
    ("Where's my phone?", Action("find_device", {"device": "phone", "ring": False})),
])
def test_a_calendar_named_like_novas_own_command_never_takes_it_over(text, action):
    result = LocalIntents(home=HOME, ntfy=FakeNtfy(), activities=lambda: NOVAS_WORDS).match(text)
    assert result["actions"] == [action]


@pytest.mark.parametrize("text", ["When's my next timer?", "What time do I have music tomorrow?",
                                  "Have I got notifications today?", "Am I at phone tomorrow?"])
def test_a_calendar_named_as_one_of_novas_own_things_is_claudes(text):
    assert _actions(text, activities=lambda: NOVAS_WORDS) is None
    # Shopping isn't one of Nova's own things, only a list's name.
    assert _actions("When's shopping tomorrow?", activities=lambda: NOVAS_WORDS)[0].params["activity"] == "shopping"


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


def test_the_phone_is_found_and_rung_without_claude():
    assert _actions("Where's my phone?") == [Action("find_device", {"device": "phone", "ring": False})]
    assert _actions("Where is the tablet?") == [Action("find_device", {"device": "tablet", "ring": False})]
    assert _actions("Find my phone.") == [Action("find_device", {"device": "phone", "ring": True})]
    assert _actions("Ring my tablet.") == [Action("find_device", {"device": "tablet", "ring": True})]
    assert _actions("Ring the Mac, please.") == [Action("find_device", {"device": "mac", "ring": True})]
    # Still the battery, and anything less plain is Claude's.
    assert _actions("What's my phone on?") == [Action("get_batteries", {"device": "phone"})]
    assert _actions("Where's my phone charger?") is None
    assert _actions("Find my phone and turn off the lamp.") is None
    # Without Home Assistant it's still Nova's, which says so, rather than Claude's.
    assert _actions("Where's my phone?", home=None) == [Action("find_device", {"device": "phone", "ring": False})]


@pytest.mark.parametrize("text", [
    "Find my phone number",
    "Ring my mum",
    "Ring my phone in five minutes",
    "Where's the Mac's charger?",
    "Where's my phone case",
    "Where is my tablet's charger",
    "Find my keys",
    "Where's the TV remote?",
    "Find the laptop bag",
    "Ring the doorbell",
])
def test_what_the_find_device_rules_mustnt_catch_is_left_alone(text):
    # 0003: local rules stay strict - only the device itself, said plainly.
    actions = _actions(text)
    assert not actions or all(action.tool != "find_device" for action in actions)


@pytest.mark.parametrize("text, device", [
    ("Send that to my Mac.", "mac"), ("Put it on the tablet.", "tablet"), ("Text this to my laptop", "laptop"),
    ("Send it to my MacBook.", "macbook"), ("Put that on my iPad please.", "ipad"),
])
def test_sending_to_the_mac_and_tablet_is_done_without_claude(text, device):
    assert _actions(text) == [Action("send_to_phone", {"device": device})]
    # Without Home Assistant there is nothing to send it through.
    assert _actions(text, home=None) is None


def test_the_send_to_device_rule_comes_before_assists_shapes():
    rules = LocalIntents(home=HOME)._rules
    sending = [n for n, (pattern, _) in enumerate(rules) if "(?P<device>mac|macbook|laptop|tablet|ipad)" in pattern]
    assisting = [n for n, (_, handle) in enumerate(rules) if getattr(handle, "__name__", "") in ("_assist", "_ask")]
    assert len(sending) == 1 and assisting and sending[0] < min(assisting)


@pytest.mark.parametrize("text", [
    "Send that to my Mac mini in the loft",
    "Put it on my iPad's list",
    "Put it on the tablet in the kitchen",
    "Send that to my mum",
    "Send the photo to my Mac",
    "Put a film on the tablet",
    "Send that to my Mac and my phone",
])
def test_what_the_send_to_device_rule_mustnt_catch_is_left_alone(text):
    actions = _actions(text)
    assert not actions or all(action.tool != "send_to_phone" for action in actions)


def test_the_find_device_rules_come_before_assists_shapes():
    # So "find my phone" is never a thing of Home Assistant's, should Assist ever learn a "find" shape.
    rules = LocalIntents(home=HOME)._rules
    finding = [n for n, (pattern, _) in enumerate(rules) if "(?P<device>phone|tablet|mac|laptop)" in pattern]
    assisting = [n for n, (_, handle) in enumerate(rules) if getattr(handle, "__name__", "") in ("_assist", "_ask")]
    assert len(finding) == 2 and assisting and max(finding) < min(assisting)


# Lists - see lists.py

@pytest.mark.parametrize("text, item, said, command", [
    ("Put milk on my shopping list.", "milk", "my shopping list", "put milk on my shopping list"),
    # Assist matches this with no list, so it isn't sent, and Nova adds it - see add_to_list.
    ("Add milk to notes.", "milk", "notes", "add milk to notes"),
    ("Add milk to the todo list, please.", "milk", "the todo list", "add milk to the todo list"),
])
def test_one_thing_is_added_without_claude(text, item, said, command):
    assert _actions(text) == [Action("add_to_list", {"item": item, "list": said, "command": command})]


@pytest.mark.parametrize("text", [
    "Add eggs, milk and bread to my shopping list.",
    # The comma said is lost with the punctuation, but still counts.
    "Add eggs, milk, bread to my list.",
    # One thing or two? Claude keeps it whole, or splits it.
    "Add salt and vinegar crisps to my list.",
    "Put eggs,milk on my list.",
])
def test_several_things_go_to_claude(text):
    assert _actions(text) is None


@pytest.mark.parametrize("text, tool", [
    ("Add ten minutes to the timer.", None), ("Add the dentist to my calendar.", None),
    ("Put the kettle on.", None), ("Put it on my phone.", "send_to_phone"), ("What's on tomorrow?", "get_calendar"),
    # Nova's own things and the devices, by any of their words.
    ("Add an hour to my alarm.", None), ("Add this to my reminders.", None), ("Put lunch in my diary.", None),
    ("Put the meeting on my schedule.", None), ("Put Drake on my iPad.", None), ("Put a film on the tablet.", None),
    ("Add a note to my agenda.", None), ("What's on my phone?", None), ("What's on the laptop?", None),
    ("What's on my Mac?", None),
    # Only pointing back, to something the conversation so far says.
    ("Add it to my shopping list.", None), ("Put that on my list.", None), ("Tick that off.", None),
    ("Take it off my list.", None), ("I've got it.", None), ("I have got this.", None), ("Cross this off my list.", None),
    # Not a list's name, nor a thing to add.
    ("Read me a story.", None), ("Read me the news.", None), ("Take the bins out.", None),
    # Two requests at once.
    ("Add milk to my list and play Drake.", None), ("Tick off milk on my list then the eggs.", None),
])
def test_what_the_list_rules_mustnt_catch_is_left_alone(text, tool):
    # 0003: local rules stay strict.
    found = LocalIntents(home=HOME, ntfy=SimpleNamespace(just_said=lambda: False)).match(text)
    assert (found and [action.tool for action in found["actions"]]) == ([tool] if tool else None)


@pytest.mark.parametrize("text, action", [
    ("What's on my shopping list?", Action("read_list", {"list": "my shopping list"})),
    ("Read me my todo list.", Action("read_list", {"list": "my todo list"})),
    ("Tick off milk.", Action("tick_off_list", {"item": "milk", "list": None, "command": "tick off milk"})),
    ("Tick milk off.", Action("tick_off_list", {"item": "milk", "list": None, "command": "tick milk off"})),
    ("Tick off milk on my shopping list.", Action("tick_off_list", {
        "item": "milk", "list": "my shopping list", "command": "tick off milk on my shopping list"})),
    ("Take milk off my shopping list.", Action("tick_off_list", {
        "item": "milk", "list": "my shopping list", "command": "take milk off my shopping list", "exact": False})),
    ("Cross the eggs off the shopping list.", Action("tick_off_list", {
        "item": "the eggs", "list": "the shopping list", "command": "cross the eggs off the shopping list",
        "exact": False})),
    # Without the word "list", only the thing itself - "take the day off work" is about the world.
    ("Take the day off work.", Action("tick_off_list", {
        "item": "the day", "list": "work", "command": "take the day off work", "exact": True})),
    ("I've got the milk.", Action("tick_off_list", {"item": "milk", "list": None, "exact": True})),
])
def test_a_list_is_read_and_things_ticked_off_without_claude(text, action):
    assert _actions(text) == [action]


@pytest.mark.parametrize("text", [
    "I've got a headache.", "Add milk to my shopping list.", "What's on my shopping list?", "Tick off milk.",
    "Take milk off my shopping list.",
])
def test_without_home_assistant_the_list_rules_leave_it_to_claude(text):
    # "I've got a headache" is a sentence about the world, which a brain without Home
    # Assistant would otherwise answer with "Lists need Home Assistant."
    assert _actions(text, home=None) is None


@pytest.mark.parametrize("text, item, said", [
    # The last " to " or " on " is the list's, so a thing can have one too.
    ("Add a trip to Ikea to my list.", "a trip to ikea", "my list"),
    ("Put a letter to the council on my list.", "a letter to the council", "my list"),
    # "To do" is a list's name, never where the thing goes.
    ("Add milk to my to do list.", "milk", "my to do list"),
    ("Add milk to my to-do list.", "milk", "my to do list"),
    ("Put the radio on my shopping list.", "the radio", "my shopping list"),
    # Without commas, three words can't be told from one thing of three words.
    ("Add eggs milk bread to my list.", "eggs milk bread", "my list"),
])
def test_a_thing_is_taken_up_to_the_last_to_or_on(text, item, said):
    command = re.sub(r"[^a-z0-9' ]", " ", text.lower()).strip()
    assert _actions(text) == [Action("add_to_list", {"item": item, "list": said, "command": " ".join(command.split())})]


@pytest.mark.parametrize("text", [
    # Only pointing back, by any of the words for it.
    "Add them to my shopping list.", "Put all of those on my list.", "Add these to my list.", "Tick them off.",
    "Tick both off my list.", "I've got them.", "Take those off my shopping list.", "Add the other one to my list.",
    # Several things, joined other ways.
    "Add eggs plus milk to my list.", "Add eggs as well as milk to my list.",
    # Possessives of Nova's own things and the devices.
    "Add a note to my phone's notes.", "Put lunch on my calendar's day.",
    # Ticking off from what isn't a list.
    "Tick milk off my calendar.", "Tick it off my list.", "Tick milk off my list and the eggs.",
])
def test_what_else_the_list_rules_mustnt_catch_is_left_alone(text):
    assert _actions(text) is None


@pytest.mark.parametrize("text", [
    # When, not a list - Claude adds it to the calendar.
    "Put the dentist on Thursday at 3.", "Add the dentist to my calendar on Thursday at 3.",
    "Put lunch with Sam on tomorrow.", "Add a haircut to Friday at 10am.", "Put the MOT on the 3rd of March.",
    "Add dinner with Sam to next week.", "Put football on today at 6 pm.", "Take the day off work tomorrow.",
    "Put the plumber on at nine.", "Put tea on at 5pm.", "Add yoga to Mondays.",
])
def test_a_day_or_time_in_a_lists_words_is_left_to_claude(text):
    assert _actions(text) is None


def test_a_list_named_after_a_day_is_still_one_said_with_list():
    assert _actions("Add the bins to my Monday jobs list.") == [Action("add_to_list", {
        "item": "the bins", "list": "my monday jobs list", "command": "add the bins to my monday jobs list"})]


def test_a_thing_is_ticked_off_a_list_said_after_it():
    assert _actions("Tick milk off my shopping list.") == [Action("tick_off_list", {
        "item": "milk", "list": "my shopping list", "command": "tick milk off my shopping list"})]


def test_ive_got_only_ticks_off_an_exact_fit_and_never_asks_assist():
    # "I've got the flu" is a sentence about the world - only "flu" itself on a list is
    # ticked off, and Assist, which has no such sentence, is never sent it.
    assert _actions("I've got the flu.") == [Action("tick_off_list", {"item": "flu", "list": None, "exact": True})]


@pytest.mark.parametrize("text, item", [
    ("Add one egg to my list.", "one egg"), ("Add the last of the bread to my list.", "the last of the bread"),
    ("Add that cake to my list.", None), ("Add the other one to my list.", None),
])
def test_a_thing_is_only_left_to_claude_when_it_points_back(text, item):
    # "That" always points back; "one" or "last" only when they're all the thing is.
    expected = None if item is None else [Action("add_to_list", {
        "item": item, "list": "my list", "command": text.lower().rstrip(".")})]
    assert _actions(text) == expected


# The PC - see pc.py

@pytest.mark.parametrize("text, action", [
    ("Turn on my PC.", "on"), ("Switch on the computer.", "on"), ("Turn the PC on.", "on"),
    ("Switch my computer on.", "on"), ("Wake my PC.", "on"), ("Wake up the computer.", "on"),
    ("Turn off the PC.", "off"), ("Switch off my computer.", "off"), ("Turn my PC off.", "off"),
    ("Switch the computer off.", "off"), ("Shut down my PC.", "off"), ("Shut the computer down.", "off"),
    ("Restart my PC.", "restart"), ("Reboot the computer.", "restart"),
    ("Put my PC to sleep.", "sleep"), ("Put the computer to sleep.", "sleep"),
    ("Turn off my PC's screen.", "screen_off"), ("Turn off the computer monitor.", "screen_off"),
    ("Lock my PC.", "lock"), ("Lock the computer.", "lock"),
    ("Is my PC on?", "status"), ("Is the computer on?", "status"),
])
def test_the_pc_is_worked_without_claude(text, action):
    # "Lock my PC" is the PC's own, not Assist's; "is my PC on?" comes before Assist's
    # "is X on", and "put my PC to sleep" before the lists' "put X to Y".
    assert _actions(text) == [Action("pc_power", {"action": action})]
    # Without Home Assistant too, which says the PC needs it.
    assert _actions(text, home=None) == [Action("pc_power", {"action": action})]


@pytest.mark.parametrize("text, tools", [
    # Still the lights, and Assist's phone.
    ("Turn on the PC lights.", ["control_home"]), ("Turn off my phone.", ["control_home"]),
    # Claude's - the music, two requests at once, a delay, an alarm, a battery or unlocking.
    ("Restart the music.", None), ("Shut down the PC and play Drake.", None),
    ("Shut down my PC in ten minutes.", None), ("Wake me up at 7.", None), ("Is my PC's battery on?", None),
    ("Is my PC on charge?", None), ("Unlock my PC.", None),
])
def test_what_the_pc_rules_mustnt_catch_is_left_alone(text, tools):
    actions = _actions(text)
    assert (None if actions is None else [action.tool for action in actions]) == tools
