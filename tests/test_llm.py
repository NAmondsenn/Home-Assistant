"""
LLM Handler Test
Tests how the model's replies are turned into actions and history, using fake
responses in place of the API, so no request is ever made.
"""

from datetime import datetime
from types import SimpleNamespace

import anthropic
import pytest

from voice_assistant import llm as llm_module
from voice_assistant.actions import Action
from voice_assistant.llm import LLMHandler


class FakeClient:
    """
    Stands in for the Anthropic client. Returns the given reply, or raises the given
    error, and keeps every request so the test can look at what was sent.
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
        self.requests.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


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


def _tool(name, **params):
    """A tool_use block, shaped like the ones the API returns."""
    return SimpleNamespace(type="tool_use", name=name, input=params)


@pytest.fixture
def llm():
    return LLMHandler(api_key=None)


def test_a_tool_call_becomes_an_action_naming_it(llm):
    assert llm._action_from_tool_use(_tool("set_timer", at="16:00", label="Call mum")) == \
        Action("set_timer", {"at": "16:00", "label": "Call mum"})


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


# Timeouts and failures

def test_client_is_given_a_timeout_and_few_retries(monkeypatch, make_config):
    made = {}
    monkeypatch.setattr(llm_module, "Anthropic", lambda **kwargs: made.update(kwargs) or SimpleNamespace())
    LLMHandler(config=make_config(llm={"timeout": 12, "max_retries": 1}), api_key="test-key")
    # The SDK's own default is 10 minutes with two retries.
    assert made["max_retries"] == 1
    assert made["timeout"] is not None


def test_web_searches_are_allowed_longer(llm):
    llm.client = FakeClient(_reply("It's sunny in Newcastle."))
    llm.process_query("search for the weather in Newcastle")
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


# History

def test_history_records_what_was_actually_said(llm):
    llm.client = FakeClient(_reply("Okay.", _tool("set_timer", duration_seconds=1200, is_reminder=True)))
    llm.process_query("remind me in 20 minutes")
    assert llm.history[-1]["assistant"] == "Okay."

    # What main.py does once the action has asked its question.
    llm.record_reply("remind me in 20 minutes", "What would you like to name this reminder?")
    assert (llm.history[-1]["user"], llm.history[-1]["assistant"]) == \
        ("remind me in 20 minutes", "What would you like to name this reminder?")

    # The question is then part of what the model sees with the answer.
    llm.client = FakeClient(_reply("Okay.", _tool("name_reminder", label="Walk the dogs")))
    llm.process_query("walk the dogs")
    sent = llm.client.requests[0]["messages"]
    assert {"role": "assistant", "content": "What would you like to name this reminder?"} in sent


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
