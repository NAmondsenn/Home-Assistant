"""
Timeline Module
Records how long each stage of an interaction takes, so a slow response can be
pinned on the stage actually responsible rather than guessed at.

One line of JSON per interaction is appended to latency.jsonl in the logs folder. Each mark is
the number of milliseconds since the wake word (or, for a follow-up question, since
listening started again), and summarise() turns a pile of them into typical (p50)
and bad-day (p95) times for each stage - see scripts/latency_report.py.

Once the file reaches MAX_BYTES it's moved to latency.jsonl.1 and started afresh, as
the logs are, keeping that one old file. It grew for ever on both machines - the
Pi's SD card, and the brain's small disk.
"""

import json
import logging
import os
import statistics
import time
from datetime import datetime
from typing import Dict, List, Optional

# Logger setup
logger = logging.getLogger(__name__)

# How big the file grows before it's started afresh - about 10,000 interactions -
# and how much of it is read at a time from its end, in bytes.
MAX_BYTES = 5_000_000
TAIL_BLOCK = 65536

# The stages reported on, each as the gap between two marks. Where a stage could
# start from more than one mark - speech synthesis starts once the action has run,
# or straight after Claude when there wasn't one - the first mark present is used.
STAGES = (
    ("Wake word to listening", ("wake",), "recording_started"),
    ("Waiting for speech", ("recording_started",), "speech_started"),
    ("End of speech to recording stop", ("speech_ended",), "recording_stopped"),
    ("Speech-to-text", ("recording_stopped",), "stt_done"),
    # Part of speech-to-text, only when it heard nothing in the user talking - see speech_to_text.py.
    ("Hearing it again", ("stt_retry_started",), "stt_retry_done"),
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
        Appends this timeline to the log as one line of JSON - to a new file once it
        has reached MAX_BYTES, with the old one kept as path + ".1".

        Best-effort: a failure here is only logged, since timing an interaction
        must never be the thing which breaks it.

        Args:
            path: The .jsonl file to append to.
        """
        if not self.marks:
            return
        try:
            line = {"at": datetime.now().isoformat(timespec="seconds"), "marks_ms": self.marks, **self.notes}
            if os.path.isfile(path) and os.path.getsize(path) >= MAX_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a") as f:
                f.write(json.dumps(line) + "\n")
        except Exception as e:
            logger.warning(f"Could not save the latency timeline: {e}")


def load(path: str, most: Optional[int] = None) -> List[Dict]:
    """
    Reads the timelines saved in a log, skipping any line which can't be read.

    Args:
        path: The .jsonl file written by Timeline.save.
        most: Only the last this many, read from the end of the file rather than
            the whole of it - or None for all of them.

    Returns:
        The timelines, oldest first.

    Raises:
        OSError: If the file can't be read.
    """
    with open(path, "rb") as f:
        lines = _last_lines(f, most) if most is not None else f.read().splitlines()
    timelines = []
    for line in lines:
        try:
            timelines.append(json.loads(line))
        except ValueError:
            continue
    return timelines


def _last_lines(f, most: int) -> List[bytes]:
    """The last few lines of a file open for reading in binary, read a block at a time from its end."""
    position = f.seek(0, os.SEEK_END)
    read = b""
    while position > 0 and read.count(b"\n") <= most:
        step = min(TAIL_BLOCK, position)
        position -= step
        f.seek(position)
        read = f.read(step) + read
    lines = read.splitlines()
    # Unless it's the start of the file, the first line read may be only the end of one.
    if position > 0:
        lines = lines[1:]
    return lines[-most:] if most else []


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
