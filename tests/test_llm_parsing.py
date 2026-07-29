#!/usr/bin/env python3
"""
LLM Action Parsing Test
Tests LLMHandler._parse_action directly with a set of sample phrases,
checking the detected action (or none) against what's expected.

_parse_action only reads input text, meaning it doesn't use the LLM.
This is useful as the test can run without an API key or network connection.
"""

import os
import sys
import logging

# Add the project root to the path so the voice_assistant package imports work
# no matter where the tests are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

def check_match(actual, expected):
    """Returns True if the variable actual is None / expected, or if actual contains all expected keys / values."""
    if expected is None:
        return actual is None
    if actual is None:
        return False
    return all(actual.get(k) == v for k, v in expected.items())

def test_llm_parsing():
    """Runs each test case through _parse_action and reports pass/fail."""

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "=" * 60)
    print("LLM ACTION PARSING TEST")
    print("=" * 60 + "\n")

    # LLMHandler initialises without an API key (it just logs a warning), and
    # _parse_action never calls the API, so this test runs fully offline.
    print("Initialising LLMHandler...")
    llm = LLMHandler(api_key=None)
    print("LLMHandler initialised\n")

    results = []

    for query, expected in TEST_CASES:
        # response is passed as an empty string since _parse_action doesn't use it.
        actual = llm._parse_action(query, "")
        passed = check_match(actual, expected)

        print("-" * 60)
        print(f"Query:  '{query}'")
        print(f"Expected:  {expected}")
        print(f"Actual:    {actual}")
        print(f"Result:    {'PASS' if passed else 'FAIL'}")

        results.append({"query": query, "passed": passed})

    # Summary
    print("\n" + "=" * 60)
    print("LLM PARSING TEST SUMMARY")
    print("=" * 60)
    passed_count = sum(1 for r in results if r["passed"])
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['query']}")
    print(f"\n{passed_count}/{len(results)} passed\n")

if __name__ == "__main__":
    test_llm_parsing()
