"""
Speaker Module.

The one owner of the audio output. A single stream is opened at startup and left
running, and every sound the assistant makes - replies, chimes, timer beeps and
alarms - is handed to it, rather than each opening a stream of its own.

Each sound used to be played with one blocking write to a stream of its own, so
nothing could stop it part way: saying "stop" to an alarm only took effect once
its 34 second sound had finished, and while it played the microphone heard it as
the user talking. The clock's thread and the main loop could also open streams at
the same moment, which PortAudio doesn't promise to cope with. Now every sound is
a Playback which can be stopped, and only PortAudio's own thread ever writes.
"""

import logging
import os
import threading
import time
from typing import Iterable, List, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr

# Logger setup
logger = logging.getLogger(__name__)

# How often the stream asks for more audio, in seconds of it.
BLOCK_SECONDS = 0.02

# How much audio PortAudio may hold ahead of the speaker, in seconds. This is
# how long a stopped sound can still be heard for, so it's kept short - but too
# short and the shared output (dmix, in /etc/asound.conf) runs dry and crackles.
# Measured on the Pi, dmix works in 125ms periods, so anything up to that gives
# 125ms, which ran for 10 seconds without once running dry.
OUTPUT_LATENCY = 0.1

# Seconds without the stream asking for audio before it's taken to have died.
STALL_SECONDS = 2.0


class Playback:
    """One sound being played, which can be waited for or stopped part way through."""

    def __init__(self, audio: np.ndarray, volume: float, kind: str, sample_rate: int,
                 streaming: bool = False):
        """
        Args:
            audio: float32 samples at the speaker's rate.
            volume: How loud, from 0 to 1. Can be changed while it plays.
            kind: What sort of sound it is, e.g. "reply" or "announcement" - so a
                whole kind can be stopped at once.
            sample_rate: The speaker's rate, for working out how long it lasts.
            streaming: True if more of it is still to come - see Speaker.play_stream.
        """
        self.audio = audio
        self.volume = volume
        self.kind = kind
        self.position = 0
        self.stopped = False
        self.sample_rate = sample_rate
        self.streaming = streaming
        self._finished = threading.Event()
        # When the last of it will have actually left the speaker - see _finish.
        self._heard_by = 0.0

    @property
    def duration(self) -> float:
        """How long it lasts, in seconds - so far, for one still streaming in."""
        return len(self.audio) / self.sample_rate

    def _append(self, audio: np.ndarray):
        """
        Adds the next piece of a streamed sound. The whole array is swapped for a
        longer one rather than changed in place, so the speaker's thread - which
        reads it without a lock - only ever sees one or the other.
        """
        self.audio = np.concatenate((self.audio, audio))

    def stop(self):
        """Stops it straight away, part way through or not."""
        self.stopped = True
        self._finish(time.monotonic())

    def _finish(self, heard_by: float):
        """
        Marks it as done.

        Args:
            heard_by: The time.monotonic() moment its last sample reaches the
                speaker. The stream holds a little audio ahead, so a sound is
                still being heard for a moment after it's been handed over.
        """
        if not self._finished.is_set():
            self._heard_by = heard_by
            self._finished.set()

    @property
    def done(self) -> bool:
        """Whether it has finished or been stopped."""
        return self._finished.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Waits until it has finished playing, or been stopped.

        Args:
            timeout: The longest to wait, in seconds. None waits for the whole
                sound - but never indefinitely, in case the speaker has died part
                way through, which would otherwise leave this waiting for good.

        Returns:
            True if it finished (or was stopped), False if the timeout came first.
        """
        if timeout is None:
            # How long a streamed sound lasts isn't known until the last of it has
            # arrived. Whatever is feeding it gives up by itself if it hangs.
            while self.streaming and not self._finished.wait(0.1):
                pass
            timeout = (len(self.audio) - self.position) / self.sample_rate + OUTPUT_LATENCY + STALL_SECONDS
            if not self._finished.wait(timeout):
                logger.warning(f"A {self.duration:.1f}s {self.kind} sound never finished playing")
                self.stop()
                return True
        elif not self._finished.wait(timeout):
            return False

        remaining = self._heard_by - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        return True


class Speaker:
    """The one always-open output stream, mixing whatever is playing."""

    def __init__(self, sample_rate: int = 48000, device: Optional[str] = None):
        """
        Opens the stream. A missing speaker isn't fatal - opening it is tried again
        the next time something is played.

        Args:
            sample_rate: Rate to play at. The Pi's shared output (see setup.sh)
                runs at 48kHz; anything else is resampled to it.
            device: Output device name, or None for the system default.
        """
        self.sample_rate = sample_rate
        self.device = device

        self._lock = threading.Lock()
        self._playing: List[Playback] = []
        # Kinds of sound refused for now - see silence().
        self._silenced = set()
        # The assistant's own sounds, kept once loaded rather than read from the
        # SD card and resampled every time.
        self._cache = {}

        self._stream = None
        self._latency = OUTPUT_LATENCY
        self._last_callback = time.monotonic()
        self._open()

    def _open(self) -> bool:
        """
        Opens (or reopens) the output stream.

        Returns:
            True if it opened.
        """
        self._discard_stream()
        try:
            stream = sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="float32",
                                     blocksize=int(self.sample_rate * BLOCK_SECONDS),
                                     latency=OUTPUT_LATENCY, device=self.device, callback=self._fill)
            stream.start()
        except Exception as e:
            logger.error(f"Could not open the speaker: {e}")
            return False

        self._stream = stream
        self._latency = stream.latency
        self._last_callback = time.monotonic()
        logger.info(f"Speaker open: {self.sample_rate}Hz, {self._latency * 1000:.0f}ms ahead")
        return True

    def _discard_stream(self) -> Optional[threading.Thread]:
        """
        Closes the current stream, if there is one, on a thread of its own - a stream
        whose device has gone can hang when it's closed. See Microphone._discard_stream.

        Returns:
            The thread doing the closing, or None if there was no stream.
        """
        stream, self._stream = self._stream, None
        if stream is None:
            return None
        closing = threading.Thread(target=self._close_quietly, args=(stream,),
                                   daemon=True, name="speaker-close")
        closing.start()
        return closing

    @staticmethod
    def _close_quietly(stream):
        """Closes a stream, ignoring whatever state its device is in."""
        try:
            stream.close(ignore_errors=True)
        except Exception as e:
            logger.debug(f"Closing an old speaker stream: {e}")

    def _fill(self, outdata, frames, time_info, status):
        """
        Called by PortAudio, on its own thread, whenever the speaker needs more audio.
        Mixes the next stretch of everything playing. Lets no exception out, since
        one would stop the stream for good.
        """
        self._last_callback = time.monotonic()
        try:
            if status:
                logger.debug(f"Speaker: {status}")
            with self._lock:
                playing = list(self._playing)

            mix = np.zeros(frames, dtype=np.float32)
            finished = []
            for playback in playing:
                if playback.stopped:
                    finished.append(playback)
                    continue
                chunk = playback.audio[playback.position:playback.position + frames]
                mix[:len(chunk)] += chunk * playback.volume
                playback.position += len(chunk)
                # One still streaming in which has caught up with what's arrived
                # just goes quiet until the next piece does.
                if playback.position >= len(playback.audio) and not playback.streaming:
                    finished.append(playback)

            np.clip(mix, -1.0, 1.0, out=mix)
            outdata[:, 0] = mix

            if finished:
                heard_by = time.monotonic() + self._latency
                with self._lock:
                    self._playing = [p for p in self._playing if p not in finished]
                for playback in finished:
                    playback._finish(heard_by)
        except Exception as e:
            outdata.fill(0)
            logger.error(f"Speaker audio dropped: {e}")

    def play(self, audio: np.ndarray, sample_rate: Optional[int] = None,
             volume: float = 1.0, kind: str = "reply", streaming: bool = False) -> Playback:
        """
        Starts playing a sound, without waiting for it.

        Args:
            audio: float samples, mono or with channels as columns.
            sample_rate: The audio's rate, if it isn't the speaker's.
            volume: How loud, from 0 (silent) to 1 (as recorded).
            kind: What sort of sound it is - see Playback.
            streaming: True if more is still to come - see play_stream.

        Returns:
            The Playback, to wait for or stop. Already finished if this kind of
            sound is silenced right now, or there's no speaker to play it on.
        """
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sample_rate and sample_rate != self.sample_rate:
            audio = soxr.resample(audio, sample_rate, self.sample_rate)

        playback = Playback(audio, max(0.0, min(1.0, float(volume))), kind, self.sample_rate, streaming)

        # A stream which has stopped asking for audio has died - usually because
        # the speaker was unplugged - so it's reopened before anything is lost to it.
        if self._stream is None or time.monotonic() - self._last_callback > STALL_SECONDS:
            logger.warning("The speaker stopped taking audio, reopening it")
            if not self._open():
                playback._finish(time.monotonic())
                return playback

        with self._lock:
            if kind in self._silenced:
                playback._finish(time.monotonic())
                return playback
            self._playing.append(playback)
        return playback

    def play_stream(self, pieces: Iterable[np.ndarray], sample_rate: Optional[int] = None,
                    volume: float = 1.0, kind: str = "reply") -> Playback:
        """
        Starts playing a sound which is still being made, each piece as soon as it
        arrives. A reply used to be synthesised whole, written to the SD card and
        read back before any of it played - so the user heard nothing until the
        last word of it had been worked out.

        Args:
            pieces: float32 mono pieces of the sound, in order, read on a thread of
                their own. Closed early, if it has a close(), once the sound is
                stopped - so a generator can stop whatever is making them.
            sample_rate: Their rate, if it isn't the speaker's.
            volume: How loud, from 0 (silent) to 1 (as recorded).
            kind: What sort of sound it is - see Playback.

        Returns:
            The Playback, to wait for or stop.
        """
        playback = self.play(np.zeros(0, dtype=np.float32), volume=volume, kind=kind, streaming=True)
        if playback.done:
            playback.streaming = False
            self._close_pieces(pieces)
            return playback
        threading.Thread(target=self._feed, args=(playback, pieces, sample_rate),
                         daemon=True, name="speaker-feed").start()
        return playback

    def _feed(self, playback: Playback, pieces: Iterable[np.ndarray], sample_rate: Optional[int]):
        """Hands a streamed sound to its Playback as it arrives - see play_stream."""
        # Resampled as one continuous stream, since resampling each piece on its own
        # leaves a click at every join.
        resampler = None
        if sample_rate and sample_rate != self.sample_rate:
            resampler = soxr.ResampleStream(sample_rate, self.sample_rate, 1, dtype="float32")
        try:
            for piece in pieces:
                if playback.done:
                    break
                piece = np.asarray(piece, dtype=np.float32)
                playback._append(resampler.resample_chunk(piece) if resampler else piece)
            else:
                if resampler:
                    playback._append(resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True))
        except Exception as e:
            logger.error(f"A streamed {playback.kind} sound failed part way: {e}")
        finally:
            playback.streaming = False
            self._close_pieces(pieces)

    @staticmethod
    def _close_pieces(pieces):
        """Closes a streamed sound's source, if it can be closed."""
        close = getattr(pieces, "close", None)
        if close is not None:
            close()

    def play_file(self, filename: str, volume: float = 1.0, kind: str = "reply",
                  cache: bool = True, start: float = 0.0, length: Optional[float] = None) -> Optional[Playback]:
        """
        Starts playing a sound file, or part of one, without waiting for it.

        Args:
            filename: Path to the audio file.
            volume: How loud, from 0 (silent) to 1 (as recorded).
            kind: What sort of sound it is - see Playback.
            cache: Keep the file once loaded. Right for the assistant's own sounds,
                wrong for a reply that's only ever played once.
            start: Seconds into the file to start from. Past the end, it carries on
                from the beginning again, as if the file were looping.
            length: Seconds to play, or None for the rest of the file.

        Returns:
            The Playback, or None if the file was missing or unreadable.
        """
        try:
            audio = self._cache.get(filename) if cache else None
            if audio is None:
                if not os.path.exists(filename):
                    logger.warning(f"Sound file not found: {filename}")
                    return None
                audio, file_rate = sf.read(filename, dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                if file_rate != self.sample_rate:
                    audio = soxr.resample(audio, file_rate, self.sample_rate)
                if cache:
                    self._cache[filename] = audio
        except Exception as e:
            logger.error(f"Could not play {filename}: {e}")
            return None

        if start or length is not None:
            audio = self._section(audio, start, length)
        return self.play(audio, volume=volume, kind=kind)

    def _section(self, audio: np.ndarray, start: float, length: Optional[float]) -> np.ndarray:
        """
        Part of a sound, as if it were looping - see play_file.

        Args:
            audio: The whole sound, at the speaker's rate.
            start: Seconds in to start from.
            length: Seconds to take, or None for the rest of it.
        """
        if len(audio) == 0:
            return audio
        begin = int(start * self.sample_rate) % len(audio)
        if length is None:
            return audio[begin:]
        count = int(length * self.sample_rate)
        repeats = (begin + count) // len(audio) + 1
        return np.tile(audio, repeats)[begin:begin + count]

    def stop(self, kinds: Optional[Iterable[str]] = None):
        """
        Stops whatever is playing straight away.

        Args:
            kinds: Only stop these kinds of sound. None stops everything.
        """
        with self._lock:
            playing = list(self._playing)
        for playback in playing:
            if kinds is None or playback.kind in kinds:
                playback.stop()

    def silence(self, kind: str):
        """
        Stops every sound of one kind, and refuses any more until allow() is called.

        Used for the timers and alarms while the user is talking to the assistant:
        stopping them alone would leave a gap for the next round to start in.

        Args:
            kind: The kind of sound, e.g. "announcement".
        """
        with self._lock:
            self._silenced.add(kind)
        self.stop([kind])

    def allow(self, kind: str):
        """
        Lets a kind of sound silenced with silence() play again.

        Args:
            kind: The kind of sound, e.g. "announcement".
        """
        with self._lock:
            self._silenced.discard(kind)

    def close(self):
        """Stops everything and closes the stream for good."""
        self.stop()
        closing = self._discard_stream()
        # Given a moment to finish, so the stream is properly gone before the
        # assistant exits - but no longer, in case its device has been unplugged.
        if closing is not None:
            closing.join(timeout=1.0)
