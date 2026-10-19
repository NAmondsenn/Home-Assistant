"""
LLM Handler Test
Tests how the model's replies are turned into actions and history, using fake
responses in place of the API, so no request is ever made.
"""

from types import SimpleNamespace

import pytest

from voice_assistant.llm import LLMHandler


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
