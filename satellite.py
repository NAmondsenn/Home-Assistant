"""
Satellite.

What runs on each Pi now that Nova's brain is on PVE2 - see docs/decisions/0006. The
satellite hears the wake word, sends the brain what the user says, and plays, pauses
and stops whatever the brain sends - speech, chimes and music - as the one owner of
its speaker. Everything from the words to the answer is worked out on the brain.

Stopping stays here, so it stays instant: the moment the wake word is heard, a reply
stops, what rings goes quiet and the music pauses, before the brain has even been
told. While the brain can't be reached, the satellite rings the alarms from its copy,
and answers the wake word with the recorded "I can't reach the server".
"""

import itertools
import logging
import os
import queue
import signal
import socket
import threading
import time
from typing import Callable, Dict, Iterator, List, Optional

import numpy as np

from voice_assistant import paths, storage, watchdog
from voice_assistant.config import Config, ConfigError
from voice_assistant.echo import make_echo_canceller
from voice_assistant.fallback import AlarmCopy, FallbackRinger, Phrase, StateCopy
from voice_assistant.logging_setup import setup_logging
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.satellite_link import SILENT_FOR, SatelliteLink
from voice_assistant.speaker import Playback, Speaker
from voice_assistant.timeline import Timeline
from voice_assistant.wake_word import WakeWordDetector

logger = logging.getLogger(__name__)

# Left while the satellite runs, and taken away when it stops properly - so finding it
# when starting means the last run didn't, which the brain is told as it connects. The
# same mark main.py left.
RUNNING_MARK = os.path.join(paths.DATA_DIR, "running")

# The pieces a recording is sent in, in seconds of it - small, so the brain hears the
# user as they speak, and can tell the moment they've stopped.
PIECE_SECONDS = 0.1

# The most of one recording which is sent, in seconds, whatever the brain says - so a
# brain which never says the user has stopped can't keep the microphone open.
LIMIT_SECONDS = 10

# How often the alarm copy is checked for what's due while the brain is away, and how
# often the brain is told how far the music has got, in seconds.
FALLBACK_CHECK = 1.0
PROGRESS_EVERY = 1.0

# How long the brain has to say it has what the satellite told it on connecting
# (away_settled), in seconds, before the satellite gives up on the connection as it
# would on a silent brain. Until then the satellite rings on from its copy, though
# connected, so the wake word only dismisses what rings - and the brain only hears of
# that once the satellite next connects.
#
# A healthy brain says so within milliseconds of starting the satellite's session.
# Even with every wait in its catch-up run to its limit, it's 12 seconds: brain.AWAY_WAIT,
# 2 seconds, for the report, and just under satellite_link.SOCKET_TIMEOUT, 2 seconds,
# for each message around it - the state, the copy and the phrase, the copy again
# once apply_away has changed it, and away_settled itself. Starting the session comes
# first, and isn't in that, so the brain logs how long it all took from the
# satellite's hello. A brain which hasn't said so by then is stuck part way through -
# while still answering pings, so SILENT_FOR never notices. (One whose AWAY_WAIT ran
# out hangs up by itself.) So it's SILENT_FOR: clear of the 12, and no longer than the
# satellite already waits on a brain which says nothing at all.
SETTLE_WAIT = SILENT_FOR


class _FromBrain:
    """One of the brain's playbacks, which it knows by its id."""

    def __init__(self, playback: Playback, kind: str, channels: int = 1,
                 pieces: Optional[queue.Queue] = None):
        """
        Args:
            playback: Where it's playing.
            kind: What sort of sound it is, e.g. "reply" or "music".
            channels: How many channels a stream's audio has.
            pieces: Where a stream's audio waits for the speaker, or None for a sound.
        """
        self.playback = playback
        self.kind = kind
        self.channels = channels
        self.pieces = pieces

    def let_go(self):
        """Ends a stream's pieces, which would otherwise wait for more for good."""
        if self.pieces is not None:
            self.pieces.put(None)


def _pieces(waiting: queue.Queue) -> Iterator[np.ndarray]:
    """A stream's audio as it arrives from the brain, until None says that's all."""
    while True:
        piece = waiting.get()
        if piece is None:
            return
        yield piece


class Satellite:
    """One Pi's microphone and speaker, for a brain elsewhere."""

    def __init__(self, config: Config):
        """
        Args:
            config: The settings.

        Raises:
            ConfigError: There's no brain to connect to, as backend.address or the
                password, NOVA_BACKEND_TOKEN, isn't set.
        """
        audio_config = config.section("audio")
        backend_config = config.section("backend")
        self.address = backend_config.get("address", "")
        self.password = os.getenv("NOVA_BACKEND_TOKEN", "")
        if not self.address or not self.password:
            raise ConfigError("A satellite needs its brain's address, as backend.address in config.yaml, "
                              "and its password, as NOVA_BACKEND_TOKEN in ~/.config/nova/.env")
        self.hostname = socket.gethostname()
        self.name = backend_config.get("satellite") or self.hostname
        self.sample_rate = audio_config.get("sample_rate", 48000)
        # Seconds of audio from just before the wake word was detected which are sent
        # first, so "Hey Nova what time is it", said in one breath, isn't cut short. The
        # brain is told how much with each wake, so this is the only setting for it.
        self.pre_roll = audio_config.get("pre_roll", 0.3)
        self.sounds_folder = paths.SOUNDS_DIR
        self.latency_file = os.path.join(paths.LOGS_DIR, "latency.jsonl")

        # The one speaker and the one microphone, as main.py had them, with what's
        # played taken back out of what's heard - see echo.py.
        self.echo = make_echo_canceller(config, self.sample_rate)
        self.speaker = Speaker(sample_rate=self.sample_rate, echo_reference=self.echo.played if self.echo else None)
        self.microphone = Microphone(sample_rate=self.sample_rate, echo=self.echo)
        self.wake = WakeWordDetector(config=config, microphone=self.microphone)

        # What the brain last sent, for while it can't be reached - see fallback.py.
        self.alarms = AlarmCopy()
        self.state = StateCopy()
        self.phrase = Phrase()
        self.ringer = FallbackRinger(self.speaker, self.sounds_folder, self.alarms, self.state)
        # The brain owns the mute, but a satellite which was muted when the brain went
        # down stays muted until it's back to say otherwise.
        self.wake.set_muted(self.state.muted)

        # Made in run(), once it's known whether the last run stopped properly.
        self.link: Optional[SatelliteLink] = None
        # Guards what the link's thread and the main loop share, below. Also held while
        # the brain is told of a wake, so it hears of that before the end of a reply
        # the wake word stopped - see _report.
        self._lock = threading.Lock()
        self._playing: Dict[str, _FromBrain] = {}
        # Where the follow-up the brain asked to hear starts, until the main loop sends it.
        self._listen_from: Optional[int] = None
        # Set by the brain's stop_listening - one for each recording, while it's sent.
        self._recording: Optional[threading.Event] = None
        # Whether the brain has had all of a recording and not yet answered it - from
        # the recording's end until its allow, at the end of the turn. Losing the brain
        # then says the phrase, as losing it part way through a recording does.
        self._turn_open = False
        # The last wake's timing, until its reply starts to play.
        self._timeline: Optional[Timeline] = None
        # The report of what happened while the brain was away, and what was still
        # ringing, as they were sent on connecting - until the brain says it has them,
        # or is lost. See _on_connected.
        self._handing_over: Optional[Dict[str, List[str]]] = None

        # What to exit with, once the main loop has been asked to stop - see stop().
        self._exit_code: Optional[int] = None
        # Set once shutting down, so a second signal can't cut the cleanup short.
        self._stopping = False
        self._finished = threading.Event()

        self._handlers: Dict[str, Callable[[Dict, bytes], None]] = {
            "listen": self._listen,
            "stop_listening": self._stop_listening,
            "play_sound": self._play_sound,
            "play_stream": self._play_stream,
            "stream_audio": self._stream_audio,
            "stream_end": self._stream_end,
            "set_playback": self._set_playback,
            "stop": self._stop,
            "silence": lambda message, payload: self.speaker.silence(message["kind"]),
            "allow": self._allow,
            "state": self._take_state,
            "alarms": lambda message, payload: self.alarms.replace(message["entries"]),
            "phrase": lambda message, payload: self.phrase.replace(payload, message["rate"]),
            "away_settled": self._away_settled,
            "restart": self._restart,
        }

    def run(self):
        """
        Connects to the brain, and listens for the wake word, until stopped.

        Raises:
            SystemExit: Once stopped - with 1 when the brain asked for a restart.
        """
        # Tells systemd it has started, and from here on that it's still working.
        watchdog.start()
        # However the loop ends, everything is closed down here, once.
        try:
            # Before the brain is reached, so it's told what still rings, and takes it over.
            self.ringer.resume(time.time())
            self.link = SatelliteLink(self.address, self.name, self.hostname, self.password, self.sample_rate,
                                      self._note_start(), self._on_message, self._on_connected)
            self.link.start()
            threading.Thread(target=self._ring_when_due, daemon=True, name="fallback").start()
            logger.info(f"Satellite '{self.name}' ready, for its brain at {self.address}")
            self._listen_forever()
        finally:
            self._clean_up()

    def stop(self, code: int = 0):
        """
        Has the main loop stop, from any thread, once it's next between pieces of audio.

        Args:
            code: What the program exits with. 1 has systemd start it again - see
                voice-assistant.service.
        """
        self._exit_code = code

    def shutdown(self, *args):
        """
        Ctrl+C or systemd's stop, as main.py's _shutdown: raises SystemExit in whatever
        the main loop was doing, and run() cleans up once that has unwound. A second
        signal while it's shutting down is ignored, so it can't cut the cleanup short.
        """
        if self._stopping:
            return
        self._stopping = True
        logger.info("Shutting down...")
        raise SystemExit(0)

    def _interrupted(self) -> bool:
        """Whether listening for the wake word has to stop - for a follow-up, or to stop altogether."""
        return self._listen_from is not None or self._exit_code is not None

    def _exit_if_asked(self):
        if self._exit_code is not None:
            raise SystemExit(self._exit_code)

    def _listen_forever(self):
        """
        Listens for the wake word, and sends each follow-up the brain asks to hear. The
        one loop owns the microphone, so while a recording is being sent, the wake word
        can't start another.
        """
        while True:
            try:
                # First, as well as once listening returns, since listening which keeps
                # going wrong never returns - which is when the brain's Restart, from
                # Home Assistant, is needed most.
                self._exit_if_asked()
                heard = self.wake.listen_once(until=self._interrupted)
                self._exit_if_asked()
                if heard is not None:
                    self._woken()
                else:
                    self._send_follow_up()
            # One thing going wrong mustn't stop the satellite for good - but it waits a
            # moment, in case it would only go wrong again straight away.
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                time.sleep(1)

    def _woken(self):
        """
        Acts on the wake word at once - stopping, pausing and dismissing, as Nova always
        has - then tells the brain, and sends it what the user says.
        """
        heard_at = self.wake.heard_at
        # The recording starts a moment before the wake word was heard - see pre_roll.
        start = max(0, heard_at - round(self.pre_roll * RATE))
        recording = None
        with self._lock:
            talked_over = any(playing.kind == "reply" and not playing.playback.done
                              for playing in self._playing.values())
            self._start_timeline()
            self.speaker.stop(["reply"])
            self.speaker.silence("announcement")
            for playing in self._playing.values():
                if playing.kind == "music":
                    playing.playback.paused = True
            dismissing = self.ringer.ringing
            if dismissing:
                # While the brain is away, the wake word dismisses what rings, and that's all.
                self.ringer.dismiss()
                self._save_timeline(outcome="dismissed")
            else:
                # Made before the brain hears of the wake, which it could answer with
                # stop_listening straight away.
                recording = self._recording = threading.Event()
                self._turn_open = False
                # Saying how much of the recording is from before the wake word, so the
                # brain knows without a setting of its own, which could differ.
                if not self.link.send("wake", talked_over=talked_over, pre_roll=heard_at - start):
                    recording = self._recording = None
        if dismissing:
            self.speaker.allow("announcement")
            return
        ended = "dropped" if recording is None else self._send_recording(start, heard_at - start, recording)
        with self._lock:
            # The brain asked to hear an answer while this was being sent, having
            # finished a question just as the wake word was said - and this recording,
            # which it has had, is what came next. Sent again as a follow-up, it was
            # ten seconds of nobody talking, with the wake word going unheard.
            self._listen_from = None
            if ended == "dropped":
                self._save_timeline(outcome="no_brain")
        if ended == "dropped":
            self._say_cant_reach()

    def _send_follow_up(self):
        """Sends the brain the user's answer to its question, from the moment it asked to hear it."""
        with self._lock:
            start, self._listen_from = self._listen_from, None
            if start is None:
                return
            recording = self._recording = threading.Event()
            self._turn_open = False
        if self._send_recording(start, 0, recording) == "dropped":
            self._say_cant_reach()

    def _send_recording(self, start: int, pre_roll: int, stopped: threading.Event) -> str:
        """
        Sends the brain what the microphone hears, until it says the user has stopped.

        Args:
            start: The microphone position to send from.
            pre_roll: How many samples from there were heard before the wake word,
                which go first, as a piece of their own - 0 for a follow-up.
            stopped: Set once the brain says the user has stopped.

        Returns:
            Why it ended: "stopped" by the brain, at the "limit", or "dropped" with the
            connection.
        """
        reader = self.microphone.listen(start=start)
        limit = LIMIT_SECONDS * RATE
        sizes = itertools.chain([pre_roll] if pre_roll else [], itertools.repeat(round(PIECE_SECONDS * RATE)))
        sent = 0
        try:
            for size in sizes:
                audio = reader.read(min(size, limit - sent))
                self._exit_if_asked()
                if stopped.is_set():
                    return "stopped"
                if not self.link.send("audio", audio.tobytes()):
                    logger.warning("Lost the brain part way through a recording")
                    return "dropped"
                sent += len(audio)
                if sent >= limit:
                    with self._lock:
                        self._turn_open = True
                    self.link.send("audio_end", reason="limit")
                    return "limit"
        except BaseException:
            # Given up on part way, as the satellite stops, so the brain isn't left
            # waiting for the rest.
            self.link.send("audio_end", reason="cancelled")
            raise
        finally:
            with self._lock:
                if self._recording is stopped:
                    self._recording = None

    def _say_cant_reach(self):
        """
        Says the brain can't be reached, in the brain's own recorded voice, and lets
        what rings by itself ring again - which the brain would have, had it answered.
        """
        self.phrase.play(self.speaker, self.state.volume("general"))
        self.speaker.allow("announcement")

    def _start_timeline(self):
        """Times a wake from now. Called with the lock held."""
        # One still waiting for its reply never got one - nothing was heard, most likely.
        self._save_timeline()
        self._timeline = Timeline()
        self._timeline.mark("wake")

    def _save_timeline(self, **notes):
        """
        Saves the last wake's timing, if it hasn't been already. Called with the lock held.

        Args:
            notes: Anything which explains the times, e.g. outcome="dismissed".
        """
        if self._timeline is not None:
            self._timeline.note(**notes)
            self._timeline.save(self.latency_file)
            self._timeline = None

    def _on_message(self, message: Dict, payload: bytes):
        """Does what the brain asks. Called on the link's own thread."""
        handler = self._handlers.get(message["type"])
        if handler is None:
            logger.warning(f"The brain sent '{message['type']}', which this satellite doesn't know")
            return
        handler(message, payload)

    def _listen(self, message: Dict, payload: bytes):
        # From the moment it's asked for - before the brain's listening sound - so an
        # answer started over the sound isn't lost.
        with self._lock:
            self._listen_from = self.microphone.position

    def _stop_listening(self, message: Dict, payload: bytes):
        # Only ever the recording being sent now. One which has already ended by itself,
        # at the limit, is over whatever the brain says.
        with self._lock:
            if self._recording is not None:
                self._recording.set()
                # Here rather than once the main loop sees it's stopped, which can be
                # after the brain's allow has arrived, when it has nothing to say.
                self._turn_open = True

    def _allow(self, message: Dict, payload: bytes):
        # Sent as the brain's turn ends, whatever came of it.
        with self._lock:
            self._turn_open = False
        self.speaker.allow(message["kind"])

    def _play_sound(self, message: Dict, payload: bytes):
        kind = message.get("kind", "reply")
        # Only ever one of the satellite's own sounds, whatever the name says.
        filename = os.path.join(self.sounds_folder, os.path.basename(message["name"]))
        playback = self.speaker.play_file(filename, volume=message.get("volume", 1.0), kind=kind,
                                          start=message.get("start") or 0.0, length=message.get("length"))
        if playback is None:
            # It can't be played, so the brain mustn't wait for it to finish.
            self.link.send("played", id=message["id"], stopped=False, position=0)
            return
        self._follow(message["id"], _FromBrain(playback, kind))

    def _play_stream(self, message: Dict, payload: bytes):
        kind = message.get("kind", "reply")
        waiting = queue.Queue()
        playback = self.speaker.play_stream(_pieces(waiting), sample_rate=message.get("rate"),
                                            volume=message.get("volume", 1.0), kind=kind,
                                            paused=bool(message.get("paused", False)))
        self._follow(message["id"], _FromBrain(playback, kind, message.get("channels", 1), waiting))

    def _stream_audio(self, message: Dict, payload: bytes):
        with self._lock:
            playing = self._playing.get(message.get("id"))
            # One the satellite has stopped, which the brain hasn't heard about yet.
            if playing is None or playing.pieces is None or playing.playback.done:
                return
            # Nova's spoken reply is streamed, where its chimes are sounds, so the first
            # of a "reply" stream is when the user starts to hear the answer.
            if playing.kind == "reply" and payload and self._timeline is not None:
                self._timeline.mark("reply_audio")
                self._save_timeline()
        audio = np.frombuffer(payload, np.int16).astype(np.float32) / 32768
        if playing.channels > 1:
            audio = audio.reshape(-1, playing.channels)
        playing.pieces.put(audio)

    def _stream_end(self, message: Dict, payload: bytes):
        with self._lock:
            playing = self._playing.get(message.get("id"))
        if playing is not None:
            playing.let_go()

    def _set_playback(self, message: Dict, payload: bytes):
        with self._lock:
            playing = self._playing.get(message.get("id"))
        # The satellite owns what plays, so one which has already ended stays ended.
        if playing is None:
            return
        if "paused" in message:
            playing.playback.paused = bool(message["paused"])
        if "volume" in message:
            playing.playback.volume = max(0.0, min(1.0, float(message["volume"])))

    def _stop(self, message: Dict, payload: bytes):
        # One of the brain's playbacks by its id - a volume beep, say, without the reply
        # playing alongside it - or whole kinds of sound.
        if "id" not in message:
            self.speaker.stop(message.get("kinds"))
            return
        with self._lock:
            playing = self._playing.get(message["id"])
        # One which has already ended has been reported, or is about to be.
        if playing is not None:
            playing.playback.stop()

    def _take_state(self, message: Dict, payload: bytes):
        self.state.update(message)
        self.wake.set_muted(self.state.muted)

    def _away_settled(self, message: Dict, payload: bytes):
        # The brain has saved what it was told as the satellite connected, and rings on
        # whatever was still ringing here - so the satellite forgets it, and stops. What
        # it stops may ring from the copy again, if the brain is lost while ringing it.
        with self._lock:
            sent, self._handing_over = self._handing_over, None
            if sent is None:
                return
            self.alarms.clear_report(sent)
            self.ringer.quiet()
            self.alarms.taken_over(sent["ringing"])

    def _restart(self, message: Dict, payload: bytes):
        logger.info("Restarting, as the brain asked")
        self.stop(1)

    def _follow(self, id: str, playing: _FromBrain):
        """Keeps one of the brain's playbacks by its id, and reports on it until it ends."""
        with self._lock:
            self._playing[id] = playing
        threading.Thread(target=self._report, args=(id, playing), daemon=True, name=f"played-{id}").start()

    def _report(self, id: str, playing: _FromBrain):
        """Tells the brain how far its music has got each second, and when any of its playbacks ends."""
        playback = playing.playback
        while not playback.wait(PROGRESS_EVERY):
            if playing.kind == "music":
                self.link.send("progress", id=id, position=playback.position)
        playing.let_go()
        # Under the lock, so a reply the wake word stopped is reported after the wake.
        with self._lock:
            if self._playing.get(id) is playing:
                del self._playing[id]
            self.link.send("played", id=id, stopped=playback.stopped, position=playback.position)

    def _on_connected(self, connected: bool):
        """Called on the link's own thread each time the brain is reached, or lost."""
        if not connected:
            self._forget_the_brain()
            return
        with self._lock:
            # The brain takes over whatever is still ringing, which it hears is - having
            # been off as it came due, it wouldn't know to ring it otherwise. It's only
            # quietened, and the report forgotten, once the brain says it has saved
            # them (see _away_settled): a brain lost before then may never have, and the
            # next would never hear of them, so an alarm would stop early. The brain
            # starts ringing it before it says so, so for that moment both ring it.
            report = {**self.alarms.away_report(), "ringing": self.ringer.ringing_ids()}
            if self.link.send("away", **report):
                self._handing_over = report
                self.link.expect("away_settled", SETTLE_WAIT)

    def _forget_the_brain(self):
        """
        Stops what the brain was playing, which nothing will now come to finish or
        stop - and says the brain can't be reached, if it went with the user waiting
        for its answer.
        """
        with self._lock:
            playing, self._playing = list(self._playing.values()), {}
            # Nobody is left to hear a follow-up.
            self._listen_from = None
            # Nor to say it has what it was told on connecting, which it may never have
            # saved - so what rings from the copy rings on, and the report is kept, for
            # the next brain.
            self._handing_over = None
            waiting, self._turn_open = self._turn_open and not self._stopping, False
            if waiting:
                self._save_timeline(outcome="no_brain")
        for each in playing:
            each.playback.stop()
            each.let_go()
        if waiting:
            # After the brain's own sounds have stopped, so it isn't stopped with them.
            # It lets the alarms ring again too.
            self._say_cant_reach()
            return
        # The brain may have silenced the alarms while it talked to the user, and can't
        # let them ring again now - when the satellite's own copy may need to.
        self.speaker.allow("announcement")

    def _ring_when_due(self):
        """Rings what falls due in the alarm copy, for as long as the brain can't be reached to ring it."""
        while not self._finished.wait(FALLBACK_CHECK):
            try:
                # Under the lock, so nothing starts ringing just as the brain is reached.
                with self._lock:
                    if not self.link.connected:
                        self.ringer.check(time.time())
            except Exception as e:
                logger.error(f"Couldn't check the alarm copy: {e}")

    def _note_start(self) -> bool:
        """
        Leaves RUNNING_MARK, which _clean_up takes away, as main.py's _note_start did.

        Returns:
            Whether the last run left one too, so never stopped properly - which the
            brain is told, and tells the user's phone.
        """
        try:
            unclean = storage.load_json(RUNNING_MARK) is not None
        except (OSError, ValueError):
            # Half written as the power went, most likely - which is a restart too.
            unclean = True
        try:
            storage.save_json(RUNNING_MARK, {"started_at": time.time()})
        except OSError as e:
            logger.warning(f"Couldn't note that the satellite is running: {e}")
        return unclean

    def _clean_up(self):
        """
        Closes everything down in order: the brain first, so nothing more arrives to
        play, then what rings, and only then the speaker and the microphone.
        """
        self._stopping = True
        self._finished.set()
        if self.link is not None:
            self.link.stop()
        self.ringer.quiet()
        with self._lock:
            self._save_timeline()
        self.speaker.close()
        self.microphone.close()
        # Stopped properly, so the next start isn't taken for a restart after a problem.
        try:
            os.remove(RUNNING_MARK)
        except OSError:
            pass
        logger.info("Shut down cleanly")


if __name__ == "__main__":
    paths.make_folders()
    # The same log main.py kept, which the README and the soak monitor point to.
    setup_logging(os.path.join(paths.LOGS_DIR, "smart_assistant.log"))
    # The satellite's password, the one secret it keeps - see paths.py.
    paths.load_secrets()
    satellite = Satellite(Config())
    # Ctrl+C and systemd's stop both end the main loop - see Satellite.shutdown.
    signal.signal(signal.SIGINT, satellite.shutdown)
    signal.signal(signal.SIGTERM, satellite.shutdown)
    satellite.run()
