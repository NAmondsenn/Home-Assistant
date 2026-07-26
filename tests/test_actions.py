#!/usr/bin/env python3
"""
Action Executor Test
Tests the ActionExecutor in isolation (e.g. Spotify / Home Assistant commands).

Unlike test_integration.py, this doesn't go through the LLM at all — it
builds action dicts directly, in the same shape LLMHandler._parse_action
produces, so it can check dispatch logic without needing an API call.
"""

import os
import sys
import logging
import yaml

# Add voice_assistant to path
sys.path.insert(0, os.path.expanduser('~/voice_assistant'))

from actions import ActionExecutor
from spotify_controller import SpotifyController

# Sample actions covering normal and exceptional cases for Spotify and Home Assistant commands.
TEST_ACTIONS = [
    {"type": "spotify", "command": "play", "query": "hip hop"},
    {"type": "spotify", "command": "pause"},
    {"type": "spotify", "command": "skip"},
    {"type": "spotify", "command": "previous"},
    {"type": "spotify", "command": "current"},
    {"type": "spotify", "command": "connect"},  # Unknown Spotify command
    {"type": "home_assistant", "entity": "light.strip", "command": "turn_on"},
    {"type": "netflix", "command": "play"},  # Unknown action type
]

def test_actions():
    """Runs each sample action through ActionExecutor and prints the result."""

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "=" * 60)
    print("ACTION EXECUTOR TEST")
    print("=" * 60 + "\n")

    # Spotify is optional, so the system can still run without it.
    try:
        spotify = SpotifyController()
    except Exception as e:
        print(f"Spotify not available: {e}")
        spotify = None

    actions = ActionExecutor(spotify=spotify)

    print("\nRunning test actions\n")

    results = []

    for i, action in enumerate(TEST_ACTIONS, 1):
        print("-" * 60)
        print(f"[{i}/{len(TEST_ACTIONS)}] Action: {action}")

        try:
            result = actions.execute(action)

            # An action is considered handled if it returns a dict with a 'success' key.
            handled = isinstance(result, dict) and "success" in result
            print(f"Result: {result}")
            results.append({"action": action, "handled": handled})
        except Exception as e:
            print(f"Error: {e}")
            results.append({"action": action, "handled": False})

        print()

    # Also check execute(None), since main.py can call this when no action
    # was detected in a query, this shouldn't raise an exception.
    print("-" * 60)
    print("[extra] Action: None (no action detected)")
    try:
        result = actions.execute(None)
        handled = isinstance(result, dict) and "success" in result
        print(f"Result: {result}")
        results.append({"action": None, "handled": handled})
    except Exception as e:
        print(f"Error: {e}")
        results.append({"action": None, "handled": False})
    print()

    # Summary
    print("=" * 60)
    print("ACTION EXECUTOR TEST SUMMARY")
    print("=" * 60)
    for r in results:
        status = "PASS" if r["handled"] else "FAIL"
        print(f"[{status}] {r['action']}")
    print()

if __name__ == "__main__":
    test_actions()
