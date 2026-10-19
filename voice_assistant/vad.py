"""
Voice Activity Module.

Tells the recorder how likely each moment of audio is to be speech, so it knows when
the user has started talking, and when they've finished.
"""

import logging

import numpy as np

# Logger setup
logger = logging.getLogger(__name__)


class LoudnessDetector:
    """
    Counts anything louder than a fixed threshold as speech, and anything quieter as
    silence.
    """

    # It only ever answers 1 or 0, so anything in between would do.
    threshold = 0.5

    def __init__(self, silence_threshold: float = 0.05):
        """
        Args:
            silence_threshold: Volume, as root mean square from 0 to 1, below which
                audio counts as silence.
        """
        self.silence_threshold = silence_threshold

    def reset(self):
        """Nothing is carried over from one frame to the next, so there's nothing to forget."""

    def speech_probability(self, frame: np.ndarray) -> float:
        """
        Args:
            frame: 16-bit samples at 16kHz.

        Returns:
            1.0 if the frame is loud enough to be speech, otherwise 0.0.
        """
        rms = np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2))
        return 1.0 if rms >= self.silence_threshold else 0.0


def make_detector(config=None):
    """
    Builds the speech detector the recorder uses.

    Args:
        config: Configs loaded from config.yaml, for the thresholds.

    Returns:
        A detector - see LoudnessDetector for what it offers.
    """
    thresholds = config.section("thresholds") if config else {}
    return LoudnessDetector(silence_threshold=thresholds.get("silence_threshold", 0.05))
