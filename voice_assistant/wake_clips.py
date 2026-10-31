"""
Wake Clips Module.

Keeps the few seconds around each wake, when switched on with wake_clips.keep, so the
voice check can be trained on real false wakes as well as the samples recorded for it.
They're of the user's voice, so they stay on this machine, readable by this user only,
in ~/.local/share/nova/voice-samples/wake/clips, and are never sent anywhere. Only a wake
keeps anything, and a muted microphone never wakes, so nothing else is ever kept.

All of it sits beside the wake word, which matters far more than any clip, so the
listening loop does nothing here but hand over a position. Everything else - the wait
for the rest of the audio, the folder, the disk - is on a daemon thread, and whatever
goes wrong there is logged and that clip is dropped.
"""

import logging
import os
import threading
import time
import wave

from voice_assistant import paths
from voice_assistant.recording import RATE

# Logger setup
logger = logging.getLogger(__name__)

FOLDER = os.path.join(paths.DATA_DIR, "voice-samples", "wake", "clips")

# How much of a clip comes before the moment the wake word was heard and after it, in
# seconds. The microphone keeps about 10, so the start is still there when the end arrives.
BEFORE_SECONDS = 2
AFTER_SECONDS = 1

# How long the audio after a wake has to arrive, in seconds - the second of it, and
# some to spare. It only takes longer if the microphone has stopped, and audio from
# whenever it starts again would be nothing to do with the wake.
GIVE_UP_SECONDS = 3.0

# How often that wait checks how far the microphone has got, in seconds.
CHECK_SECONDS = 0.05


def _private_folder(path: str):
    """
    Makes a folder readable by this user only, and any above it which is missing too -
    makedirs would leave those to the umask. One already there is made private as
    well, but not those above it.

    Args:
        path: The folder to make, which can already be there.
    """
    parent = os.path.dirname(path)
    # Empty for a relative path with nothing above it, which is already there.
    if parent and not os.path.isdir(parent):
        _private_folder(parent)
    os.makedirs(path, mode=0o700, exist_ok=True)
    # makedirs leaves a folder which was already there as it was.
    os.chmod(path, 0o700)


class WakeClips:
    """Keeps the seconds around a wake on a thread of their own, one clip at a time."""

    def __init__(self, microphone, folder: str = FOLDER, most: int = 400):
        """
        Touches nothing yet, so a folder which can't be made can't stop the satellite
        starting - it's only found out when a clip is kept, and only costs that clip.

        Args:
            microphone: The shared Microphone.
            folder: Where the clips go.
            most: How many clips there can be. At that many no more are kept, and it
                warns once. Nova never deletes any - having recordings disappear by
                themselves would be a surprise.
        """
        self.microphone = microphone
        self.folder = folder
        self.most = most
        # Held while a clip is being kept, from the wake until its thread ends.
        self._keeping = threading.Lock()
        self._warned = False

    def keep(self, heard_at: int, score: float):
        """
        Starts keeping the clip around a wake, and returns at once. A wake while one is
        being kept isn't kept, so a wake word heard twice in a row can't pile up threads.

        Args:
            heard_at: The microphone position the wake word was heard at.
            score: How sure the wake word detector was.
        """
        if not self._keeping.acquire(blocking=False):
            return
        try:
            threading.Thread(target=self._keep, args=(heard_at, score, time.time()),
                             daemon=True, name="wake-clip").start()
        except Exception as e:
            self._keeping.release()
            logger.error(f"Couldn't start keeping a clip of a wake: {e}")

    def _keep(self, heard_at: int, score: float, woken_at: float):
        """Keeps one clip, or logs why it couldn't be. Runs on the clip's own thread."""
        try:
            _private_folder(self.folder)
            if self._full():
                return
            # Only the very first seconds of a run have less before them, as _woken's
            # pre-roll allows for - reading before the first sample would be silence.
            start = max(0, heard_at - BEFORE_SECONDS * RATE)
            end = heard_at + AFTER_SECONDS * RATE
            if not self._arrives(end):
                logger.warning("The microphone stopped just after a wake, so its clip wasn't kept")
                return
            reader = self.microphone.listen(start=start)
            audio = reader.read(end - start)
            # A start which has gone from the microphone's buffer is quietly moved on,
            # and that would be seconds of the room after the wake with no wake in them.
            if reader.position != end:
                logger.warning("Got to the audio of a wake too late, so its clip wasn't kept")
                return
            when = time.strftime("%Y%m%d-%H%M%S", time.localtime(woken_at))
            self._write(os.path.join(self.folder, f"{when}-{score:.2f}.wav"), audio)
        except Exception as e:
            logger.error(f"Couldn't keep a clip of a wake: {e}")
        finally:
            self._keeping.release()

    def _full(self) -> bool:
        """Whether there are as many clips as there can be, warning the first time there are."""
        clips = sum(name.endswith(".wav") for name in os.listdir(self.folder))
        if clips < self.most:
            return False
        if not self._warned:
            self._warned = True
            logger.warning(f"There are {clips} wake clips, the most there can be, so no more will be "
                           f"kept until some are moved away from {self.folder}")
        return True

    def _arrives(self, position: int) -> bool:
        """
        Waits for the microphone to reach a position, without reading from it.

        Leaving the wait to read() would do harm. A read which has to wait takes itself
        for the main loop: it reopens a microphone which has gone quiet, and tells the
        watchdog all is well. Beside the main loop's own read, that's two threads
        opening a stream at once, which PortAudio doesn't allow for, and a stream left
        open for good if it survives that - and a main loop which had hung wouldn't be
        restarted. So reading waits until there's nothing to wait for.

        Args:
            position: The position to wait for.

        Returns:
            True once it's been reached, or False if it hasn't been in GIVE_UP_SECONDS.
        """
        give_up_at = time.monotonic() + GIVE_UP_SECONDS
        while self.microphone.position < position:
            if time.monotonic() >= give_up_at:
                return False
            time.sleep(CHECK_SECONDS)
        return True

    @staticmethod
    def _write(path: str, audio):
        """
        Writes a clip as a 16-bit WAV file at 16kHz, readable by this user only, or
        not at all - a full disk leaves no half of one to be trained on.

        Args:
            path: The file to write.
            audio: 16-bit samples at 16kHz, as the microphone gives them.

        Raises:
            OSError: It couldn't be written. If that's because the name is taken, which
                the hour the clocks go back can do, the clip already there is left alone.
        """
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as raw, wave.open(raw, "wb") as file:
                file.setnchannels(1)
                file.setsampwidth(2)
                file.setframerate(RATE)
                file.writeframes(audio.tobytes())
        except BaseException:
            os.remove(path)
            raise
