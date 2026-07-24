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
import yaml

# Add voice_assistant to path
sys.path.insert(0, os.path.expanduser('~/voice_assistant'))

from llm import LLMHandler
from actions import ActionExecutor
from text_to_speech import TextToSpeech
from spotify_controller import SpotifyController

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
    with open(os.path.expanduser('~/config.yaml'), 'r') as f:
        config = yaml.safe_load(f)

    # Retrieve assistant name from config.yaml
    assistant_config = config.get('assistant', {})
    assistant_name = assistant_config.get('name', 'Assistant')

    # Initialise modules
    print("Initialising modules...")

    # Retrieve llm conversation config
    llm_config = config.get('llm', {})
    conversation_config = config.get('conversation', {})

    llm = LLMHandler(
        api_key=None,
        model=llm_config.get('model', 'claude-haiku-4-5-20251001'),
        max_tokens=llm_config.get('max_tokens', 150),
        temperature=llm_config.get('temperature', 0.7),
        history_length=conversation_config.get('history_length', 5),
        assistant_name=assistant_name
    )

    # Spotify and TTS are both optional, the test still runs and reports
    # results even if either is unavailable. 
    try:
        spotify = SpotifyController()
    except Exception as e:
        print(f"Spotify not available: {e}")
        spotify = None

    actions = ActionExecutor(spotify=spotify)

    # TTS is required, meaning no Piper TTS install will fail the test.
    tts = TextToSpeech(config)

    print("\nAll modules initialised\n")

    # Runs each test query through: LLM -> action executor -> TTS,
    # and reports pass/fail per stage rather than stopping at the first failure.
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
            if response_text:
                tts_file = f"test_integration_{i}.wav"
                tts_result = tts.synthesize(response_text, tts_file)
                if tts_result is None:
                    query_result["tts_ok"] = False
                    print("   TTS failed to generate audio")
                else:
                    print(f"   TTS saved to: {tts_file}")

        except Exception as e:
            print(f"   Error during query: {e}")
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