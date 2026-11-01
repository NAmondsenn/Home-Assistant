#!/usr/bin/env python3
"""
Integration Test
Tests how the LLMHandler, ActionExecutor, and TextToSpeech work together
using a fixed set of sample queries.

This doesn't use the microphone / wake word
detector. It feeds text straight in, allowing users to test the home assistant
without a microphone connected.
"""

import os
import sys
import logging

# Add the project root to the path so the voice_assistant package imports work
# no matter where the scripts are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from voice_assistant import paths
from voice_assistant.config import Config
from voice_assistant.llm import LLMHandler
from voice_assistant.actions import ActionExecutor
from voice_assistant.text_to_speech import TextToSpeech
from voice_assistant.spotify_controller import SpotifyController

# Sample queries covering the main paths through the system: a plain
# conversational query, a Spotify action, and a request for the lights - which is only
# answered in words here, as no Home Assistant is wired in below.
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

    # Load config.yaml, and the keys in ~/.config/nova/.env, which the services load too -
    # without them there's no key for Claude or Spotify.
    print("Loading configuration...")
    paths.load_secrets()
    config = Config()

    # Initialise modules
    print("Initialising modules...")

    # Spotify and TTS are both optional, the test still runs and reports
    # results even if either is unavailable.
    try:
        spotify = SpotifyController()
    except Exception as e:
        print(f"Spotify not available: {e}")
        spotify = None

    # The music player is the executor's music, as it is in a session.
    actions = ActionExecutor(music=spotify)

    # LLMHandler pulls its own model / max_tokens / temperature / history_length / assistant_name from config.
    # It's offered the tools the executor can run, as a session's is - with none, it could never ask for an action.
    llm = LLMHandler(config=config, api_key=None, tools=actions.tool_schemas())

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

            if llm_response.get('success') and response_text:
                query_result["llm_ok"] = True
                print(f"LLM response: '{response_text}'")
            else:
                print("LLM did not return a usable response")

            # Step 2: Action execution, for each tool the model called. Each comes back as an
            # ActionResult, a failure of which with nothing to say is the one nobody would hear of.
            for action in llm_response.get('actions', []):
                print(f"Detected action: {action}")

                action_result = actions.execute(action)
                if not action_result.ok and not action_result.say:
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