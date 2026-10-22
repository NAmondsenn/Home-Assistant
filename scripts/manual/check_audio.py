#!/usr/bin/env python3
"""
Audio Check
Checks the microphone and speaker on their own: lists the sound devices, records
until you stop talking, saves it, and plays it back - through the same Microphone
and Speaker the assistant uses.

Unlike check_pipeline.py and check_integration.py, this doesn't use Whisper, the
LLM, or TTS. It's a quick hardware check, useful for confirming the mic and speaker
setup works before running the full pipeline.
"""

import os
import sys
import logging

import sounddevice as sd
import soundfile as sf

# Add the project root to the path so the voice_assistant package imports work
# no matter where the scripts are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from voice_assistant.config import Config
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.recording import record_until_silence
from voice_assistant.speaker import Speaker
from voice_assistant.vad import make_detector


def check_audio():
    """Lists the devices, then records, saves and plays back one recording."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    print("\n" + "=" * 60)
    print("AUDIO CHECK")
    print("=" * 60 + "\n")

    config = Config()
    audio_config = config.section('audio')
    thresholds_config = config.section('thresholds')
    mic_rate = audio_config.get('sample_rate', 48000)

    # Lists the sound devices, to check the input and output are the expected ones.
    print("-" * 60)
    print("[1/3] Sound devices")
    print(sd.query_devices())

    microphone = Microphone(sample_rate=mic_rate)
    speaker = Speaker(sample_rate=mic_rate)
    try:
        # Records through the same microphone stream and speech detector the satellite uses,
        # which keep their audio at 16kHz.
        print("-" * 60)
        print("[2/3] Recording until you stop talking")
        input("Press Enter, then speak and pause when you're done...")
        recording = record_until_silence(
            microphone, make_detector(config),
            silence_duration=thresholds_config.get('silence_duration', 0.6),
            timeout=10.0
        )
        sf.write("test_audio.wav", recording, RATE)
        print(f"Recorded {len(recording) / RATE:.1f}s, saved to test_audio.wav")
        print("Play with: aplay test_audio.wav\n")

        # Plays it back through the shared speaker, confirming the round trip works.
        print("-" * 60)
        print("[3/3] Playback")
        input("Press Enter to play it back...")
        speaker.play(recording, sample_rate=RATE).wait()
        print("Playback complete\n")

        print("=" * 60)
        print("AUDIO CHECK COMPLETE")
        print("=" * 60)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    finally:
        speaker.close()
        microphone.close()


if __name__ == "__main__":
    check_audio()
