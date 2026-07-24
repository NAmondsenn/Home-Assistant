#!/usr/bin/env python3
"""
Full Voice Pipeline Test
Tests the complete flow: Record -> Whisper -> Claude -> Piper -> Output

Run manually to check the full pipeline works end-to-end without needing
the wake word detector.
"""

import sys
import os
import logging
import yaml
import librosa
import soundfile as sf

# Add voice_assistant to path
sys.path.insert(0, os.path.expanduser('~/voice_assistant'))

from audio import AudioManager
from speech_to_text import SpeechToText
from llm import LLMHandler
from text_to_speech import TextToSpeech

def test_pipeline():
    """
    Runs an interactive loop which records, transcribes, responds and provides an output.
    This is used for manual testing of the full pipeline flow.
    """

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "=" * 60)
    print("VOICE ASSISTANT END-TO-END PIPELINE TEST")
    print("=" * 60 + "\n")

    # Load config
    print("Loading configuration...")
    with open(os.path.expanduser('~/config.yaml'), 'r') as f:
        config = yaml.safe_load(f)

    # Retrieves assistant name from config.yaml
    assistant_config = config.get('assistant', {})
    assistant_name = assistant_config.get('name', 'Assistant')

    # Initialise modules
    print("Initialising modules...")

    # Retrieves audio config
    audio_config = config.get('audio', {})
    mic_rate = audio_config.get('sample_rate', 48000)
    whisper_rate = 16000  # Whisper expected sample rate

    audio = AudioManager(
        sample_rate=mic_rate,
        channels=audio_config.get('channels', 1),
        chunk_size=audio_config.get('chunk_size', 2048),
        input_device=audio_config.get('input_device', 'default'),
        output_device=audio_config.get('output_device', 'default')
    )

    # Retrieves Whisper config
    whisper_config = config.get('whisper', {})
    stt = SpeechToText(
        model_size=whisper_config.get('model', 'base.en'),
        device=whisper_config.get('device', 'cpu'),
        compute_type=whisper_config.get('compute_type', 'int8'),
        fallback_model=whisper_config.get('fallback_model', 'tiny.en')
    )

    # LLM config - this defaults here match llm.py's own defaults so this test
    # behaves the same as the real app when config.yaml doesn't override them.
    llm_config = config.get('llm', {})
    conversation_config = config.get('conversation', {})

    llm = LLMHandler(
        api_key=None,  # Loaded from .env
        model=llm_config.get('model', 'claude-haiku-4-5-20251001'),
        max_tokens=llm_config.get('max_tokens', 150),
        temperature=llm_config.get('temperature', 0.7),
        history_length=conversation_config.get('history_length', 5),
        assistant_name=assistant_name
    )

    # Import TTS
    # TTS is optional for this test - if Piper isn't installed, the test
    # still runs through transcription and the LLM response, just skips speech.
    try:
        tts = TextToSpeech(config)
        tts_available = True
    except Exception as e:
        print(f"TTS not available: {e}")
        print("Run install_piper.sh to install Piper TTS")
        tts_available = False

    print("\nAll modules initialised\n")

    # Main test loop
    while True:
        print("-" * 60)
        print("Press Enter to start recording (or 'q' to quit)")
        user_input = input("> ")

        if user_input.lower() == 'q':
            print("\nExiting...")
            break

        try:
            # Step 1: Record audio
            print("\n[1/4] Recording... (speak now, will auto-stop on silence)")
            audio_data = audio.record_until_silence(
                silence_duration=2.0,
                timeout=10.0
            )
            print("Recording complete")

            # Resample from mic rate to Whisper's expected rate.
            if mic_rate != whisper_rate:
                print(f"Resampling {mic_rate}Hz -> {whisper_rate}Hz...")
                audio_data = librosa.resample(
                    audio_data.astype('float32'),
                    orig_sr=mic_rate,
                    target_sr=whisper_rate
                )

            # Step 2: Speech-to-text
            print("\n[2/4] Transcribing...")
            transcription = stt.transcribe(audio_data)

            if not transcription or not transcription.get('text'):
                print("No speech detected")
                continue

            text = transcription['text']
            confidence = transcription.get('confidence', 0)

            print(f"Transcription: '{text}'")
            print(f"Confidence: {confidence:.1%}")

            # Step 3: Get LLM response
            print("\n[3/4] Getting AI response...")
            llm_response = llm.process_query(text)

            response_text = llm_response['response']
            action = llm_response.get('action')

            print(f"Response: '{response_text}'")

            if action:
                print(f"Action: {action}")

            # Step 4: Text-to-speech
            if tts_available:
                print("\n[4/4] Generating speech...")
                tts_file = "test_response.wav"
                tts.synthesize(response_text, tts_file)

                print("Speech generated")
                print("\nPlaying response...")

                # Load the WAV file and resample to match the playback rate.
                tts_audio, tts_sr = sf.read(tts_file)

                if tts_sr != mic_rate:
                    print(f"Resampling TTS {tts_sr}Hz -> {mic_rate}Hz...")
                    tts_audio = librosa.resample(
                        tts_audio.astype('float32'),
                        orig_sr=tts_sr,
                        target_sr=mic_rate
                    )

                audio.play(tts_audio, sample_rate=mic_rate)

                print("Playback complete")
            else:
                print("\n[4/4] TTS not available, skipping speech generation")
                print(f"Response would have been: '{response_text}'")

            print("\n" + "=" * 60)
            print("PIPELINE TEST COMPLETE")
            print("=" * 60)

        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
            break
        except Exception as e:
            print(f"\nError: {e}")
            import traceback
            traceback.print_exc()

    print("\nGoodbye!\n")

if __name__ == "__main__":
    test_pipeline()