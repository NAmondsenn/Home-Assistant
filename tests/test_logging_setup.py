"""
Logging Setup Test
Checks the log file is trimmed as it grows, and that what's said can be kept out of
the logs entirely.
"""

import logging
from logging.handlers import RotatingFileHandler

import pytest

from voice_assistant import logging_setup
from voice_assistant.logging_setup import TRANSCRIPT


@pytest.fixture
def logs(tmp_path):
    """Sets logging up into a temporary folder, and takes it down again afterwards."""
    handlers = logging_setup.setup_logging(str(tmp_path / "logs" / "assistant.log"))
    yield handlers, tmp_path / "logs" / "assistant.log"
    for handler in handlers:
        logging.getLogger().removeHandler(handler)
        handler.close()
    logging_setup.log_transcripts(True)


def test_the_log_file_is_trimmed_as_it_grows(logs):
    # It used to be a plain FileHandler, which grew without limit on the SD card.
    handlers, _ = logs
    rotating = [h for h in handlers if isinstance(h, RotatingFileHandler)]
    assert rotating and rotating[0].maxBytes == logging_setup.MAX_LOG_BYTES
    assert rotating[0].backupCount == logging_setup.OLD_LOGS_KEPT


def test_what_was_said_can_be_kept_out_of_the_logs(logs):
    handlers, path = logs
    log = logging.getLogger("voice_assistant.test")

    logging_setup.log_transcripts(False)
    log.info("Processing query: 'what time is it'", extra=TRANSCRIPT)
    log.info("Wake word detected")
    logging_setup.log_transcripts(True)
    log.info("Processing query: 'pause the music'", extra=TRANSCRIPT)
    for handler in handlers:
        handler.flush()

    written = path.read_text()
    assert "what time is it" not in written
    assert "Wake word detected" in written and "pause the music" in written
