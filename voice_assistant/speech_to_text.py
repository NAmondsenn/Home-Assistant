import logging
import numpy as np
from faster_whisper import WhisperModel
from typing import Optional, Dict, Union
import time

# Logger setup
logger = logging.getLogger(__name__)

class SpeechToText:
    def __init__(
        self,
        # Small, English-only model which is faster and uses less memory.
        model_size: str = "base.en", 
        device: str = "cpu",
        # Use int8 quantization for faster inference and lower memory usage.
        compute_type: str = "int8",
        fallback_model: Optional[str] = None
    ):

       # Initialise the SpeechToText class with the specified model size, 
       # device, compute type, and optional fallback model.
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.fallback_model = fallback_model

        # Load the Whisper model using the specified parameters.
        # This logs before and after loading the model to provide feedback on the process.
        # If loading fails and a fallback_model was provided, this is tried
        # instead of crashing the entire assistant on startup.
        logger.info(f"Loading Whisper model: {model_size}")
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
            logger.info(f"Whisper model loaded: {model_size}")
        except Exception as e:
            if fallback_model:
                logger.warning(f"Failed to load {model_size}, falling back to {fallback_model}: {e}")
                self.model = WhisperModel(fallback_model, device=device, compute_type=compute_type)
                logger.info(f"Whisper model loaded: {fallback_model}")
            else:
                raise

        # Runs a warm-up process to avoid latency on the first transcription query.
        self._warmup()
        
    def _warmup(self):
        """
        Warm up model to avoid first-query latency.
        This method runs a dummy transcription to ensure that the model is ready for real-time use.
        It provides feedback on whether the warm-up was successful or if it encountered any issues.
        """
        logger.info("Warming up Whisper model...")
        # Provides audio at 16kHz sample rate, which is the expected input for Whisper.
        dummy_audio = np.zeros(16000, dtype=np.float32)
        try:
            list(self.model.transcribe(dummy_audio, beam_size=1))
            logger.info("Model warm-up complete")
        except Exception as e:
            logger.warning(f"Warm-up failed: {e}")
            
    def transcribe(
        self,
        audio: Union[np.ndarray, str],
        language: str = "en",
        beam_size: int = 3,
        vad_filter: bool = True
        ) -> Dict[str, any]:
        """
        Transcribes audio into text using the Whisper model.

        Args:
            audio: Array of raw audio samples.
            language: Default language for transcription (English).
            beam_size: Decoding beam size; higher = slower but more accurate (default: 3).
            vad_filter: Enable Voice Activity Detection to skip silence.

        Returns:
            A dictionary containing the transcribed text, detected language, confidence score, and duration.
        """
        source = f"file: {audio}" if isinstance(audio, str) else "raw audio array"

        # Logs the start of the transcription process starts a timer to track how long the transcription takes.
        logger.info(f"Transcribing {source}...")
        start_time = time.time()

        # try/except block which attempts to transcribe the audio and handles any exceptions that may occur during the process.
        try:
            # Passes audio into the Whisper model.
            segments, info = self.model.transcribe(
                audio,
                language=language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                vad_parameters=dict(min_silence_duration_ms=500)
            )

            # Whisper returns segments of text, which are concatenated into a single string.
            text = " ".join(segment.text for segment in segments).strip()
            # Calculates the duration of the transcription process 
            duration = time.time() - start_time

            # The result dictionary is constructed to include the transcribed text, 
            # detected language, confidence score, and duration of the transcription.
            result = {
                'text': text,
                'language': info.language,
                'confidence': info.language_probability,
                'duration': duration
            }

            # Logs the completion of the transcription process alongside its duration, returning the result.
            logger.info(f"Transcription complete ({duration:.2f}s): '{text}'")
            return result

        # Exception handling to catch any errors that occur during the transcription process. 
        # If an error occurs, it logs the error, returning a result dictionary with empty / default values, 
        # along with the error message. 
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return {
                'text': '',
                'language': None,
                'confidence': 0.0,
                'duration': 0.0,
                'error': str(e)
            }

    def transcribe_file(self, filepath: str, **kwargs) -> Dict[str, any]:
        """
        Transcribe an audio file from disk.

        Forwards the file path to transcribe().
        Any extra arguments (e.g. beam_size, language) also get passed through.

        Args:
            filepath: Path to the audio file to transcribe.
            **kwargs: Any other arguments accepted by transcribe() (e.g. beam_size, language, vad_filter).

        Returns:
            A dictionary containing the transcribed text, detected language, confidence score, and duration.
        """
        return self.transcribe(audio=filepath, **kwargs)

# Test function - runs when you execute this file directly
# Records 5 seconds of audio from the microphone, saves it to a temporary WAV file, transcribes it,
# and prints the result (text, detected language, confidence, and duration).
if __name__ == "__main__":
    import pyaudio
    import wave
    
    logging.basicConfig(level=logging.INFO)
    
    print("\n=== Speech-to-Text Test ===\n")
    
    # Initialise speech-to-text model
    stt = SpeechToText(model_size="base.en")
    
    # Connects to the microphone and informs the user that reccording is starting.
    pa = pyaudio.PyAudio()
    print("\nRecording 5 seconds of audio...")
    print("Speak now!")

    # Opens the mic stream at 48kHz sample rate, 16-bit depth, mono channel, and a buffer size of 2048 frames.
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=48000,
        input=True,
        frames_per_buffer=2048,
        input_device_index=0
    )

    # Records audio chunks lasting for 5 seconds and appends them to a list.
    frames = []
    for _ in range(int(48000/2048 * 5)):
        data = stream.read(2048, exception_on_overflow=False)
        frames.append(data)

    # Stops the microphone stream and terminates the PyAudio instance.
    stream.close()
    pa.terminate()
    
    # Writes the collected frames to temp_test.wav at the mic's native 48kHz rate.
    with wave.open("temp_test.wav", 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b''.join(frames))
    
    # Transcribes the file using the transcribe_file() method.
    result = stt.transcribe_file("temp_test.wav")

    # Prints out the results dictionary in a readable form.
    print(f"\n{'='*40}")
    print(f"Transcription: '{result['text']}'")
    print(f"Language: {result['language']}")
    print(f"Confidence: {result['confidence']:.2%}")
    print(f"Duration: {result['duration']:.2f}s")
    print(f"{'='*40}\n")
