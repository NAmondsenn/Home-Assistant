#!/usr/bin/env python3
"""
Wake Word Detector Test
Listens and counts the amount of wake word detections,
until the user stops the test using Ctrl + C.

Useful for tuning the sensitivity, too many missed activations would mean
the sensitivity is too low, too many false positives means it's too high.
"""

import argparse
import os
import sys
import logging
import time

# Add the project root to the path so the voice_assistant package imports work
# no matter where the tests are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_assistant.config import Config
from voice_assistant.wake_word import WakeWordDetector

logger = logging.getLogger(__name__)

def parse_args():
    """CLI args, allows sensitivity to be tuned without editing config.yaml each time."""
    parser = argparse.ArgumentParser(description="Wake word detector test")
    parser.add_argument(
        '--sensitivity',
        type=float,
        default=None,
        help="Override the wake_word_confidence value from config.yaml for this run"
    )
    return parser.parse_args()


def test_wake_word():
    """Runs WakeWordDetector in a loop, counting and timing detections until interrupted."""

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    args = parse_args()

    print("\n" + "=" * 60)
    print("WAKE WORD DETECTOR TEST")
    print("=" * 60 + "\n")

    # Load config
    print("Loading configuration...")
    config = Config()

    if args.sensitivity is not None:
        print(f"Overriding config sensitivity with CLI value: {args.sensitivity}")

    print("Initialising wake word detector...")
    # WakeWordDetector pulls its wake word phrase / model and default sensitivity from config
    detector = WakeWordDetector(config=config, sensitivity=args.sensitivity)
    print(f"Wake word detector ready (sensitivity={detector.sensitivity})\n")

    print(f"Say '{detector.wake_word_phrase}' as many times as you like.")
    print("Press Ctrl + C to exit to summary.\n")

    detection_times = []
    confidences = []
    consecutive_errors = 0
    start_time = time.time()

    try:
        while True:
            try:
                result = detector.listen_once()
                consecutive_errors = 0
            except Exception:
                # Don't let one bad frame kill the entire test, but give up after
                # repeated failures in a row - that means there is no working
                # microphone rather than a transient glitch.
                consecutive_errors += 1
                logger.exception("listen_once() raised an error, continuing test")
                if consecutive_errors >= 3:
                    print("\nlisten_once() failed 3 times in a row - is a microphone connected?")
                    print("Aborting to summary.")
                    break
                continue

            detection_times.append(time.time())
            count = len(detection_times)
            elapsed = detection_times[-1] - start_time

            # listen_once() may return a confidence score, or nothing useful - handles both
            confidence = result if isinstance(result, (int, float)) else None
            if confidence is not None:
                confidences.append(confidence)
                print(f"Detected: ({count} total, at {elapsed:.1f}s, confidence={confidence:.2f})")
            else:
                print(f"Detected: ({count} total, at {elapsed:.1f}s)")
    except KeyboardInterrupt:
        pass

    # Summary
    total_time = time.time() - start_time
    count = len(detection_times)

    print("\n" + "=" * 60)
    print("WAKE WORD TEST SUMMARY")
    print("=" * 60)
    print(f"Total detections: {count}")
    print(f"Total listening time: {total_time:.1f}s")

    if count > 1:
        # Finds the average time between detections if there is more than one.
        # This could help identify unusually fast repeat triggers.
        gaps = [detection_times[i] - detection_times[i - 1] for i in range(1, count)]
        avg_gap = sum(gaps) / len(gaps)
        print(f"Average time between detections: {avg_gap:.1f}s")

    if confidences:
        avg_confidence = sum(confidences) / len(confidences)
        print(f"Average detection confidence: {avg_confidence:.2f}")
        print(f"Lowest detection confidence: {min(confidences):.2f}")

if __name__ == "__main__":
    test_wake_word()
