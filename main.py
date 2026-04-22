"""
Jarvis Main Orchestrator
State machine that ties all modules together for autonomous operation.

States: IDLE → LISTENING → PROCESSING → SPEAKING → IDLE
"""

import os
import sys
import time
import logging
import signal
import yaml
import librosa
import soundfile as sf
from pathlib import Path
from dotenv import load_dotenv

# Add voice_assistant to path
sys.path.insert(0, os.path.expanduser("~/voice_assistant"))

from audio import AudioManager
from speech_to_text import SpeechToText
from llm import LLMHandler
from text_to_speech import TextToSpeech
from wake_word import WakeWordDetector

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.expanduser("~/logs/jarvis.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# States
IDLE = "IDLE"
LISTENING = "LISTENING"
PROCESSING = "PROCESSING"
SPEAKING = "SPEAKING"


class Jarvis:
    def __init__(self):
        logger.info("Initializing Jarvis...")

        # Load config
        with open(os.path.expanduser("~/config.yaml"), "r") as f:
            self.config = yaml.safe_load(f)

        self.audio_config = self.config.get("audio", {})
        self.mic_rate = self.audio_config.get("sample_rate", 48000)
        self.whisper_rate = 16000
        self.state = IDLE
        self.running = False

        # Temp file for TTS output
        self.temp_dir = Path(os.path.expanduser("~/temp_audio"))
        self.temp_dir.mkdir(exist_ok=True)
        self.tts_file = str(self.temp_dir / "response.wav")

        # Initialize all modules
        logger.info("Loading audio...")
        self.audio = AudioManager(
            sample_rate=self.mic_rate,
            channels=self.audio_config.get("channels", 1),
            chunk_size=self.audio_config.get("chunk_size", 2048)
        )

        logger.info("Loading Whisper...")
        whisper_config = self.config.get("whisper", {})
        self.stt = SpeechToText(
            model_size=whisper_config.get("model", "base.en"),
            device=whisper_config.get("device", "cpu"),
            compute_type=whisper_config.get("compute_type", "int8"),
            fallback_model=whisper_config.get("fallback_model", "tiny.en")
        )

        logger.info("Loading Claude...")
        llm_config = self.config.get("llm", {})
        conversation_config = self.config.get("conversation", {})
        self.llm = LLMHandler(
            api_key=None,
            model=llm_config.get("model", "claude-haiku-4-5-20251001"),
            max_tokens=150,
            temperature=llm_config.get("temperature", 0.7),
            history_length=conversation_config.get("history_length", 5)
        )

        logger.info("Loading Piper TTS...")
        self.tts = TextToSpeech(self.config)

        logger.info("Loading Porcupine wake word...")
        threshold_config = self.config.get("thresholds", {})
        self.wake_detector = WakeWordDetector(
            sensitivity=threshold_config.get("wake_word_confidence", 0.5)
        )

        # Handle Ctrl+C gracefully
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        logger.info("✅ Jarvis initialized and ready!")

    def _shutdown(self, signum, frame):
        logger.info("Shutting down Jarvis...")
        self.running = False
        self.wake_detector.cleanup()
        sys.exit(0)

    def _set_state(self, state):
        logger.info(f"State: {self.state} → {state}")
        self.state = state

    def run(self):
        """Main loop."""
        self.running = True
        logger.info("Jarvis is running. Say 'Jarvis' to activate.")
        print("\n🤖 Jarvis is ready. Say 'Jarvis' to activate.\n")

        while self.running:
            try:
                # IDLE: wait for wake word
                self._set_state(IDLE)
                self.wake_detector.listen_once()

                # Play a short acknowledgement beep (optional - just print for now)
                print("\n🎙️ Listening...")

                # LISTENING: record until silence
                self._set_state(LISTENING)
                audio_data = self.audio.record_until_silence(
                    silence_duration=self.config.get("thresholds", {}).get("silence_duration", 2.0),
                    timeout=self.config.get("thresholds", {}).get("recording_timeout", 10.0)
                )

                if audio_data is None or len(audio_data) == 0:
                    logger.warning("No audio recorded, returning to idle")
                    continue

                # Resample from mic rate to Whisper rate
                if self.mic_rate != self.whisper_rate:
                    audio_data = librosa.resample(
                        audio_data.astype("float32"),
                        orig_sr=self.mic_rate,
                        target_sr=self.whisper_rate
                    )

                # PROCESSING: transcribe + get LLM response
                self._set_state(PROCESSING)

                transcription = self.stt.transcribe(audio_data)
                if not transcription or not transcription.get("text", "").strip():
                    logger.warning("No speech detected")
                    continue

                text = transcription["text"]
                logger.info(f"Heard: '{text}'")
                print(f"🗣️  You: {text}")

                llm_response = self.llm.process_query(text)
                response_text = llm_response["response"]
                action = llm_response.get("action")

                logger.info(f"Response: '{response_text}'")
                print(f"🤖 Jarvis: {response_text}")

                if action:
                    logger.info(f"Action: {action}")
                    # Future: execute action via Home Assistant

                # SPEAKING: synthesize and play response
                self._set_state(SPEAKING)

                # Raise wake word sensitivity during playback to reduce false triggers
                self.wake_detector.sensitivity = self.config.get("thresholds", {}).get("wake_word_confidence_speaking", 0.8)

                self.tts.synthesize(response_text, self.tts_file)

                tts_audio, tts_sr = sf.read(self.tts_file)
                target_sr = self.mic_rate
                if tts_sr != target_sr:
                    tts_audio = librosa.resample(
                        tts_audio.astype("float32"),
                        orig_sr=tts_sr,
                        target_sr=target_sr
                    )

                self.audio.play(tts_audio, sample_rate=target_sr)

                # Restore normal sensitivity
                self.wake_detector.sensitivity = self.config.get("thresholds", {}).get("wake_word_confidence", 0.5)

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                print(f"❌ Error: {e}")
                time.sleep(1)  # Brief pause before retrying
                continue


if __name__ == "__main__":
    # Make sure logs directory exists
    os.makedirs(os.path.expanduser("~/logs"), exist_ok=True)

    jarvis = Jarvis()
    jarvis.run()
