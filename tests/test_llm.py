"""
LLM Handler Test
Tests how the model's replies are turned into actions and history, using fake
responses in place of the API, so no request is ever made.
"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import anthropic
import pytest

from voice_assistant import llm as llm_module
from voice_assistant.actions import Action, ActionExecutor
from voice_assistant.home_assistant import CANT_REACH
from voice_assistant.llm import LLMHandler


class FakeClient:
    """
    Stands in for the Anthropic client. Returns the given reply - or each of a list of
    them in turn - or raises the given error, and keeps every request so the test can
    look at what was sent, its messages as they were then.
    """

    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.options = []
        self.messages = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        outcome = self.outcome.pop(0) if isinstance(self.outcome, list) else self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _reply(text, *tools, stop_reason="end_turn"):
    """A model response with some text and, optionally, tool calls."""
    content = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(content=content + list(tools), stop_reason=stop_reason)


def _error(cls):
    """
    An instance of one of the SDK's error types. Built without calling __init__,
    since the real ones expect an HTTP request or response to be attached.
    """
    error = cls.__new__(cls)
    error.args = ("simulated",)
    return error


def _tool(name, use_id="toolu_1", **params):
    """A tool_use block, shaped like the ones the API returns."""
    return SimpleNamespace(type="tool_use", id=use_id, name=name, input=params)


@pytest.fixture
def llm():
    return LLMHandler(api_key=None)


def test_a_tool_call_becomes_an_action_naming_it(llm):
    # Marked as the model's, so it can only run what it was offered - see ActionExecutor.execute.
    assert llm._action_from_tool_use(_tool("set_timer", at="16:00", label="Call mum")) == \
        Action("set_timer", {"at": "16:00", "label": "Call mum"}, from_model=True)


def test_an_album_is_only_played_when_asked_for_by_name(llm):
    # Titles are often both a song and an album, and the model tends to assume the album.
    llm.client = FakeClient(_reply("Okay.", _tool("play_music", query="The Chronic by Dr. Dre", search_type="album")))
    assert llm.process_query("play the chronic")["actions"][0].params["search_type"] == "track"
    assert llm.process_query("play the chronic album")["actions"][0].params["search_type"] == "album"


# What the model is told

def test_the_time_is_given_on_the_12_hour_clock(llm, monkeypatch):
    # Told it was "19:36", the model would sometimes say that straight back, which
    # Piper reads out as "nineteen thirty-six" - more like a year than a time.
    # A plain local time, rather than one with a time zone, so it's still 19:36
    # whatever time zone the machine running the tests is set to.
    class EveningDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 19, 19, 36)

    monkeypatch.setattr(llm_module, "datetime", EveningDatetime)
    llm.client = FakeClient(_reply("It's 7:36 PM."))
    llm.process_query("what time is it")

    said = llm.client.requests[0]["messages"][-1]["content"]
    assert "7:36 PM" in said and said.endswith("what time is it")
    assert "19:36" not in said


def test_the_instructions_are_the_same_every_time_so_they_can_be_cached(llm, monkeypatch):
    # The time to the minute, and the timers running, used to be in the system
    # prompt, so it changed every minute and could never be cached.
    times = iter([datetime(2026, 10, 19, 19, 36), datetime(2026, 10, 19, 21, 5)])

    class MovingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    monkeypatch.setattr(llm_module, "datetime", MovingDatetime)
    llm.client = FakeClient(_reply("Okay."))
    llm.process_query("what time is it")
    llm.process_query("cancel the walk the frogs reminder", context="a walk the dogs reminder for 9:30 PM")

    first, second = (request["system"] for request in llm.client.requests)
    assert first == second and first[-1]["cache_control"] == {"type": "ephemeral"}
    assert "walk the dogs reminder" in llm.client.requests[1]["messages"][-1]["content"]


# What Claude is told beside what the user said

LUNCHTIME = datetime(2026, 10, 21, 11, 50, tzinfo=ZoneInfo("Europe/London"))
LAMP = "Nova is in the Bedroom. Lights: Lamp (light.lamp, Bedroom) off"


def test_the_timers_are_told_as_they_always_were_when_there_are_no_lights():
    assert LLMHandler._situation(LUNCHTIME, None) == (
        "[Wednesday 21 October 2026, 11:50 AM (BST). Timers and reminders running: none.] ")
    assert LLMHandler._situation(LUNCHTIME, "timer 1: 5 minutes") == (
        "[Wednesday 21 October 2026, 11:50 AM (BST). Timers and reminders running: timer 1: 5 minutes.] ")


def test_the_lights_follow_the_timers_in_the_same_brackets():
    # Their own sentences, not part of the timers' list: "Timers and reminders running:
    # Nova is in the Bedroom" would have Claude reading the lights as a timer.
    assert LLMHandler._situation(LUNCHTIME, None, LAMP) == (
        "[Wednesday 21 October 2026, 11:50 AM (BST). Timers and reminders running: none. "
        "Nova is in the Bedroom. Lights: Lamp (light.lamp, Bedroom) off.] ")
    assert "running: timer 1: 5 minutes. Nova is in the Bedroom." in LLMHandler._situation(
        LUNCHTIME, "timer 1: 5 minutes", LAMP)


def test_lights_which_already_end_in_a_full_stop_arent_given_another():
    assert LLMHandler._situation(LUNCHTIME, None, "Nova is in the Bedroom. There are no lights.").endswith(
        "There are no lights.] ")


def test_the_lights_reach_claude_with_the_message(llm):
    llm.client = FakeClient(_reply("Okay."))
    llm.process_query("dim the lights", context="timer 1: 5 minutes", lights=LAMP)
    said = llm.client.requests[0]["messages"][-1]["content"]
    assert "Timers and reminders running: timer 1: 5 minutes. Nova is in the Bedroom. Lights: Lamp" in said
    assert said.endswith("off.] dim the lights")


def test_the_instructions_are_the_same_with_the_lights_in_the_message(llm):
    llm.client = FakeClient(_reply("Okay."))
    llm.process_query("what time is it")
    llm.process_query("dim the lights", lights=LAMP)
    first, second = (request["system"] for request in llm.client.requests)
    assert first == second


# Timeouts and failures

def test_client_is_given_a_timeout_and_few_retries(monkeypatch, make_config):
    made = {}
    monkeypatch.setattr(llm_module, "Anthropic", lambda **kwargs: made.update(kwargs) or SimpleNamespace())
    LLMHandler(config=make_config(llm={"timeout": 12, "max_retries": 1}), api_key="test-key")
    # The SDK's own default is 10 minutes with two retries.
    assert made["max_retries"] == 1
    assert made["timeout"] is not None


def test_web_searches_are_allowed_longer(llm):
    llm.client = FakeClient(_reply("It's sunny in Bristol."))
    llm.process_query("search for the weather in Bristol")
    assert llm.client.options == [{"timeout": llm.search_timeout}]


@pytest.mark.parametrize("error, spoken", [
    (anthropic.APITimeoutError, "took too long"),
    (anthropic.APIConnectionError, "can't reach the internet"),
    (anthropic.AuthenticationError, "API key"),
    (anthropic.RateLimitError, "too many requests"),
    (anthropic.APIStatusError, "having problems"),
    (ValueError, "trouble connecting"),
])
def test_each_kind_of_failure_says_what_went_wrong(llm, error, spoken):
    llm.client = FakeClient(_error(error))
    result = llm.process_query("what's the capital of France")
    assert result["success"] is False and spoken in result["response"]


def test_a_failed_request_still_handles_music_offline(llm):
    llm.client = FakeClient(_error(anthropic.APIConnectionError))
    result = llm.process_query("pause the music")
    assert result["actions"] == [Action("control_playback", {"action": "pause"})]


# Actions

def test_every_tool_call_is_run_not_just_the_first(llm):
    # "Pause the music and set a ten minute timer" comes back as two tool calls,
    # and only the first used to be acted on.
    llm.client = FakeClient(_reply("Okay.", _tool("control_playback", action="pause"),
                                   _tool("set_timer", duration_seconds=600, is_reminder=False)))
    result = llm.process_query("pause the music and set a ten minute timer")

    assert [action.tool for action in result["actions"]] == ["control_playback", "set_timer"]


# Reading before answering

SUNSET = "Sun next setting (sensor.sun_next_setting): 2026-10-20 17:52"


def _reader():
    """An LLMHandler offered home_status as a read tool."""
    return LLMHandler(api_key=None, read_tools=["home_status"])


def _results(message):
    """A user message's tool_results, as (id, content) pairs."""
    return [(result["tool_use_id"], result["content"]) for result in message["content"]]


def test_a_read_tools_answer_goes_back_to_claude():
    llm = _reader()
    asking = _reply("Let me check.", _tool("home_status", use_id="t1", about="sunset"))
    llm.client = FakeClient([asking, _reply("Sunset's at 5:52 PM.")])
    read = []

    def reader(action):
        read.append(action)
        return SUNSET

    result = llm.process_query("when's sunset?", read=reader)
    assert read == [Action("home_status", {"about": "sunset"}, from_model=True)]
    first, second = llm.client.requests
    assert second["messages"][:-2] == first["messages"]
    assert second["messages"][-2:] == [
        {"role": "assistant", "content": asking.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": SUNSET}]}]
    assert (result["response"], result["actions"], result["success"]) == ("Sunset's at 5:52 PM.", [], True)
    # Only the final reply is kept, as for any other turn.
    assert (llm.history[-1]["assistant"], llm.history[-1]["tools"]) == ("Sunset's at 5:52 PM.", [])
    # Tools may still be called in the round after a read.
    assert "tool_choice" not in second


def test_two_reads_at_once_are_each_answered_in_turn():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sunset"),
                                    _tool("home_status", use_id="t2", about="navidrome")),
                             _reply("Sunset's at 5:52 PM, and Navidrome is up.")])
    result = llm.process_query("when's sunset, and is navidrome up?", read=lambda action: action.params["about"])
    assert _results(llm.client.requests[1]["messages"][-1]) == [("t1", "sunset"), ("t2", "navidrome")]
    assert result["response"] == "Sunset's at 5:52 PM, and Navidrome is up."


def test_a_tool_called_after_reading_is_an_action():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="desk lamp")),
                             _reply("Okay.", _tool("set_lights", use_id="t2", on=False, light="light.desk_lamp"))])
    result = llm.process_query("if the desk lamp's on, turn it off", read=lambda action: "Desk lamp: on")
    assert result["actions"] == [Action("set_lights", {"on": False, "light": "light.desk_lamp"}, from_model=True)]


def test_reads_stop_after_two_rounds():
    # A model which keeps reading can't hold the answer up for long: the last round may
    # call no tool, so Claude says what it found - and a read it calls anyway is dropped,
    # never said as it is.
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id=f"t{n}", about="sunset")) for n in range(5)])
    result = llm.process_query("when's sunset?", read=lambda action: SUNSET)
    assert len(llm.client.requests) == 1 + llm_module.MOST_READS == 3
    first, second, third = llm.client.requests
    assert "tool_choice" not in first and "tool_choice" not in second
    assert third["tool_choice"] == {"type": "none"}
    assert _results(second["messages"][-1]) == [("t0", SUNSET)]
    assert _results(third["messages"][-1]) == [("t1", SUNSET)]
    assert (result["response"], result["actions"]) == ("Sorry, I couldn't find that out.", [])


def test_a_read_without_a_way_to_read_is_never_said():
    llm = _reader()
    llm.client = FakeClient([_reply("Okay.", _tool("home_status", about="sunset"))])
    result = llm.process_query("when's sunset?")
    assert (result["response"], result["actions"]) == ("Sorry, I couldn't find that out.", [])
    assert len(llm.client.requests) == 1


def test_a_read_tool_beside_another_tool_is_dropped():
    # Read and then asked again, the timer beside it could be asked for twice - so the read
    # is dropped, and the timer and Claude's words kept.
    llm = _reader()
    llm.client = FakeClient([_reply("I'll set that timer.", _tool("home_status", use_id="t1", about="sunset"),
                                    _tool("set_timer", use_id="t2", duration_seconds=600))])
    read = []
    result = llm.process_query("when's sunset, and set a ten minute timer", read=read.append)
    assert read == [] and len(llm.client.requests) == 1
    assert result["actions"] == [Action("set_timer", {"duration_seconds": 600}, from_model=True)]
    assert result["response"] == "I'll set that timer."
    assert llm.history[-1]["tools"] == [("set_timer", {"duration_seconds": 600})]


# Speech from what's playing

def test_whats_playing_is_told_only_while_something_plays():
    assert LLMHandler._situation(LUNCHTIME, None, LAMP, ["Firefox", "Windows PC"]) == (
        "[Wednesday 21 October 2026, 11:50 AM (BST). Timers and reminders running: none. "
        "Nova is in the Bedroom. Lights: Lamp (light.lamp, Bedroom) off. Playing in this room: Firefox, Windows PC.] ")
    assert "Playing" not in LLMHandler._situation(LUNCHTIME, None, LAMP, [])
    assert "Playing" not in LLMHandler._situation(LUNCHTIME, None, LAMP)


def test_whats_playing_reaches_claude_with_the_message(llm):
    llm.client = FakeClient(_reply("It's 11:50 AM."))
    llm.process_query("what time is it", lights=LAMP, playing=["Bedroom TV"])
    llm.process_query("what time is it", lights=LAMP)
    first, second = (request["messages"][-1]["content"] for request in llm.client.requests)
    assert first.endswith("off. Playing in this room: Bedroom TV.] what time is it")
    assert "Playing" not in second


def test_claude_is_always_told_about_not_for_me_so_the_cache_holds(llm):
    # Told only while something plays, the instructions and tools would change as a film
    # starts and stops, and be read through again in full.
    llm.tools = [{"name": "set_lights", "description": "Lights.", "input_schema": {"type": "object"}}]
    llm.client = FakeClient(_reply("Okay."))
    llm.process_query("what time is it", playing=["Bedroom TV"])
    llm.process_query("what time is it")
    first, second = llm.client.requests
    assert first["system"] == second["system"] and first["tools"] == second["tools"]
    assert "call not_for_me and nothing else" in first["system"][0]["text"]
    assert "when in doubt" not in first["system"][0]["text"].lower()
    assert "clear command or question for a voice assistant" in first["system"][0]["text"]
    assert "Never make conversation" in first["system"][0]["text"]
    assert "never with any other tool" in first["tools"][-1]["description"]
    assert "doubt" not in first["tools"][-1]["description"]
    assert [tool["name"] for tool in first["tools"]] == ["set_lights", "not_for_me"]
    assert first["tools"][-1]["input_schema"] == {"type": "object", "properties": {}}


def test_not_for_me_runs_nothing_else_from_the_same_response(llm):
    # A line from a film - "turn the lights off, Jack" - must never work the lights, even if
    # Claude asked for them beside it.
    llm.history = [{"user": "what time is it", "assistant": "It's 7:36 PM.", "at": 0.0}]
    kept = [dict(turn) for turn in llm.history]
    llm.client = FakeClient(_reply("Okay.", _tool("not_for_me", use_id="t1"),
                                   _tool("set_lights", use_id="t2", on=False, light="light.lamp")))
    result = llm.process_query("turn the lights off, Jack", playing=["Bedroom TV"])
    assert result == {"response": "", "actions": [], "success": True, "not_for_me": True}
    # Not in the thread, the user's words included.
    assert llm.history == kept
    assert len(llm.client.requests) == 1


def test_not_for_me_beside_a_read_reads_nothing():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sunset"),
                                    _tool("not_for_me", use_id="t2"))])
    read = []
    result = llm.process_query("when's sunset?", read=read.append, playing=["Bedroom TV"])
    assert read == [] and len(llm.client.requests) == 1
    assert result["not_for_me"] is True and result["actions"] == []


def test_not_for_me_after_reading_keeps_nothing_of_the_reads():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sunset")),
                             _reply("", _tool("not_for_me", use_id="t2"))])
    result = llm.process_query("when's sunset?", read=lambda action: SUNSET, playing=["Bedroom TV"])
    assert len(llm.client.requests) == 2
    assert (result["not_for_me"], result["actions"], llm.history) == (True, [], [])


def test_an_ordinary_answer_isnt_marked_not_for_me(llm):
    llm.client = FakeClient(_reply("It's 7:36 PM."))
    assert "not_for_me" not in llm.process_query("what time is it", playing=["Bedroom TV"])


# Claude can't be reached while something plays: the keyword parser would act on a line
# from the film - "pause the music, Jack" - that Claude was never there to rule out.

def test_no_api_key_runs_no_fallback_action_while_something_plays(llm):
    result = llm.process_query("pause the music", playing=["Bedroom TV"])
    assert result == {"response": "Sorry, I'm not connected to my language model right now.",
                      "actions": [], "success": False}
    assert llm.process_query("pause the music")["actions"] == [Action("control_playback", {"action": "pause"})]


def test_offline_runs_no_fallback_action_while_something_plays(llm, monkeypatch):
    monkeypatch.setattr(llm_module.network, "online", lambda: False)
    llm.client = FakeClient(_reply("Okay."))
    result = llm.process_query("pause the music", playing=["Bedroom TV"])
    assert result == {"response": llm_module.OFFLINE_MESSAGE, "actions": [], "success": False}
    assert llm.process_query("pause the music")["actions"] == [Action("control_playback", {"action": "pause"})]


def test_a_failed_request_runs_no_fallback_action_while_something_plays(llm, monkeypatch):
    monkeypatch.setattr(llm_module.network, "online", lambda: True)
    error = _error(anthropic.APIConnectionError)
    llm.client = FakeClient(error)
    result = llm.process_query("pause the music", playing=["Bedroom TV"])
    assert result == {"response": llm._failure_message(error), "actions": [], "success": False}
    assert llm.process_query("pause the music")["actions"] == [Action("control_playback", {"action": "pause"})]


def test_home_assistant_down_reaches_claude_rather_than_being_said():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sunset")),
                             _reply("I can't reach Home Assistant to check.")])
    home = SimpleNamespace(status=lambda about: {"success": False, "message": CANT_REACH})
    executor = ActionExecutor(home=home)
    result = llm.process_query("when's sunset?", read=lambda action: executor.execute(action).say)
    assert _results(llm.client.requests[1]["messages"][-1]) == [("t1", CANT_REACH)]
    assert (result["response"], result["actions"]) == ("I can't reach Home Assistant to check.", [])


def test_a_read_that_fails_tells_claude_so():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sunset")),
                             _reply("Sorry, I couldn't check that.")])

    def broken(action):
        raise RuntimeError("bug")

    result = llm.process_query("when's sunset?", read=broken)
    answered, = llm.client.requests[1]["messages"][-1]["content"]
    assert (answered["tool_use_id"], answered["is_error"]) == ("t1", True)
    assert result["response"] == "Sorry, I couldn't check that."


def test_a_failure_after_reading_runs_no_fallback_action():
    # The offline parser would take "are the lights on" for a light command.
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="lights")),
                             _error(anthropic.APIConnectionError)])
    result = llm.process_query("are the lights on", read=lambda action: "Desk lamp: on")
    assert (result["actions"], result["success"]) == ([], False)
    assert llm._fallback_actions("are the lights on") != []


def test_a_paused_search_after_reading_is_carried_on():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sunset")),
                             _reply("", stop_reason="pause_turn"),
                             _reply("Sunset's at 5:52 PM.")])
    result = llm.process_query("when's sunset?", read=lambda action: SUNSET)
    assert len(llm.client.requests) == 3 and result["response"] == "Sunset's at 5:52 PM."


# History

def test_history_records_what_was_actually_said(llm):
    llm.client = FakeClient(_reply("Okay.", _tool("set_timer", duration_seconds=1200, is_reminder=True)))
    llm.process_query("remind me in 20 minutes")
    assert llm.history[-1]["assistant"] == "Okay."

    # What the conversation does once the action has asked its question.
    llm.record_reply("remind me in 20 minutes", "What would you like to name this reminder?")
    assert (llm.history[-1]["user"], llm.history[-1]["assistant"]) == \
        ("remind me in 20 minutes", "What would you like to name this reminder?")

    # The question is then part of what the model sees with the answer.
    llm.client = FakeClient(_reply("Okay.", _tool("name_reminder", label="Walk the dogs")))
    llm.process_query("walk the dogs")
    sent = llm.client.requests[0]["messages"]
    assert {"role": "assistant", "content": "What would you like to name this reminder?"} in sent


def test_what_a_tool_said_is_shown_to_the_model_as_the_tools_own_answer(llm):
    # Kept only as text, the weather a tool had fetched looked to the model like
    # figures it had made up - asked next to "send the weather report to my phone",
    # it said so, and fetched it all again.
    llm.client = FakeClient(_reply("Okay.", _tool("get_weather", day="today")))
    llm.process_query("what's the weather")
    llm.record_reply("what's the weather", "It's 10 degrees and cloudy. Also, your timer's going.",
                     ["It's 10 degrees and cloudy."])

    llm.client = FakeClient(_reply("Okay.", _tool("get_weather", day="tomorrow")))
    llm.process_query("and tomorrow?")
    sent = llm.client.requests[0]["messages"]
    assert sent[0] == {"role": "user", "content": "what's the weather"}
    asked, answered = sent[1]["content"], sent[2]["content"]
    assert asked[0] == {"type": "text", "text": "Okay."}
    assert (asked[1]["type"], asked[1]["name"], asked[1]["input"]) == ("tool_use", "get_weather", {"day": "today"})
    assert answered == [{"type": "tool_result", "tool_use_id": asked[1]["id"], "content": "It's 10 degrees and cloudy."}]
    assert sent[3] == {"role": "assistant", "content": "It's 10 degrees and cloudy. Also, your timer's going."}


def test_a_tool_which_said_nothing_still_answers_the_model(llm):
    # Confirmed by a chime or the music changing, nothing's recorded - but the API
    # refuses a tool call left without an answer.
    llm.client = FakeClient(_reply("Okay.", _tool("control_playback", action="pause"),
                                   _tool("set_timer", duration_seconds=600, is_reminder=False)))
    llm.process_query("pause the music and set a ten minute timer")
    llm.client = FakeClient(_reply("You're welcome."))
    llm.process_query("thanks")
    asked, answered = llm.client.requests[0]["messages"][1:3]
    assert [result["content"] for result in answered["content"]] == ["Done.", "Done."]
    assert [result["tool_use_id"] for result in answered["content"]] == \
        [use["id"] for use in asked["content"] if use["type"] == "tool_use"]
    assert len({result["tool_use_id"] for result in answered["content"]}) == 2


def test_record_reply_only_changes_the_matching_exchange(llm):
    llm.history = [{"user": "turn it up", "assistant": "Okay."}]
    llm.record_reply("something else", "Volume 80 percent.")
    assert llm.history[-1]["assistant"] == "Okay."


def test_old_exchanges_are_forgotten_when_a_new_conversation_starts(llm):
    # This morning's "play that one" shouldn't colour tonight's "yes".
    llm.history = [{"user": "play that one", "assistant": "Which one?", "at": 0.0},
                   {"user": "what time is it", "assistant": "It's 7:36 PM.", "at": 900.0}]
    llm.forget_stale_history(now=1000.0)
    assert [turn["user"] for turn in llm.history] == ["what time is it"]


def test_exchanges_remember_when_they_happened(llm):
    llm.client = FakeClient(_reply("It's 7:36 PM."))
    llm.process_query("what time is it")
    assert "at" in llm.history[-1]


def test_offline_claude_isnt_waited_on(llm, monkeypatch):
    # With the network cable out, a request used to wait out its timeout and retry -
    # over half a minute - before saying anything.
    monkeypatch.setattr(llm_module.network, "online", lambda: False)
    llm.client = FakeClient(_reply("Okay."))
    result = llm.process_query("pause the music")

    assert llm.client.requests == []
    assert result["response"] == llm_module.OFFLINE_MESSAGE
    assert result["actions"] == [Action("control_playback", {"action": "pause"})]


def test_reply_lengths_are_the_ones_set(make_config):
    # They were once quietly raised to at least 300, and 400 for a search, whatever
    # config.yaml said - so changing the setting changed nothing.
    llm = LLMHandler(config=make_config(llm={"max_tokens": 200, "max_tokens_search": 500}), api_key=None)
    llm.client = FakeClient(_reply("Paris."))
    llm.process_query("what's the capital of France")
    llm.process_query("search for the weather in Bristol")
    assert [request["max_tokens"] for request in llm.client.requests] == [200, 500]


def test_thinking_is_adaptive_at_low_effort_and_no_temperature_is_sent(llm):
    # Haiku 5.5 refuses any request which sets a temperature, so none is sent unless
    # one is configured.
    llm.client = FakeClient(_reply("Paris."))
    llm.process_query("what's the capital of France")
    request = llm.client.requests[-1]
    assert request["thinking"] == {"type": "adaptive"} and request["output_config"] == {"effort": "low"}
    assert "temperature" not in request


def test_a_temperature_which_is_set_is_sent(make_config):
    llm = LLMHandler(config=make_config(llm={"temperature": 0.3}), api_key=None)
    llm.client = FakeClient(_reply("Paris."))
    llm.process_query("what's the capital of France")
    assert llm.client.requests[-1]["temperature"] == 0.3


def test_the_effort_is_the_one_set(make_config):
    llm = LLMHandler(config=make_config(llm={"effort": "medium"}), api_key=None)
    llm.client = FakeClient(_reply("Paris."))
    llm.process_query("what's the capital of France")
    assert llm.client.requests[-1]["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("effort", [None, "", "mediun", 3])
def test_an_effort_which_isnt_one_falls_back_to_low_with_a_warning(make_config, caplog, effort):
    with caplog.at_level("WARNING"):
        llm = LLMHandler(config=make_config(llm={"effort": effort}), api_key=None)
    llm.client = FakeClient(_reply("Paris."))
    llm.process_query("what's the capital of France")
    assert llm.client.requests[-1]["output_config"] == {"effort": "low"}
    assert any("effort" in record.getMessage() for record in caplog.records if record.levelname == "WARNING")


# Claude's working-out is never said

def _thinking(text="The user said 'Light', which is ambiguous..."):
    """
    A thinking block, with the fields the API's have and no text. Its text is filled in
    here, which is stricter than the API's own default of an empty one.
    """
    return SimpleNamespace(type="thinking", thinking=text, signature="sig")


def _thought(response, *blocks):
    """The response with thinking blocks put before what it holds."""
    response.content = list(blocks) + response.content
    return response


def test_a_thinking_block_is_not_said(llm):
    llm.client = FakeClient(_thought(_reply("Paris."), _thinking()))
    assert llm.process_query("what's the capital of France")["response"] == "Paris."


def test_a_block_which_carries_text_but_isnt_text_is_not_said(llm):
    # Only a block which says it's text is taken for speech, so a future kind of block
    # with a text field - or a thinking one given one - can't be spoken.
    odd = SimpleNamespace(type="thinking", text="Working out which Friday...", thinking="x", signature="s")
    llm.client = FakeClient(_thought(_reply("Paris."), odd))
    assert llm.process_query("what's the capital of France")["response"] == "Paris."


def test_a_thinking_block_never_reaches_the_history(llm):
    llm.client = FakeClient(_thought(_reply("Paris."), _thinking("Working out which city...")))
    llm.process_query("what's the capital of France")
    assert "Working out" not in repr(llm.history)
    llm.client = FakeClient(_reply("Yes."))
    llm.process_query("and is it big")
    assert "Working out" not in repr(llm.client.requests[0]["messages"])


def test_a_thinking_block_then_not_for_me_is_silent(llm):
    llm.client = FakeClient(_thought(_reply("", _tool("not_for_me", use_id="t1")), _thinking()))
    result = llm.process_query("turn the lights off, Jack", playing=["Bedroom TV"])
    assert result == {"response": "", "actions": [], "success": True, "not_for_me": True}
    assert llm.history == []


def test_a_thinking_block_goes_back_unchanged_beside_a_reads_answer():
    llm = _reader()
    thought = _thinking()
    first = _thought(_reply("", _tool("home_status", use_id="t1", about="sunset")), thought)
    llm.client = FakeClient([first, _thought(_reply("Sunset's at 5:52 PM."), _thinking("Done."))])
    result = llm.process_query("when's sunset?", read=lambda action: SUNSET)
    assert result["response"] == "Sunset's at 5:52 PM."
    sent = llm.client.requests[1]["messages"]
    assistant = sent[-2]
    assert assistant["role"] == "assistant" and assistant["content"][0] is thought
    assert assistant["content"] == first.content and assistant["content"][0].thinking == _thinking().thinking


def _warnings(caplog):
    return [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]


def _no_empty_assistant_turn(request):
    """Whether no assistant message in the request has nothing in it, which the API refuses."""
    return all(message["content"] for message in request["messages"] if message["role"] == "assistant")


@pytest.mark.parametrize("response", [
    _thought(_reply("", stop_reason="max_tokens"), _thinking()),
    _thought(_reply("", stop_reason="end_turn"), SimpleNamespace(type="redacted_thinking", data="x")),
    _reply("", stop_reason="refusal"),
    _reply("", stop_reason="end_turn"),
], ids=["thinking cut off", "redacted thinking alone", "refusal", "plain empty"])
def test_a_reply_with_nothing_in_it_says_something_and_warns(llm, caplog, response):
    llm.client = FakeClient([response, _reply("Yes.")])
    with caplog.at_level("WARNING"):
        result = llm.process_query("what have I got on next Friday")
    assert result["response"] == llm_module.DIDNT_WORK and result["actions"] == []
    assert any(response.stop_reason in warning for warning in _warnings(caplog))

    # What's said is what's kept, so the next request doesn't send an empty turn.
    llm.process_query("and the Friday after")
    assert _no_empty_assistant_turn(llm.client.requests[-1])
    assert llm.history[0]["assistant"] == llm_module.DIDNT_WORK


def test_a_reply_cut_off_while_thinking_after_a_read_says_something_and_warns(caplog):
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sunset")),
                             _thought(_reply("", stop_reason="max_tokens"), _thinking())])
    with caplog.at_level("WARNING"):
        result = llm.process_query("when's sunset?", read=lambda action: SUNSET)
    assert result["response"] == llm_module.COULDNT_FIND_OUT
    assert any("max_tokens" in warning for warning in _warnings(caplog))


def test_thinking_on_the_last_round_after_two_reads_is_sent_back_and_answered():
    llm = _reader()
    llm.client = FakeClient([_thought(_reply("", _tool("home_status", use_id="t1", about="sunset")), _thinking("One")),
                             _thought(_reply("", _tool("home_status", use_id="t2", about="sunrise")), _thinking("Two")),
                             _thought(_reply("Sunset's at 5:52 PM."), _thinking("Three"))])
    result = llm.process_query("when's sunset?", read=lambda action: SUNSET)
    assert result["response"] == "Sunset's at 5:52 PM."
    first, second, last = llm.client.requests
    assert "tool_choice" not in first and last["tool_choice"] == {"type": "none"}
    assert last["thinking"] == {"type": "adaptive"}
    assistant_turns = [m["content"] for m in last["messages"] if m["role"] == "assistant"]
    assert [turn[0].thinking for turn in assistant_turns[-2:]] == ["One", "Two"]


def test_a_reply_cut_off_in_its_text_is_said_but_warned_about(llm, caplog):
    llm.client = FakeClient(_reply("It's a very long story, so", stop_reason="max_tokens"))
    with caplog.at_level("WARNING"):
        result = llm.process_query("tell me a story")
    assert result["response"] == "It's a very long story, so"
    assert any("max_tokens" in warning for warning in _warnings(caplog))


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
@pytest.mark.parametrize("call, said", [
    (_tool("set_timer", duration_seconds=1200), "Setting a 20 minute timer."),
    (_tool("home_status", about="sunset"), "Let me check."),
    (_tool("remember", fact="Likes tea"), "I'll remember that."),
], ids=["set_timer", "home_status", "remember"])
def test_nothing_is_run_or_claimed_from_a_reply_which_stopped_short(caplog, stop_reason, call, said):
    # Its words go with its tools, whichever tool: "Setting a 20 minute timer." said with no timer set.
    llm = _reader()
    llm.client = FakeClient([_reply(said, call, stop_reason=stop_reason), _reply("Yes.")])
    with caplog.at_level("WARNING"):
        result = llm.process_query("set a timer for 20 minutes")
    assert result["actions"] == [] and result["response"] == llm_module.DIDNT_WORK
    assert any(stop_reason in warning for warning in _warnings(caplog))
    assert len(llm.client.requests) == 1
    assert llm.history[0]["tools"] == [] and llm.history[0]["assistant"] == llm.history[0]["model_text"]
    assert said not in llm.history[0]["assistant"]

    llm.process_query("and a second")
    assert said not in repr(llm.client.requests[-1]["messages"])


def test_a_reply_which_stopped_short_after_a_read_says_it_couldnt_find_out(caplog):
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sun")),
                             _reply("Setting it.", _tool("set_timer", duration_seconds=60), stop_reason="max_tokens")])
    result = llm.process_query("when's sunset?", read=lambda action: SUNSET)
    assert result["actions"] == [] and result["response"] == llm_module.COULDNT_FIND_OUT


def test_a_reply_which_stopped_short_isnt_also_said_to_be_cut_off_mid_sentence(llm, caplog):
    llm.client = FakeClient(_reply("Setting it.", _tool("set_timer", duration_seconds=60), stop_reason="max_tokens"))
    with caplog.at_level("WARNING"):
        llm.process_query("set a timer for a minute")
    assert not any("mid-sentence" in warning for warning in _warnings(caplog))


def test_a_refusal_is_warned_of_with_its_category_even_with_words_beside_it(llm, caplog):
    details = SimpleNamespace(type="refusal", category="general_harms")
    response = _reply("I can't help with that.", stop_reason="refusal")
    response.stop_details = details
    llm.client = FakeClient(response)
    with caplog.at_level("WARNING"):
        result = llm.process_query("call the police, Sarah")
    assert result["response"] == "I can't help with that."
    assert any("refus" in warning and "general_harms" in warning for warning in _warnings(caplog))


def test_a_refusal_with_no_details_is_still_warned_of(llm, caplog):
    llm.client = FakeClient(_reply("I can't help with that.", stop_reason="refusal"))
    with caplog.at_level("WARNING"):
        llm.process_query("call the police, Sarah")
    assert any("refus" in warning for warning in _warnings(caplog))


def test_a_read_isnt_made_from_a_reply_which_stopped_short():
    llm = _reader()
    llm.client = FakeClient([_reply("", _tool("home_status", use_id="t1", about="sun"), stop_reason="max_tokens")])
    read = []
    result = llm.process_query("when's sunset?", read=read.append)
    assert read == [] and len(llm.client.requests) == 1
    assert result["response"] == llm_module.DIDNT_WORK


def test_thinking_on_a_web_search_reply_isnt_said(llm):
    search = [SimpleNamespace(type="server_tool_use", id="s1", name="web_search", input={"query": "weather"}),
              SimpleNamespace(type="web_search_tool_result", tool_use_id="s1", content=[])]
    llm.client = FakeClient(_thought(_reply("It's sunny in Bristol."), _thinking("Checking the results..."), *search))
    result = llm.process_query("search for the weather in Bristol")
    assert result["response"] == "It's sunny in Bristol."
    assert "web_search" in [tool["name"] for tool in llm.client.requests[0]["tools"]]
    assert "Checking" not in repr(llm.history)


def test_thinking_goes_back_unchanged_on_a_pause_turn_continuation(llm):
    thought = _thinking("Looking it up...")
    paused = _thought(_reply("", stop_reason="pause_turn"), thought,
                      SimpleNamespace(type="server_tool_use", id="s1", name="web_search", input={"query": "weather"}))
    llm.client = FakeClient([paused, _thought(_reply("It's sunny in Bristol."), _thinking("Done."))])
    result = llm.process_query("search for the weather in Bristol")
    assert result["response"] == "It's sunny in Bristol."
    assert len(llm.client.requests) == 2
    assistant = llm.client.requests[1]["messages"][-1]
    assert assistant["role"] == "assistant" and assistant["content"] == paused.content
    assert assistant["content"][0] is thought and thought.thinking == "Looking it up..."


def test_a_thinking_block_with_text_isnt_taken_for_empty(llm):
    llm.client = FakeClient(_thought(_reply("Okay."), _thinking()))
    assert llm.process_query("thanks")["response"] == "Okay."


@pytest.mark.parametrize("said, item, where", [
    # Claude would split them; offline, they're kept as one thing rather than lost.
    ("Add eggs, milk and bread to my shopping list.", "eggs milk and bread", "shopping"),
    ("add milk to notes", "milk", "notes"),
    ("Add salt and vinegar crisps to the todo list.", "salt and vinegar crisps", "todo"),
    # The last " to " is the list's, and "to do" is a list's name.
    ("Add a trip to Ikea to my list.", "a trip to ikea", "list"),
    ("Add milk to my to-do list.", "milk", "to do"),
    ("Add one egg to my list.", "one egg", "list"),
    ("Add the last of the bread to my list.", "the last of the bread", "list"),
])
def test_offline_several_things_are_added_as_one(llm, said, item, where):
    llm.client = FakeClient(_error(anthropic.APIConnectionError))
    assert llm.process_query(said)["actions"] == [Action("add_to_list", {"item": item, "list": where})]


@pytest.mark.parametrize("said", ["Add ten minutes to the timer.", "Add the dentist to my calendar.",
                                  "Add it to my list.", "Add to my list.", "What should I add to my list?",
                                  "Add them to my shopping list.", "Add all of those to my list.",
                                  "Add a note to my phone's notes.", "Add that cake to my list.",
                                  "Add the other one to my list."])
def test_offline_what_isnt_a_list_isnt_added_to_one(llm, said):
    llm.client = FakeClient(_error(anthropic.APIConnectionError))
    assert llm.process_query(said)["actions"] == []


def test_web_search_is_switched_off_straight_away(make_config):
    config = make_config(web_search={"enabled": True})
    llm = LLMHandler(config=config, api_key=None)
    llm.client = FakeClient([_reply("It's sunny."), _reply("It's sunny.")])
    llm.process_query("search for the weather in Bristol")
    config.apply({("web_search", "enabled"): False})
    llm.process_query("search for the weather in Bristol")

    first, second = llm.client.requests
    assert "web_search" in [tool["name"] for tool in first["tools"]]
    assert "web_search" not in [tool["name"] for tool in second.get("tools", [])]


def test_a_shorter_thread_is_used_at_the_next_conversation(make_config):
    config = make_config(conversation={"history_max_age": 300})
    llm = LLMHandler(config=config, api_key=None)
    llm.history = [{"user": "play that one", "assistant": "Which one?", "at": 900.0}]
    config.apply({("conversation", "history_max_age"): 60})
    llm.forget_stale_history(now=1000.0)
    assert llm.history == []


def test_without_a_config_the_constructors_values_are_used():
    llm = LLMHandler(api_key=None)
    assert llm.history_max_age == 300 and llm.web_search_enabled is True
