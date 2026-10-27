"""
Speech-to-Text Module.

Speech-to-text on the brain, with Nemotron (see nemotron.py) for every satellite's
recordings. Each recording is heard as it's made, through a stream of its own, so the
words are mostly worked out by the time the user stops - and words it imagines in
noise, such as a lone "Thank you.", are thrown away.

A lone short word - "Pause.", "Wait.", "Play." - often comes back as nothing at all,
since Nemotron has no words around it to go on. So a recording the user spoke in which
comes to nothing is heard again, said twice, then after a "Please" - see
.superpowers/sdd/pause-investigation.md.
"""

import logging
import queue
import re
import threading
import numpy as np
from typing import Any, Optional, Dict, Tuple, Union
import time
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Things speech-to-text is known to "hear" in noise, breathing or silence - Whisper,
# trained on a lot of subtitled video, did it most, and Nemotron does too. Only thrown
# away when they're the whole transcript, so a real "set a timer, thank you" is kept.
HALLUCINATIONS = {"you", "thank you", "thanks", "bye", "bye bye",
                  "thanks for watching", "thank you for watching"}

RATE = 16000

# The quiet between the two copies of a recording heard again, and after the "Please"
# before one, in seconds. On the brain, at 0.3s 26 of 31 lone words which had come
# back as nothing were heard; at 0.5s and more the halves disagreed more often
# ("Ludder Lutter").
GAP = 0.3

# How loud Piper's "Please" has to be to count as the word rather than the quiet
# around it, which is trimmed off.
PLEASE_QUIET = 0.01


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


def _words(text: str) -> list:
    """The words in text as they're compared - in lower case, without punctuation."""
    return re.findall(r"[a-z0-9']+", text.lower())


def _said_twice(text: str) -> str:
    """
    What a recording heard again said twice over was, if Nemotron heard the same both times.

    Args:
        text: What it heard, e.g. "Pause. Pause."

    Returns:
        The first of the two, e.g. "Pause." - or '' if the halves differ, as "Cause
        pause" does, so a word only half heard isn't acted on.
    """
    said = [word for word in text.split() if _words(word)]
    half = len(said) // 2
    if not said or len(said) % 2 or _words(" ".join(said[:half])) != _words(" ".join(said[half:])):
        return ""
    return " ".join(said[:half]).rstrip(",;:")


def _after_please(text: str) -> str:
    """
    What a recording heard again after a "Please" was.

    Args:
        text: What it heard, e.g. "Please pause."

    Returns:
        It without the "Please", e.g. "pause." - or '' unless it starts with "Please"
        and has more after, since otherwise Nemotron didn't hear what it was given.
    """
    heard = re.match(r"\W*please\b\W*(.*)", text, flags=re.IGNORECASE)
    return heard.group(1).strip() if heard and _words(heard.group(1)) else ""


def make_please(tts) -> Optional[np.ndarray]:
    """
    Says "Please" with Piper, to go before a recording heard again - see
    NemotronSpeechToText.hear_again. Made once, as the brain starts, rather than for
    each recording which needs it.

    Args:
        tts: The brain's TextToSpeech.

    Returns:
        It, as float32 samples at 16kHz without Piper's quiet before and after - or
        None if Piper couldn't say it, when a recording is only heard again said twice.
    """
    try:
        import soxr

        pieces = list(tts.stream("Please", remember=False))
        audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
        loud = np.flatnonzero(np.abs(audio) >= PLEASE_QUIET)
        if not len(loud):
            logger.warning("Piper didn't say 'Please', so a recording heard as nothing is only heard again said twice")
            return None
        word = audio[loud[0]:loud[-1] + 1]
        return np.asarray(soxr.resample(word, tts.sample_rate, RATE), dtype=np.float32)
    except Exception as e:
        logger.warning(f"Couldn't have Piper say 'Please', so a recording heard as nothing is only heard "
                       f"again said twice: {e}")
        return None


class NemotronSpeechToText:
    """
    Speech-to-text on the brain, with Nemotron - see voice_assistant/nemotron.py - for
    every satellite's recordings. It hears each one as it's made, through a stream of
    its own, so the words are ready soon after the user stops, as they were when the
    backend heard for the Pi.
    """

    def __init__(self, nemotron, please: Optional[np.ndarray] = None):
        """
        Args:
            nemotron: The Nemotron, loaded - one, which every satellite's recordings share.
            please: Piper's "Please", from make_please, to hear a recording again after -
                or None not to.
        """
        self.nemotron = nemotron
        self.please = please

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
            text = self._hear_whole(audio)
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return {"text": "", "language": None, "language_probability": 0.0, "duration": 0.0, "error": str(e)}
        return _heard(text, time.time() - started)

    def hear_again(self, speech: np.ndarray) -> Tuple[str, str]:
        """
        Hears a recording Nemotron heard nothing in again: said twice over, which gives
        it words around the one said to go on, and if the two don't agree, after a
        "Please" - which is never wrong, but on the brain heard fewer.

        Args:
            speech: The recording, without the quiet it stopped after.

        Returns:
            What was heard, or '' - and how: "twice", "please" or "nothing".

        Raises:
            Exception: Whatever Nemotron raises.
        """
        gap = np.zeros(int(GAP * RATE), dtype=np.float32)
        twice = self._hear_whole(np.concatenate([speech, gap, speech]))
        heard = _said_twice(twice)
        logger.info(f"Heard nothing, so listening again with it said twice: '{heard}' (heard as '{twice}')",
                    extra=TRANSCRIPT)
        if heard:
            return heard, "twice"
        if self.please is None:
            return "", "nothing"
        after = self._hear_whole(np.concatenate([self.please, gap, speech]))
        heard = _after_please(after)
        logger.info(f"Heard nothing again, so listening once more after 'Please': '{heard}' (heard as '{after}')",
                    extra=TRANSCRIPT)
        return heard, "please" if heard else "nothing"

    def _hear_whole(self, audio: np.ndarray) -> str:
        """
        What Nemotron hears in a whole recording, through a stream of its own.

        Raises:
            Exception: Whatever Nemotron raises.
        """
        stream = self.nemotron.start()
        self.nemotron.add(stream, np.asarray(audio, dtype=np.float32))
        return str(self.nemotron.finish(stream)).strip()


# What a NemotronTranscription is told once the recording is over.
_FINISHED, _CANCELLED = "finished", "cancelled"

# How long to wait for Nemotron to finish a recording, and to stop on one which came
# to nothing, in seconds. It has the words about 0.3 seconds after the user stops (see
# nemotron.py), so these only run out on one which has hung - which the session then
# says went wrong, rather than waiting for good while its satellite gets silence.
FINISH_LIMIT = 20.0
CANCEL_LIMIT = 5.0


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
        # Whether it was given any of the recording - which is only once the recording
        # has found the user talking (see recording.record_until_silence).
        self._speech = False
        self._thread = threading.Thread(target=self._hear, name="nemotron", daemon=True)
        self._thread.start()

    def add(self, audio: np.ndarray):
        """
        More of the recording, as it's made. Returns straight away.

        Args:
            audio: float32 samples at 16kHz, carrying on from the last.
        """
        self._speech = True
        self._waiting.put(audio)

    def finish(self, audio: np.ndarray, timeline=None, quiet_after: float = 0.0) -> Dict[str, Any]:
        """
        The words, once Nemotron has caught up with the end of the recording - heard
        again if the user spoke and it came to nothing (see NemotronSpeechToText.hear_again).

        Args:
            audio: The whole recording, which is transcribed afresh if anything went
                wrong while listening, so nothing said is lost.
            timeline: The Timeline to time hearing it again on, if it is.
            quiet_after: Seconds of quiet the recording stopped after, left out when
                it's heard again - the two copies are to be GAP apart.

        Returns:
            What NemotronSpeechToText.transcribe() returns - its 'duration' only how
            long was left to do once the recording stopped.

        Raises:
            TimeoutError: Nemotron hasn't finished within FINISH_LIMIT, so has hung -
                and hearing the whole recording again would only hang too.
        """
        started = time.time()
        self._waiting.put(_FINISHED)
        self._thread.join(FINISH_LIMIT)
        if self._thread.is_alive():
            raise TimeoutError(f"Nemotron hadn't finished the recording after {FINISH_LIMIT:.0f}s")
        if self._failed is not None:
            logger.warning(f"Couldn't transcribe the recording as it was made, so starting again "
                           f"on the whole of it: {self._failed}")
            heard = self._stt.transcribe(audio)
        else:
            heard = _heard(str(self._text).strip(), time.time() - started, after_recording=True)
        if heard["text"] or not self._speech or "error" in heard:
            return heard

        if timeline:
            timeline.mark("stt_retry_started")
        speech = np.asarray(audio, dtype=np.float32)[:max(0, len(audio) - int(quiet_after * RATE))]
        try:
            text, how = self._stt.hear_again(speech)
        except Exception as e:
            logger.error(f"Couldn't listen to the recording again: {e}")
            text, how = "", "nothing"
        if timeline:
            timeline.mark("stt_retry_done")
            timeline.note(heard_again=how)
        return _heard(text, time.time() - started, after_recording=True)

    def cancel(self):
        """
        The recording came to nothing, so Nemotron stops without finishing it - or is
        left to, if it hasn't within CANCEL_LIMIT, as one which has hung never would.
        """
        self._waiting.put(_CANCELLED)
        self._thread.join(CANCEL_LIMIT)
        if self._thread.is_alive():
            logger.warning(f"Nemotron hadn't stopped after {CANCEL_LIMIT:.0f}s, so it's left to")

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
