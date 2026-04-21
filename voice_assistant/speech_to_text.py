"""
Speech-to-Text Module
Handles audio transcription using faster-whisper.
"""

import logging
import numpy as np
from faster_whisper import WhisperModel
from typing import Optional, Dict
import time

logger = logging.getLogger(__name__)


class SpeechToText:
    """Transcribes speech using Whisper."""
    
    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        fallback_model: Optional[str] = None
    ):
        """
        Initialize Whisper model.
        
        Args:
            model_size: Model size (tiny.en, base.en, small.en, etc.)
            device: Device to run on (cpu or cuda)
            compute_type: Computation precision (int8, float16, float32)
            fallback_model: Fallback model if primary is too slow
        """
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.fallback_model = fallback_model
        
        logger.info(f"Loading Whisper model: {model_size}")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        logger.info(f"Whisper model loaded: {model_size}")
        
        # Warm up model with dummy audio
        self._warmup()
        
    def _warmup(self):
        """Warm up model to avoid first-query latency."""
        logger.info("Warming up Whisper model...")
        dummy_audio = np.zeros(16000, dtype=np.float32)
        try:
            list(self.model.transcribe(dummy_audio, beam_size=1))
            logger.info("Model warm-up complete")
        except Exception as e:
            logger.warning(f"Warm-up failed: {e}")
            
    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        language: str = "en",
        beam_size: int = 5,
        vad_filter: bool = True
    ) -> Dict[str, any]:
        """
        Transcribe audio to text.
        
        Args:
            audio: Audio data as numpy array (float32, -1 to 1)
            sample_rate: Sample rate of audio
            language: Language code (en, es, fr, etc.)
            beam_size: Beam size for decoding (higher = more accurate, slower)
            vad_filter: Use voice activity detection to filter silence
            
        Returns:
            Dict with 'text', 'language', 'confidence', 'duration'
        """
        logger.info("Transcribing audio...")
        start_time = time.time()
        
        try:
            segments, info = self.model.transcribe(
                audio,
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                vad_parameters=dict(min_silence_duration_ms=500)
            )
            
            text_segments = []
            for segment in segments:
                text_segments.append(segment.text)
                
            text = " ".join(text_segments).strip()
            duration = time.time() - start_time
            
            result = {
                'text': text,
                'language': info.language,
                'confidence': info.language_probability,
                'duration': duration
            }
            
            logger.info(f"Transcription complete ({duration:.2f}s): '{text}'")
            return result
            
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return {
                'text': '',
                'language': 'en',
                'confidence': 0.0,
                'duration': 0.0,
                'error': str(e)
            }
            
    def transcribe_file(self, filepath: str) -> Dict[str, any]:
        """
        Transcribe audio file.
        
        Args:
            filepath: Path to audio file
            
        Returns:
            Transcription result dict
        """
        logger.info(f"Transcribing file: {filepath}")
        start_time = time.time()
        
        try:
            segments, info = self.model.transcribe(
                filepath,
                beam_size=5,
                vad_filter=True
            )
            
            text_segments = []
            for segment in segments:
                text_segments.append(segment.text)
                
            text = " ".join(text_segments).strip()
            duration = time.time() - start_time
            
            result = {
                'text': text,
                'language': info.language,
                'confidence': info.language_probability,
                'duration': duration
            }
            
            logger.info(f"File transcription complete ({duration:.2f}s)")
            return result
            
        except Exception as e:
            logger.error(f"File transcription failed: {e}")
            return {
                'text': '',
                'language': 'en',
                'confidence': 0.0,
                'duration': 0.0,
                'error': str(e)
            }


# Test function - runs when you execute this file directly
if __name__ == "__main__":
    import pyaudio
    import wave
    
    logging.basicConfig(level=logging.INFO)
    
    print("\n=== Speech-to-Text Test ===\n")
    
    # Initialize STT
    stt = SpeechToText(model_size="base.en")
    
    # Record audio
    pa = pyaudio.PyAudio()
    print("\nRecording 5 seconds...")
    print("🎤 Speak now!")
    
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=48000,
        input=True,
        frames_per_buffer=2048,
        input_device_index=0
    )
    
    frames = []
    for _ in range(int(48000/2048 * 5)):
        data = stream.read(2048, exception_on_overflow=False)
        frames.append(data)
        
    stream.close()
    pa.terminate()
    
    # Save temp file
    with wave.open("temp_test.wav", 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b''.join(frames))
    
    # Transcribe
    result = stt.transcribe_file("temp_test.wav")
    
    print(f"\n{'='*40}")
    print(f"Transcription: '{result['text']}'")
    print(f"Language: {result['language']}")
    print(f"Confidence: {result['confidence']:.2%}")
    print(f"Duration: {result['duration']:.2f}s")
    print(f"{'='*40}\n")
