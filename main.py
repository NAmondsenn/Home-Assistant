import os, sys, time, logging, signal, yaml, librosa, soundfile as sf
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, os.path.expanduser("~/voice_assistant"))
from audio import AudioManager
from speech_to_text import SpeechToText
from llm import LLMHandler
from text_to_speech import TextToSpeech
from wake_word import WakeWordDetector
from spotify_controller import SpotifyController

load_dotenv()
os.makedirs(os.path.expanduser("~/logs"), exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(os.path.expanduser("~/logs/jarvis.log")), logging.StreamHandler()])
logger = logging.getLogger(__name__)

class Jarvis:
    def __init__(self):
        with open(os.path.expanduser("~/config.yaml")) as f:
            self.config = yaml.safe_load(f)
        audio_cfg = self.config.get("audio", {})
        self.mic_rate = audio_cfg.get("sample_rate", 48000)
        self.whisper_rate = 16000
        self.temp_dir = Path(os.path.expanduser("~/temp_audio"))
        self.temp_dir.mkdir(exist_ok=True)
        self.tts_file = str(self.temp_dir / "response.wav")
        self.running = False

        logger.info("Loading modules...")
        self.audio = AudioManager(sample_rate=self.mic_rate, channels=audio_cfg.get("channels", 1), chunk_size=audio_cfg.get("chunk_size", 2048))
        w = self.config.get("whisper", {})
        self.stt = SpeechToText(model_size=w.get("model", "base.en"), device=w.get("device", "cpu"), compute_type=w.get("compute_type", "int8"), fallback_model=w.get("fallback_model", "tiny.en"))
        l = self.config.get("llm", {})
        self.llm = LLMHandler(api_key=None, model=l.get("model", "claude-haiku-4-5-20251001"), max_tokens=150, temperature=l.get("temperature", 0.7), history_length=self.config.get("conversation", {}).get("history_length", 5))
        self.tts = TextToSpeech(self.config)
        
        logger.info("Loading Spotify...")
        try:
            self.spotify = SpotifyController()
        except Exception as e:
            logger.warning(f"Spotify initialization failed: {e}")
            self.spotify = None
        
        self.wake = WakeWordDetector(sensitivity=0.5)
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        logger.info("Jarvis ready!")

    def _shutdown(self, *args):
        logger.info("Shutting down...")
        self.running = False
        self.wake.cleanup()
        sys.exit(0)

    def _execute_spotify_action(self, action):
        command = action.get("command")
        if command == "play":
            query = action.get("query")
            return self.spotify.play(query)
        elif command == "pause":
            return self.spotify.pause()
        elif command == "skip":
            return self.spotify.skip()
        elif command == "previous":
            return self.spotify.previous()
        elif command == "current":
            result = self.spotify.current_track()
            if result.get("success"):
                return {"success": True, "message": f"Playing {result['track']} by {result['artist']}"}
            return result
        return {"success": False}

    def run(self):
        self.running = True
        print("\n🤖 Jarvis is ready. Say Hey Jarvis to activate.\n")
        while self.running:
            try:
                self.wake.listen_once()
                print("\n🎙️  Listening...")
                thresholds = self.config.get("thresholds", {})
                audio_data = self.audio.record_until_silence(silence_duration=thresholds.get("silence_duration", 2.0), timeout=thresholds.get("recording_timeout", 10.0))
                if audio_data is None or len(audio_data) == 0:
                    continue
                if self.mic_rate != self.whisper_rate:
                    audio_data = librosa.resample(audio_data.astype("float32"), orig_sr=self.mic_rate, target_sr=self.whisper_rate)
                transcription = self.stt.transcribe(audio_data)
                if not transcription or not transcription.get("text", "").strip():
                    continue
                text = transcription["text"]
                print(f"🗣️  You: {text}")
                result = self.llm.process_query(text)
                response_text = result["response"]
                action = result.get("action")
                
                if action and action["type"] == "spotify" and self.spotify:
                    spotify_result = self._execute_spotify_action(action)
                    if spotify_result.get("success"):
                        response_text = spotify_result.get("message", response_text)
                
                print(f"🤖 Jarvis: {response_text}")
                self.tts.synthesize(response_text, self.tts_file)
                tts_audio, tts_sr = sf.read(self.tts_file)
                if tts_sr != self.mic_rate:
                    tts_audio = librosa.resample(tts_audio.astype("float32"), orig_sr=tts_sr, target_sr=self.mic_rate)
                self.audio.play(tts_audio, sample_rate=self.mic_rate)
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                time.sleep(1)

if __name__ == "__main__":
    Jarvis().run()
