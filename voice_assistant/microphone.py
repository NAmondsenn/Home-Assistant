"""
Microphone Module.

One input stream, opened once at startup and left running, which everything that
listens reads from - first the wake word detector, then the recorder. The last few
seconds of audio are kept at 16kHz, the rate openWakeWord, Silero and Whisper all
expect, so nothing downstream needs to resample.

Opening the microphone afresh for each stage used to lose words: the wake word's
stream was closed the moment it was heard, and nothing listened again until the
recording stream had opened, after the listening sound - so "Hey Nova what time is
it", said in one breath, reached Whisper as "It's time to...". With one stream
nothing is missed, and a recording can even start a moment before the wake word
was detected.
"""

import logging
import threading
import time
from typing import Optional, Tuple

import numpy as np
import sounddevice as sd
import soxr

# Logger setup
logger = logging.getLogger(__name__)

# The rate everything downstream works at.
RATE = 16000

# How often the stream hands over audio, in seconds of it.
BLOCK_SECONDS = 0.03

# How much audio PortAudio may hold before it has to be handed over, in seconds. The
# Pi's shared input (dsnoop, in /etc/asound.conf) delivers audio in 125ms bursts,
# and PortAudio's default buffer holds only one burst, so the slightest delay
# overflowed it - measured on the Pi, about a fifth of the audio went missing that
# way, while arecord on the same input lost nothing. Two bursts' worth loses none.
INPUT_LATENCY = 0.25

# The recorder checks for speech 30ms at a time, the frame length openWakeWord's
# copy of Silero is built around.
FRAME = 480

# Once the user has started speaking, a frame only counts towards the silence which
# ends the recording when it's this far below the speech threshold - the same margin
# Silero's own tools use, so a word trailing off doesn't flicker in and out of
# "silence" and end the recording early.
SILENCE_MARGIN = 0.15

# The longest wait between attempts to reopen a microphone which has gone quiet.
MAX_RETRY_SECONDS = 30.0


class Microphone:
    """
    The one always-open microphone stream, and the last few seconds of what it heard.

    Positions are counted in samples at 16kHz since the assistant started, and only
    ever go up, so anything reading from it can keep its own place - see listen().
    """

    def __init__(self, sample_rate: int = 48000, device: Optional[str] = None,
                 buffer_seconds: float = 10.0, stall_timeout: float = 2.0):
        """
        Opens the stream. A missing microphone isn't fatal here - opening it is
        retried whenever something is waiting for audio, so the assistant still
        starts, and picks the microphone up once it's plugged in.

        Args:
            sample_rate: Rate to capture at, before conversion to 16kHz. The Pi's
                shared input (see setup.sh) runs at 48kHz.
            device: Input device name, or None for the system default.
            buffer_seconds: How much recent audio is kept.
            stall_timeout: Seconds without any audio before the stream is taken to
                have died, and is reopened.
        """
        self.capture_rate = sample_rate
        self.device = device
        self.stall_timeout = stall_timeout
        self.capacity = int(buffer_seconds * RATE)

        self._buffer = np.zeros(self.capacity, dtype=np.int16)
        self._total = 0
        self._condition = threading.Condition()

        # When the newest sample arrived, so a position can be turned back into a time.
        self._newest_at = time.monotonic()
        # When audio last arrived, or a reopen was last tried - whichever is later.
        self._last_activity = time.monotonic()
        self._retry_delay = stall_timeout
        self._recovering = False

        self._stream = None
        # Bumped every time the stream is reopened, so a late block from a stream
        # which has since been replaced is thrown away rather than mixed in.
        self._generation = 0
        self._open()

    def _open(self) -> bool:
        """
        Opens (or reopens) the input stream.

        Returns:
            True if it opened.
        """
        self._discard_stream()
        self._generation += 1
        generation = self._generation

        # Each stream has a resampler of its own, since it carries a little audio
        # over from one block to the next, which mustn't leak across a reopen.
        resampler = (soxr.ResampleStream(self.capture_rate, RATE, 1, dtype="float32")
                     if self.capture_rate != RATE else None)

        def on_audio(indata, frames, time_info, status):
            # Called by PortAudio, on its own thread, for every block captured. Lets
            # no exception out, since one would stop the stream for good.
            try:
                if generation != self._generation:
                    return
                if status:
                    logger.debug(f"Microphone: {status}")
                audio = indata[:, 0]
                if resampler is not None:
                    audio = resampler.resample_chunk(audio)
                self._append((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16))
            except Exception as e:
                logger.error(f"Microphone audio dropped: {e}")

        try:
            stream = sd.InputStream(samplerate=self.capture_rate, channels=1, dtype="float32",
                                    blocksize=int(self.capture_rate * BLOCK_SECONDS),
                                    latency=INPUT_LATENCY, device=self.device, callback=on_audio)
            stream.start()
        except Exception as e:
            logger.error(f"Could not open the microphone: {e}")
            return False

        self._stream = stream
        logger.info(f"Microphone open: capturing at {self.capture_rate}Hz, kept at {RATE}Hz")
        return True

    def _discard_stream(self) -> Optional[threading.Thread]:
        """
        Closes the current stream, if there is one, without waiting for it.

        A stream whose microphone has been unplugged can hang when it's closed, and
        take whatever closed it down with it - which is how unplugging the
        microphone once left the assistant deaf, and unable even to shut down when
        asked. So it's closed on a thread of its own, which is left to it.

        Returns:
            The thread doing the closing, or None if there was no stream.
        """
        stream, self._stream = self._stream, None
        if stream is None:
            return None
        closing = threading.Thread(target=self._close_quietly, args=(stream,),
                                   daemon=True, name="microphone-close")
        closing.start()
        return closing

    @staticmethod
    def _close_quietly(stream):
        """Closes a stream, ignoring whatever state its device is in."""
        try:
            stream.close(ignore_errors=True)
        except Exception as e:
            logger.debug(f"Closing an old microphone stream: {e}")

    def _append(self, samples: np.ndarray):
        """Adds newly heard audio to the buffer, overwriting the oldest."""
        with self._condition:
            if len(samples) > self.capacity:
                self._total += len(samples) - self.capacity
                samples = samples[-self.capacity:]

            start = self._total % self.capacity
            first = min(len(samples), self.capacity - start)
            self._buffer[start:start + first] = samples[:first]
            self._buffer[:len(samples) - first] = samples[first:]
            self._total += len(samples)

            self._newest_at = self._last_activity = time.monotonic()
            self._retry_delay = self.stall_timeout
            if self._recovering:
                self._recovering = False
                logger.info("Microphone working again")
            self._condition.notify_all()

    @property
    def position(self) -> int:
        """How many samples have been heard so far - the position of "now"."""
        with self._condition:
            return self._total

    def time_at(self, position: int) -> float:
        """
        When a position was heard, as a time.monotonic() value - for timing marks.

        Args:
            position: A position, as from the position property.
        """
        with self._condition:
            return self._newest_at - (self._total - position) / RATE

    def listen(self, start: Optional[int] = None) -> "MicrophoneReader":
        """
        Starts reading the audio from a position onwards.

        Args:
            start: Where to start - now if None, or an earlier position to pick up
                audio which has already been heard.

        Returns:
            A reader which keeps its own place - see MicrophoneReader.
        """
        return MicrophoneReader(self, self.position if start is None else start)

    def _read(self, start: int, count: int) -> Tuple[int, np.ndarray]:
        """
        Waits until `count` samples from `start` have been heard, then returns them,
        reopening the stream along the way if it seems to have died.

        Returns:
            Tuple of (where the samples actually start, the samples). That's later
            than asked for if the start had already dropped out of the buffer.
        """
        while True:
            with self._condition:
                if self._total < start + count:
                    # Waits in short slices, so a dead stream is noticed, and so a
                    # shutdown signal is never held up for long.
                    self._condition.wait(timeout=0.25)

                if self._total >= start + count:
                    oldest = self._total - self.capacity
                    if start < oldest:
                        logger.warning(f"Fell {(oldest - start) / RATE:.1f}s behind the microphone, "
                                       f"skipping ahead")
                        start = oldest
                    return start, self._slice(start, count)

                stalled = time.monotonic() - self._last_activity >= self._retry_delay

            if stalled:
                self._recover()

    def _slice(self, start: int, count: int) -> np.ndarray:
        """Copies samples out of the ring buffer. Called with the lock held."""
        offset = start % self.capacity
        first = min(count, self.capacity - offset)
        return np.concatenate([self._buffer[offset:offset + first], self._buffer[:count - first]])

    def _recover(self):
        """
        Reopens a stream which has stopped delivering audio - usually because the
        microphone was unplugged. Each attempt which doesn't bring audio back waits
        twice as long before the next, up to MAX_RETRY_SECONDS, so a microphone left
        unplugged doesn't fill the log.
        """
        logger.warning(f"No audio from the microphone for {self._retry_delay:.0f}s, reopening it")
        self._recovering = True
        self._open()
        with self._condition:
            self._last_activity = time.monotonic()
            self._retry_delay = min(self._retry_delay * 2, MAX_RETRY_SECONDS)

    def record_until_silence(self, detector, listen_from: Optional[int] = None,
                             keep_before: float = 0.0, timeout: float = 10.0,
                             silence_duration: float = 0.8, speech_timeout: float = 3.0,
                             min_speech: float = 0.12, timeline=None) -> np.ndarray:
        """
        Records until the user has spoken and then gone quiet, or a timeout is reached.

        Args:
            detector: Says how likely each frame is to be speech - see vad.py.
            listen_from: Position to start listening from (see position), such as
                the moment the wake word was heard. Now if None.
            keep_before: Seconds of audio from before listen_from to keep as well.
                It isn't listened to for speech, but goes to Whisper with the rest,
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

        Returns:
            The recording as float32 samples at 16kHz, or an empty array if the user
            never started talking.
        """
        logger.info("Recording until silence...")
        if listen_from is None:
            listen_from = self.position
        if timeline:
            timeline.mark("recording_started")

        reader = self.listen(start=max(0, listen_from - int(keep_before * RATE)))
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

        for i in range(max_frames):
            frame = reader.read(FRAME)
            kept.append(frame)
            probability = detector.speech_probability(frame)

            if not speech_started:
                # Gives up early if the user never started talking, so a false wake
                # word doesn't leave the assistant recording silence.
                if i >= speech_timeout_frames:
                    logger.info(f"No speech within {speech_timeout:.1f}s, cancelling")
                    return np.array([], dtype=np.float32)

                speech_frames = speech_frames + 1 if probability >= detector.threshold else 0
                if speech_frames >= frames_to_start:
                    speech_started = True
                    speech_ended_at = reader.position
                    logger.info("Speech detected, listening for the end of the query...")
                    if timeline:
                        timeline.mark("speech_started",
                                      at=self.time_at(reader.position - speech_frames * FRAME))
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
                timeline.mark("speech_ended", at=self.time_at(speech_ended_at))
            timeline.mark("recording_stopped")

        audio = np.concatenate(kept).astype(np.float32) / 32768.0
        logger.info(f"Recorded {len(audio)} samples")
        return audio

    def close(self):
        """Stops the stream for good."""
        self._generation += 1
        closing = self._discard_stream()
        # Given a moment to finish, so the stream is properly gone before the
        # assistant exits - but no longer, in case its microphone has been unplugged.
        if closing is not None:
            closing.join(timeout=1.0)


class MicrophoneReader:
    """One reader's place in the microphone's audio, which moves on as it reads."""

    def __init__(self, microphone: Microphone, position: int):
        """
        Args:
            microphone: The microphone to read from.
            position: Where to start reading - see Microphone.position.
        """
        self.microphone = microphone
        self.position = position

    def read(self, count: int) -> np.ndarray:
        """
        The next samples, waiting for them to be heard if need be.

        Args:
            count: How many samples to read.

        Returns:
            16-bit samples at 16kHz.
        """
        start, samples = self.microphone._read(self.position, count)
        self.position = start + count
        return samples
