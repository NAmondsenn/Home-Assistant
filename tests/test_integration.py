#!/usr/bin/env python3
"""
Integration Test
Tests how the LLMHandler, ActionExecutor, and TextToSpeech work together
using a fixed set of sample queries.

Unlike test_pipeline.py, this doesn't use the microphone / wake word
detector. It feeds text straight in, allowing users to test the home assistant
without a microphone connected.
"""

import os
import sys
import logging

# Add the project root to the path so the voice_assistant package imports work
# no matter where the tests are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import Config
from voice_assistant.llm import LLMHandler
from voice_assistant.actions import ActionExecutor
from voice_assistant.text_to_speech import TextToSpeech
from voice_assistant.spotify_controller import SpotifyController

# Sample queries covering the main paths through the system: a plain
# conversational query, a Spotify action, and a Home Assistant action.
TEST_QUERIES = [
    "Hello, how are you?",
    "Play some pop music",
    "Turn on the lights",
]

def test_integration():
    """Runs each sample query through the LLM and action pipeline, printing the result of each stage."""

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "=" * 60)
    print("VOICE ASSISTANT INTEGRATION TEST")
    print("=" * 60 + "\n")

    # Load config.yaml
    print("Loading configuration...")
    config = Config()

    # Initialise modules
    print("Initialising modules...")

    # LLMHandler pulls its own model / max_tokens / temperature / history_length / assistant_name from config
    llm = LLMHandler(config=config, api_key=None)

    # Spotify and TTS are both optional, the test still runs and reports
    # results even if either is unavailable.
    try:
        spotify = SpotifyController()
    except Exception as e:
        print(f"Spotify not available: {e}")
        spotify = None

    actions = ActionExecutor(spotify=spotify)

    # TTS is optional, meaning no Piper TTS install won't fail the test.
    try:
        tts = TextToSpeech(config)
        tts_available = True
    except Exception as e:
        print(f"TTS not available: {e}")
        tts = None
        tts_available = False

    print("\nAll modules initialised\n")

    # Runs each test query through: LLM -> action executor -> TTS,
    # and reports pass / fail per stage rather than stopping at the first failure.
    results = []

    for i, query in enumerate(TEST_QUERIES, 1):
        print("-" * 60)
        print(f"[{i}/{len(TEST_QUERIES)}] Query: '{query}'")

        query_result = {"query": query, "llm_ok": False, "action_ok": True, "tts_ok": True}

        try:
            # Step 1: LLM
            llm_response = llm.process_query(query)
            response_text = llm_response.get('response', '')
            action = llm_response.get('action')

            if llm_response.get('success') and response_text:
                query_result["llm_ok"] = True
                print(f"LLM response: '{response_text}'")
            else:
                print("LLM did not return a usable response")

            if action:
                print(f"Detected action: {action}")

                # Step 2: Action execution
                action_result = actions.execute(action)
                if not action_result.get("success") and not action_result.get("message"):
                    query_result["action_ok"] = False
                print(f"   Action result: {action_result}")

            # Step 3: TTS (only if the LLM produced something to speak)
            if response_text and tts_available:
                tts_file = f"test_integration_{i}.wav"
                tts_result = tts.synthesise(response_text, tts_file)
                if tts_result is None:
                    query_result["tts_ok"] = False
                    print("TTS failed to generate audio")
                else:
                    print(f"TTS saved to: {tts_file}")
            elif response_text:
                print("TTS not available, skipping speech generation")

        except Exception as e:
            print(f"Error during query: {e}")
            query_result["llm_ok"] = False

        results.append(query_result)
        print()

    # Test summary
    print("=" * 60)
    print("INTEGRATION TEST SUMMARY")
    print("=" * 60)
    for r in results:
        status = "PASS" if r["llm_ok"] and r["action_ok"] and r["tts_ok"] else "FAIL"
        print(f"[{status}] {r['query']}")
    print()

if __name__ == "__main__":
    test_integration()