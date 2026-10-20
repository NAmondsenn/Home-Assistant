#!/usr/bin/env python3
"""
Action Executor Test
Tests the ActionExecutor in isolation (e.g. Spotify / Home Assistant commands).

Unlike check_integration.py, this doesn't go through the LLM at all — it
builds Actions directly, just as a tool call from the model becomes one, so it
can check dispatch logic without needing an API call.
"""

import os
import sys
import logging

# Add the project root to the path so the voice_assistant package imports work
# no matter where the scripts are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from voice_assistant.actions import Action, ActionExecutor, ActionResult
from voice_assistant.spotify_controller import SpotifyController

# Sample actions covering normal and exceptional cases.
TEST_ACTIONS = [
    Action("play_music", {"query": "hip hop"}),
    Action("control_playback", {"action": "pause"}),
    Action("control_playback", {"action": "skip"}),
    Action("control_playback", {"action": "previous"}),
    Action("control_playback", {"action": "current"}),
    Action("control_playback", {"action": "connect"}),  # Unknown playback action
    Action("play_netflix"),  # Unknown tool
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

    actions = ActionExecutor(music=spotify)

    print("\nRunning test actions\n")

    results = []

    for i, action in enumerate(TEST_ACTIONS, 1):
        print("-" * 60)
        print(f"[{i}/{len(TEST_ACTIONS)}] Action: {action}")

        try:
            result = actions.execute(action)

            # An action is considered handled if it comes back as an ActionResult.
            handled = isinstance(result, ActionResult)
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
        handled = isinstance(result, ActionResult)
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
