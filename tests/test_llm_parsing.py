"""
LLM Action Parsing Test
Tests LLMHandler._parse_action directly with a set of sample phrases,
checking the detected action (or none) against what's expected.

_parse_action only reads input text, meaning it doesn't use the LLM.
This is useful as the test can run without an API key or network connection.
"""

import pytest

from voice_assistant.actions import Action
from voice_assistant.llm import LLMHandler

# Tests each case: (query, expected action, or None if no action should be detected).
TEST_CASES = [
    ("Hello, how are you?", None),
    ("What's nine plus ten?", None),
    ("Play some house music", Action("play_music", {"query": "house music"})),
    ("Play", Action("play_music", {"query": None})),
    ("Pause Spotify", Action("control_playback", {"action": "pause"})),
    ("Stop the song", Action("control_playback", {"action": "pause"})),
    ("Skip this song", Action("control_playback", {"action": "skip"})),
    ("Next track", Action("control_playback", {"action": "skip"})),
    ("previous song", Action("control_playback", {"action": "previous"})),
    ("What's playing right now?", Action("control_playback", {"action": "current"})),
    ("What song is this?", Action("control_playback", {"action": "current"})),
]


@pytest.mark.parametrize("query, expected", TEST_CASES)
def test_parse_action(query, expected):
    """The keyword fallback finds the expected action."""
    # LLMHandler initialises without an API key (it just logs a warning), and
    # _parse_action never calls the API, so this test runs fully offline.
    llm = LLMHandler(api_key=None)
    actual = llm._parse_action(query, "")

    assert actual == expected
