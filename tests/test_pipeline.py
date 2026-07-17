#!/usr/bin/env python3
"""
End-to-End Voice Pipeline Test
Tests the complete flow: Record → Whisper → Gemini → Piper → Play
"""

import sys
import os
import logging
import yaml

# Add voice_assistant to path
sys.path.insert(0, os.path.expanduser('~/voice_assistant'))

from audio import AudioManager
from speech_to_text import SpeechToText
from llm import LLMHandler


def test_pipeline():
    """Test the complete voice pipeline"""
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("\n" + "="*60)
    print("JARVIS END-TO-END PIPELINE TEST")
    print("="*60 + "\n")
    
    # Load config
    print("Loading configuration...")
    with open(os.path.expanduser('~/config.yaml'), 'r') as f:
        config = yaml.safe_load(f)
    
    # Initialize modules
    print("Initializing modules...")
    
    # Audio config
    audio_config = config.get('audio', {})
    audio = AudioManager(
        sample_rate=audio_config.get('sample_rate', 48000),
        channels=audio_config.get('channels', 1),
        chunk_size=audio_config.get('chunk_size', 2048),
        input_device=audio_config.get('input_device', 'default'),
        output_device=audio_config.get('output_device', 'default')
    )
    
    # Whisper STT config
    whisper_config = config.get('whisper', {})
    stt = SpeechToText(
        model_size=whisper_config.get('model', 'base.en'),
        device=whisper_config.get('device', 'cpu'),
        compute_type=whisper_config.get('compute_type', 'int8'),
        fallback_model=whisper_config.get('fallback_model', 'tiny.en')
    )
    
    # LLM config
    llm_config = config.get('llm', {})
    max_tokens = llm_config.get('max_tokens')
    # Handle "None" string from YAML as actual None
    if isinstance(max_tokens, str) and max_tokens.lower() == 'none':
        max_tokens = None
    
    conversation_config = config.get('conversation', {})
    
    llm = LLMHandler(
        api_key=None,  # Will load from env (.env file)
        model=llm_config.get('model', 'gemini-2.5-flash'),
        max_tokens=max_tokens,
        temperature=llm_config.get('temperature', 0.7),
        history_length=conversation_config.get('history_length', 5)
    )
    
    # Import TTS
    try:
        from text_to_speech import TextToSpeech
        tts = TextToSpeech(config)
        tts_available = True
    except Exception as e:
        print(f"⚠️  TTS not available: {e}")
        print("   Run install_piper.sh to install Piper TTS")
        tts_available = False
    
    print("\n✅ All modules initialized\n")
    
    # Test loop
    while True:
        print("-" * 60)
        print("Press Enter to start recording (or 'q' to quit)")
        user_input = input("> ")
        
        if user_input.lower() == 'q':
            print("\nExiting...")
            break
        
        try:
            # Step 1: Record audio
            print("\n[1/4] 🎤 Recording... (speak now, will auto-stop on silence)")
            audio_data = audio.record_until_silence(
                silence_duration=2.0,
                timeout=10.0
            )
            print("✅ Recording complete")
            
            # Resample from mic rate (48kHz) to Whisper rate (16kHz)
            import librosa
            mic_rate = audio_config.get('sample_rate', 48000)
            whisper_rate = 16000
            if mic_rate != whisper_rate:
                print(f"   Resampling {mic_rate}Hz → {whisper_rate}Hz...")
                audio_data = librosa.resample(
                    audio_data,
                    orig_sr=mic_rate,
                    target_sr=whisper_rate
                )
            
            # Step 2: Speech-to-text
            print("\n[2/4] 🔤 Transcribing...")
            transcription = stt.transcribe(audio_data)
            
            if not transcription or not transcription.get('text'):
                print("❌ No speech detected")
                continue
            
            text = transcription['text']
            confidence = transcription.get('confidence', 0)
            
            print(f"✅ Transcription: '{text}'")
            print(f"   Confidence: {confidence:.1%}")
            
            # Step 3: Get LLM response
            print("\n[3/4] 🤖 Getting AI response...")
            llm_response = llm.process_query(text)
            
            response_text = llm_response['response']
            action = llm_response.get('action')
            
            print(f"✅ Response: '{response_text}'")
            
            if action:
                print(f"   Action: {action}")
            
            # Step 4: Text-to-speech
            if tts_available:
                print("\n[4/4] 🔊 Generating speech...")
                tts_file = "test_response.wav"
                tts.synthesize(response_text, tts_file)
                
                print("✅ Speech generated")
                print(f"\n🎧 Playing response...")
                
                # Load the WAV file and resample to match audio module's rate
                import soundfile as sf
                tts_audio, tts_sr = sf.read(tts_file)
                
                # Resample to match the AudioManager's sample rate (48kHz)
                target_sr = audio_config.get('sample_rate', 48000)
                if tts_sr != target_sr:
                    print(f"   Resampling TTS {tts_sr}Hz → {target_sr}Hz...")
                    tts_audio = librosa.resample(
                        tts_audio.astype('float32'),
                        orig_sr=tts_sr,
                        target_sr=target_sr
                    )
                
                audio.play(tts_audio, sample_rate=target_sr)
                
                print("✅ Playback complete")
            else:
                print("\n[4/4] ⚠️  TTS not available - skipping speech generation")
                print(f"   Response would have been: '{response_text}'")
            
            print("\n" + "="*60)
            print("PIPELINE TEST COMPLETE")
            print("="*60)
            
        except KeyboardInterrupt:
            print("\n\nInterrupted by user")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()
    
    print("\nGoodbye!\n")


if __name__ == "__main__":
    test_pipeline()
