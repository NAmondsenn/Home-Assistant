"""
Dropouts Test
Counting the times the speaker ran dry or the microphone overflowed, and saying so
in the log without flooding it.
"""

import logging

from voice_assistant.dropouts import Dropouts


def test_every_dropout_is_counted_but_logged_at_most_once_a_minute(caplog):
    dropouts = Dropouts("The speaker ran dry")
    with caplog.at_level(logging.WARNING):
        for second in (0, 0.01, 0.02, 30, 61, 62):
            dropouts.note(now=1000 + second)
    assert dropouts.total == 6
    assert [record.getMessage() for record in caplog.records] == [
        "The speaker ran dry - 1 time(s) since Nova started",
        "The speaker ran dry - 5 time(s) since Nova started",
    ]
