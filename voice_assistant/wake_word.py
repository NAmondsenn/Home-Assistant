import os
import logging
import numpy as np
import sounddevice as sd
import librosa
from openwakeword.model import Model

logger = logging.getLogger(__name__)

MIC_RATE = 48000 # Microphone sample rate (48kHz)
OWW_RATE = 16000 # openWakeWord model expects 16kHz audio
CHUNK_SIZE = 3840  # audio processing chunk size (80ms of audio at 48kHz)
# New constraints for custom wake word
WAKE_WORD_MODEL_PATH = os.path.expanduser("~/models/hey_nova.onnx")
WAKE_WORD_KEY = "hey_nova"

class WakeWordDetector:
    """
    A simple wake word detector using the openWakeWord library. It listens for the phrase "Hey Nova" and triggers an action 
    when detected. The sensitivity can be adjusted to make detection more or less strict, with a value between 0 and 1.
    """
    def __init__(self, sensitivity=0.5):
        self.sensitivity = sensitivity
        logger.info("Loading openWakeWord model...")
        self.model = Model(wakeword_models=[WAKE_WORD_MODEL_PATH])
        logger.info("openWakeWord initialised with 'hey_nova' model")

    def listen_once(self):
        """
        This method opens the mic stream and listens for the wake word. It processes audio in chunks of 80ms, 
        resamples it to 16kHz, and checks for the wake word. This method will loop indefinitely until the wake word is detected, 
        at which point it will return True.
        """
        logger.info("Listening for 'Hey Nova'...")
        self.model.reset()

        with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
            while True:
                audio_data, _ = stream.read(CHUNK_SIZE)
                audio_array = audio_data.flatten().astype(np.float32) / 32768.0

                # Resamples the audio to 16kHz for the openWakeWord model
                audio_16k = librosa.resample(audio_array, orig_sr=MIC_RATE, target_sr=OWW_RATE)
                audio_16k_int = (audio_16k * 32768.0).astype(np.int16)
                
                # Feeds audio into the openWakeWord model and gets the prediction score for "hey_nova"
                prediction = self.model.predict(audio_16k_int)
                score = prediction.get(WAKE_WORD_KEY, 0)

                # Returns True and logs the confidence score if the wake word is detected
                if score >= self.sensitivity:  
                    logger.info(f"Wake word detected! (confidence: {score:.2f})")
                    return True


# Only runs this test if the script is executed directly. It will continuously listen for the wake word and print a message each time it is detected.
# This is useful for testing the wake word detection functionality in isolation and is useful for configuring sensitivity / testing reliability. 
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    print("\n=== Wake Word Test ===")
    print("Say 'Hey Nova' to trigger detection (Ctrl+C to stop)\n")
    detector = WakeWordDetector(sensitivity=0.5)
    try:
        count = 0
        while True:
            detector.listen_once()
            count += 1
            print(f"Wake word detected! ({count} times)")
    except KeyboardInterrupt:
        print("\nStopping...")
