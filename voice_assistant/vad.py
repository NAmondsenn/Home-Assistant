"""
Voice Activity Module.

Tells the recorder how likely each moment of audio is to be speech, so it knows when
the user has started talking, and when they've finished.

Loudness alone used to decide this, against a fixed threshold (silence_threshold),
which can't tell a voice from any other sound: a TV, a fan or music in the room
kept a recording going to its 10 second cap, while someone speaking quietly could
be cut off mid-sentence. Silero's voice activity model listens for speech itself,
whatever else is going on.
"""

import logging

import numpy as np

# Logger setup
logger = logging.getLogger(__name__)


class SileroDetector:
    """
    Silero's voice activity model - the copy openWakeWord downloads alongside its
    own models (see setup.sh), so it needs nothing installed beyond what the wake
    word does.
    """

    def __init__(self, threshold: float = 0.5):
        """
        Args:
            threshold: How sure the model has to be that a frame is speech, from 0 to 1.

        Raises:
            Exception: If the model can't be loaded - see make_detector.
        """
        # Imported here rather than at the top, so that with openWakeWord or the
        # model missing, make_detector can fall back to loudness rather than the
        # whole assistant failing to start.
        from openwakeword.vad import VAD
        self.model = VAD()
        self.threshold = threshold

    def reset(self):
        """Forgets the audio heard so far, ready for a new recording."""
        self.model.reset_states()

    def speech_probability(self, frame: np.ndarray) -> float:
        """
        Args:
            frame: 16-bit samples at 16kHz - 30ms of them suits the model best.

        Returns:
            How likely the frame is to be speech, from 0 to 1.
        """
        return float(self.model.predict(frame, frame_size=len(frame)))


class LoudnessDetector:
    """
    The old way of telling, kept for when Silero can't be loaded: anything louder
    than a fixed threshold counts as speech, and anything quieter as silence.
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
    Builds the speech detector the recorder uses: Silero if it can be loaded,
    otherwise loudness.

    Args:
        config: Configs loaded from config.yaml, for the thresholds.

    Returns:
        A detector - see SileroDetector for what it offers.
    """
    thresholds = config.section("thresholds") if config else {}
    try:
        detector = SileroDetector(threshold=thresholds.get("vad_threshold", 0.5))
    except Exception as e:
        logger.warning(f"Silero VAD unavailable, telling speech apart by loudness instead: {e}")
        return LoudnessDetector(silence_threshold=thresholds.get("silence_threshold", 0.05))

    logger.info("Telling speech apart with Silero VAD")
    return detector
