"""
Text-to-Speech Module using Piper
Converts text responses to natural-sounding speech
"""

import subprocess
import os
import wave
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TextToSpeech:
    def __init__(self, config):
        """
        Initialize Piper TTS
        
        Args:
            config: Dictionary containing TTS configuration
        """
        self.config = config
        self.piper_path = os.path.expanduser("~/models/piper/piper")
        self.model_path = os.path.expanduser("~/models/en_GB-alan-medium.onnx")
        self.temp_dir = Path("temp_audio")
        self.temp_dir.mkdir(exist_ok=True)
        
        # Verify Piper is installed
        if not os.path.exists(self.piper_path):
            raise FileNotFoundError(
                f"Piper binary not found at {self.piper_path}. "
                "Run install_piper.sh first."
            )
        
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"Voice model not found at {self.model_path}. "
                "Run install_piper.sh first."
            )
        
        logger.info(f"Piper TTS initialized with model: {self.model_path}")
        
        # Test Piper on startup (warmup)
        self._warmup()
    
    def _warmup(self):
        """Run a test synthesis to warm up the model"""
        try:
            logger.info("Warming up Piper TTS...")
            test_file = self.temp_dir / "warmup.wav"
            self.synthesize("Ready.", str(test_file))
            test_file.unlink()  # Delete warmup file
            logger.info("Piper warmup complete")
        except Exception as e:
            logger.warning(f"Warmup failed: {e}")
    
    def synthesize(self, text, output_file):
        """
        Convert text to speech and save as WAV file
        
        Args:
            text: Text to synthesize
            output_file: Path to save WAV file
            
        Returns:
            Path to generated WAV file
        """
        try:
            # Clean text (remove special characters that might break TTS)
            text = text.replace('"', "'").replace('\n', ' ').strip()
            
            if not text:
                logger.warning("Empty text provided to TTS")
                return None
            
            logger.info(f"Synthesizing: '{text[:50]}...'")
            
            # Run Piper via subprocess
            # Echo text into Piper, output to file
            cmd = f'echo "{text}" | {self.piper_path} --model {self.model_path} --output_file {output_file}'
            
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10  # Prevent hanging
            )
            
            if result.returncode != 0:
                logger.error(f"Piper failed: {result.stderr}")
                return None
            
            # Verify output file exists
            if not os.path.exists(output_file):
                logger.error(f"Output file not created: {output_file}")
                return None
            
            # Get audio duration for logging
            with wave.open(output_file, 'rb') as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                duration = frames / float(rate)
            
            logger.info(f"Generated {duration:.2f}s of audio")
            return output_file
            
        except subprocess.TimeoutExpired:
            logger.error("Piper synthesis timed out")
            return None
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None
    
    def synthesize_to_array(self, text):
        """
        Synthesize text and return as numpy array
        
        Args:
            text: Text to synthesize
            
        Returns:
            tuple: (audio_array, sample_rate) or (None, None) on error
        """
        try:
            import numpy as np
            import soundfile as sf
            
            # Generate to temp file
            temp_file = self.temp_dir / f"temp_{os.getpid()}.wav"
            result = self.synthesize(text, str(temp_file))
            
            if result is None:
                return None, None
            
            # Read WAV file to array
            audio_array, sample_rate = sf.read(str(temp_file))
            
            # Clean up temp file
            temp_file.unlink()
            
            return audio_array, sample_rate
            
        except Exception as e:
            logger.error(f"Error converting to array: {e}")
            return None, None


def main():
    """Test the TTS module"""
    import yaml
    
    # Load config
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Initialize TTS
    tts = TextToSpeech(config)
    
    # Test phrases
    test_phrases = [
        "Hello, I am Jarvis, your personal voice assistant.",
        "The weather today is partly cloudy with a high of twenty two degrees.",
        "I'm turning on the living room lights now.",
        "That would be four.",
        "Why did the scarecrow win an award? Because he was outstanding in his field!"
    ]
    
    print("\n=== Testing Piper TTS ===\n")
    
    for i, phrase in enumerate(test_phrases, 1):
        print(f"[{i}/{len(test_phrases)}] Synthesizing: '{phrase}'")
        output_file = f"test_tts_{i}.wav"
        
        result = tts.synthesize(phrase, output_file)
        
        if result:
            print(f"✅ Saved to: {output_file}")
            print(f"   Play with: aplay {output_file}")
        else:
            print(f"❌ Failed")
        print()


if __name__ == "__main__":
    main()
