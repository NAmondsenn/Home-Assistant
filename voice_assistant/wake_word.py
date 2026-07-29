import os
import re
import logging
import numpy as np
import sounddevice as sd
import librosa
from openwakeword.model import Model

logger = logging.getLogger(__name__)

# Resolves paths relative to this file's own location, so the project
# works when cloned / run from anywhere.
PROJECT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))

MIC_RATE = 48000  # Microphone sample rate (48kHz)
OWW_RATE = 16000  # openWakeWord model expects 16kHz audio
CHUNK_SIZE = 3840  # audio processing chunk size (80ms of audio at 48kHz)

# Fallback wake word phrase if no config is passed in
DEFAULT_WAKE_WORD = "Hey Nova"

def _wake_word_filename(phrase):
    """
    Turns a wake word phrase into the model filename convention
    its .onnx model uses.
    """
    return re.sub(r'\s+', '_', phrase.strip().lower())

class WakeWordDetector:
    """
    A simple wake word detector using the openWakeWord library. The wake word phrase, which's
    .onnx model gets loaded, comes from config.yaml rather than being hardcoded.
    The sensitivity can be adjusted to make detection more or less strict, with a value
    between 0 and 1.
    """

    def __init__(self, config=None, sensitivity=None):
        # Pulls sensitivity from config.yaml's "thresholds" section and the wake word
        # phrase from "assistant".
        thresholds_config = config.section("thresholds") if config else {}
        assistant_config = config.section("assistant") if config else {}

        self.sensitivity = sensitivity if sensitivity is not None else thresholds_config.get("wake_word_confidence",
                                                                                             0.5)
        self.wake_word_phrase = assistant_config.get("wake_word", DEFAULT_WAKE_WORD)
        self.wake_word_key = _wake_word_filename(self.wake_word_phrase)
        model_path = os.path.join(PROJECT_DIRECTORY, "models", f"{self.wake_word_key}.onnx")

        logger.info(f"Loading openWakeWord model for '{self.wake_word_phrase}'...")
        self.model = Model(wakeword_models=[model_path])
        logger.info(f"openWakeWord initialised with '{self.wake_word_key}' model")

    def listen_once(self):
        """
        This method opens the mic stream and listens for the wake word. It processes audio in chunks of 80ms,
        resamples it to 16kHz, and checks for the wake word. This method will loop indefinitely until the
        wake word is detected, at which point it will return True.
        """
        logger.info(f"Listening for '{self.wake_word_phrase}'...")
        self.model.reset()

        with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype='int16', blocksize=CHUNK_SIZE) as stream:
            while True:
                audio_data, _ = stream.read(CHUNK_SIZE)
                audio_array = audio_data.flatten().astype(np.float32) / 32768.0

                # Resamples the audio to 16kHz for the openWakeWord model
                audio_16k = librosa.resample(audio_array, orig_sr=MIC_RATE, target_sr=OWW_RATE)
                audio_16k_int = (audio_16k * 32768.0).astype(np.int16)

                # Feeds audio into the openWakeWord model and gets the prediction score for the configured wake word
                prediction = self.model.predict(audio_16k_int)
                score = prediction.get(self.wake_word_key, 0)

                # Returns True and logs the confidence score if the wake word is detected
                if score >= self.sensitivity:
                    logger.info(f"Wake word detected! (confidence: {score:.2f})")
                    return True
