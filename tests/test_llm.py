"""
LLM Handler Test
Tests how the model's replies are turned into actions and history, using fake
responses in place of the API, so no request is ever made.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
import pytest

from voice_assistant import llm as llm_module
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


@pytest.mark.parametrize("params, repeat", [
    ({"hour": 7, "minute": 0}, None),
    ({"hour": 7, "minute": 0, "repeat": "weekdays"}, "weekdays"),
    ({"hour": 7, "minute": 0, "repeat": "custom", "days": ["monday", "wednesday"]}, ["monday", "wednesday"]),
    # "custom" with no days is passed through as-is, so the clock asks which days.
    ({"hour": 7, "minute": 0, "repeat": "custom"}, "custom"),
])
def test_set_alarm_tool_becomes_a_clock_action(llm, params, repeat):
    action = llm._action_from_tool_use(_tool("set_alarm", **params))
    assert action == {"type": "clock", "command": "set_alarm", "hour": 7, "minute": 0, "repeat": repeat}


def test_volume_without_a_category_is_left_for_the_executor(llm):
    action = llm._action_from_tool_use(_tool("set_volume", direction="up"))
    assert action["type"] == "volume" and action["category"] is None


# What the model is told

def test_the_time_is_given_on_the_12_hour_clock(llm, monkeypatch):
    # Told it was "19:36", the model would sometimes say that straight back, which
    # Piper reads out as "nineteen thirty-six" - more like a year than a time.
    class EveningDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 19, 19, 36, tzinfo=timezone(timedelta(hours=1), "BST"))

    monkeypatch.setattr(llm_module, "datetime", EveningDatetime)
    llm.client = FakeClient(_reply("It's 7:36 PM."))
    llm.process_query("what time is it")

    system_prompt = llm.client.requests[0]["system"]
    assert "7:36 PM" in system_prompt
    assert "19:36" not in system_prompt


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
    assert result["actions"] == [{"type": "spotify", "command": "pause"}]


# Actions

def test_every_tool_call_is_run_not_just_the_first(llm):
    # "Pause the music and set a ten minute timer" comes back as two tool calls,
    # and only the first used to be acted on.
    llm.client = FakeClient(_reply("Okay.", _tool("control_playback", action="pause"),
                                   _tool("set_timer", duration_seconds=600, is_reminder=False)))
    result = llm.process_query("pause the music and set a ten minute timer")

    assert [(a["type"], a["command"]) for a in result["actions"]] == [("spotify", "pause"),
                                                                       ("clock", "set_timer")]


# History

def test_history_records_what_was_actually_said(llm):
    llm.client = FakeClient(_reply("Okay.", _tool("set_timer", duration_seconds=1200, is_reminder=True)))
    llm.process_query("remind me in 20 minutes")
    assert llm.history[-1]["assistant"] == "Okay."

    # What main.py does once the action has asked its question.
    llm.record_reply("remind me in 20 minutes", "What would you like to name this reminder?")
    assert llm.history[-1] == {"user": "remind me in 20 minutes",
                               "assistant": "What would you like to name this reminder?"}

    # The question is then part of what the model sees with the answer.
    llm.client = FakeClient(_reply("Okay.", _tool("name_reminder", label="Walk the dogs")))
    llm.process_query("walk the dogs")
    sent = llm.client.requests[0]["messages"]
    assert {"role": "assistant", "content": "What would you like to name this reminder?"} in sent


def test_record_reply_only_changes_the_matching_exchange(llm):
    llm.history = [{"user": "turn it up", "assistant": "Okay."}]
    llm.record_reply("something else", "Volume 80 percent.")
    assert llm.history[-1]["assistant"] == "Okay."
