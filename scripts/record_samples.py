#!/usr/bin/env python3
"""
Record Samples
Records you saying a list of things, through Nova's own microphone, echo cancelling
and end-of-speech detection, so each recording is just what speech-to-text would be
given - for scripts/compare_speech_to_text.py to measure models on.

Stop Nova first, so it isn't listening as well, and start it again afterwards:

    systemctl --user stop voice-assistant
    venv/bin/python scripts/record_samples.py my_prompts.txt
    systemctl --user start voice-assistant

The prompts file has one thing to say on each line. A line in square brackets, like
"[from across the room]", says how the ones after it are said, and a "*" on its own
is one you make up. Lines starting with "#" are left out. scripts/sample_prompts.txt
is a list to start from: copy it somewhere outside the project, and add the songs,
playlists, lights and the like you actually ask for.

Running it again carries on from the first prompt not yet recorded. The recordings
are of your voice, so they're kept outside the project, readable by you only - in
~/.local/share/nova/voice-samples unless you give another folder - and are never
sent anywhere. Delete them once you're done with them.
"""

import argparse
import os
import sys
import wave
from typing import Callable, Dict, List, Optional

import numpy as np

# Add the project root to the path so the voice_assistant package imports work
# no matter where the script is run from.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIRECTORY)

from voice_assistant import paths
from voice_assistant.storage import load_json, save_json

RATE = 16000


def parse_prompts(text: str) -> List[Dict[str, str]]:
    """
    The things to say, from a prompts file - see the top of this file.

    Args:
        text: The file's contents.

    Returns:
        Each prompt's "text" - empty for one the user makes up - and the
        "condition" it's said in.
    """
    prompts, condition = [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            condition = line[1:-1].strip()
        else:
            prompts.append({"text": "" if line == "*" else line, "condition": condition})
    return prompts


def save_wav(path: str, audio: np.ndarray):
    """
    Writes a recording as a 16-bit WAV file at 16kHz, readable by this user only.

    Args:
        path: The file to write.
        audio: float32 samples at 16kHz, as the microphone records them.
    """
    samples = np.clip(np.round(audio * 32768), -32768, 32767).astype(np.int16)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as raw, wave.open(raw, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(samples.tobytes())


def _what_was_said(prompt: Dict[str, str], ask: Callable[[str], str]) -> Optional[str]:
    """
    What the user actually said, since it isn't always just what was written.

    Returns:
        The words, or None to record it again.
    """
    while True:
        if prompt["text"]:
            typed = ask("Said it differently? Type what you said - Enter if not, r to record it again: ").strip()
        else:
            typed = ask("Type exactly what you said - r to record it again: ").strip()
        if typed.lower() == "r":
            return None
        if typed or prompt["text"]:
            return typed or prompt["text"]


def record_samples(prompts: List[Dict[str, str]], folder: str, record: Callable[[], np.ndarray],
                   ask: Callable[[str], str], show: Callable[[str], None] = print) -> int:
    """
    Records each prompt not recorded yet, keeping each one as soon as it's made, so a
    session can be stopped and carried on later.

    Args:
        prompts: From parse_prompts.
        folder: Where the recordings go, listed in samples.json there.
        record: Records the user until they stop talking - float32 samples at 16kHz,
            or nothing if nothing was heard.
        ask: Asks the user something, and gives back what they typed.
        show: Shows the user a line.

    Returns:
        How many were recorded this time.
    """
    os.makedirs(folder, mode=0o700, exist_ok=True)
    listing = os.path.join(folder, "samples.json")
    samples = load_json(listing, default=[])
    done = {sample["number"] for sample in samples}
    recorded = 0
    for number, prompt in enumerate(prompts, 1):
        if number in done:
            continue
        how = f" - {prompt['condition']}" if prompt["condition"] else ""
        show(f"\n{number} of {len(prompts)}{how}: {prompt['text'] or 'anything you would ask Nova'}")
        while True:
            answer = ask("Enter to record, s to skip, q to stop: ").strip().lower()
            if answer == "q":
                return recorded
            if answer == "s":
                break
            audio = record()
            if len(audio) == 0:
                show("Nothing heard - try again.")
                continue
            show(f"Recorded {len(audio) / RATE:.1f}s.")
            said = _what_was_said(prompt, ask)
            if said is None:
                continue
            name = f"{number:03d}.wav"
            save_wav(os.path.join(folder, name), audio)
            samples.append({"number": number, "file": name, "text": said, "prompt": prompt["text"],
                            "condition": prompt["condition"]})
            save_json(listing, samples, indent=1)
            recorded += 1
            break
    return recorded


def main():
    parser = argparse.ArgumentParser(description="Records you saying a list of things, for "
                                                 "compare_speech_to_text.py.")
    parser.add_argument("prompts", help="the things to say - see scripts/sample_prompts.txt")
    parser.add_argument("folder", nargs="?", default=os.path.join(paths.DATA_DIR, "voice-samples"),
                        help="where the recordings go")
    arguments = parser.parse_args()
    with open(arguments.prompts) as file:
        prompts = parse_prompts(file.read())
    folder = os.path.expanduser(arguments.folder)

    # Imported here rather than at the top, so the rest can be tested on a machine
    # without the Pi's sound packages.
    from voice_assistant.config import Config
    from voice_assistant.echo import make_echo_canceller
    from voice_assistant.microphone import Microphone
    from voice_assistant.speaker import Speaker
    from voice_assistant.vad import make_detector

    # The same microphone, echo cancelling and end-of-speech detection as Nova, with
    # its settings - see main.py.
    config = Config()
    rate = config.section("audio").get("sample_rate", 48000)
    echo = make_echo_canceller(config, rate)
    speaker = Speaker(sample_rate=rate, echo_reference=echo.played if echo else None)
    microphone = Microphone(sample_rate=rate, echo=echo)
    detector = make_detector(config)
    silence = config.section("thresholds").get("silence_duration", 0.6)

    def record() -> np.ndarray:
        # The listening sound, as after "Hey Nova", to say when to start.
        playback = speaker.play_file(os.path.join(paths.SOUNDS_DIR, "listening.wav"), volume=0.5)
        if playback is not None:
            playback.wait()
        # Longer than Nova allows to start talking and to talk for, since reading the
        # prompt takes a moment, and the made up ones can be longer.
        return microphone.record_until_silence(detector, listen_from=microphone.position,
                                               silence_duration=silence, speech_timeout=5.0, timeout=15.0)

    print(f"{len(prompts)} things to say. Each recording is kept in {folder} as it's made.")
    try:
        recorded = record_samples(prompts, folder, record, input)
        print(f"\nRecorded {recorded} this time. Compare models on them with scripts/compare_speech_to_text.py, "
              f"and delete {folder} once you're done.")
    except (KeyboardInterrupt, EOFError):
        print(f"\nStopped - what was recorded is kept in {folder}. Run this again to carry on.")
    finally:
        microphone.close()
        speaker.close()
        if echo:
            echo.close()


if __name__ == "__main__":
    main()
