"""
Text-to-Speech Module
Handles converting text responses to speech using Piper.
"""

import subprocess
import os
import wave
import logging
from pathlib import Path

# Logger setup
logger = logging.getLogger(__name__)


class TextToSpeech:
    """Synthesises and outputs speech from text given to Piper TTS."""

    def __init__(self, config):
        """
        Initialise the TTS engine.

        Args:
            config: Dict loaded from config.yaml.
                    Used to look up the assistant's name.
        """

        # Stores the config dict and resolves the paths to Piper TTS and the voice model.
        self.config = config
        self.piper_path = os.path.expanduser("~/models/piper/piper")
        self.model_path = os.path.expanduser("~/models/en_GB-alan-medium.onnx")

        # Creates / reuses a temporary folder for generated audio.
        self.temp_dir = Path("temp_audio")
        self.temp_dir.mkdir(exist_ok=True)

        # Reads the assistant's name from the "assistant" section in config.yaml.
        assistant_config = self.config.get("assistant", {})
        self.name = assistant_config.get("name", "Assistant")

        # Verify Piper is installed, raising error if not.
        if not os.path.exists(self.piper_path):
            raise FileNotFoundError(
                f"Piper not found at {self.piper_path}. "
                "Run install_piper.sh first."
            )

        # Same check but for the voice model.
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"Voice model not found at {self.model_path}. "
                "Run install_piper.sh first."
            )

        logger.info(f"Piper TTS initialised with model: {self.model_path}")

        # Test Piper on startup using _warmup().
        self._warmup()

    def _warmup(self):
        """
        Method which is used to warm up the Piper TTS model.
        This is to avoid cold-start latency for the first actual response.
        """
        try:
            logger.info("Warming up Piper TTS...")
            test_file = self.temp_dir / "warmup.wav"
            self.synthesise("Ready.", str(test_file))
            test_file.unlink()  # Deletes the warmup file
            logger.info("Piper warmup complete")
        # Catches an error and logs warmup failure.
        # This prevents the system crashing on startup if the warmup fails.
        except Exception as e:
            logger.warning(f"Warmup failed: {e}")

    def synthesise(self, text, output_file):
        """
        Convert text to speech using Piper and save it as a WAV file.

        Args:
            text: Text to synthesise.
            output_file: Path to write the WAV file to.

        Returns:
            The output file path on success, or None if synthesis failed.
        """
        try:
            # Cleans the text, removing special characters that may break Piper TTS.
            # This also strips quotation marks and new lines.
            text = text.replace('"', "'").replace('\n', ' ').strip()

            if not text:
                logger.warning("No text provided.")
                return None

            logger.info(f"Synthesising: '{text[:50]}...'")

            # Runs Piper and inputs text via echo, this then outputs to a file.
            cmd = f'echo "{text}" | {self.piper_path} --model {self.model_path} --output_file {output_file}'

            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10
            )

            # Logs non-zero return codes as this means Piper reported an error.
            if result.returncode != 0:
                logger.error(f"Piper failed: {result.stderr}")
                return None

            # Safety check to verify the output file exists.
            if not os.path.exists(output_file):
                logger.error(f"No output file found: {output_file}")
                return None

            # Retrieves and logs the audio duration
            # This is done by reading the WAV header
            with wave.open(output_file, 'rb') as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                duration = frames / float(rate)

            logger.info(f"Generated {duration:.2f}s of audio")
            return output_file

        # Exception handling for timeouts or other unexpected errors.
        except subprocess.TimeoutExpired:
            logger.error("Piper synthesis timed out")
            return None
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None

    def synthesise_to_array(self, text):
        """
        Synthesise text and return the audio as a numpy array instead of a file.

        Args:
            text: Text to synthesise.

        Returns:
            Tuple of (audio_array, sample_rate), or (None, None) on failure.
        """
        try:
            import numpy as np
            import soundfile as sf

            # Creates a temporary file which Piper writes to, this is deleted upon use.
            temp_file = self.temp_dir / f"temp_{os.getpid()}.wav"
            result = self.synthesise(text, str(temp_file))

            if result is None:
                return None, None

            audio_array, sample_rate = sf.read(str(temp_file))

            temp_file.unlink()

            return audio_array, sample_rate
        # Exception handling which logs any unexpected errors.
        except Exception as e:
            logger.error(f"Error converting to array: {e}")
            return None, None


def main():
    """
    Manual testing which only runs if text_to_speech.py is ran directly.
    This synthesises a list of sample phrases with Piper. 
    The audio is not played upon test completion but is saved to a file which can be opened manually.    
    """
    import yaml

    # Loads config.yaml
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Initialise TTS
    tts = TextToSpeech(config)

    # Test phrases to be synthesised.
    test_phrases = [
        f"Hello, I am {tts.name}, your personal voice assistant.",
        "The weather today is partly cloudy with a high of twenty two degrees.",
        "I'm turning on the living room lights now.",
        "That would be four.",
        "What's good G"
    ]

    print("\n=== Testing Piper TTS ===\n")

    # Runs synthesis for each phrase and reports success / failure per phrase.
    for i, phrase in enumerate(test_phrases, 1):
        print(f"[{i}/{len(test_phrases)}] Synthesising: '{phrase}'")
        output_file = f"test_tts_{i}.wav"

        result = tts.synthesise(phrase, output_file)

        if result:
            print(f"Saved to: {output_file}")
            # Terminal command to play the test file.
            print(f"Play with: aplay {output_file}")
        else:
            print(f"Failed.")
        print()

# Tests the TextToSpeech class manually.
if __name__ == "__main__":
    main()