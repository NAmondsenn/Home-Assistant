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
import yaml

# Add voice_assistant to path
sys.path.insert(0, os.path.expanduser('~/voice_assistant'))

from wake_word import WakeWordDetector

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


def load_config():
    """Loads config.yaml, with a clean message output."""
    config_path = os.path.expanduser('~/config.yaml')
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Config file not found at {config_path}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Config file is broken YAML: {e}")
        sys.exit(1)


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
    config = load_config()

    assistant_config = config.get('assistant', {})
    wake_word = assistant_config.get('wake_word', 'Hey Assistant')

    thresholds = config.get('thresholds', {})
    sensitivity = args.sensitivity if args.sensitivity is not None else thresholds.get('wake_word_confidence', 0.5)

    if args.sensitivity is not None:
        print(f"Overriding config sensitivity with CLI value: {sensitivity}")

    print(f"Initialising wake word detector (sensitivity={sensitivity})...")
    detector = WakeWordDetector(sensitivity=sensitivity)
    print("Wake word detector ready\n")

    print(f"Say '{wake_word}' as many times as you like.")
    print("Press Ctrl + C to exit to summary.\n")

    detection_times = []
    confidences = []
    start_time = time.time()

    try:
        while True:
            try:
                result = detector.listen_once()
            except Exception:
                # Don't let one bad frame kill the entire test.
                logger.exception("listen_once() raised an error, continuing test")
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