"""
Timeline Module
Records how long each stage of an interaction takes, so a slow response can be
pinned on the stage actually responsible rather than guessed at.

One line of JSON per interaction is appended to latency.jsonl in the logs folder. Each mark is
the number of milliseconds since the wake word (or, for a follow-up question, since
listening started again), and summarise() turns a pile of them into typical (p50)
and bad-day (p95) times for each stage - see scripts/latency_report.py.
"""

import json
import logging
import statistics
import time
from datetime import datetime
from typing import Dict, List, Optional

# Logger setup
logger = logging.getLogger(__name__)

# The stages reported on, each as the gap between two marks. Where a stage could
# start from more than one mark - speech synthesis starts once the action has run,
# or straight after Claude when there wasn't one - the first mark present is used.
STAGES = (
    ("Wake word to listening", ("wake",), "recording_started"),
    ("Waiting for speech", ("recording_started",), "speech_started"),
    ("End of speech to recording stop", ("speech_ended",), "recording_stopped"),
    ("Speech-to-text", ("recording_stopped",), "stt_done"),
    ("Claude", ("stt_done",), "llm_done"),
    ("Action", ("llm_done",), "action_done"),
    # Speech plays as each sentence is ready, so this is how long until the first one is.
    ("Text-to-speech", ("action_done", "llm_done"), "tts_done"),
    ("Playback", ("tts_done",), "playback_done"),
    # The one which matters most: from the user going quiet to the reply starting to play.
    ("End of speech to reply ready", ("speech_ended",), "tts_done"),
)


class Timeline:
    """The moments one interaction reached each stage, plus a few notes about it."""

    def __init__(self, **notes):
        """
        Args:
            notes: Details to keep alongside the times, e.g. follow_up=True.
        """
        self.started = time.monotonic()
        self.marks: Dict[str, int] = {}
        self.notes = dict(notes)

    def mark(self, stage: str, at: Optional[float] = None):
        """
        Notes that a stage has been reached.

        Args:
            stage: Its name, e.g. "stt_done".
            at: When it happened, as a time.monotonic() value. Defaults to now - only
                given for a moment which has already passed, like the user's last word.
        """
        moment = time.monotonic() if at is None else at
        self.marks[stage] = round((moment - self.started) * 1000)

    def note(self, **notes):
        """Adds details which help explain the times, e.g. how long the reply was."""
        self.notes.update(notes)

    def save(self, path: str):
        """
        Appends this timeline to the log as one line of JSON.

        Best-effort: a failure here is only logged, since timing an interaction
        must never be the thing which breaks it.

        Args:
            path: The .jsonl file to append to.
        """
        if not self.marks:
            return
        try:
            line = {"at": datetime.now().isoformat(timespec="seconds"), "marks_ms": self.marks, **self.notes}
            with open(path, "a") as f:
                f.write(json.dumps(line) + "\n")
        except Exception as e:
            logger.warning(f"Could not save the latency timeline: {e}")


def load(path: str) -> List[Dict]:
    """
    Reads every timeline saved in a log, skipping any line which can't be read.

    Args:
        path: The .jsonl file written by Timeline.save.

    Returns:
        The timelines, oldest first.
    """
    timelines = []
    with open(path) as f:
        for line in f:
            try:
                timelines.append(json.loads(line))
            except ValueError:
                continue
    return timelines


def summarise(timelines: List[Dict]) -> List[Dict]:
    """
    Works out how long each stage takes across many interactions.

    Args:
        timelines: Saved timelines, as returned by load().

    Returns:
        One dict per stage which appears at least once, with its 'stage' name, the
        number of interactions it was seen in ('count'), and its 'p50', 'p95' and
        'max' times in milliseconds.
    """
    rows = []
    for name, starts, end in STAGES:
        gaps = []
        for timeline in timelines:
            marks = timeline.get("marks_ms", {})
            start = next((marks[s] for s in starts if s in marks), None)
            if start is not None and end in marks:
                gaps.append(marks[end] - start)
        if not gaps:
            continue

        gaps.sort()
        # quantiles needs at least two values; with one, every percentile is that value.
        p95 = statistics.quantiles(gaps, n=20, method="inclusive")[18] if len(gaps) > 1 else gaps[0]
        rows.append({"stage": name, "count": len(gaps), "p50": round(statistics.median(gaps)),
                     "p95": round(p95), "max": gaps[-1]})
    return rows
