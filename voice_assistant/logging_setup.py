"""
Logging Setup Module.

Where the assistant's logs go, and whether what's said is kept in them.

Everything used to go to one file which was never trimmed, so it grew without limit
on the SD card, and every word said to the assistant - and every reply - was kept
in it for good, as well as in the system journal.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import List

# Passed as `extra` on any log line holding what was said, or a reply, so those
# lines can be left out - see log_transcripts.
TRANSCRIPT = {"transcript": True}

# A log file is started afresh once it reaches this size, keeping this many old ones.
MAX_LOG_BYTES = 5_000_000
OLD_LOGS_KEPT = 3


class _TranscriptFilter(logging.Filter):
    """Leaves out log lines marked TRANSCRIPT while transcripts are switched off."""

    def __init__(self):
        super().__init__()
        self.keep_transcripts = True

    def filter(self, record: logging.LogRecord) -> bool:
        return self.keep_transcripts or not getattr(record, "transcript", False)


_transcripts = _TranscriptFilter()


def setup_logging(log_file: str) -> List[logging.Handler]:
    """
    Sends log lines to a file which is trimmed as it grows, and to the console - the
    system journal, when running as a service.

    Args:
        log_file: Path to the log file. Its folder is created if need be.

    Returns:
        The handlers added, so they can be taken off again.
    """
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handlers = [RotatingFileHandler(log_file, maxBytes=MAX_LOG_BYTES, backupCount=OLD_LOGS_KEPT),
                logging.StreamHandler()]

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_transcripts)
        root.addHandler(handler)
    return handlers


def log_transcripts(enabled: bool):
    """
    Whether what's said to the assistant, and its replies, are kept in the logs -
    logging.log_transcripts in config.yaml.

    Args:
        enabled: False to leave them out.
    """
    _transcripts.keep_transcripts = bool(enabled)


def transcripts_logged() -> bool:
    """Whether what's said is being kept in the logs - see log_transcripts."""
    return _transcripts.keep_transcripts
