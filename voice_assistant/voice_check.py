"""
Voice Check Module.

Tells the user saying "Hey Nova" from anything else which sounds like it - a film,
mostly - so the satellite wakes only for them. It's a logistic regression on the wake
word model's own features for the moment it fired, trained on the satellite by
scripts/train_voice_check.py from the user's recordings and wake clips: the same
regression openWakeWord's custom verifier trains, but scored here as well as the wake
word rather than in place of it.

It's kept as plain numbers in JSON, not as the pickle openWakeWord saves, since loading
a pickle runs whatever code is in it. And it's a model of the user's voice, so it lives
in the data folder, readable by this user only - never in the repository, which is public.

A missing check means none, and Nova wakes on the wake word alone, as before there was
one. So does a check which won't load, with a warning: the user shut out by a broken
file is worse than a film waking Nova.
"""

import datetime
import logging
import math
import os
from typing import Dict, Optional

import numpy as np

from voice_assistant import paths, storage

logger = logging.getLogger(__name__)

FILE = os.path.join(paths.DATA_DIR, "voice_check.json")

VERSION = 1

# The wake word model Nova's own wake word has - as wake_word.py names it - and how many
# frames of features it judges each moment on.
DEFAULT_MODEL = "hey_nova"
DEFAULT_FRAMES = 16

# How many numbers openWakeWord's features have for each frame, whatever the wake word.
# A fitted pipeline only knows how many there are in all, so this splits them into frames.
FEATURES = 96


class VoiceCheck:
    """A trained check, scoring how sure it is a moment's features are the user's voice."""

    def __init__(self, mean: np.ndarray, scale: np.ndarray, weights: np.ndarray, bias: float):
        """
        Args:
            mean: The scaler's mean of each feature, flattened row by row.
            scale: The scaler's scale of each, every one above 0.
            weights: The regression's weight for each.
            bias: The regression's intercept.
        """
        self.mean = np.asarray(mean, dtype=np.float64)
        self.scale = np.asarray(scale, dtype=np.float64)
        self.weights = np.asarray(weights, dtype=np.float64)
        self.bias = float(bias)
        # Whether the last score wasn't a number, so a run of them is warned of once.
        self._broken = False

    def score(self, features: np.ndarray) -> float:
        """
        How sure the check is that a moment is the user's voice - the number
        scikit-learn's predict_proba gives for the pipeline it was trained as.

        Args:
            features: The wake word model's features for the moment, as openWakeWord's
                get_features gives them - shaped (1, 16, 96) for hey_nova.

        Returns:
            From 0 to 1 - and 1, so it passes, if the numbers overflow, as the
            user shut out is worse than a film waking Nova.

        Raises:
            ValueError: The features aren't as many as the check was trained on.
        """
        x = np.asarray(features, dtype=np.float64).reshape(-1)
        if x.size != self.weights.size:
            raise ValueError(f"{x.size} features, but the voice check was trained on {self.weights.size}")
        # Finite numbers can still overflow - a scale of 1e-320 makes any feature
        # infinite - which is told apart below, so numpy needn't warn of it as well.
        with np.errstate(all="ignore"):
            z = float(np.dot(self.weights, (x - self.mean) / self.scale)) + self.bias
        if not math.isfinite(z):
            if not self._broken:
                logger.warning("The voice check's numbers overflowed, so it's letting the wake word through "
                               "until they don't")
            self._broken = True
            return 1.0
        self._broken = False
        # math.exp overflows past about 709, so it's only ever given a z at or under 0 - a
        # moment that far from the user's voice, or that close to it, is simply 0 or 1.
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        e = math.exp(z)
        return e / (1.0 + e)


def _is_number(value) -> bool:
    # True and False are ints to Python, but never a number anyone meant to save.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _numbers(data: dict, key: str, length: int) -> np.ndarray:
    """One of the check's lists of numbers, which must be finite and as long as the features."""
    values = data.get(key)
    if not isinstance(values, list) or len(values) != length:
        raise ValueError(f"'{key}' isn't a list of {length} numbers")
    if not all(_is_number(value) for value in values):
        raise ValueError(f"'{key}' has something in it which isn't a number")
    array = np.array(values, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        raise ValueError(f"'{key}' has a number in it which isn't finite")
    return array


def _from_data(data, model: str, window: int) -> VoiceCheck:
    """
    A check from what its file holds, if every part of it is as it should be.

    Raises:
        ValueError: Saying what isn't.
    """
    if not isinstance(data, dict):
        raise ValueError("it isn't a JSON object")
    if not _is_number(data.get("version")) or data.get("version") != VERSION:
        raise ValueError(f"it's version {data.get('version')!r}, not {VERSION}")
    if data.get("model") != model:
        # Another wake word's model has other features - scoring them would mean nothing.
        raise ValueError(f"it's for the {data.get('model')!r} wake word model, not {model!r}")
    frames, features = data.get("frames"), data.get("features")
    if not _is_count(frames) or not _is_count(features):
        raise ValueError("its 'frames' and 'features' aren't both whole numbers above 0")
    if (frames, features) != (window, FEATURES):
        # It would load, and then fail on every moment the wake model scored.
        raise ValueError(f"it's for {frames} frames of {features} features, not the wake word model's "
                         f"{window} of {FEATURES}")
    length = frames * features
    mean = _numbers(data, "mean", length)
    scale = _numbers(data, "scale", length)
    weights = _numbers(data, "weights", length)
    # scikit-learn's scaler never has a scale of 0 or under, and one would divide by 0.
    if not np.all(scale > 0):
        raise ValueError("'scale' has a number in it which isn't above 0")
    bias = data.get("bias")
    if not _is_number(bias) or not math.isfinite(bias):
        raise ValueError("'bias' isn't a finite number")
    return VoiceCheck(mean, scale, weights, bias)


def load(path: str, model: str = DEFAULT_MODEL, frames: int = DEFAULT_FRAMES) -> Optional[VoiceCheck]:
    """
    Loads the trained check. Never raises: anything wrong means no check, so Nova
    wakes on the wake word alone rather than not at all.

    Args:
        path: Where the check is saved - FILE, unless it's a test.
        model: The wake word model listened with, which the check must have been
            trained on.
        frames: How many frames of features that model judges each moment on - its
            model_inputs in openWakeWord.

    Returns:
        The check, or None if there isn't one, or it won't load - which logs a warning.
    """
    missing = object()
    try:
        # A file of JSON null is broken, not missing, so it mustn't look like no file.
        data = storage.load_json(path, default=missing)
        if data is missing:
            return None
        return _from_data(data, model, frames)
    except Exception as e:
        # Anything at all, since it's loaded as the satellite starts and must never stop
        # it - a number too big for a float raises OverflowError, and JSON nested too
        # deeply RecursionError.
        logger.warning(f"Couldn't use the voice check in {path}, so any voice saying the wake word "
                       f"wakes Nova: {e}")
        return None


def from_pipeline(pipeline, model: str, counts: Dict[str, int]) -> dict:
    """
    What to save of a pipeline trained by openWakeWord's train_verifier_model - a
    flattening of the features, a StandardScaler, then a LogisticRegression - as plain
    numbers.

    Args:
        pipeline: The fitted pipeline, trained with 1 for the user's voice and 0 for not.
        model: The wake word model whose features it was trained on.
        counts: How many recordings and clips of each kind it was trained on, to say in
            the file.

    Returns:
        The check's data, for save.

    Raises:
        ValueError: It wasn't trained on 0 and 1, or on whole frames of features.
    """
    scaler, regression = pipeline[-2], pipeline[-1]
    # predict_proba's second column is the second class, which has to be the user's voice.
    if [int(label) for label in regression.classes_] != [0, 1]:
        raise ValueError(f"it was trained on {list(regression.classes_)}, not 0 and 1")
    weights = regression.coef_[0]
    if len(weights) % FEATURES:
        raise ValueError(f"it was trained on {len(weights)} features, which isn't frames of {FEATURES}")
    return {
        "version": VERSION,
        "model": model,
        "frames": len(weights) // FEATURES,
        "features": FEATURES,
        "mean": [float(value) for value in scaler.mean_],
        "scale": [float(value) for value in scaler.scale_],
        "weights": [float(value) for value in weights],
        "bias": float(regression.intercept_[0]),
        "trained": datetime.date.today().isoformat(),
        "counts": dict(counts),
    }


def save(data: dict, path: str):
    """
    Saves a check atomically, readable by this user only.

    Args:
        data: The check's data, from from_pipeline.
        path: Where it goes - FILE, unless it's a test.

    Raises:
        ValueError: It wouldn't load - nothing is written, so the last check, if any, stays.
        OSError: It couldn't be written, and the last check, if any, is left as it was.
    """
    _from_data(data, data.get("model"), data.get("frames"))
    storage.save_json(path, data)
