"""
Recording Module.

Records the user's query until they stop talking, from any source of audio: the
local microphone, or - on the brain - a feed of audio which a satellite sent over the
network. Both are read the same way, so the same tested algorithm, and the same
handling of pauses and false wake words, serves them.
"""

import logging
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

# Logger setup
logger = logging.getLogger(__name__)

# The rate everything downstream works at.
RATE = 16000

# The recorder checks for speech 30ms at a time, the frame length openWakeWord's
# copy of Silero is built around.
FRAME = 480

# Once the user has started speaking, a frame only counts towards the silence which
# ends the recording when it's this far below the speech threshold - the same margin
# Silero's own tools use, so a word trailing off doesn't flicker in and out of
# "silence" and end the recording early.
SILENCE_MARGIN = 0.15

# A pause at least this long before the user starts talking means the moment kept
# from before listening started is no longer wanted - see record_until_silence.
PAUSE_SECONDS = 0.2

# How much is kept from just before the user started talking, once anything earlier
# has been dropped, so the first sound of their first word isn't clipped.
LEAD_IN_SECONDS = 0.15


def record_until_silence(source, detector, listen_from: Optional[int] = None,
                         keep_before: float = 0.0, timeout: float = 10.0,
                         silence_duration: float = 0.6, speech_timeout: float = 3.0,
                         min_speech: float = 0.12, timeline=None,
                         heard: Optional[Callable[[np.ndarray], None]] = None) -> np.ndarray:
    """
    Records until the user has spoken and then gone quiet, or a timeout is reached.

    Args:
        source: Where the audio comes from - a Microphone, or an AudioFeed. It needs
            position, listen(start) and time_at(position).
        detector: Says how likely each frame is to be speech - see vad.py.
        listen_from: Position to start listening from (see position), such as
            the moment the wake word was heard. Now if None.
        keep_before: Seconds of audio from before listen_from to keep as well.
            It isn't listened to for speech, but goes to speech-to-text with the rest,
            so words said straight after the wake word - which can come before
            it's even been detected - aren't cut off.
        timeout: Maximum recording duration in seconds, from listen_from.
        silence_duration: How long the user has to be quiet, once they've
            started talking, for the recording to stop.
        speech_timeout: How long to wait for the user to start talking before
            giving up, so a false wake word doesn't sit recording silence.
        min_speech: Seconds of continuous speech which count as the user
            starting to talk. Anything shorter - the end of the wake word, or
            the listening sound, which the microphone hears too - doesn't.
        timeline: Optional Timeline to mark when listening started, when speech
            started and ended, and when the recording stopped.
        heard: Given the recording as it's made, so speech-to-text can work out
            the words while the user is still saying them: everything up to the
            moment they're found to be talking, then each 30ms after it, as
            float32 samples which add up to exactly what's returned. Nothing is
            given until the user starts talking, so a false wake word costs
            nothing.

    Returns:
        The recording as float32 samples at 16kHz, or an empty array if the user
        never started talking.
    """
    logger.info("Recording until silence...")
    if listen_from is None:
        listen_from = source.position
    if timeline:
        timeline.mark("recording_started")

    reader = source.listen(start=max(0, listen_from - int(keep_before * RATE)))
    kept = []
    if reader.position < listen_from:
        kept.append(reader.read(listen_from - reader.position))

    detector.reset()
    max_frames = int(timeout * RATE / FRAME)
    speech_timeout_frames = int(speech_timeout * RATE / FRAME)
    frames_to_start = max(1, round(min_speech * RATE / FRAME))
    frames_to_stop = max(1, round(silence_duration * RATE / FRAME))

    speech_frames = 0
    silent_frames = 0
    speech_started = False
    # Where the user last made a sound - the moment they actually stopped talking,
    # as opposed to when the silence after it was long enough to stop recording.
    speech_ended_at = None
    # Whether there was a pause before the user started talking - see below.
    quiet_frames = 0
    paused = False

    for i in range(max_frames):
        frame = reader.read(FRAME)
        kept.append(frame)
        if speech_started and heard:
            heard(frame.astype(np.float32) / 32768.0)
        if len(frame) < FRAME:
            # A source which has ended (see AudioFeed.end) gives what's left, which
            # is too short for the detector. Nothing more is coming, so whatever
            # was said is the recording - unless the user hadn't started yet.
            if not speech_started:
                logger.info("The audio ended before any speech, cancelling")
                return np.array([], dtype=np.float32)
            break
        probability = detector.speech_probability(frame)

        if not speech_started:
            # Gives up early if the user never started talking, so a false wake
            # word doesn't leave the assistant recording silence.
            if i >= speech_timeout_frames:
                logger.info(f"No speech within {speech_timeout:.1f}s, cancelling")
                return np.array([], dtype=np.float32)

            speech_frames = speech_frames + 1 if probability >= detector.threshold else 0
            quiet_frames = 0 if speech_frames else quiet_frames + 1
            paused = paused or quiet_frames * FRAME >= PAUSE_SECONDS * RATE
            if speech_frames >= frames_to_start:
                speech_started = True
                speech_ended_at = reader.position
                # The moment kept from before listening started is there for "Hey
                # Nova what time is it" said in one breath. When the user paused
                # after the wake word instead - for the listening sound, usually -
                # it only holds the end of "Nova", which Whisper hears as "Turn
                # over," or "Get over." and which then stops a plain "set a timer"
                # being recognised. So everything before the pause is dropped -
                # now, before any of it is handed over, since it can't be taken
                # back once speech-to-text has heard it.
                if paused:
                    keep_from = reader.position - speech_frames * FRAME - int(LEAD_IN_SECONDS * RATE)
                    so_far = np.concatenate(kept)
                    kept = [so_far[max(0, keep_from - (reader.position - len(so_far))):]]
                if heard:
                    heard(np.concatenate(kept).astype(np.float32) / 32768.0)
                logger.info("Speech detected, listening for the end of the query...")
                if timeline:
                    timeline.mark("speech_started",
                                  at=source.time_at(reader.position - speech_frames * FRAME))
        elif probability < detector.threshold - SILENCE_MARGIN:
            silent_frames += 1
            if silent_frames >= frames_to_stop:
                logger.info(f"Silence detected after {(reader.position - listen_from) / RATE:.1f}s")
                break
        else:
            silent_frames = 0
            speech_ended_at = reader.position

    if timeline:
        if speech_started:
            timeline.mark("speech_ended", at=source.time_at(speech_ended_at))
        timeline.mark("recording_stopped")

    audio = np.concatenate(kept).astype(np.float32) / 32768.0
    logger.info(f"Recorded {len(audio)} samples")
    return audio


class AudioFeed:
    """
    A source of audio fed from elsewhere, such as a satellite's microphone arriving
    over the network, which record_until_silence reads just as it reads a Microphone.

    Positions are counted in samples at 16kHz since the first one was fed. Everything
    fed is kept, since one feed is only ever one query's worth of audio.
    """

    def __init__(self):
        self._samples = np.zeros(0, dtype=np.int16)
        # The position each piece fed ends at, and when it arrived, for time_at.
        self._arrivals: List[Tuple[int, float]] = []
        self._ended = False
        self._condition = threading.Condition()

    def feed(self, samples: np.ndarray):
        """
        Adds newly arrived audio, and wakes anything waiting for it.

        Args:
            samples: 16-bit samples at 16kHz.
        """
        with self._condition:
            self._samples = np.concatenate([self._samples, samples.astype(np.int16)])
            self._arrivals.append((len(self._samples), time.monotonic()))
            self._condition.notify_all()

    def end(self):
        """
        Says nothing more is coming. A reader which is waiting gets what's left, and
        so the recording stops.
        """
        with self._condition:
            self._ended = True
            self._condition.notify_all()

    @property
    def position(self) -> int:
        """How many samples have been fed so far - the position of "now"."""
        with self._condition:
            return len(self._samples)

    def listen(self, start: Optional[int] = None) -> "AudioFeedReader":
        """
        Starts reading the audio from a position onwards.

        Args:
            start: Where to start - now if None.

        Returns:
            A reader which keeps its own place.
        """
        return AudioFeedReader(self, self.position if start is None else start)

    def time_at(self, position: int) -> float:
        """
        When a position arrived, as a time.monotonic() value - for timing marks.
        Within a piece it's counted back from the piece's arrival, since the piece
        was all heard before it was sent.

        Args:
            position: A position, as from the position property.
        """
        with self._condition:
            for end, arrived in self._arrivals:
                if position <= end:
                    return arrived - (end - position) / RATE
            return self._arrivals[-1][1] if self._arrivals else time.monotonic()

    def _read(self, start: int, count: int) -> np.ndarray:
        """
        Waits until `count` samples from `start` have been fed, or the feed has
        ended, then returns them.

        Returns:
            The samples - fewer than asked for, or none, only if the feed ended.
        """
        with self._condition:
            self._condition.wait_for(lambda: self._ended or len(self._samples) >= start + count)
            return self._samples[start:start + count].copy()


class AudioFeedReader:
    """One reader's place in an AudioFeed, which moves on as it reads."""

    def __init__(self, feed: AudioFeed, position: int):
        """
        Args:
            feed: The feed to read from.
            position: Where to start reading - see AudioFeed.position.
        """
        self.feed = feed
        self.position = position

    def read(self, count: int) -> np.ndarray:
        """
        The next samples, waiting for them to be fed if need be.

        Args:
            count: How many samples to read.

        Returns:
            16-bit samples at 16kHz - fewer than `count` only once the feed has ended.
        """
        samples = self.feed._read(self.position, count)
        self.position += len(samples)
        return samples
