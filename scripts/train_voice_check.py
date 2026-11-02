#!/usr/bin/env python3
"""
Train Voice Check
Trains Nova's voice check - a check that it's you saying "Hey Nova", to tell you from a
film - on what scripts/record_wake_samples.py recorded and on the wake clips the
satellite kept, and saves it where the satellite loads it from. Run it on the Pi:

    venv/bin/python scripts/train_voice_check.py labels.txt
    systemctl --user restart voice-assistant

It learns from the wake word model's own features, 80ms at a time, as Nova hears them,
each file after 2 seconds of silence:

    you            the moments scoring 0.5 or more, of each recording in
                   wake/hey_nova and each clip marked pos
    not you        every moment of each recording in wake/not_hey_nova and each
                   clip marked neg

The labels file says what each clip in wake/clips was, one to a line - its file name,
then pos (you, asking Nova something), neg (it woke with nobody talking to it) or drop
(not sure, so left out). Clips which aren't listed are left out too.

    2026-11-01-191502-0.81.wav pos
    2026-11-01-203317-0.66.wav neg

Before training on everything, it says how the check does on clips it hasn't learnt
from: --rounds times, a quarter of each kind of clip is held back and the rest trained
on, and a held-back clip counts as kept if any moment of it the wake model scored 0.5
or more passes the check - at 0.2, 0.3 and 0.5, the settings worth choosing between. A
clip with no such moment when it's scored again is counted on its own line. The
recordings are always trained on, and each run gives the same results as the last.
Then it trains on everything and saves the check, readable by you only, as
~/.local/share/nova/voice_check.json. The satellite loads it when it starts.

It deletes nothing. Once you're happy with the check, the recordings and the clips can
be deleted, and wake_clips.keep switched back off.
"""

import argparse
import os
import sys
import wave
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

# Add the project root to the path so the voice_assistant package imports work no
# matter where the script is run from, and this folder so record_wake_samples imports too.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIRECTORY)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from record_wake_samples import HEY_NOVA_FOLDER, MIN_SCORE, NOT_HEY_NOVA_FOLDER, RATE
from voice_assistant import voice_check, wake_clips

# Where record_wake_samples.py and the satellite keep what's trained on.
WAKE_FOLDER = os.path.dirname(wake_clips.FOLDER)
CLIPS_FOLDER = os.path.basename(wake_clips.FOLDER)

LABELS = ("pos", "neg", "drop")

# How many times clips are held back to say how the check does. Each round trains it
# again, so more is slower; 20 is what the trial on the Pi did.
ROUNDS = 20

# The settings the held-back results are given at - from letting every one of the
# user's requests through in the trial (0.2) to stopping the most false wakes (0.5).
SETTINGS = (0.2, 0.3, 0.5)

# openWakeWord works through audio 80ms at a time - wake_word.py's CHUNK_SAMPLES, which
# isn't imported, so this file needn't load openWakeWord for --help or its tests.
CHUNK_SAMPLES = 1280

# Seconds of silence the model hears after a reset, before each file. A reset leaves it
# remembering random noise, which the next second or so of features would be partly
# made of - and different every run - while the satellite has always heard the room
# for a couple of seconds before anything can wake it. 2 seconds clears it all, as
# openWakeWord's own predict_clip pads a clip with silence.
LEAD_IN_SECONDS = 2

# Each 80ms of a file: the wake word model's score, and its features for that moment.
Steps = List[Tuple[float, np.ndarray]]


def read_labels(path: str) -> Dict[str, str]:
    """
    Reads the labels file.

    Args:
        path: The file, a clip's file name and its label to a line. Blank lines are skipped.

    Returns:
        Each clip's label, by its file name.

    Raises:
        ValueError: A line isn't a name and one of LABELS, or a clip's listed twice.
        OSError: The file couldn't be read.
    """
    labels = {}
    with open(path) as file:
        for number, line in enumerate(file, start=1):
            parts = line.split()
            if not parts:
                continue
            if len(parts) != 2 or parts[1] not in LABELS:
                raise ValueError(f"line {number} of {path} isn't a clip's file name and one of "
                                 f"{', '.join(LABELS)}: {line.strip()!r}")
            name, label = parts
            if name in labels:
                raise ValueError(f"{name} is listed twice in {path}")
            labels[name] = label
    return labels


def read_wav(path: str) -> np.ndarray:
    """
    Reads a recording or a clip, as both scripts that make them write them.

    Args:
        path: A 16-bit mono WAV file at 16kHz.

    Returns:
        Its samples as 16-bit integers, which is what the wake word model is given.

    Raises:
        ValueError: It's in another format.
        OSError: It couldn't be read.
    """
    with wave.open(path) as file:
        if (file.getnchannels(), file.getsampwidth(), file.getframerate()) != (1, 2, RATE):
            raise ValueError(f"{path} isn't 16-bit mono at {RATE}Hz")
        return np.frombuffer(file.readframes(file.getnframes()), dtype=np.int16)


def steps(audio: np.ndarray, model, key: str) -> Steps:
    """
    Scores a file as the satellite listens, 80ms at a time from a model which has been
    reset and then heard LEAD_IN_SECONDS of silence, keeping the features of each
    moment - what the satellite scores the check on.

    Args:
        audio: 16-bit samples at 16kHz.
        model: The openWakeWord model.
        key: The wake word model's name in what it predicts.

    Returns:
        The score and features of each whole 80ms. A part left over at the end isn't
        scored, as openWakeWord's own training leaves it.
    """
    model.reset()
    silence = np.zeros(CHUNK_SAMPLES, dtype=np.int16)
    for _ in range(LEAD_IN_SECONDS * RATE // CHUNK_SAMPLES):
        model.predict(silence)
    found = []
    for start in range(0, len(audio) - CHUNK_SAMPLES + 1, CHUNK_SAMPLES):
        score = float(model.predict(audio[start:start + CHUNK_SAMPLES]).get(key, 0))
        features = model.preprocessor.get_features(model.model_inputs[key])
        found.append((score, features[0]))
    return found


def training_set(positives: List[Steps], negatives: List[Steps]) -> Tuple[np.ndarray, np.ndarray]:
    """
    What the check is trained on: the moments of the user's files the wake model scored
    MIN_SCORE or more - the rest of each is the room before and after "Hey Nova" - and
    every moment of the others, since a film can sound like anything.

    Args:
        positives: The steps of each of the user's files.
        negatives: The steps of each of the others.

    Returns:
        The features of each moment, and 1 for each of the user's and 0 for the others'.

    Raises:
        ValueError: There are none of either.
    """
    yes = [features for found in positives for score, features in found if score >= MIN_SCORE]
    no = [features for found in negatives for _, features in found]
    if not yes:
        raise ValueError(f"no moment of a \"Hey Nova\" recording or a pos clip scored {MIN_SCORE} or "
                         f"more, so there's nothing of your voice to learn from")
    if not no:
        raise ValueError("there are no other sounds to learn from")
    return np.array(yes + no), np.array([1] * len(yes) + [0] * len(no))


def best_check_score(pipeline, found: Steps) -> Optional[float]:
    """
    The check's best score for a clip, on the moments of it the wake model scored
    MIN_SCORE or more - the ones which could have woken Nova.

    Args:
        pipeline: A trained check, as openWakeWord's trainer gives it.
        found: The clip's steps.

    Returns:
        The best, or None if no moment reached MIN_SCORE.
    """
    moments = [features for score, features in found if score >= MIN_SCORE]
    if not moments:
        return None
    return float(np.max(pipeline.predict_proba(np.array(moments))[:, 1]))


def _share(count: int, total: int, things: str) -> str:
    """For example "6 of 7 requests (86%)"."""
    return f"{count} of {total} {things}" + (f" ({round(100 * count / total)}%)" if total else "")


def held_back(recordings: Tuple[List[Steps], List[Steps]], requests: List[Steps], false_wakes: List[Steps],
              rounds: int, fit: Callable, show: Callable[[str], None]):
    """
    Says how the check does on clips it hasn't learnt from, holding back a quarter of
    each kind of clip a number of times, at each of SETTINGS. A held-back clip with no
    moment at MIN_SCORE or more, scored again from silence, couldn't have woken Nova
    whatever the check said, so it's counted on its own rather than as kept or stopped.

    Args:
        recordings: The steps of the "Hey Nova" recordings and of the others, always trained on.
        requests: The steps of each clip marked pos.
        false_wakes: The steps of each clip marked neg.
        rounds: How many times.
        fit: openWakeWord's train_verifier_model.
        show: Shows the user a line.
    """
    held_requests, held_false_wakes = len(requests) // 4, len(false_wakes) // 4
    if not held_requests and not held_false_wakes:
        show("Too few clips to hold any back, so there's nothing to say how the check does.")
        return
    show(f"Held back a quarter of the clips {rounds} times - {held_requests} requests and "
         f"{held_false_wakes} false wakes each time:")
    # The same clips held back each run - and the features and the training are the same
    # each run too - so the results only change when the files or the labels do.
    rng = np.random.default_rng(0)
    kept = {setting: [0, 0] for setting in SETTINGS}
    reached, unheard = [0, 0], [0, 0]
    for _ in range(rounds):
        out_requests = set(rng.permutation(len(requests))[:held_requests])
        out_false_wakes = set(rng.permutation(len(false_wakes))[:held_false_wakes])
        pipeline = fit(*training_set(
            recordings[0] + [found for i, found in enumerate(requests) if i not in out_requests],
            recordings[1] + [found for i, found in enumerate(false_wakes) if i not in out_false_wakes]))
        for clips, held, side in ((requests, out_requests, 0), (false_wakes, out_false_wakes, 1)):
            for i in held:
                best = best_check_score(pipeline, clips[i])
                if best is None:
                    unheard[side] += 1
                    continue
                reached[side] += 1
                for setting in SETTINGS:
                    kept[setting][side] += best >= setting
    for setting in SETTINGS:
        requests_kept, false_wakes_kept = kept[setting]
        show(f"  at {setting}, kept {_share(requests_kept, reached[0], 'requests')} and stopped "
             f"{_share(reached[1] - false_wakes_kept, reached[1], 'false wakes')}")
    if any(unheard):
        show(f"  {unheard[0]} requests and {unheard[1]} false wakes held back never scored {MIN_SCORE} or more "
             f"again, so they're left out")


def _wavs(folder: str) -> List[str]:
    """The WAV files in a folder, in order - none if there's no folder."""
    if not os.path.isdir(folder):
        return []
    return sorted(name for name in os.listdir(folder) if name.endswith(".wav"))


def train(wake_folder: str, labels_path: str, check_path: str, model: str, extract: Callable[[str], Steps],
          rounds: int = ROUNDS, show: Callable[[str], None] = print, fit: Optional[Callable] = None) -> dict:
    """
    Trains the check on the recordings and the labelled clips, says how it does on
    held-back clips, and saves it. Deletes nothing.

    Args:
        wake_folder: The folder holding hey_nova, not_hey_nova and clips.
        labels_path: The labels file.
        check_path: Where the check is saved.
        model: The wake word model's name, saved with the check.
        extract: Scores a file - steps, for the file at a path.
        rounds: How many times clips are held back.
        show: Shows the user a line.
        fit: openWakeWord's train_verifier_model, unless it's a test.

    Returns:
        The check's data, as saved.

    Raises:
        ValueError: A label doesn't make sense, a labelled clip isn't there, or there's
            nothing to learn from.
        OSError: A file couldn't be read, or the check couldn't be saved.
    """
    if fit is None:
        # Imported here, so scikit-learn is only loaded to train.
        from openwakeword.custom_verifier_model import train_verifier_model as fit

    labels = read_labels(labels_path)
    clips_folder = os.path.join(wake_folder, CLIPS_FOLDER)
    clips = _wavs(clips_folder)
    missing = sorted(set(labels) - set(clips))
    if missing:
        raise ValueError(f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} in {labels_path} "
                         f"but not in {clips_folder}")
    hey_nova = _wavs(os.path.join(wake_folder, HEY_NOVA_FOLDER))
    not_hey_nova = _wavs(os.path.join(wake_folder, NOT_HEY_NOVA_FOLDER))
    pos = [name for name in clips if labels.get(name) == "pos"]
    neg = [name for name in clips if labels.get(name) == "neg"]
    dropped = sum(labels.get(name) == "drop" for name in clips)
    unlisted = sum(name not in labels for name in clips)

    show(f"Scoring {len(hey_nova)} recordings of \"Hey Nova\", {len(not_hey_nova)} of other sounds and "
         f"{len(pos) + len(neg)} clips...")
    recordings = ([extract(os.path.join(wake_folder, HEY_NOVA_FOLDER, name)) for name in hey_nova],
                  [extract(os.path.join(wake_folder, NOT_HEY_NOVA_FOLDER, name)) for name in not_hey_nova])
    requests = [extract(os.path.join(clips_folder, name)) for name in pos]
    false_wakes = [extract(os.path.join(clips_folder, name)) for name in neg]

    held_back(recordings, requests, false_wakes, rounds, fit, show)

    pipeline = fit(*training_set(recordings[0] + requests, recordings[1] + false_wakes))
    counts = {HEY_NOVA_FOLDER: len(hey_nova), NOT_HEY_NOVA_FOLDER: len(not_hey_nova), "pos": len(pos),
              "neg": len(neg)}
    data = voice_check.from_pipeline(pipeline, model, counts)
    voice_check.save(data, check_path)
    show(f"Trained on {len(hey_nova)} recordings of \"Hey Nova\", {len(not_hey_nova)} of other sounds, "
         f"{len(pos)} clips marked pos and {len(neg)} marked neg - {dropped} marked drop and {unlisted} not "
         f"listed were left out.")
    show(f"Saved the check as {check_path}. Restart Nova to use it.")
    return data


def _rounds(text: str) -> int:
    """For argparse: a whole number above zero."""
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be above zero")
    return number


def main():
    parser = argparse.ArgumentParser(description="Trains Nova's voice check - see the top of this file.")
    parser.add_argument("labels", help="the labels file, a clip's file name and pos, neg or drop to a line")
    parser.add_argument("--rounds", type=_rounds, default=ROUNDS,
                        help=f"how many times clips are held back to say how it does (default {ROUNDS})")
    arguments = parser.parse_args()

    # Imported here rather than at the top, so --help and the rest can be used and
    # tested on a machine without the Pi's wake word packages.
    from voice_assistant.config import Config
    from voice_assistant.wake_word import WakeWordDetector

    def show(line: str):
        # Over ssh, Python holds output back until a block's worth has built up, which
        # would leave the user waiting to see how far it's got.
        print(line, flush=True)

    # The same wake word model as Nova, so the features are the ones it scores the check on.
    detector = WakeWordDetector(config=Config())
    try:
        train(WAKE_FOLDER, arguments.labels, voice_check.FILE, detector.wake_word_key,
              lambda path: steps(read_wav(path), detector.model, detector.wake_word_key),
              rounds=arguments.rounds, show=show)
    except (OSError, ValueError) as e:
        sys.exit(f"Couldn't train the voice check: {e}")


if __name__ == "__main__":
    main()
