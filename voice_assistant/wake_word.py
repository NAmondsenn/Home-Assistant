import logging
import numpy as np
import sounddevice as sd
import librosa
from openwakeword.model import Model

logger = logging.getLogger(__name__)

MIC_RATE = 48000
OWW_RATE = 16000
CHUNK_SIZE = 3840  # 80ms at 48kHz

class WakeWordDetector:
    def __init__(self, sensitivity=0.5):
        self.sensitivity = sensitivity
        logger.info("Loading openWakeWord model...")
        self.model = Model()
        logger.info("openWakeWord initialized with 'hey_jarvis' model")

    def listen_once(self):
        logger.info("Listening for 'Hey Jarvis'...")
        self.model.reset()

        with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
            while True:
                audio_data, _ = stream.read(CHUNK_SIZE)
                audio_array = audio_data.flatten().astype(np.float32) / 32768.0
                audio_16k = librosa.resample(audio_array, orig_sr=MIC_RATE, target_sr=OWW_RATE)
                audio_16k_int = (audio_16k * 32768.0).astype(np.int16)
                prediction = self.model.predict(audio_16k_int)
                score = prediction.get("hey_jarvis", 0)
                if score >= self.sensitivity:
                    logger.info(f"Wake word detected! (confidence: {score:.2f})")
                    return True

    def cleanup(self):
        logger.info("openWakeWord cleaned up")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    print("\n=== Wake Word Test ===")
    print("Say 'Hey Jarvis' to trigger detection (Ctrl+C to stop)\n")
    detector = WakeWordDetector(sensitivity=0.5)
    try:
        count = 0
        while True:
            detector.listen_once()
            count += 1
            print(f"✅ Wake word detected! ({count} times)")
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        detector.cleanup()
