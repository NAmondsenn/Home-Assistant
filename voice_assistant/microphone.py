"""
Microphone Module.

One input stream, opened once at startup and left running, which everything that
listens reads from - first the wake word detector, then the recorder. The last few
seconds of audio are kept at 16kHz, the rate openWakeWord, Silero and speech-to-text all
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

from voice_assistant import watchdog
from voice_assistant.dropouts import Dropouts
# Where the recording algorithm and its constants now live; named here too so the
# tests, and anything else which reached for them on the microphone, still find them.
from voice_assistant.recording import FRAME, LEAD_IN_SECONDS, RATE  # noqa: F401

# Logger setup
logger = logging.getLogger(__name__)

# How often the stream hands over audio, in seconds of it.
BLOCK_SECONDS = 0.03

# How much audio PortAudio may hold before it has to be handed over, in seconds -
# slack, not delay, since each burst is handed over as soon as it arrives. With
# ALSA's own 125ms bursts, PortAudio's default buffer held only one, and the
# slightest delay overflowed it - about a fifth of the audio went missing that way
# on the Pi. setup.sh now has the shared input (dsnoop, in /etc/asound.conf) work
# in 21ms bursts, so audio reaches the wake word about 50ms after it was heard
# rather than 130ms, and this leaves room for several of them.
INPUT_LATENCY = 0.25

# The longest wait between attempts to reopen a microphone which has gone quiet.
MAX_RETRY_SECONDS = 30.0


class Microphone:
    """
    The one always-open microphone stream, and the last few seconds of what it heard.

    Positions are counted in samples at 16kHz since the assistant started, and only
    ever go up, so anything reading from it can keep its own place - see listen().
    """

    def __init__(self, sample_rate: int = 48000, device: Optional[str] = None,
                 buffer_seconds: float = 10.0, stall_timeout: float = 2.0, echo=None):
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
            echo: The EchoCanceller, which takes what the assistant played back out
                of what's heard, before anything listens to it - see echo.py.
        """
        self.capture_rate = sample_rate
        self.echo = echo
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
        self.overflowed = Dropouts("The microphone overflowed, so some of what was said was lost")
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
                if status and status.input_overflow:
                    self.overflowed.note()
                audio = indata[:, 0]
                if resampler is not None:
                    audio = resampler.resample_chunk(audio)
                samples = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
                if self.echo is not None:
                    samples = self.echo.process(samples, getattr(time_info, "inputBufferAdcTime", None))
                self._append(samples)
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
            # Waiting on the microphone, even one being reopened, is the main loop
            # working as it should - see watchdog.py.
            watchdog.beat()
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
