"""
Text-to-Speech Module
Handles converting text responses to speech using Piper.

Piper used to be started afresh for every reply, loading its voice each time, and
the whole reply was written to a WAV file on the SD card before any of it could
play. Measured on the Pi, loading the voice alone took 0.35s of every reply. Now a
Piper process is started ahead of time, so its voice is already loaded when there's
something to say, and its audio is handed over as each sentence is ready, straight
from memory.
"""

import json
import logging
import os
import re
import select
import subprocess
import threading
import time
import wave
from collections import OrderedDict
from typing import Callable, Iterator, Optional

import numpy as np

from voice_assistant import paths
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Seconds to wait for Piper's next sentence before giving up on it. A long sentence
# takes about a second on the Pi, so this only ever runs out if Piper has hung.
SENTENCE_TIMEOUT = 10.0

# How many recent things said are kept, already synthesised. A timer says the same
# "Your 10 minute timer is done" every round until it's dismissed.
RECENT_PHRASES = 8


def speakable(text: str) -> str:
    """
    Rewrites what Piper reads wrongly. Its text reader takes a time on the hour
    digit by digit - "2:00 PM" came out as "two zero zero P M", "7:05" as "seven
    zero five" - whether the time came from the clock or from Claude. "3:30 PM" it
    reads properly, so that's left alone. Claude's "12 AM" and "12 PM" become
    "midnight" and "noon", as Nova's own times are (see clock.spoken_clock_time).

    Args:
        text: What's to be said.

    Returns:
        e.g. "from 2 PM until 10 PM", "7 oh 5 AM", "the 6 o'clock one", "until midnight".
    """
    # The full stop of "a.m." goes too, unless it also ends the sentence.
    text = re.sub(r"\b12(?::00)?\s*([AaPp])\.?\s?[Mm]\b(?:\.(?=\s+[a-z]))?",
                  lambda match: "midnight" if match.group(1) in "Aa" else "noon", text)
    text = re.sub(r"\b(\d{1,2}):00(?=\s*[AaPp]\.?\s?[Mm]\b)", r"\1", text)
    text = re.sub(r"\b(\d{1,2}):00\b", r"\1 o'clock", text)
    return re.sub(r"\b(\d{1,2}):0(\d)\b", r"\1 oh \2", text)


class TextToSpeech:
    """Synthesises and outputs speech from text given to Piper TTS."""

    def __init__(self, config):
        """
        Initialise the TTS engine.

        Args:
            config: Configs loaded from config.yaml.
        """

        # Stores the config and resolves the paths to Piper TTS and the voice model.
        self.config = config
        self.piper_path = os.path.join(paths.MODELS_DIR, "piper", "piper")
        self.model_path = os.path.join(paths.MODELS_DIR, "en_GB-alan-medium.onnx")

        # Reads the assistant's name from the "assistant" section in config.yaml.
        assistant_config = self.config.section("assistant")
        self.name = assistant_config.get("name", "Assistant")

        # Verify Piper is installed, raising error if not.
        if not os.path.exists(self.piper_path):
            raise FileNotFoundError(
                f"Piper not found at {self.piper_path}. "
                "Run setup.sh to install Piper TTS"
            )

        # Same check but for the voice model.
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"Voice model not found at {self.model_path}. "
                "Run setup.sh to install Piper TTS"
            )

        # The voice's own rate, from the settings file which comes with it.
        self.sample_rate = self._voice_rate(self.model_path + ".json")

        self._lock = threading.Lock()
        # A Piper process with its voice loaded, waiting for something to say.
        self._waiting: Optional[subprocess.Popen] = None
        self._recent: "OrderedDict[str, np.ndarray]" = OrderedDict()

        logger.info(f"Piper TTS initialised with model: {self.model_path}")

        # Test Piper on startup using _warmup().
        self._warmup()

    @staticmethod
    def _voice_rate(settings_file: str) -> int:
        """
        The sample rate a Piper voice speaks at.

        Args:
            settings_file: The voice's .onnx.json file.

        Returns:
            Its rate - or 22050, which every medium-quality voice uses, if the file
            can't be read.
        """
        try:
            with open(settings_file, encoding="utf-8") as file:
                return int(json.load(file)["audio"]["sample_rate"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"Couldn't read the voice's sample rate, assuming 22050Hz: {e}")
            return 22050

    def _warmup(self):
        """
        Says something to nobody at startup, so a broken Piper shows up in the log
        straight away rather than at the first reply - and leaves the next process
        loaded and waiting.
        """
        logger.info("Warming up Piper TTS...")
        if list(self.stream("Ready.", remember=False)):
            logger.info("Piper warmup complete")
        else:
            logger.warning("Warmup failed: Piper said nothing")

    def _start_piper(self) -> subprocess.Popen:
        """Starts a Piper process, which loads its voice and then waits for text."""
        return subprocess.Popen(
            [self.piper_path, "--model", self.model_path, "--output_raw", "--quiet"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _take_piper(self) -> subprocess.Popen:
        """
        A Piper process ready for some text: the one already loaded and waiting if
        there is one, or a fresh one if two things are being said at once.
        """
        with self._lock:
            process, self._waiting = self._waiting, None
        if process is None or process.poll() is not None:
            process = self._start_piper()
        return process

    def _prepare_next(self):
        """
        Starts the next Piper loading its voice, ready for the next thing to say.
        Only once the last has finished, since loading while it was still speaking
        would slow it down.
        """
        with self._lock:
            if self._waiting is not None and self._waiting.poll() is None:
                return
            try:
                self._waiting = self._start_piper()
            except OSError as e:
                logger.error(f"Couldn't start Piper: {e}")
                self._waiting = None

    def stream(self, text: str, on_start: Optional[Callable[[], None]] = None,
               remember: bool = True) -> Iterator[np.ndarray]:
        """
        Synthesises text, a sentence at a time, as Piper works through it.

        Args:
            text: What to say.
            on_start: Called once the first of the audio is ready, e.g. to time it.
            remember: Keep it in case the same is said again soon - see RECENT_PHRASES.

        Yields:
            The audio, as float32 samples at self.sample_rate. Nothing if synthesis
            failed, which is logged.
        """
        # Quotation marks and new lines are taken out - a new line would be taken
        # as the start of something else to say.
        text = speakable(text.replace('"', "'").replace("\n", " ").strip())
        if not text:
            logger.warning("No text provided.")
            return

        with self._lock:
            recent = self._recent.get(text)
            if recent is not None:
                self._recent.move_to_end(text)
        if recent is not None:
            if on_start:
                on_start()
            yield recent
            return

        logger.info(f"Synthesising: '{text[:50]}...'", extra=TRANSCRIPT)
        process = None
        pieces = []
        try:
            process = self._take_piper()
            # Passed on stdin rather than as an argument or through a shell, so
            # nothing in a reply can ever be run as a command.
            process.stdin.write(text.encode("utf-8") + b"\n")
            process.stdin.close()

            leftover = b""
            while True:
                ready, _, _ = select.select([process.stdout], [], [], SENTENCE_TIMEOUT)
                if not ready:
                    logger.error("Piper stopped responding part way through")
                    return
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    break
                # Samples are two bytes each, and a read can end between the two.
                data = leftover + data
                whole = len(data) - len(data) % 2
                leftover = data[whole:]
                if not whole:
                    continue
                piece = np.frombuffer(data[:whole], dtype="<i2").astype(np.float32) / 32768
                if on_start and not pieces:
                    on_start()
                pieces.append(piece)
                yield piece

            if process.wait(timeout=2) != 0:
                logger.error(f"Piper failed: {process.stderr.read().decode(errors='replace').strip()}")
                return
            audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
            logger.info(f"Generated {len(audio) / self.sample_rate:.2f}s of audio")
            if remember and len(audio):
                self._remember(text, audio)
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            # Also reached when whoever was reading stops early - the reply was
            # silenced part way - so Piper isn't left working on it.
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()
            self._prepare_next()

    def _remember(self, text: str, audio: np.ndarray):
        """Keeps something just said, dropping the least recently said once full."""
        with self._lock:
            self._recent[text] = audio
            self._recent.move_to_end(text)
            while len(self._recent) > RECENT_PHRASES:
                self._recent.popitem(last=False)

    def synthesise(self, text, output_file):
        """
        Convert text to speech using Piper and save it as a WAV file. The assistant
        itself streams instead (see stream) - this is for trying the voice out.

        Args:
            text: Text to synthesise.
            output_file: Path to write the WAV file to.

        Returns:
            The output file path on success, or None if synthesis failed.
        """
        pieces = list(self.stream(text))
        if not pieces:
            return None
        audio = np.concatenate(pieces)
        with wave.open(output_file, "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(self.sample_rate)
            file.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        return output_file

    def close(self):
        """Stops the Piper process waiting for something to say."""
        with self._lock:
            process, self._waiting = self._waiting, None
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def main():
    """
    Manual testing, run from the project folder as "python -m voice_assistant.text_to_speech".
    This synthesises a list of sample phrases with Piper.
    The audio is not played upon test completion but is saved to a file which can be opened manually.
    """
    from voice_assistant.config import Config

    # Loads config.yaml
    config = Config()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Initialise TTS
    tts = TextToSpeech(config)

    # Test phrases to be synthesised.
    test_phrases = [
        f"Hello, I am {tts.name}, your personal voice assistant.",
        "The weather today is partly cloudy with a high of twenty two degrees.",
        "I'm turning on the living room lights now.",
        "That would be four.",
        "What's good G"
    ]

    print("\n=== Testing Piper TTS ===\n")

    # Runs synthesis for each phrase and reports success / failure per phrase.
    for i, phrase in enumerate(test_phrases, 1):
        print(f"[{i}/{len(test_phrases)}] Synthesising: '{phrase}'")
        output_file = f"test_tts_{i}.wav"

        started = time.monotonic()
        result = tts.synthesise(phrase, output_file)

        if result:
            print(f"Saved to: {output_file} ({time.monotonic() - started:.2f}s)")
            # Terminal command to play the test file.
            print(f"Play with: aplay {output_file}")
        else:
            print("Failed.")
        print()
    tts.close()

# Tests the TextToSpeech class manually.
if __name__ == "__main__":
    main()
