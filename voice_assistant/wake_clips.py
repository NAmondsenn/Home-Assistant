"""
Wake Clips Module.

Keeps the few seconds around each wake, when switched on with wake_clips.keep, so the
voice check can be trained on real false wakes as well as the samples recorded for it.
They're of the user's voice, so they stay on this machine, readable by this user only,
in ~/.local/share/nova/voice-samples/wake/clips, and are never sent anywhere. Only a wake
keeps anything, and mute wins: it only stops the wake word being heard, while the audio
goes on flowing, so a clip is dropped if the microphone was muted around it, as far as
the satellite has heard by then.

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
from typing import Callable

from voice_assistant import paths, storage
from voice_assistant.recording import RATE

# Logger setup
logger = logging.getLogger(__name__)

FOLDER = os.path.join(paths.DATA_DIR, "voice-samples", "wake", "clips")

# How much of a clip comes before the moment the wake word was heard and after it, in
# seconds. The microphone keeps about 10, so the start is still there when the end arrives.
BEFORE_SECONDS = 2
AFTER_SECONDS = 1

# How long the audio after a wake has to arrive, in seconds from the wake - the second
# of it, and some to spare. It only takes longer if the microphone has stopped, or this
# thread was held up, and audio from whenever it starts again would be nothing to do
# with the wake. It's under the 2 seconds the main loop waits before reopening a stream,
# so a reopened stream can never finish a clip.
GIVE_UP_SECONDS = 1.5

# How long a clip waits before it's written, in seconds, for news of a mute. A mute
# reaches the satellite well after the tap - through Home Assistant or the settings
# page, the brain, the network and a save to each machine's disk - so one made in the
# last moments of the second after a wake can still be on its way once that's been heard.
MUTE_NEWS_SECONDS = 0.5

# How often the wait for the audio after a wake checks how far the microphone has got,
# in seconds.
CHECK_SECONDS = 0.05


def clip_start(heard_at: int) -> int:
    """
    Where a wake's clip starts. Only the very first seconds of a run have less before
    them - reading before the first sample would be silence.

    Args:
        heard_at: The microphone position the wake word was heard at.

    Returns:
        A microphone position.
    """
    return max(0, heard_at - BEFORE_SECONDS * RATE)


class WakeClips:
    """Keeps the seconds around a wake on a thread of their own, one clip at a time."""

    def __init__(self, microphone, muted: Callable[[int], bool], folder: str = FOLDER, most: int = 400):
        """
        Touches nothing yet, so a folder which can't be made can't stop the satellite
        starting - it's only found out when a clip is kept, and only costs that clip.

        Args:
            microphone: The shared Microphone.
            muted: Given a position, says whether any of the audio from there on was
                heard muted - at any moment since, not just now, as a mute and an
                unmute can both come in the second a clip waits for. There's no default,
                so nothing can leave it out and keep what was said while muted.
            folder: Where the clips go.
            most: How many clips there can be. At that many no more are kept, and it
                warns once. Nova never deletes any - having recordings disappear by
                themselves would be a surprise.
        """
        self.microphone = microphone
        self.muted = muted
        self.folder = folder
        self.most = most
        # Said every start, so a switch left on can't go unnoticed.
        logger.info(f"Keeping a clip around each wake, up to {most}, in {folder}")
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
            threading.Thread(target=self._keep, args=(heard_at, score, time.time(), time.monotonic() + GIVE_UP_SECONDS),
                             daemon=True, name="wake-clip").start()
        except Exception as e:
            self._keeping.release()
            logger.error(f"Couldn't start keeping a clip of a wake: {e}")

    def _keep(self, heard_at: int, score: float, woken_at: float, give_up_at: float):
        """
        Keeps one clip, or logs why it couldn't be. Runs on the clip's own thread.

        Args:
            heard_at: The microphone position the wake word was heard at.
            score: How sure the wake word detector was.
            woken_at: When that was, by time.time().
            give_up_at: When the audio stops being worth waiting for, by time.monotonic().
        """
        try:
            storage.private_folder(self.folder)
            if self._full():
                return
            start = clip_start(heard_at)
            end = heard_at + AFTER_SECONDS * RATE
            if not self._arrives(end, give_up_at):
                logger.warning("The second after a wake didn't arrive in time, so its clip wasn't kept")
                return
            if self._muted(start):
                return
            reader = self.microphone.listen(start=start)
            audio = reader.read(end - start)
            # A start which has gone from the microphone's buffer is quietly moved on,
            # and that would be seconds of the room after the wake with no wake in them.
            if reader.position != end:
                logger.warning("Got to the audio of a wake too late, so its clip wasn't kept")
                return
            time.sleep(MUTE_NEWS_SECONDS)
            if self._muted(start):
                return
            when = time.strftime("%Y%m%d-%H%M%S", time.localtime(woken_at))
            name = f"{when}-{score:.2f}.wav"
            self._write(os.path.join(self.folder, name), audio)
            logger.info(f"Kept a wake clip, {name}, score {score:.2f}")
        except Exception as e:
            logger.error(f"Couldn't keep a clip of a wake: {e}")
        finally:
            self._keeping.release()

    def _muted(self, start: int) -> bool:
        """Whether a clip takes in anything heard muted, saying so if it does - and no more than that."""
        if not self.muted(start):
            return False
        logger.info("Wake clip not kept, muted")
        return True

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

    def _arrives(self, position: int, give_up_at: float) -> bool:
        """
        Waits for the microphone to reach a position, without reading from it.

        Leaving the wait to read() would do harm. A read which has to wait takes itself
        for the main loop: it reopens a microphone which has gone quiet, and tells the
        watchdog all is well. Beside the main loop's own read, that's two threads
        opening a stream at once, which PortAudio doesn't allow for, and a stream left
        open for good if it survives that - and a main loop which had hung wouldn't be
        restarted. So reading waits until there's nothing to wait for. The one read
        still beats the watchdog once, before it looks at anything, which does no harm:
        a clip only follows a wake the main loop has just delivered, and the watchdog
        allows it minutes.

        Args:
            position: The position to wait for.
            give_up_at: When to stop waiting, by time.monotonic().

        Returns:
            True once it's been reached, or False if it wasn't by then - even if it's
            there now, since that could be a reopened stream's audio.
        """
        while time.monotonic() < give_up_at:
            if self.microphone.position >= position:
                return True
            time.sleep(CHECK_SECONDS)
        return False

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
