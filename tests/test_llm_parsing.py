"""
LLM Action Parsing Test
Tests LLMHandler._parse_action directly with a set of sample phrases,
checking the detected action (or none) against what's expected.

_parse_action only reads input text, meaning it doesn't use the LLM.
This is useful as the test can run without an API key or network connection.
"""

import pytest

from voice_assistant.llm import LLMHandler

# Tests each case: (query, expected action dict, or None if no action should be detected).
TEST_CASES = [
    ("Hello, how are you?", None),
    ("What's nine plus ten?", None),
    ("Play some house music", {"type": "spotify", "command": "play", "query": "house music"}),
    ("Play", {"type": "spotify", "command": "play"}),
    ("Pause Spotify", {"type": "spotify", "command": "pause"}),
    ("Stop the song", {"type": "spotify", "command": "pause"}),
    ("Skip this song", {"type": "spotify", "command": "skip"}),
    ("Next track", {"type": "spotify", "command": "skip"}),
    ("previous song", {"type": "spotify", "command": "previous"}),
    ("What's playing right now?", {"type": "spotify", "command": "current"}),
    ("What song is this?", {"type": "spotify", "command": "current"}),
    ("Turn on the lights", {"type": "home_assistant", "entity": "light.strip", "command": "turn_on"}),
    ("Turn off the lamp", {"type": "home_assistant", "entity": "light.strip", "command": "turn_off"}),
    ("Lights on", {"type": "home_assistant", "entity": "light.strip", "command": "turn_on"}),
]


@pytest.mark.parametrize("query, expected", TEST_CASES)
def test_parse_action(query, expected):
    """The keyword fallback finds the expected action, comparing only the keys that matter."""
    # LLMHandler initialises without an API key (it just logs a warning), and
    # _parse_action never calls the API, so this test runs fully offline.
    llm = LLMHandler(api_key=None)
    actual = llm._parse_action(query, "")

    if expected is None:
        assert actual is None
    else:
        assert actual is not None
        assert {key: actual.get(key) for key in expected} == expected
