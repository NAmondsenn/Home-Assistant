import logging
import os
import re
import numpy as np
from faster_whisper import WhisperModel
from typing import Any, List, Optional, Dict, Union
import time
from voice_assistant import paths
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Things Whisper is known to "hear" in noise, breathing or silence - it was trained
# on a lot of subtitled video. Only thrown away when they're the whole transcript,
# so a real "set a timer, thank you" is kept.
HALLUCINATIONS = {"you", "thank you", "thanks", "bye", "bye bye",
                  "thanks for watching", "thank you for watching"}

# A segment counts as Whisper guessing when it's both fairly sure there was no
# speech, and unsure of the words it came up with anyway.
NO_SPEECH_PROBABILITY = 0.6
LOW_AVERAGE_LOGPROB = -1.0


def looks_like_hallucination(text: str, segments) -> bool:
    """
    Whether a transcription is most likely Whisper finding words in noise.

    Any text at all used to be treated as a request, so a false wake word followed
    by a cough could come back as "Thank you." and be answered.

    Args:
        text: The whole transcription.
        segments: Whisper's segments for it, with their no_speech_prob and avg_logprob.

    Returns:
        True if it should be thrown away.
    """
    if re.sub(r"[^a-z' ]", "", text.lower()).strip() in HALLUCINATIONS:
        return True
    return bool(segments) and all(segment.no_speech_prob > NO_SPEECH_PROBABILITY
                                  and segment.avg_logprob < LOW_AVERAGE_LOGPROB
                                  for segment in segments)

class SpeechToText:
    def __init__(
        self,
        # Small, English-only model which is faster and uses less memory.
        model_size: str = "base.en", 
        device: str = "cpu",
        # Use int8 quantisation for faster inference and lower memory usage.
        compute_type: str = "int8",
        fallback_model: Optional[str] = None,
        initial_prompt: Optional[str] = None,
        beam_size: int = 1,
        # Where the model files are kept - see download_root in config.yaml.
        download_root: Optional[str] = None
    ):

       # Initialise the SpeechToText class with the specified model size, 
       # device, compute type, and optional fallback model.
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.fallback_model = fallback_model
        # Words the model is told to expect, which biases it towards names it would
        # otherwise mangle - "Spotify" and the assistant's own name especially.
        self.initial_prompt = initial_prompt
        self.beam_size = beam_size

        # Load the Whisper model using the specified parameters.
        # This logs before and after loading the model to provide feedback on the process.
        # If loading fails and a fallback_model was provided, this is tried
        # instead of crashing the entire assistant on startup.
        logger.info(f"Loading Whisper model: {model_size}")
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type,
                                      download_root=download_root)
            logger.info(f"Whisper model loaded: {model_size}")
        except Exception as e:
            if fallback_model:
                logger.warning(f"Failed to load {model_size}, falling back to {fallback_model}: {e}")
                self.model = WhisperModel(fallback_model, device=device, compute_type=compute_type,
                                          download_root=download_root)
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
        # A second of very quiet noise at 16kHz, which is the expected input for Whisper.
        # Noise rather than silence, so the decoder has something to work through as well.
        dummy_audio = (np.random.default_rng(0).standard_normal(16000) * 0.01).astype(np.float32)
        try:
            # transcribe() only sets things up - the segments it returns are a generator,
            # worked out as they're read. So they have to be read here, or the model never
            # actually runs and the first real query pays the whole start-up cost anyway.
            segments, _ = self.model.transcribe(dummy_audio, beam_size=1, language="en")
            list(segments)
            logger.info("Model warm-up complete")
        except Exception as e:
            logger.warning(f"Warm-up failed: {e}")
            
    def transcribe(
        self,
        audio: Union[np.ndarray, str],
        language: str = "en",
        beam_size: Optional[int] = None,
        vad_filter: bool = True
        ) -> Dict[str, Any]:
        """
        Transcribes audio into text using the Whisper model.

        Args:
            audio: Array of raw audio samples.
            language: Default language for transcription (English).
            beam_size: Decoding beam size; higher = slower but more accurate.
                       Defaults to the value given when the model was set up.
            vad_filter: Enable Voice Activity Detection to skip silence.

        Returns:
            A dictionary containing the transcribed text, detected language, how sure it
            was of the language, and duration. The text is empty if it looks like
            Whisper only imagined it - see looks_like_hallucination.
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
                beam_size=beam_size if beam_size is not None else self.beam_size,
                vad_filter=vad_filter,
                vad_parameters=dict(min_silence_duration_ms=500),
                # Nudges the model towards the words this assistant actually hears,
                # so names like "Spotify" aren't turned into something similar sounding.
                initial_prompt=self.initial_prompt
            )

            # Whisper returns segments of text, which are concatenated into a single string.
            segments = list(segments)
            text = " ".join(segment.text for segment in segments).strip()
            # Calculates the duration of the transcription process 
            duration = time.time() - start_time

            # Logs the completion of the transcription process alongside its duration.
            logger.info(f"Transcription complete ({duration:.2f}s): '{text}'", extra=TRANSCRIPT)

            if text and looks_like_hallucination(text, segments):
                logger.info(f"Ignoring '{text}' - most likely Whisper finding words in noise", extra=TRANSCRIPT)
                text = ""

            # The result dictionary is constructed to include the transcribed text,
            # detected language and duration of the transcription. language_probability
            # is how sure it is of the language - always 1 for an English-only model,
            # so it says nothing about how well the words were heard.
            return {
                'text': text,
                'language': info.language,
                'language_probability': info.language_probability,
                'duration': duration
            }

        # Exception handling to catch any errors that occur during the transcription process. 
        # If an error occurs, it logs the error, returning a result dictionary with empty / default values, 
        # along with the error message. 
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return {
                'text': '',
                'language': None,
                'language_probability': 0.0,
                'duration': 0.0,
                'error': str(e)
            }


class MoonshineSpeechToText:
    """
    Moonshine, a speech-to-text model made for small devices, in place of Whisper.

    Measured on the Pi on 26 spoken commands, its small streaming model took 0.83s
    typically against Whisper base's 2.16s, and got fewer words wrong - 1.5% to
    4.5% - with neither making words up from silence or noise. Whisper spends most
    of its time on a 30 second window however short the command; Moonshine works on
    just the audio there is. Its one slip worth knowing: a lone "stop" came back as
    "soap", which is allowed for - see Clock.STOP_WORDS and intents.py.
    """

    def __init__(self, model: str = "small-streaming", download_root: Optional[str] = None,
                 keyterms: Optional[List[str]] = None):
        """
        Args:
            model: Which Moonshine model - "tiny-streaming", "small-streaming" or
                "medium-streaming".
            download_root: Where the model files are kept, downloaded there if they
                aren't yet.
            keyterms: Words to listen out for - names it would otherwise mishear,
                like Whisper's initial_prompt.
        """
        # Imported here rather than at the top, so Whisper alone still works on a
        # machine without Moonshine installed.
        import moonshine_voice

        # Moonshine only knows its models by their hyphenated names.
        architecture = moonshine_voice.string_to_model_arch(model.strip().lower().replace("_", "-"))
        logger.info(f"Loading Moonshine model: {model}")
        model_path, architecture = moonshine_voice.get_model_for_language(
            "en", architecture, cache_root=download_root, on_progress=lambda fraction, name: None)
        self.transcriber = moonshine_voice.Transcriber(model_path=model_path, model_arch=architecture)
        if keyterms:
            self.transcriber.set_keyterms([term for term in keyterms if "," not in term])
        logger.info(f"Moonshine model loaded: {model}")
        # The first transcription loads everything else it needs, so it's done now
        # rather than on the user's first request.
        self.transcribe((np.random.default_rng(0).standard_normal(16000) * 0.01).astype(np.float32))

    def transcribe(self, audio: np.ndarray) -> Dict[str, Any]:
        """
        Transcribes a recording.

        Args:
            audio: float32 samples at 16kHz, from -1 to 1.

        Returns:
            A dictionary with the 'text' and how long it took ('duration') - the text
            empty if it's one of the things speech models imagine in noise.
        """
        logger.info("Transcribing raw audio array...")
        started = time.time()
        try:
            transcript = self.transcriber.transcribe_without_streaming(np.asarray(audio, dtype=np.float32).tolist(), 16000)
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return {"text": "", "language": None, "language_probability": 0.0, "duration": 0.0, "error": str(e)}
        text = " ".join(line.text.strip() for line in transcript.lines).strip()
        duration = time.time() - started
        logger.info(f"Transcription complete ({duration:.2f}s): '{text}'", extra=TRANSCRIPT)
        if text and re.sub(r"[^a-z' ]", "", text.lower()).strip() in HALLUCINATIONS:
            logger.info(f"Ignoring '{text}' - most likely words imagined in noise", extra=TRANSCRIPT)
            text = ""
        return {"text": text, "language": "en", "language_probability": 1.0, "duration": duration}


def make_speech_to_text(config):
    """
    The speech-to-text model speech_to_text.engine in config.yaml names - Moonshine
    or Whisper - falling back to Whisper if Moonshine can't be loaded.

    Args:
        config: Configs loaded from config.yaml.
    """
    engine = (config.section("speech_to_text").get("engine") or "whisper").lower()
    if engine == "moonshine":
        settings = config.section("moonshine")
        try:
            return MoonshineSpeechToText(
                model=settings.get("model", "small-streaming"),
                download_root=os.path.join(paths.PROJECT_DIRECTORY, settings.get("download_root", "models/moonshine")),
                keyterms=settings.get("keyterms"))
        except Exception as e:
            logger.error(f"Couldn't load Moonshine, so using Whisper instead: {e}")

    whisper = config.section("whisper")
    return SpeechToText(model_size=whisper.get("model", "base.en"), device=whisper.get("device", "cpu"),
                        compute_type=whisper.get("compute_type", "int8"),
                        fallback_model=whisper.get("fallback_model", "tiny.en"),
                        initial_prompt=whisper.get("initial_prompt"),
                        beam_size=whisper.get("beam_size", 1),
                        download_root=os.path.join(paths.PROJECT_DIRECTORY,
                                                   whisper.get("download_root", "models/whisper")))
