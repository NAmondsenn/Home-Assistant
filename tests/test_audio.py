#!/usr/bin/env python3
"""
Audio Manager Test
Tests AudioManager on its own: device listing, fixed-duration recording,
silence-triggered recording, resampling, and playback.

Unlike test_pipeline.py and test_integration.py, this doesn't use Whisper,
the LLM, or TTS. This is just a hardware/AudioManager safety check which is useful
for confirming the mic and speaker setup works before running the full pipeline.
"""

import os
import sys
import logging
import yaml

# Add voice_assistant to path
sys.path.insert(0, os.path.expanduser('~/voice_assistant'))

from audio import AudioManager

def test_audio():
    """Runs a series of manual AudioManager checks: devices, recording, resampling, playback."""

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "=" * 60)
    print("AUDIO MANAGER TEST")
    print("=" * 60 + "\n")

    # Load config
    print("Loading configuration...")
    with open(os.path.expanduser('~/config.yaml'), 'r') as f:
        config = yaml.safe_load(f)

    audio_config = config.get('audio', {})
    mic_rate = audio_config.get('sample_rate', 48000)
    whisper_rate = 16000  # Whisper's expected sample rate.

    print("Initialising AudioManager...")
    audio = AudioManager(
        sample_rate=mic_rate,
        channels=audio_config.get('channels', 1),
        chunk_size=audio_config.get('chunk_size', 2048),
        input_device=audio_config.get('input_device', 'default'),
        output_device=audio_config.get('output_device', 'default')
    )
    print("AudioManager initialised\n")

    try:
        # Lists available audio devices.
        # This is to check that the input/output devices are correct.
        print("-" * 60)
        print("[1/5] Listing available audio devices")
        audio.list_devices()

        # Records audio for a known duration and saves it.
        # Allows users to manually check the recorded audio.
        print("-" * 60)
        print("[2/5] Fixed-duration recording (3 seconds)")
        input("Press Enter, then speak for 3 seconds...")
        fixed_audio = audio.record(duration=3.0)
        audio.save_wav(fixed_audio, "test_audio_fixed.wav")
        print(f"Recorded {len(fixed_audio)} samples, saved to test_audio_fixed.wav")
        print("Play with: aplay test_audio_fixed.wav\n")

        # Tests the recording_until_silence function to ensure silence-triggered recording works.
        print("-" * 60)
        print("[3/5] Silence-triggered recording (say something, then go quiet)")
        input("Press Enter, then speak and pause when you're done...")
        silence_audio = audio.record_until_silence(
            silence_duration=audio_config.get('silence_duration', 2.0),
            timeout=10.0
        )
        audio.save_wav(silence_audio, "test_audio_silence.wav")
        duration_recorded = len(silence_audio) / mic_rate
        print(f"Recorded {len(silence_audio)} samples ({duration_recorded:.1f}s), saved to test_audio_silence.wav")
        print("Play with: aplay test_audio_silence.wav\n")

        # This is a test to see if audio resampling works correctly.
        # This resamples recorded audio from the configured mic rate to Whisper's expected rate.
        print("-" * 60)
        print("[4/5] Resample round-trip check")
        resampled = audio.resample(fixed_audio, mic_rate, whisper_rate)
        expected_samples = int(len(fixed_audio) * whisper_rate / mic_rate)

        # Allow a small tolerance since resampling doesn't always land on an exact count.
        tolerance = whisper_rate * 0.05  # 5% of a second's worth of samples
        if abs(len(resampled) - expected_samples) <= tolerance:
            print(f"Resample OK: {len(fixed_audio)} samples @ {mic_rate}Hz -> {len(resampled)} samples @ {whisper_rate}Hz")
        else:
            print(f"Resample looks incorrect: expected ~{expected_samples} samples, received {len(resampled)}")
        print()

        # Plays the fixed-duration recording back through the configured
        # output device, confirming the full record -> play round trip works.
        print("-" * 60)
        print("[5/5] Playback")
        input("Press Enter to play back the 3-second recording...")
        audio.play(fixed_audio, sample_rate=mic_rate)
        print("Playback complete\n")

        print("=" * 60)
        print("AUDIO TEST COMPLETE")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Always release PyAudio resources, even if a step above failed.
        audio.close()
        print("\nAudioManager closed")

if __name__ == "__main__":
    test_audio()