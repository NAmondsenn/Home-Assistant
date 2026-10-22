import logging
import queue
import re
import threading
import numpy as np
from typing import Any, Optional, Dict, Union
import time
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


def _heard(text: str, duration: float, after_recording: bool = False) -> Dict[str, Any]:
    """
    What Nemotron heard, logged, and as transcribe() returns it.

    Args:
        text: The words.
        duration: How long it took, in seconds.
        after_recording: Whether that was only the time since the recording stopped,
            the rest having been done while the user was talking.

    Returns:
        The 'text' - empty if it's one of the things speech models imagine in noise -
        and the 'duration'.
    """
    took = f"{duration:.2f}s after the recording stopped" if after_recording else f"{duration:.2f}s"
    logger.info(f"Transcription complete ({took}): '{text}'", extra=TRANSCRIPT)
    if text and re.sub(r"[^a-z' ]", "", text.lower()).strip() in HALLUCINATIONS:
        logger.info(f"Ignoring '{text}' - most likely words imagined in noise", extra=TRANSCRIPT)
        text = ""
    return {"text": text, "language": "en", "language_probability": 1.0, "duration": duration}


class NemotronSpeechToText:
    """
    Speech-to-text on the brain, with Nemotron - see voice_assistant/nemotron.py - for
    every satellite's recordings. It hears each one as it's made, through a stream of
    its own, so the words are ready soon after the user stops, as they were when the
    backend heard for the Pi.
    """

    def __init__(self, nemotron):
        """
        Args:
            nemotron: The Nemotron, loaded - one, which every satellite's recordings share.
        """
        self.nemotron = nemotron

    @property
    def loaded(self) -> bool:
        """Whether there's a model to hear with - what Home Assistant's Backend shows."""
        return self.nemotron is not None

    def listen(self) -> "NemotronTranscription":
        """Speech-to-text for the next recording, worked out as it's heard."""
        return NemotronTranscription(self)

    def transcribe(self, audio: np.ndarray) -> Dict[str, Any]:
        """
        Transcribes a whole recording at once - for when hearing it as it was made went wrong.

        Args:
            audio: float32 samples at 16kHz, from -1 to 1.

        Returns:
            A dictionary with the 'text' and how long it took ('duration') - the text
            empty if it's one of the things speech models imagine in noise.
        """
        started = time.time()
        try:
            stream = self.nemotron.start()
            self.nemotron.add(stream, np.asarray(audio, dtype=np.float32))
            text = self.nemotron.finish(stream)
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return {"text": "", "language": None, "language_probability": 0.0, "duration": 0.0, "error": str(e)}
        return _heard(str(text).strip(), time.time() - started)


# What a NemotronTranscription is told once the recording is over.
_FINISHED, _CANCELLED = "finished", "cancelled"


class NemotronTranscription:
    """
    One recording, heard by Nemotron as it's made - see NemotronSpeechToText.

    Nemotron works on a thread of its own, so the recording needn't wait: each
    look at the recording takes a moment, and the recording is what notices the user
    has stopped, so it mustn't wait for it.
    """

    def __init__(self, stt: NemotronSpeechToText):
        """
        Args:
            stt: The speech-to-text it's for.
        """
        self._stt = stt
        # The recording as it's made, then _FINISHED or _CANCELLED once it's over.
        self._waiting: "queue.Queue[Union[np.ndarray, str]]" = queue.Queue()
        self._text: Optional[str] = None
        self._failed: Optional[Exception] = None
        self._thread = threading.Thread(target=self._hear, name="nemotron", daemon=True)
        self._thread.start()

    def add(self, audio: np.ndarray):
        """
        More of the recording, as it's made. Returns straight away.

        Args:
            audio: float32 samples at 16kHz, carrying on from the last.
        """
        self._waiting.put(audio)

    def finish(self, audio: np.ndarray) -> Dict[str, Any]:
        """
        The words, once Nemotron has caught up with the end of the recording.

        Args:
            audio: The whole recording, which is transcribed afresh if anything went
                wrong while listening, so nothing said is lost.

        Returns:
            What NemotronSpeechToText.transcribe() returns - its 'duration' only how
            long was left to do once the recording stopped.
        """
        started = time.time()
        self._waiting.put(_FINISHED)
        self._thread.join()
        if self._failed is not None:
            logger.warning(f"Couldn't transcribe the recording as it was made, so starting again "
                           f"on the whole of it: {self._failed}")
            return self._stt.transcribe(audio)
        return _heard(str(self._text).strip(), time.time() - started, after_recording=True)

    def cancel(self):
        """The recording came to nothing, so Nemotron stops without finishing it."""
        self._waiting.put(_CANCELLED)
        self._thread.join()

    def _hear(self):
        """Gives Nemotron the recording as it arrives, until it's over - on a thread of its own."""
        nemotron = self._stt.nemotron
        try:
            stream = nemotron.start()
            while True:
                piece = self._waiting.get()
                if piece is _CANCELLED:
                    return
                if piece is _FINISHED:
                    self._text = nemotron.finish(stream)
                    return
                nemotron.add(stream, piece)
        except Exception as e:
            self._failed = e
