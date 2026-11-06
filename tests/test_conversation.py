"""
Conversation Test
Drives the rules for answering the user with text alone - no microphone, speaker,
Whisper or Claude - using fakes for the clock, local commands, Claude and the
actions, so each rule can be checked on its own.
"""

import logging

import pytest

from voice_assistant.actions import Action, ActionExecutor, ActionResult
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import Clock
from voice_assistant.conversation import LIGHTS_TIMEOUT, Conversation, Reply
from voice_assistant.ha_registry import Entry, Snapshot
from voice_assistant.intents import LocalIntents
from voice_assistant.pc import PCPower
from voice_assistant.timeline import Timeline


class FakeClock:
    """Stands in for the Clock: what's ringing, and the word rules it really uses."""

    looks_like_dismissal = staticmethod(Clock.looks_like_dismissal)
    answer_to_offer = staticmethod(Clock.answer_to_offer)

    def __init__(self, ringing=False, timers=()):
        self.ringing = ringing
        self.timers = list(timers)
        self.dismissed = 0
        self.silenced = []

    def is_ringing(self, satellite=None):
        self.asked_about = satellite
        return self.ringing

    def dismiss(self, satellite=None):
        self.dismissed += 1
        self.dismissed_on = satellite
        self.ringing = False
        return {"success": True, "message": "Okay, stopped."}

    def silence_insistent(self, entries):
        self.silenced.append(entries)

    def describe_all(self):
        return list(self.timers)


class FakeIntents:
    def __init__(self, handles=None):
        self.handles = handles or {}

    def match(self, text, only=None):
        handled = self.handles.get(text)
        if handled and only is not None and not all(action.tool in only for action in handled["actions"]):
            return None
        return handled


class FakeLLM:
    """Stands in for Claude, answering each request with the next reply given."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked, self.remembered, self.recorded, self.tool_answers = [], [], [], []
        self.contexts, self.lights, self.playing = [], [], []
        # What Claude reads before answering, and what it's told - see LLMHandler.process_query.
        self.to_read, self.read = [], []
        # The tools it's offered, and those of them it only reads from - as each request found them.
        self.tools, self.read_tools, self.offered = [], set(), []

    def forget_stale_history(self):
        pass

    def process_query(self, text, context=None, lights=None, read=None, playing=()):
        self.asked.append(text)
        self.contexts.append(context)
        self.lights.append(lights)
        self.playing.append(list(playing))
        self.offered.append((list(self.tools), set(self.read_tools)))
        self.read += [read(action) for action in self.to_read]
        return self.replies.pop(0)

    def remember(self, text, reply):
        self.remembered.append((text, reply))

    def record_reply(self, text, spoken, tool_answers=()):
        self.recorded.append((text, spoken))
        self.tool_answers.append(list(tool_answers))


class FakeActions:
    def __init__(self, results=None, home=None):
        self.results = results or {}
        self.home = home
        self.run = []
        self.params = []

    def execute(self, action):
        self.run.append(action.tool)
        self.params.append(action.params)
        return self.results.get(action.tool, ActionResult(True, "Done."))

    def tool_schemas(self):
        return []

    def read_tools(self):
        return ["home_status"]


class FakeHome:
    """Stands in for Home Assistant: the lights it describes, and how long it was given to read them."""

    def __init__(self, lights):
        self.lights = lights
        self.timeouts = []

    def describe_lights(self, timeout=None):
        self.timeouts.append(timeout)
        return self.lights


class FakeMusic:
    def __init__(self):
        self.waited = 0

    def wait(self):
        self.waited += 1


def _answer(text, *actions):
    return {"response": text, "actions": list(actions), "success": True}


# Claude's answer when what it heard was speech from what's playing - see LLMHandler.process_query.
NOT_FOR_ME = {"response": "", "actions": [], "success": True, "not_for_me": True}


def _conversation(clock=None, intents=None, llm=None, actions=None, ringing=None, **settings):
    made = Conversation(clock or FakeClock(), intents or FakeIntents(), llm or FakeLLM(), actions or FakeActions(),
                        FakeMusic(), **settings)
    made.start(ringing)
    return made


# Answering a question

def test_a_plain_answer_is_said_and_remembered():
    llm = FakeLLM(_answer("Paris."))
    reply = _conversation(llm=llm).handle("what's the capital of France")
    assert (reply.say, reply.sounds, reply.listen_again) == ("Paris.", [], False)
    assert llm.recorded == [("what's the capital of France", "Paris.")]


def test_claude_reads_home_status_rather_than_it_being_said():
    # What home_status reads is for Claude to answer from, not to be said as it is.
    llm = FakeLLM(_answer("Sunset's at 5:52 PM."))
    llm.to_read = [Action("home_status", {"about": "sunset"}, from_model=True)]
    actions = FakeActions({"home_status": ActionResult(
        True, "Sun next setting (sensor.sun_next_setting): 2026-10-20 17:52")})
    reply = _conversation(llm=llm, actions=actions).handle("when's sunset?")
    assert llm.read == ["Sun next setting (sensor.sun_next_setting): 2026-10-20 17:52"]
    assert (actions.run, actions.params) == (["home_status"], [{"about": "sunset"}])
    assert reply.say == "Sunset's at 5:52 PM."
    assert llm.recorded == [("when's sunset?", "Sunset's at 5:52 PM.")]


class FakeCalendar:
    """Stands in for GoogleCalendar: the activity calendars Google listed last."""

    def __init__(self, *names):
        self.names = list(names)

    def activity_calendars(self):
        return {name.lower(): {"id": f"calendar-{name.lower()}", "name": name, "primary": False, "writable": True}
                for name in self.names}


def test_claude_is_told_the_activities():
    calendar = FakeCalendar("Uni", "Work")
    actions = ActionExecutor(calendar=calendar)
    llm = FakeLLM(_answer("Okay."), _answer("Okay."))
    conversation = _conversation(llm=llm, actions=actions)

    def told():
        tools, read_tools = llm.offered[-1]
        [activity] = [tool for tool in tools if tool["name"] == "get_activity"]
        # The tools only read from are the same executor's, at the same time - so none is run as an action.
        assert read_tools == set(actions.read_tools()) and "home_status" in read_tools
        return activity

    conversation.handle("when have I got gym")
    activity = told()
    assert activity["description"].endswith("The user's activities: Uni and Work.")
    said = activity["input_schema"]["properties"]["activity"]["description"].lower()
    assert "work" not in said and "uni" not in said

    # A calendar added in Google is named from the next request - in order, so a list
    # Google gives in another order doesn't change the tools, and the API's cache.
    calendar.names.append("Gym")
    conversation.handle("when have I got gym")
    assert told()["description"].endswith("The user's activities: Gym, Uni and Work.")


def test_a_local_command_never_reaches_claude():
    llm = FakeLLM()
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime")})
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("pause")
    assert llm.asked == [] and llm.remembered == [("pause", "Okay.")]
    assert reply.sounds == [("success_chime.wav", "general")] and reply.say == ""


def test_where_my_phone_is_never_reaches_claude_when_asked_locally():
    # Claude's history goes to Anthropic with every later request, so a place said for a
    # local "where's my phone?" is kept from it - it only hears that it was said.
    where = "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    intents = FakeIntents({"where's my phone": _answer("Okay.", Action("find_device", {"device": "phone",
                                                                                       "ring": False}))})
    actions = FakeActions({"find_device": ActionResult(True, where, private=True)})
    llm = FakeLLM()
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("where's my phone")
    assert reply.say == where and llm.asked == []
    assert llm.remembered == [("where's my phone", "I said where your phone is.")]
    assert llm.recorded == [("where's my phone", "I said where your phone is.")]
    assert llm.tool_answers == [["I said where your phone is."]]



def test_where_a_device_is_never_reaches_claude_even_when_claude_asked():
    # Claude needs no place to answer anything it's asked, so its own call is kept from it too -
    # beside anything else the turn said, which it keeps.
    where = "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    llm = FakeLLM(_answer("Okay.", Action("find_device", {"device": "phone"}, from_model=True),
                          Action("set_timer", {"duration_seconds": 600}, from_model=True)))
    actions = FakeActions({"find_device": ActionResult(True, where, private=True),
                           "set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    reply = _conversation(llm=llm, actions=actions).handle("where did I leave my phone, and set a ten minute timer")
    assert reply.say == f"{where} Timer set for 10 minutes."
    assert llm.recorded == [("where did I leave my phone, and set a ten minute timer",
                             "I said where your phone is. Timer set for 10 minutes.")]
    assert llm.tool_answers == [["I said where your phone is.", "Timer set for 10 minutes."]]
    assert "Example Road" not in str((llm.recorded, llm.tool_answers, llm.remembered))


def test_where_my_phone_is_still_found_locally_when_nova_was_listening_for_an_answer():
    # After "Anything else?", "where's my phone?" can't be its answer, so it's still Nova's own -
    # and Claude, which isn't asked, never hears where.
    where = "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    intents = LocalIntents(home=object())
    actions = FakeActions({"find_device": ActionResult(True, where, private=True)})
    llm = FakeLLM(_answer("Okay."))
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("Where's my phone?", follow_up=True)
    assert reply.say == where and llm.asked == [] and actions.run == ["find_device"]
    assert llm.remembered == [("Where's my phone?", "I said where your phone is.")]
    assert "Example Road" not in str((llm.recorded, llm.tool_answers))
    # Anything else in the window is still the conversation's, so "stop" isn't the music's.
    _conversation(intents=intents, llm=llm, actions=actions).handle("pause", follow_up=True)
    assert llm.asked == ["pause"]


def test_the_answer_to_a_question_isnt_taken_for_a_local_command():
    # "Stop" answering "which timer?" needs the conversation so far to make sense of.
    llm = FakeLLM(_answer("Okay."))
    intents = FakeIntents({"stop": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    _conversation(intents=intents, llm=llm).handle("stop", follow_up=True)
    assert llm.asked == ["stop"]


def test_actions_wait_for_the_music_to_have_paused_and_say_their_own_words():
    llm = FakeLLM(_answer("Okay.", Action("set_timer", {"duration_seconds": 600})))
    actions = FakeActions({"set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    conversation = _conversation(llm=llm, actions=actions)
    reply = conversation.handle("set a ten minute timer")
    assert conversation.music.waited == 1 and reply.say == "Timer set for 10 minutes."


def test_what_each_action_said_is_kept_for_the_model():
    llm = FakeLLM(_answer("Okay.", Action("get_weather", {"day": "today"}), Action("set_timer", {"duration_seconds": 600})))
    actions = FakeActions({"get_weather": ActionResult(True, "It's 10 degrees."),
                           "set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    _conversation(llm=llm, actions=actions).handle("what's the weather, and set a ten minute timer")
    assert llm.tool_answers == [["It's 10 degrees.", "Timer set for 10 minutes."]]


def test_a_question_is_listened_for_but_only_so_many_times_in_a_row():
    llm = FakeLLM(*[_answer("Which one?")] * 3)
    conversation = _conversation(llm=llm, max_follow_ups=2)
    assert [conversation.handle("cancel it").listen_again for _ in range(3)] == [True, True, False]


# What Home Assistant doesn't understand

def _lights_assist_didnt_understand(llm=None, home=None, **settings):
    """A conversation in which "dim the lights" goes to Assist, which matches nothing, and so to Claude."""
    intents = FakeIntents({"dim the lights": _answer("Okay.", Action("control_home", {"command": "dim the lights"}))})
    actions = FakeActions({"control_home": ActionResult(False, "Sorry, I couldn't understand that", understood=False),
                           "set_lights": ActionResult(True, "Done.", confirm="chime")}, home=home)
    llm = llm or FakeLLM(_answer("Okay.", Action("set_lights", {"on": True, "brightness": 30})))
    return _conversation(intents=intents, llm=llm, actions=actions, **settings), llm, actions


def test_a_local_command_assist_didnt_understand_goes_to_claude():
    conversation, llm, actions = _lights_assist_didnt_understand()
    reply = conversation.handle("dim the lights")
    assert llm.asked == ["dim the lights"] and actions.run == ["control_home", "set_lights"]
    assert "couldn't understand" not in reply.say and reply.sounds == [("success_chime.wav", "general")]


def test_a_request_which_falls_through_to_claude_isnt_remembered_twice():
    # Claude keeps the exchanges it answers itself, so remembering the local command's
    # "Okay." as well would give it the same request twice.
    conversation, llm, _ = _lights_assist_didnt_understand()
    conversation.handle("dim the lights")
    assert llm.remembered == []


def test_what_claude_says_after_a_fall_through_is_kept_and_listened_for():
    llm = FakeLLM(_answer("Which room?"))
    conversation, llm, actions = _lights_assist_didnt_understand(llm=llm)
    reply = conversation.handle("dim the lights")
    assert (reply.say, reply.listen_again) == ("Which room?", True)
    assert llm.recorded == [("dim the lights", "Which room?")] and actions.last_answer == ("dim the lights", "Which room?")


def test_a_fall_through_inside_a_routine_is_answered_like_any_other_step():
    evening = FakeRoutines({"name": "Evening", "phrases": ["evening"], "steps": ["dim the lights"]})
    conversation, llm, _ = _lights_assist_didnt_understand(routines=evening)
    reply = conversation.handle("evening")
    assert llm.asked == ["dim the lights"] and reply.sounds == [("success_chime.wav", "general")]


def test_a_local_command_assist_understood_but_couldnt_do_stays_with_assist():
    # "Understood" is about Assist having matched a sentence, not about it having worked:
    # the lamp being unreachable isn't something Claude can do anything about.
    intents = FakeIntents({"lamp off": _answer("Okay.", Action("control_home", {"command": "lamp off"}))})
    actions = FakeActions({"control_home": ActionResult(False, "The lamp is unavailable")})
    llm = FakeLLM()
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("lamp off")
    assert llm.asked == [] and reply.say == "The lamp is unavailable"
    assert llm.remembered == [("lamp off", "Okay.")]


# What Claude is told about the lights

LAMP_OFF = "Nova is in the Bedroom. Lights: Bedroom Light (light.bedroom_light, Bedroom) off; Lamp (light.lamp, Bedroom) off"


def test_claude_is_told_about_the_lights_beside_the_timers():
    # Separately: the lights aren't a timer, so they aren't put in the list of timers.
    home = FakeHome(LAMP_OFF)
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(clock=FakeClock(timers=["timer 1: 5 minutes"]), llm=llm, actions=FakeActions(home=home)).handle("is it sunny")
    assert llm.contexts == ["timer 1: 5 minutes"] and llm.lights == [LAMP_OFF]
    assert "Nova is in the Bedroom" in llm.lights[0]


def test_claude_is_told_about_the_lights_with_no_timers_running():
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(llm=llm, actions=FakeActions(home=FakeHome(LAMP_OFF))).handle("is it sunny")
    assert llm.contexts == [None] and llm.lights == [LAMP_OFF]


def test_claude_is_told_when_novas_room_isnt_known():
    told = "Nova's room isn't known. Lights: Lamp (light.lamp, Bedroom) off"
    llm = FakeLLM(_answer("Which room?"))
    _conversation(llm=llm, actions=FakeActions(home=FakeHome(told))).handle("dim the lights")
    assert llm.lights == [told]


def test_claude_goes_without_the_lights_when_they_cant_be_read():
    # describe_lights is "" when Home Assistant is down, or too slow to wait for.
    llm = FakeLLM(_answer("It's sunny."), _answer("It's sunny."))
    home = FakeHome("")
    _conversation(clock=FakeClock(timers=["timer 1: 5 minutes"]), llm=llm, actions=FakeActions(home=home)).handle("is it sunny")
    _conversation(llm=llm, actions=FakeActions(home=home)).handle("is it sunny")
    assert llm.contexts == ["timer 1: 5 minutes", None] and llm.lights == [None, None]


def test_without_home_assistant_claude_is_only_told_about_the_timers():
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(clock=FakeClock(timers=["timer 1: 5 minutes"]), llm=llm).handle("is it sunny")
    assert llm.contexts == ["timer 1: 5 minutes"] and llm.lights == [None]


def test_a_slow_home_assistant_is_only_waited_for_a_moment_to_read_the_lights():
    # Every request to Claude asks for the lights, so waiting out Home Assistant's own
    # five seconds each time would slow every reply when it's down.
    home = FakeHome(LAMP_OFF)
    _conversation(llm=FakeLLM(_answer("It's sunny.")), actions=FakeActions(home=home)).handle("is it sunny")
    assert home.timeouts == [LIGHTS_TIMEOUT] and 0 < LIGHTS_TIMEOUT <= 2


def test_the_lights_are_only_read_when_claude_is_asked():
    home = FakeHome(LAMP_OFF)
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    _conversation(intents=intents, actions=FakeActions(home=home)).handle("pause")
    assert home.timeouts == []


def test_a_fall_through_tells_claude_about_the_lights_too():
    home = FakeHome(LAMP_OFF)
    conversation, llm, _ = _lights_assist_didnt_understand(home=home)
    conversation.handle("dim the lights")
    assert llm.lights == [LAMP_OFF]


# Something ringing

def test_stop_while_ringing_is_handled_without_claude():
    clock, llm = FakeClock(ringing=True), FakeLLM()
    reply = _conversation(clock=clock, llm=llm).handle("Stop.")
    assert clock.dismissed == 1 and llm.asked == []
    assert reply.sounds == [("success_chime.wav", "general")]


def test_stop_heard_as_so_while_ringing_is_still_a_stop():
    # Heard over a ringing timer on the Pi, "So" went to Claude, which replied "Your
    # message just says 'So'" and asked what was wanted.
    clock, llm = FakeClock(ringing=True), FakeLLM()
    _conversation(clock=clock, llm=llm).handle("So")
    assert clock.dismissed == 1 and llm.asked == []


def test_only_whats_ringing_where_the_user_is_talking_is_stopped():
    # The kitchen's "stop" dismissed the bedroom's alarm, which couldn't be heard there.
    clock = FakeClock(ringing=True)
    _conversation(clock=clock, satellite="kitchen").handle("Stop.")
    assert clock.asked_about == "kitchen" and clock.dismissed_on == "kitchen"


def test_without_the_chime_a_dismissal_is_shown_not_said():
    reply = _conversation(clock=FakeClock(ringing=True), chime_enabled=False).handle("Stop.")
    assert (reply.sounds, reply.shown, reply.say) == ([], "Okay, stopped.", "")


def test_talking_over_a_timer_silences_it_after_the_request_is_dealt_with():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's sunny."))
    ringing = {"insistent": True, "insistent_entries": ["timer 1"], "reminder": None, "reminder_sounding": False}
    _conversation(clock=clock, llm=llm, ringing=ringing).handle("what's the weather like")
    assert clock.silenced == [["timer 1"]] and clock.dismissed == 0


def test_a_quiet_reminder_is_offered_once_and_the_answer_heard():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."), _answer("Paris."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)

    reply = conversation.handle("what time is it")
    assert reply.say == "It's 7:36 PM. Also, your walk the dogs reminder is still going. Shall I turn it off?"
    assert reply.listen_again

    # "No" here means leave it, where said to something ringing it means stop.
    assert conversation.handle("no", follow_up=True).say == "Okay, I'll leave it."
    assert clock.dismissed == 0
    # And it isn't offered again in the same conversation.
    assert "reminder" not in conversation.handle("what's the capital of France").say


def test_yes_to_the_reminder_offer_turns_it_off():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)
    conversation.handle("what time is it")
    assert conversation.handle("yes please", follow_up=True).sounds == [("success_chime.wav", "general")]
    assert clock.dismissed == 1


def test_a_new_conversation_forgets_an_unanswered_offer():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."), _answer("Okay, leaving it."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)
    conversation.handle("what time is it")
    clock.ringing = False
    conversation.start()
    conversation.handle("no")
    assert llm.asked[-1] == "no"


# Talked over

def test_stop_said_over_a_reply_only_stops_the_talking():
    # Taken as a command, "stop" paused the music too - which was only quiet while
    # the assistant was talking.
    llm = FakeLLM()
    intents = FakeIntents({"Stop.": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions()
    conversation = _conversation(intents=intents, llm=llm, actions=actions)
    conversation.start(talked_over=True)
    for said in ("Stop.", "Okay, thanks.", "Shut up.", "Soap.", "So."):
        reply = conversation.handle(said)
        assert (reply.say, reply.sounds, reply.listen_again) == ("", [], False)
    assert actions.run == [] and llm.asked == [] and llm.remembered == []


def test_a_request_said_over_a_reply_is_answered_as_usual():
    llm = FakeLLM(_answer("Paris."))
    conversation = _conversation(llm=llm)
    conversation.start(talked_over=True)
    assert conversation.handle("what's the capital of France").say == "Paris."


def test_stop_said_after_the_reply_has_finished_is_still_a_command():
    intents = FakeIntents({"Stop.": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions()
    _conversation(intents=intents, actions=actions).handle("Stop.")
    assert actions.run == ["control_playback"]


# Speech from what's playing

class FakePlaying:
    """Stands in for Session.playing_here: what plays in the room each time it's asked, in turn."""

    def __init__(self, *each):
        self.each = [list(names) for names in each]
        self.asked = 0

    def __call__(self):
        self.asked += 1
        return self.each.pop(0) if len(self.each) > 1 else self.each[0]


def test_claude_is_told_whats_playing_in_the_room():
    llm = FakeLLM(_answer("It's sunny."), _answer("It's sunny."))
    conversation = _conversation(llm=llm, playing=FakePlaying(["Firefox", "Windows PC"], []))
    conversation.handle("is it sunny")
    conversation.handle("is it sunny")
    assert llm.playing == [["Firefox", "Windows PC"], []]


def test_without_anything_to_say_whats_playing_claude_is_told_nothing_plays():
    llm = FakeLLM(_answer("It's sunny."))
    _conversation(llm=llm).handle("is it sunny")
    assert llm.playing == [[]]


def test_speech_from_whats_playing_is_ignored_without_a_sound():
    llm = FakeLLM(NOT_FOR_ME)
    actions = FakeActions()
    conversation = _conversation(llm=llm, actions=actions, playing=FakePlaying(["Bedroom TV"]))
    timeline = Timeline()
    reply = conversation.handle("I'll never let go, Jack.", timeline=timeline)
    assert (reply.say, reply.sounds, reply.shown, reply.listen_again) == ("", [], "", False)
    assert timeline.notes["outcome"] == "not_for_me"
    # Nothing in the thread, and nothing kept to send to the phone.
    assert llm.remembered == [] and llm.recorded == [] and not hasattr(actions, "last_answer")
    assert actions.run == [] and conversation.music.waited == 0


def test_whats_playing_is_read_once_as_the_request_is_built():
    # Read again afterwards, a film stopped while Claude thought would have its line taken
    # for a request - or one started, a request taken for the film.
    playing = FakePlaying(["Bedroom TV"], [])
    reply = _conversation(llm=FakeLLM(NOT_FOR_ME), playing=playing).handle("I'll never let go, Jack.")
    assert playing.asked == 1 and reply == Reply()


def test_ignoring_speech_is_logged_without_the_words(caplog):
    caplog.set_level(logging.INFO)
    _conversation(llm=FakeLLM(NOT_FOR_ME), playing=FakePlaying(["Bedroom TV"])).handle("I'll never let go, Jack.")
    assert "Taken as speech from what's playing, so ignored" in caplog.messages
    assert not any("let go" in message for message in caplog.messages)


def test_something_ringing_carries_on_when_speech_from_whats_playing_is_ignored():
    # The user didn't speak, so hasn't heard it.
    clock = FakeClock(ringing=True)
    ringing = {"insistent": True, "insistent_entries": ["timer 1"], "reminder": None, "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=FakeLLM(NOT_FOR_ME), playing=FakePlaying(["Bedroom TV"]),
                                 ringing=ringing)
    assert conversation.handle("Fire!") == Reply()
    assert clock.silenced == [] and clock.dismissed == 0


def test_not_for_me_with_nothing_playing_is_taken_as_not_caught():
    llm = FakeLLM(NOT_FOR_ME)
    actions = FakeActions()
    reply = _conversation(llm=llm, actions=actions, playing=FakePlaying([])).handle("turn the lights off")
    assert (reply.say, reply.sounds, reply.listen_again) == ("Sorry, I didn't catch that.", [], False)
    assert actions.run == [] and llm.remembered == [] and llm.recorded == []


def test_not_for_me_without_anything_to_say_whats_playing_is_taken_as_not_caught():
    assert _conversation(llm=FakeLLM(NOT_FOR_ME)).handle("turn the lights off").say == "Sorry, I didn't catch that."


def test_an_unfounded_not_for_me_is_noted_on_the_timeline():
    # What the prompt is tuned by: how often Claude says "the film" with nothing playing.
    timeline = Timeline()
    _conversation(llm=FakeLLM(NOT_FOR_ME), playing=FakePlaying([])).handle("turn the lights off", timeline=timeline)
    assert timeline.notes["outcome"] == "not_for_me_unfounded"


# What the llm would never give - kept here so a slip there still runs nothing.
NOT_FOR_ME_WITH_ACTIONS = {**NOT_FOR_ME, "actions": [Action("set_lights", {"on": False})]}


@pytest.mark.parametrize("playing", [["Bedroom TV"], []])
def test_not_for_me_runs_nothing_even_with_actions_beside_it(playing):
    actions = FakeActions()
    conversation = _conversation(llm=FakeLLM(NOT_FOR_ME_WITH_ACTIONS), actions=actions, playing=FakePlaying(playing))
    conversation.handle("turn the lights off, Jack")
    assert actions.run == [] and conversation.music.waited == 0


def test_not_for_me_after_a_fall_through_runs_nothing_even_with_actions_beside_it():
    conversation, llm, actions = _lights_assist_didnt_understand(llm=FakeLLM(NOT_FOR_ME_WITH_ACTIONS),
                                                                 playing=FakePlaying(["Bedroom TV"]))
    assert conversation.handle("dim the lights") == Reply()
    assert actions.run == ["control_home"]


def test_a_follow_up_gets_the_same_check():
    # Heard without the wake word, a follow-up is the likeliest to be the film's.
    llm = FakeLLM(_answer("Which room?"), NOT_FOR_ME)
    conversation = _conversation(llm=llm, playing=FakePlaying(["Bedroom TV"]))
    assert conversation.handle("turn the lights on").listen_again is True
    assert conversation.handle("Get out of the house!", follow_up=True) == Reply()
    assert llm.playing == [["Bedroom TV"], ["Bedroom TV"]]
    assert llm.recorded == [("turn the lights on", "Which room?")]


def test_a_fall_through_from_assist_gets_the_same_check():
    playing = FakePlaying(["Bedroom TV"])
    conversation, llm, actions = _lights_assist_didnt_understand(llm=FakeLLM(NOT_FOR_ME), playing=playing)
    assert conversation.handle("dim the lights") == Reply()
    assert actions.run == ["control_home"] and llm.remembered == [] and llm.recorded == []


@pytest.mark.parametrize("said", ["pause", "stop"])
def test_local_commands_never_reach_claude_while_something_plays(said):
    # Nova's own rules, so it's their pause and stop that are kept from Claude.
    llm = FakeLLM()
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime")})
    playing = FakePlaying(["Bedroom TV"])
    reply = _conversation(intents=LocalIntents(), llm=llm, actions=actions, playing=playing).handle(said)
    assert llm.asked == [] and (actions.run, actions.params) == (["control_playback"], [{"action": "pause"}])
    assert reply.sounds == [("success_chime.wav", "general")] and playing.asked == 0


def test_a_routines_steps_are_never_taken_for_whats_playing():
    # The user's own words for the routine, not something heard - so Claude isn't told
    # what's playing, and a not_for_me is taken as not caught rather than dropping a step.
    evening = FakeRoutines({"name": "Evening", "phrases": ["evening"], "steps": ["is it sunny"]})
    llm = FakeLLM(NOT_FOR_ME)
    reply = _conversation(llm=llm, routines=evening, playing=FakePlaying(["Bedroom TV"])).handle("evening")
    assert llm.playing == [[]] and reply.say == "Sorry, I didn't catch that."


def test_a_routine_step_taken_for_whats_playing_leaves_the_turn_noted_as_the_routine():
    # not_for_me_unfounded counts what Claude heard, which a routine's step wasn't.
    evening = FakeRoutines({"name": "Evening", "phrases": ["evening"], "steps": ["is it sunny"]})
    timeline = Timeline()
    _conversation(llm=FakeLLM(NOT_FOR_ME), routines=evening).handle("evening", timeline=timeline)
    assert timeline.notes["outcome"] == "routine"


# Sending to the phone

def test_whats_said_is_kept_for_sending_to_the_phone():
    actions = FakeActions()
    _conversation(llm=FakeLLM(_answer("Paris.")), actions=actions).handle("what's the capital of France")
    assert actions.last_answer == ("what's the capital of France", "Paris.")


def test_sending_it_doesnt_change_what_would_be_sent_next_time():
    actions = FakeActions({"send_to_phone": ActionResult(True, "Sent.")})
    actions.last_answer = ("what's the capital of France", "Paris.")
    intents = FakeIntents({"send that to my phone": _answer("Okay.", Action("send_to_phone"))})
    _conversation(intents=intents, actions=actions).handle("send that to my phone")
    assert actions.last_answer == ("what's the capital of France", "Paris.")


# Routines

class FakeRoutines:
    def __init__(self, *routines):
        self.routines = routines

    def called(self, text):
        return next((routine for routine in self.routines if text in routine["phrases"]), None)


MORNING = {"name": "Good morning", "phrases": ["good morning"],
           "steps": ["what's the weather", "pause", "turn on the lights"]}


def test_a_routines_steps_are_each_done_as_if_said_and_their_answers_said_together():
    llm = FakeLLM(_answer("It's sunny."), _answer("Okay.", Action("home_command", {"request": "turn on the lights"})))
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime"),
                           "home_command": ActionResult(True, "Turned on the lights", confirm="chime")})
    reply = _conversation(intents=intents, llm=llm, actions=actions, routines=FakeRoutines(MORNING)).handle("good morning")

    assert llm.asked == ["what's the weather", "turn on the lights"]
    assert actions.run == ["control_playback", "home_command"]
    # Two chimes would only be one sound twice.
    assert reply.say == "It's sunny." and reply.sounds == [("success_chime.wav", "general")]


def test_a_routine_runs_a_home_assistant_script_through_assist():
    class RecordingActions(FakeActions):
        def execute(self, action):
            self.run.append(action)
            return ActionResult(True, "Done.", confirm="chime")

    actions = RecordingActions()
    goodnight = {"name": "Goodnight", "phrases": ["goodnight"], "steps": ["run goodnight"]}
    _conversation(intents=LocalIntents(home=object()), actions=actions, routines=FakeRoutines(goodnight)).handle("goodnight")
    assert actions.run == [Action("control_home", {"command": "run goodnight", "wider": True})]


def test_a_step_which_is_a_routines_own_words_doesnt_start_it_again():
    llm = FakeLLM(_answer("Going."))
    reply = _conversation(llm=llm, routines=FakeRoutines({"name": "Loop", "phrases": ["go"], "steps": ["go"]})).handle("go")
    assert llm.asked == ["go"] and reply.say == "Going."


def test_the_answer_to_a_question_isnt_taken_for_a_routine():
    llm = FakeLLM(_answer("Okay."))
    _conversation(llm=llm, routines=FakeRoutines(MORNING)).handle("good morning", follow_up=True)
    assert llm.asked == ["good morning"]


def test_a_question_a_step_asks_isnt_listened_for():
    llm = FakeLLM(_answer("Which one?"))
    routine = {"name": "Lights", "phrases": ["lights"], "steps": ["turn on a light"]}
    reply = _conversation(llm=llm, routines=FakeRoutines(routine)).handle("lights")
    assert reply.say == "Which one?" and not reply.listen_again


# Asking before shutting the PC down

class FakePCHome:
    """Stands in for Home Assistant with the desk PC on, noting each button pressed."""

    def __init__(self):
        entries = [Entry("button.desk_off", "mqtt", "desk", None, "desk_off", "Shutdown", (), False),
                   Entry("button.desk_again", "mqtt", "desk", None, "desk_again", "Restart", (), False),
                   Entry("sensor.desk_cpu", "mqtt", "desk", None, "desk_cpu", "CPU Load", (), False)]
        self.registry_read = Snapshot(entries={entry.entity_id: entry for entry in entries},
                                      devices={"desk": {"name": "Desk PC", "maker": "HASS.Agent Team"}},
                                      areas={}, read_at=0.0)
        self.pressed = []

    def snapshot(self):
        return self.registry_read

    def states(self):
        return [{"entity_id": entity, "state": "12", "attributes": {}} for entity in self.registry_read.entries]

    def press(self, entity_id):
        self.pressed.append(entity_id)

    def describe_lights(self, timeout=None):
        return ""


class AskingActions(ActionExecutor):
    """The real actions, bar dim_lights, which asks "Which light?"."""

    # The tools are looked up on the class itself.
    tools = ActionExecutor.tools

    def execute(self, action):
        if action.tool == "dim_lights":
            return ActionResult(False, "Which light?")
        return super().execute(action)


def _pc_conversation(tmp_path, *replies):
    """A conversation with the real local rules and actions, and the PC on a fake Home Assistant."""
    home = FakePCHome()
    actions = AskingActions(home=home, pc=PCPower(home, ChoiceStore(str(tmp_path / "pcs.json"), {"wake": {}})))
    llm = FakeLLM(*replies)
    return _conversation(intents=LocalIntents(), llm=llm, actions=actions), home, llm


def test_shutting_down_asks_first_and_only_a_yes_does_it(tmp_path):
    conversation, home, llm = _pc_conversation(tmp_path)
    reply = conversation.handle("Shut down the PC.")
    assert (reply.say, reply.listen_again) == ("Shut down the PC?", True)
    assert home.pressed == []
    # Read before the follow-up window would send it to Claude.
    assert conversation.handle("yes", follow_up=True).say == "Shutting down the PC."
    assert home.pressed == ["button.desk_off"] and llm.asked == []

    conversation, home, llm = _pc_conversation(tmp_path)
    conversation.handle("Shut down the PC.")
    assert conversation.handle("no", follow_up=True).say == "Okay, I'll leave it."
    # And once answered, a later yes is nobody's.
    conversation.handle("Restart the PC.")
    assert conversation.handle("leave it", follow_up=True).say == "Okay, I'll leave it."
    assert home.pressed == []

    # Anything else drops it, and is a fresh request - so a yes after that is Claude's.
    conversation, home, llm = _pc_conversation(tmp_path, _answer("It's sunny. Anything else?"), _answer("Okay."))
    conversation.handle("Shut down the PC.")
    assert conversation.handle("what's the weather", follow_up=True).say == "It's sunny. Anything else?"
    conversation.handle("yes", follow_up=True)
    assert home.pressed == [] and llm.asked == ["what's the weather", "yes"]


def test_claudes_shutdown_asks_first_too(tmp_path):
    conversation, home, llm = _pc_conversation(
        tmp_path, _answer("Okay.", Action("pc_power", {"action": "restart"}, from_model=True)))
    assert conversation.handle("could you reboot the thing on my desk").say == "Restart the PC?"
    assert home.pressed == []
    assert conversation.handle("go on", follow_up=True).say == "Restarting the PC."
    assert home.pressed == ["button.desk_again"]


def test_a_yes_in_a_new_conversation_presses_nothing(tmp_path):
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay."), _answer("Okay."))
    conversation.handle("Shut down the PC.")
    conversation.start()
    conversation.handle("yes")
    assert home.pressed == [] and llm.asked == ["yes"]

    # Nor does a yes said to any other question.
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Shall I play some music?"), _answer("Okay."))
    conversation.handle("I'm bored")
    conversation.handle("yes", follow_up=True)
    assert home.pressed == []

    # Nor one said with no question listened for - the follow-ups had run out.
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay."))
    conversation.follow_ups = conversation.max_follow_ups
    assert not conversation.handle("Shut down the PC.").listen_again
    conversation.handle("yes")
    assert home.pressed == []


@pytest.mark.parametrize("answer", ["yes", "Yes please.", "Nova, yeah.", "yep", "go ahead", "Go on.", "sure",
                                    "Do it.", "Okay, do it.", "ok do it please", "Yes.", "'yes'",
                                    "\u2018yes\u2019"])
def test_only_a_plain_yes_shuts_the_pc_down(tmp_path, answer):
    conversation, home, llm = _pc_conversation(tmp_path)
    conversation.handle("Shut down the PC.")
    assert conversation.handle(answer, follow_up=True).say == "Shutting down the PC."
    assert home.pressed == ["button.desk_off"]


@pytest.mark.parametrize("answer, said", [
    # Backing out - "stop" and "cancel" most of all, which a reminder's offer takes for a yes.
    ("stop", "Okay, I'll leave it."), ("Cancel.", "Okay, I'll leave it."), ("no thanks", "Okay, I'll leave it."),
    ("Never mind.", "Okay, I'll leave it."), ("don't", "Okay, I'll leave it."), ("Nope.", "Okay, I'll leave it."),
    ("leave it", "Okay, I'll leave it."),
    # Neither, so a fresh request, Claude's - nor a yes with more said after it.
    ("shut up", "Okay."), ("thanks", "Okay."), ("fine", "Okay."), ("okay thanks", "Okay."),
    ("yes, play music", "Okay."), ("yes in a minute", "Okay."), ("absolutely", "Okay."),
    # Digits and other scripts are words too - "Yes, 10." most likely means in ten minutes.
    ("Yes, 10.", "Okay."), ("Yes. 10:30", "Okay."), ("yes 10", "Okay."), ("yes \u4e0d\u8981", "Okay."),
])
def test_no_or_anything_else_said_back_presses_nothing(tmp_path, answer, said):
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay."), _answer("Okay."))
    conversation.handle("Shut down the PC.")
    assert conversation.handle(answer, follow_up=True).say == said
    assert home.pressed == []
    assert llm.asked == ([] if said.startswith("Okay, I'll") else [answer])
    # Either way, it's gone.
    conversation.handle("yes", follow_up=True)
    assert home.pressed == []


@pytest.mark.parametrize("actions", [
    # The PC's question isn't the whole reply...
    [Action("pc_power", {"action": "off"}, from_model=True), Action("dim_lights", {}, from_model=True)],
    [Action("dim_lights", {}, from_model=True), Action("pc_power", {"action": "off"}, from_model=True)],
    # ...or there are two to answer.
    [Action("pc_power", {"action": "off"}, from_model=True),
     Action("pc_power", {"action": "restart"}, from_model=True)],
])
def test_a_yes_to_more_than_the_pcs_question_presses_nothing(tmp_path, actions):
    conversation, home, llm = _pc_conversation(tmp_path, _answer("Okay.", *actions), _answer("Okay."))
    reply = conversation.handle("shut it all down")
    assert reply.listen_again and reply.say.count("?") == 2
    conversation.handle("yes", follow_up=True)
    assert home.pressed == [] and llm.asked == ["shut it all down", "yes"]


def test_the_chime_can_be_a_setting_read_at_each_answer():
    # The session passes a function reading the running settings, so a save changes the next answer.
    chime = [True]
    conversation = _conversation(chime_enabled=lambda: chime[0])
    assert conversation.chime_enabled is True
    chime[0] = False
    assert conversation.chime_enabled is False


def test_the_done_chime_is_switched_off_straight_away():
    chime = [True]
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime")})
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    conversation = _conversation(intents=intents, actions=actions, chime_enabled=lambda: chime[0])
    assert conversation.handle("pause").sounds == [("success_chime.wav", "general")]
    chime[0] = False
    assert conversation.handle("pause").sounds == []
