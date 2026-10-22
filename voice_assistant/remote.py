"""
Remote Module.

The brain's view of one satellite - see docs/decisions/0006, and satellite.py for the
other end. Everything Nova plays and hears goes through a satellite now, as it went
through the Pi's own speaker and microphone when Nova ran there: RemoteSpeaker offers
what speaker.py's Speaker offered, sending each sound to the satellite to play, and
each wake word arrives with an AudioFeed of what the user says, which
record_until_silence reads as it read the microphone.

One RemoteSatellite is one connection. When the satellite goes, everything waiting on
it ends - every playback is done and every feed ended - so nothing on the brain is left
waiting for a satellite which isn't there.
"""

import itertools
import logging
import os
import queue
import threading
from time import monotonic
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np

from voice_assistant import protocol
from voice_assistant.recording import AudioFeed

logger = logging.getLogger(__name__)

# How long hearing nothing at all from a satellite means it has gone: three of the pings
# it sends every 5 seconds (see satellite_link.py) missed. A satellite which loses its
# power never closes its connection, which would otherwise leave its session waiting.
SILENT_FOR = 15.0

# The most of a stream one message carries, in seconds of it - small, so the satellite
# starts on a reply as soon as its first words are made, and even music in stereo is
# far inside protocol.MAX_AUDIO.
PIECE_SECONDS = 0.1


def _as_16_bit(audio: np.ndarray) -> bytes:
    """float32 samples as the 16-bit little-endian ones the satellite plays, channels interleaved."""
    return np.clip(audio * 32768, -32768, 32767).astype("<i2").tobytes()


def _with_channels(piece: np.ndarray, channels: int) -> np.ndarray:
    """A piece made mono, or stereo with the same on both sides, to match its stream."""
    if channels == 1:
        return piece.mean(axis=1) if piece.ndim > 1 else piece
    return np.column_stack([piece, piece]) if piece.ndim == 1 else piece


def _close(pieces):
    """Closes a stream's source, if it can be closed, so whatever is making it stops."""
    close = getattr(pieces, "close", None)
    if close is not None:
        close()


class RemotePlayback:
    """
    One sound playing on a satellite, as speaker.py's Playback is one playing here:
    something to wait for, pause, or stop part way through.
    """

    def __init__(self, speaker: "RemoteSpeaker", id: str, kind: str, volume: float, paused: bool = False,
                 rate: Optional[int] = None):
        """
        Args:
            speaker: What it's playing on, which sends the satellite its changes.
            id: How the satellite knows it.
            kind: What sort of sound it is, e.g. "reply" or "music".
            volume: How loud, from 0 to 1.
            paused: Whether it starts paused.
            rate: A stream's own sample rate, or None for one of the satellite's sounds.
        """
        self.id = id
        self.kind = kind
        # How far it has got, as the satellite last said, in samples at its speaker's rate.
        self.sample_rate = speaker.sample_rate
        self.position = 0
        self._speaker = speaker
        self._volume = volume
        self._paused = paused
        self._rate = rate
        # How many samples of a stream have been sent, at its own rate.
        self._sent = 0
        self._finished = threading.Event()
        # Notified whenever it moves on or finishes - see _wait_to_send.
        self._changed = threading.Condition()
        # Keeps each change together with the message saying so, so two made at once
        # reach the satellite in the order they were made here.
        self._setting = threading.Lock()

    @property
    def paused(self) -> bool:
        """Whether the brain last paused it."""
        return self._paused

    @paused.setter
    def paused(self, paused: bool):
        with self._setting:
            self._paused = bool(paused)
            # Sent even when it's unchanged here: the satellite pauses the music by
            # itself when it hears the wake word, so carrying on may be news to it.
            if not self.done:
                self._speaker._send("set_playback", id=self.id, paused=self._paused)

    @property
    def volume(self) -> float:
        """How loud, from 0 to 1."""
        return self._volume

    @volume.setter
    def volume(self, volume: float):
        with self._setting:
            self._volume = max(0.0, min(1.0, float(volume)))
            if not self.done:
                self._speaker._send("set_playback", id=self.id, volume=self._volume)

    @property
    def duration(self) -> float:
        """
        How long a stream lasts, in seconds - so far, while more of it is still to be
        sent. 0 for one of the satellite's sounds, whose length only it knows.
        """
        return self._sent / self._rate if self._rate else 0.0

    @property
    def done(self) -> bool:
        """Whether it has finished or been stopped, or the satellite has gone."""
        return self._finished.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Waits until the satellite says it has finished playing, or it's stopped.

        Args:
            timeout: The longest to wait, in seconds. None waits until it's done -
                which the satellite going ends too, so never for good.

        Returns:
            True if it finished (or was stopped), False if the timeout came first.
        """
        return self._finished.wait(timeout)

    def stop(self):
        """Stops it straight away, part way through or not - and nothing else."""
        if self.done:
            return
        # Told first, and only then finished here - see RemoteSpeaker.stop.
        self._speaker._send("stop", id=self.id)
        self._finish()

    def _moved(self, position: int):
        """Notes how far the satellite has got, as it reports while the music plays."""
        with self._changed:
            self.position = int(position)
            self._changed.notify_all()

    def _finish(self, position: Optional[int] = None):
        """
        Marks it as done.

        Args:
            position: How far it got, if the satellite has said.
        """
        with self._changed:
            if position is not None:
                self.position = int(position)
            self._finished.set()
            self._changed.notify_all()

    def _wait_to_send(self, most_ahead: Optional[float]) -> bool:
        """
        Waits while a stream is more than most_ahead seconds ahead of what the
        satellite has played, so it never holds more than that of it.

        Args:
            most_ahead: Seconds of it the satellite may hold, or None for no limit.

        Returns:
            False once it's done, when there's no point sending any more.
        """
        def ready():
            ahead = self._sent / self._rate - self.position / self.sample_rate
            return self.done or not most_ahead or ahead <= most_ahead

        with self._changed:
            self._changed.wait_for(ready)
        return not self.done


class RemoteSpeaker:
    """
    A satellite's speaker, as the brain plays through it - with what speaker.py's
    Speaker offers its callers. The satellite's own sounds are played by name, and
    everything else, speech and music, is streamed to it.
    """

    def __init__(self, send: Callable[..., bool], sample_rate: int):
        """
        Args:
            send: Sends the satellite a message - RemoteSatellite.send.
            sample_rate: What the satellite's speaker plays at.
        """
        self.sample_rate = sample_rate
        self._send = send
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        # What's playing, by id, until the satellite says it has ended, or goes.
        self._playing: Dict[str, RemotePlayback] = {}

    def play(self, audio: np.ndarray, sample_rate: Optional[int] = None, volume: float = 1.0,
             kind: str = "reply") -> RemotePlayback:
        """
        Starts playing a sound, without waiting for it.

        Args:
            audio: float samples, mono or with channels as columns - played in mono,
                as Speaker.play does.
            sample_rate: The audio's rate, if it isn't the satellite's.
            volume: How loud, from 0 (silent) to 1 (as recorded).
            kind: What sort of sound it is, e.g. "reply" or "announcement".

        Returns:
            The RemotePlayback, to wait for or stop. Already finished if the satellite
            isn't connected, as Speaker's is with no speaker to play it on.
        """
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return self.play_stream([audio], sample_rate, volume, kind)

    def play_stream(self, pieces: Iterable[np.ndarray], sample_rate: Optional[int] = None, volume: float = 1.0,
                    kind: str = "reply", paused: bool = False,
                    most_ahead: Optional[float] = None) -> RemotePlayback:
        """
        Starts playing a sound which is still being made, sending each piece to the
        satellite as it arrives, on a thread of its own.

        Args:
            pieces: float32 pieces of the sound, in order - mono, or a column per
                channel. Closed, if they have a close(), once the sound is stopped or
                the satellite goes, so a generator can stop whatever is making them.
            sample_rate: Their rate, if it isn't the satellite's. The satellite
                resamples them.
            volume: How loud, from 0 (silent) to 1 (as recorded).
            kind: What sort of sound it is, e.g. "reply" or "music".
            paused: Start it paused, e.g. a song asked for while Nova is talking.
            most_ahead: For music, the most seconds of it to send ahead of what the
                satellite has played, so it doesn't hold a whole song at once. None
                sends it all as fast as it comes, as for speech.

        Returns:
            The RemotePlayback, to wait for or stop. Already finished if the satellite
            isn't connected, with its pieces closed, as Speaker's is with no speaker.
        """
        rate = sample_rate or self.sample_rate
        # Music is the one thing Nova plays in stereo - navidrome.py decodes it so - and
        # everything else is mono. Deciding by the kind, rather than waiting for the
        # first piece to say, lets the satellite hear of the stream at once, so a wake
        # word heard before the first words of a reply are made still stops it.
        channels = 2 if kind == "music" else 1
        volume = max(0.0, min(1.0, float(volume)))
        playback = RemotePlayback(self, str(next(self._ids)), kind, volume, bool(paused), rate)
        if not self._start(playback, "play_stream", rate=rate, channels=channels, volume=volume, kind=kind,
                           paused=bool(paused)):
            _close(pieces)
            return playback
        # The satellite only says how far it has got with music, so only music can be held back.
        most_ahead = most_ahead if kind == "music" else None
        threading.Thread(target=self._feed, args=(playback, pieces, channels, most_ahead),
                         daemon=True, name="remote-feed").start()
        return playback

    def play_file(self, filename: str, volume: float = 1.0, kind: str = "reply", cache: bool = True,
                  start: float = 0.0, length: Optional[float] = None) -> RemotePlayback:
        """
        Has the satellite play one of its own sounds, or part of one, without waiting for it.

        Args:
            filename: The sound - only its name is sent, and the satellite plays its
                own file of that name.
            volume: How loud, from 0 (silent) to 1 (as recorded).
            kind: What sort of sound it is, e.g. "reply" or "announcement".
            cache: Unused - the satellite keeps its own sounds once loaded.
            start: Seconds into the sound to start from, as if it were looping.
            length: Seconds to play, or None for the rest of it.

        Returns:
            The RemotePlayback, to wait for or stop. Already finished if the satellite
            isn't connected, as Speaker's is with no speaker. Never None, as Speaker's
            is for a missing file: the files are the satellite's, and one it can't
            play is reported finished at once.
        """
        volume = max(0.0, min(1.0, float(volume)))
        playback = RemotePlayback(self, str(next(self._ids)), kind, volume)
        self._start(playback, "play_sound", name=os.path.basename(filename), volume=volume, kind=kind,
                    start=start, length=length)
        return playback

    def stop(self, kinds: Optional[Iterable[str]] = None):
        """
        Stops whatever is playing on the satellite straight away.

        Args:
            kinds: Only stop these kinds of sound. None stops everything.
        """
        kinds = None if kinds is None else list(kinds)
        # Told first, and only then finished here: whatever starts once these are done -
        # the next song, say - reaches the satellite after the stop, so isn't stopped by it.
        self._send("stop", kinds=kinds)
        self._finish(lambda playback: kinds is None or playback.kind in kinds)

    def silence(self, kind: str):
        """
        Stops every sound of one kind, and has the satellite refuse any more until
        allow() is called - see Speaker.silence.

        Args:
            kind: The kind of sound, e.g. "announcement".
        """
        self._send("silence", kind=kind)
        self._finish(lambda playback: playback.kind == kind)

    def allow(self, kind: str):
        """
        Lets a kind of sound silenced with silence() play again.

        Args:
            kind: The kind of sound, e.g. "announcement".
        """
        self._send("allow", kind=kind)

    def close(self):
        """Stops everything playing. The connection itself is the RemoteSatellite's to close."""
        self.stop()

    def _start(self, playback: RemotePlayback, message: str, **fields) -> bool:
        """
        Tells the satellite of a new sound.

        Args:
            playback: The sound.
            message: "play_stream" or "play_sound".
            fields: What the message carries besides the sound's id.

        Returns:
            Whether the satellite was told. If it wasn't, the sound is finished
            already, so nothing waits on it.
        """
        # Kept by its id first, so the satellite's word that it has ended can't arrive
        # before there's anything to end.
        with self._lock:
            self._playing[playback.id] = playback
        if self._send(message, id=playback.id, **fields):
            return True
        with self._lock:
            self._playing.pop(playback.id, None)
        playback._finish()
        return False

    def _feed(self, playback: RemotePlayback, pieces: Iterable[np.ndarray], channels: int,
              most_ahead: Optional[float]):
        """Sends a stream's pieces to the satellite as they arrive - see play_stream."""
        step = max(1, round(playback._rate * PIECE_SECONDS))
        try:
            for piece in pieces:
                piece = _with_channels(np.asarray(piece, dtype=np.float32), channels)
                for begin in range(0, len(piece), step):
                    part = piece[begin:begin + step]
                    if not playback._wait_to_send(most_ahead):
                        return
                    if not self._send("stream_audio", _as_16_bit(part), id=playback.id):
                        return
                    playback._sent += len(part)
        except Exception as e:
            logger.error(f"A streamed {playback.kind} sound failed part way: {e}")
        finally:
            _close(pieces)
        # What was sent plays to the end, and the satellite then says it has.
        if not playback.done:
            self._send("stream_end", id=playback.id)

    def _finish(self, which: Callable[[RemotePlayback], bool]):
        """Marks the playbacks the satellite has been told to stop as done, without waiting for it to say."""
        with self._lock:
            stopped = [playback for playback in self._playing.values() if which(playback)]
        for playback in stopped:
            playback._finish()

    def _played(self, id: str, position: Optional[int]):
        """The satellite has finished or stopped a sound."""
        with self._lock:
            playback = self._playing.pop(id, None)
        if playback is not None:
            playback._finish(position)

    def _progress(self, id: str, position: int):
        """The satellite says how far the music has got."""
        with self._lock:
            playback = self._playing.get(id)
        if playback is not None:
            playback._moved(position)

    def _gone(self):
        """The satellite has gone, so nothing it was playing will ever be said to end."""
        with self._lock:
            playing: List[RemotePlayback] = list(self._playing.values())
            self._playing = {}
        for playback in playing:
            playback._finish()


class RemoteSatellite:
    """
    The brain's view of one satellite, for as long as it stays connected: its speaker,
    the wake words it hears and what's said after them, and what it's told to keep.
    """

    def __init__(self, stream, hello: Dict):
        """
        Args:
            stream: The connection, once protocol.admit has let the satellite in - as
                satellite_link's _Stream makes of a socket with a timeout. Its reads
                wake at each timeout, for serve() to notice a satellite gone quiet, and
                no send blocks for longer.
            hello: What admit returned - the satellite's name, hostname and sample_rate.
        """
        self.name = hello["name"]
        self.hostname = hello["hostname"]
        self.sample_rate = hello["sample_rate"]
        self.speaker = RemoteSpeaker(self.send, self.sample_rate)
        # Each wake word heard, in order: {"talked_over": whether a reply was playing,
        # "feed": an AudioFeed of what the user says next}.
        self.wakes: "queue.Queue[Dict]" = queue.Queue()
        # What rang and what was dismissed on the satellite while it couldn't reach
        # the brain, as {"rang": [ids], "dismissed": [ids]} - which it says as it connects.
        self.away_reports: "queue.Queue[Dict]" = queue.Queue()
        self._stream = stream
        # Guards writing to the stream, so messages sent from several threads at once
        # can't be interleaved.
        self._lock = threading.Lock()
        self._connected = True
        # Where the recording the satellite is sending goes, while there is one.
        self._feed_lock = threading.Lock()
        self._feed: Optional[AudioFeed] = None
        self._last_heard = monotonic()
        self._handlers: Dict[str, Callable[[Dict, bytes], None]] = {
            "ping": lambda message, payload: self.send("pong"),
            "wake": self._woken,
            "audio": self._audio,
            "audio_end": lambda message, payload: self._end_feed(),
            "played": lambda message, payload: self.speaker._played(message["id"], message.get("position")),
            "progress": lambda message, payload: self.speaker._progress(message["id"], message["position"]),
            "away": self._away,
        }

    @property
    def connected(self) -> bool:
        """Whether the satellite is still there, and hasn't been let go."""
        return self._connected

    def serve(self):
        """
        Reads what the satellite sends, and acts on it, until it goes - or nothing at
        all has been heard from it for SILENT_FOR seconds, or close() is called. Then
        every feed is ended and every playback done.
        """
        self._last_heard = monotonic()
        self._stream.when_idle = self._check_in
        self._stream.when_heard = self._heard
        try:
            while True:
                # Every pass, as well as when the satellite is quiet: while it sends a
                # recording, reads never time out, but close() still has to be noticed.
                self._check_in()
                message, payload = protocol.receive(self._stream)
                self._handle(message, payload)
        except (OSError, protocol.ProtocolError) as e:
            if self._connected:
                logger.info(f"Lost the {self.name} satellite ({e})")
        finally:
            self._lost()

    def send(self, kind: str, payload: bytes = b"", /, **fields) -> bool:
        """
        Sends the satellite a message. Safe from several threads at once, and never
        blocks for longer than the stream's timeout.

        Args:
            kind: What it is, e.g. "play_stream".
            payload: Audio to go with it - at most protocol.MAX_AUDIO bytes, the most
                the satellite will take.
            fields: Anything else it carries - even a "kind", as the arguments above
                are positional only.

        Returns:
            False if it wasn't sent: the satellite isn't connected, the connection
            failed just now, or the audio was too long.
        """
        if len(payload) > protocol.MAX_AUDIO:
            # The satellite would drop the connection over it - and be sent it again
            # as soon as it was back.
            logger.error(f"Can't send the {self.name} satellite '{kind}' with {len(payload)} bytes of audio, "
                         f"more than the {protocol.MAX_AUDIO} it takes")
            return False
        with self._lock:
            if not self._connected:
                return False
            try:
                protocol.send(self._stream, kind, payload, **fields)
                return True
            except OSError as e:
                logger.warning(f"Couldn't send '{kind}' to the {self.name} satellite ({e})")
                # Set before anything else can be sent after what may be half a message.
                self._connected = False
        self._lost()
        return False

    def listen(self) -> AudioFeed:
        """
        Has the satellite send what the user says from now - their answer to Nova's
        question - until stop_listening(), or its 10 second limit.

        Returns:
            A feed of it. Already ended if the satellite isn't connected.
        """
        # Before the satellite is asked, so the first of it has somewhere to go.
        feed = self._new_feed()
        self.send("listen")
        return feed

    def stop_listening(self):
        """
        Has the satellite stop sending what the user says, once the brain has heard
        them stop, and ends its feed. Any of it already on its way is dropped.
        """
        self._end_feed()
        self.send("stop_listening")

    def set_state(self, volumes: Dict[str, float], muted: bool) -> bool:
        """
        Sends the satellite its volumes and its mute, which the brain owns - for it to
        mute its wake word by, and ring its alarms by while it can't reach the brain.

        Args:
            volumes: Each category's volume from 0 to 1, by name - "general",
                "alarm" and "music".
            muted: Whether it's muted.

        Returns:
            Whether it was sent.
        """
        return self.send("state", volumes=volumes, muted=muted)

    def send_alarms(self, entries: List[Dict]) -> bool:
        """
        Sends the satellite its copy of the timers and alarms which ring on it, to ring
        from while it can't reach the brain.

        Args:
            entries: As clock.copy_for gives them.

        Returns:
            Whether it was sent.
        """
        return self.send("alarms", entries=entries)

    def send_phrase(self, audio: bytes, rate: int) -> bool:
        """
        Sends the satellite the recorded "I can't reach the server", for it to say when
        it can't.

        Args:
            audio: 16-bit little-endian mono samples - at most protocol.MAX_AUDIO bytes.
            rate: Their sample rate.

        Returns:
            Whether it was sent.
        """
        return self.send("phrase", audio, rate=rate)

    def restart(self) -> bool:
        """
        Has the satellite restart - Home Assistant's Restart button.

        Returns:
            Whether it was asked.
        """
        return self.send("restart")

    def close(self):
        """
        Lets the satellite go - when it has connected again, say, and this is the old
        connection. Everything waiting on it ends now, and serve() returns as soon as
        its stream next wakes. The socket is the caller's to close once it has.
        """
        self._lost()

    def _check_in(self):
        """Run between messages, and whenever the satellite is quiet for a moment: ends serve() if it's time."""
        if not self._connected:
            raise ConnectionError("Let go")
        if monotonic() - self._last_heard >= SILENT_FOR:
            # Not a ConnectionError, which the stream takes for the socket closing, and
            # says so instead.
            raise TimeoutError(f"Nothing heard from it for {SILENT_FOR:.0f} seconds")

    def _heard(self):
        self._last_heard = monotonic()

    def _handle(self, message: Dict, payload: bytes):
        """Acts on one message from the satellite."""
        handler = self._handlers.get(message["type"])
        if handler is None:
            logger.warning(f"The {self.name} satellite sent '{message['type']}', which the brain doesn't know")
            return
        # One bad message mustn't lose the satellite.
        try:
            handler(message, payload)
        except Exception:
            logger.exception(f"Handling '{message['type']}' from the {self.name} satellite failed")

    def _woken(self, message: Dict, payload: bytes):
        self.wakes.put({"talked_over": bool(message.get("talked_over")), "feed": self._new_feed()})

    def _audio(self, message: Dict, payload: bytes):
        with self._feed_lock:
            feed = self._feed
        # None once the brain has stopped listening, while the last of it is still arriving.
        if feed is not None:
            feed.feed(np.frombuffer(payload, "<i2"))

    def _away(self, message: Dict, payload: bytes):
        self.away_reports.put({"rang": list(message.get("rang") or []),
                               "dismissed": list(message.get("dismissed") or [])})

    def _new_feed(self) -> AudioFeed:
        """Starts a feed for the recording the satellite is about to send, ending any before it."""
        feed = AudioFeed()
        with self._feed_lock:
            # Checked under the lock, which _lost takes once it's no longer connected,
            # so a feed is never left waiting on a satellite which has gone.
            if not self._connected:
                feed.end()
                return feed
            old, self._feed = self._feed, feed
        if old is not None:
            old.end()
        return feed

    def _end_feed(self):
        """Ends the feed of the recording being sent, if there is one."""
        with self._feed_lock:
            feed, self._feed = self._feed, None
        if feed is not None:
            feed.end()

    def _lost(self):
        """Ends everything waiting on the satellite, now it has gone. Safe to call more than once."""
        self._connected = False
        self._end_feed()
        self.speaker._gone()
