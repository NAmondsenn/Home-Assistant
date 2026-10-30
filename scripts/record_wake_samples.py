#!/usr/bin/env python3
"""
Record Wake Samples
Records what Nova's voice check needs to learn from: you saying "Hey Nova", and the
sounds which must not wake it - a film playing, or you talking. They're recorded
through Nova's own microphone and echo cancelling, so each one is just what the wake
word model hears.

Stop Nova first, so it isn't listening as well, and start it again afterwards:

    systemctl --user stop voice-assistant
    venv/bin/python scripts/record_wake_samples.py hey-nova --count 30 --label close
    systemctl --user start voice-assistant

There are three kinds, one for each run, and none of them ask you to type:

    hey-nova --count N --label close   Nova blips, then listens for 3 seconds, N
                                       times, about 5 seconds apart. Say "Hey Nova"
                                       after each blip. Each take's best wake word
                                       score is shown, and one under 0.2 - nothing
                                       said, or too far away - is taken again, at
                                       most twice more.
    film --seconds N                   Records N seconds of a film playing, with
                                       nobody talking, in 10 second files.
    talk --seconds N                   The same, for you talking normally without
                                       saying the wake word.

--label says how or where it was recorded, like "close", "across-the-room" or
"kitchen", and is part of each file's name. It's "film" and "talk" for those two if
left out. Running again adds to what's there, numbering on from the last file with
the same label.

The recordings are of your voice, so they're kept outside the project, readable by
you only - in ~/.local/share/nova/voice-samples/wake, in hey_nova and not_hey_nova -
and are never sent anywhere. Ctrl-C stops a run and keeps every take or file already
finished.
"""

import argparse
import os
import re
import sys
import time
from typing import Callable, Tuple

import numpy as np

# Add the project root to the path so the voice_assistant package imports work no
# matter where the script is run from, and this folder so record_samples imports too.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIRECTORY)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from record_samples import save_wav
from voice_assistant import paths
from voice_assistant.wake_word import CHUNK_SAMPLES

RATE = 16000

# How long each "Hey Nova" take is, in seconds - long enough for the phrase, with
# room either side so the model has the lead-in and the tail it's judged on.
TAKE_SECONDS = 3.0

# The seconds left after a take before the next blip, so a take starts about 5 seconds
# after the last one did.
PAUSE_SECONDS = 2.0

# A take whose best wake word score is under this wasn't heard saying it at all.
MIN_SCORE = 0.2

# How many more times a take under MIN_SCORE is tried, before it's kept as it is.
RETAKES = 2

# How long each file of a film or of talking is, in seconds.
FILE_SECONDS = 10

HEY_NOVA_FOLDER = "hey_nova"
NOT_HEY_NOVA_FOLDER = "not_hey_nova"


def _private_folder(path: str):
    """
    Makes a folder readable by this user only, and any above it which is missing too -
    makedirs would leave those to the umask.

    Args:
        path: The folder to make, which can already be there.
    """
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        _private_folder(parent)
    os.makedirs(path, mode=0o700, exist_ok=True)


def next_number(folder: str, label: str) -> int:
    """
    The number for the next file with a label, one on from the highest there.

    Args:
        folder: Where the recordings are.
        label: Part of each file's name, as in "close-007.wav".

    Returns:
        1 if there are none yet.
    """
    pattern = re.compile(rf"{re.escape(label)}-(\d+)\.wav")
    numbers = [int(match.group(1)) for name in os.listdir(folder) if (match := pattern.fullmatch(name))]
    return max(numbers, default=0) + 1


def _save(folder: str, label: str, audio: np.ndarray) -> str:
    """Saves a recording as the next file with its label, and says which."""
    name = f"{label}-{next_number(folder, label):03d}.wav"
    save_wav(os.path.join(folder, name), audio)
    return name


def record_hey_nova(count: int, label: str, wake_folder: str, blip: Callable[[], None],
                    take: Callable[[], Tuple[np.ndarray, float]], pause: Callable[[float], None] = time.sleep,
                    show: Callable[[str], None] = print) -> int:
    """
    Records "Hey Nova" said a number of times, a blip before each, keeping each take as
    soon as it's made.

    Args:
        count: How many takes to keep.
        label: Part of each file's name.
        wake_folder: The folder holding hey_nova and not_hey_nova.
        blip: Plays the sound saying to start, and waits until it's over.
        take: Records one take - float32 samples at 16kHz, and the best wake word
            score heard during it.
        pause: Waits this many seconds between takes.
        show: Shows the user a line.

    Returns:
        How many were kept. A take which is still scoring too low after its retakes
        is kept all the same, so this is always count, unless it was stopped.
    """
    folder = os.path.join(wake_folder, HEY_NOVA_FOLDER)
    _private_folder(folder)
    kept = 0
    try:
        for number in range(1, count + 1):
            for attempt in range(RETAKES + 1):
                blip()
                audio, score = take()
                if score >= MIN_SCORE or attempt == RETAKES:
                    break
                show(f"{number} of {count}: {score:.2f} - too low, taking it again")
            _save(folder, label, audio)
            kept += 1
            show(f"{number} of {count}: {score:.2f}" + ("" if score >= MIN_SCORE else " - kept, though low"))
            if number < count:
                pause(PAUSE_SECONDS)
    except KeyboardInterrupt:
        show(f"Stopped - kept {kept} of {count} in {folder}.")
    return kept


def record_stretch(seconds: float, label: str, wake_folder: str, read: Callable[[int], np.ndarray],
                   show: Callable[[str], None] = print) -> int:
    """
    Records one long stretch of sound that mustn't wake Nova, in files of FILE_SECONDS,
    with no gaps between them.

    Args:
        seconds: How long to record for. The last file is shorter if it isn't a
            whole number of FILE_SECONDS.
        label: Part of each file's name.
        wake_folder: The folder holding hey_nova and not_hey_nova.
        read: Waits for this many samples (up to a second's) of the microphone, and
            gives them back as float32 at 16kHz, carrying on from where the last call
            stopped.
        show: Shows the user a line.

    Returns:
        How many files were kept.
    """
    folder = os.path.join(wake_folder, NOT_HEY_NOVA_FOLDER)
    _private_folder(folder)
    total = round(seconds * RATE)
    kept = 0
    try:
        for done in range(0, total, FILE_SECONDS * RATE):
            wanted = min(FILE_SECONDS * RATE, total - done)
            # A second at a time. The microphone only keeps 10 seconds, so one read of a
            # whole file would be late by the time it was woken, and skip the start.
            pieces = [read(min(RATE, wanted - taken)) for taken in range(0, wanted, RATE)]
            _save(folder, label, np.concatenate(pieces))
            kept += 1
            show(f"{min(done + FILE_SECONDS * RATE, total) / RATE:g} of {seconds:g} seconds")
    except KeyboardInterrupt:
        show(f"Stopped - kept {kept} files in {folder}.")
    return kept


def listen_for_take(microphone, model, wake_word_key: str) -> Tuple[np.ndarray, float]:
    """
    Records one take, scoring it as Nova's wake word model does while it goes: 80ms at
    a time, from a model which has been reset, so what came before isn't remembered.

    Args:
        microphone: The shared Microphone, read from now onwards.
        model: The openWakeWord model.
        wake_word_key: The model's name in what it predicts.

    Returns:
        TAKE_SECONDS of float32 samples at 16kHz, and the highest score of any 80ms.
    """
    model.reset()
    reader = microphone.listen()
    chunks, best = [], 0.0
    # Whole chunks, so the last is scored with the rest - and trimmed afterwards.
    for _ in range(-(-round(TAKE_SECONDS * RATE) // CHUNK_SAMPLES)):
        chunk = reader.read(CHUNK_SAMPLES)
        chunks.append(chunk)
        best = max(best, float(model.predict(chunk).get(wake_word_key, 0)))
    # The microphone gives 16-bit samples, which save_wav wants as floats.
    audio = np.concatenate(chunks)[:round(TAKE_SECONDS * RATE)].astype(np.float32) / 32768.0
    return audio, best


def _label(text: str) -> str:
    """For argparse: a label which is safe to put in a file name."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise argparse.ArgumentTypeError("use only letters, numbers, - and _")
    return text


def _count(text: str) -> int:
    """For argparse: a whole number above zero."""
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be above zero")
    return number


def _positive(text: str) -> float:
    """For argparse: a number above zero."""
    try:
        number = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not number > 0:
        raise argparse.ArgumentTypeError("must be above zero")
    return number


def main():
    parser = argparse.ArgumentParser(description="Records what Nova's voice check needs to learn "
                                                 "from - see the top of this file.")
    kinds = parser.add_subparsers(dest="kind", required=True)
    hey_nova = kinds.add_parser("hey-nova", help="you saying Hey Nova, after a blip")
    hey_nova.add_argument("--count", type=_count, required=True,
                          help="how many takes")
    hey_nova.add_argument("--label", type=_label, required=True, help="how it's said, like close")
    for kind, what in (("film", "a film playing, with nobody talking"),
                       ("talk", "you talking normally, without the wake word")):
        sub = kinds.add_parser(kind, help=what)
        sub.add_argument("--seconds", type=_positive, required=True, help="how long to record for")
        sub.add_argument("--label", type=_label, default=kind, help=f"default {kind}")
    arguments = parser.parse_args()
    wake_folder = os.path.join(paths.DATA_DIR, "voice-samples", "wake")

    # Imported here rather than at the top, so the rest can be tested on a machine
    # without the Pi's sound packages.
    from voice_assistant.config import Config
    from voice_assistant.echo import make_echo_canceller
    from voice_assistant.microphone import Microphone
    from voice_assistant.speaker import Speaker
    from voice_assistant.wake_word import WakeWordDetector

    def show(line: str):
        # The controller reads this over ssh as it comes, so it isn't held back.
        print(line, flush=True)

    # The same microphone and echo cancelling as Nova, with its settings - see session.py.
    config = Config()
    rate = config.section("audio").get("sample_rate", 48000)
    echo = make_echo_canceller(config, rate)
    speaker = Speaker(sample_rate=rate, echo_reference=echo.played if echo else None)
    microphone = Microphone(sample_rate=rate, echo=echo)
    try:
        if arguments.kind == "hey-nova":
            detector = WakeWordDetector(config=config, microphone=microphone)
            show(f"Scoring with the '{detector.wake_word_key}' model. Say \"Hey Nova\" after each blip.")

            def blip():
                # The listening sound, as after "Hey Nova". Waited for, so a take starts
                # once it's over and doesn't have it in the recording.
                playback = speaker.play_file(os.path.join(paths.SOUNDS_DIR, "listening.wav"), volume=0.5)
                if playback is not None:
                    playback.wait()

            kept = record_hey_nova(arguments.count, arguments.label, wake_folder, blip,
                                   lambda: listen_for_take(microphone, detector.model, detector.wake_word_key),
                                   show=show)
            show(f"Kept {kept} in {wake_folder}. Delete them once you're done with them.")
        else:
            reader = microphone.listen()
            kept = record_stretch(arguments.seconds, arguments.label, wake_folder,
                                  lambda count: reader.read(count).astype(np.float32) / 32768.0, show=show)
            show(f"Kept {kept} files in {wake_folder}. Delete them once you're done with them.")
    finally:
        microphone.close()
        speaker.close()
        if echo:
            echo.close()


if __name__ == "__main__":
    main()
