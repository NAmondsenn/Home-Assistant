"""Tests for recording until the user stops, from a feed of audio."""

import numpy as np

from voice_assistant.recording import FRAME, RATE, AudioFeed, record_until_silence


class FakeDetector:
    """Calls a frame speech if it's loud, which the tests make it or not."""

    threshold = 0.5

    def reset(self):
        pass

    def speech_probability(self, frame):
        return 1.0 if np.abs(frame).mean() > 1000 else 0.0


def seconds(length, value):
    return np.full(int(length * RATE) // FRAME * FRAME, value, dtype=np.int16)


def record(feed, **kwargs):
    settings = dict(silence_duration=0.6, speech_timeout=0.9, timeout=5.0)
    settings.update(kwargs)
    return record_until_silence(feed, FakeDetector(), **settings)


def test_a_fed_recording_stops_once_the_user_goes_quiet():
    feed = AudioFeed()
    feed.feed(seconds(0.5, 2000))
    feed.feed(seconds(1.0, 0))

    audio = record(feed, listen_from=0)

    assert abs(len(audio) / RATE - 1.1) < 0.05


def test_a_feed_which_ends_part_way_gives_what_was_heard():
    feed = AudioFeed()
    feed.feed(seconds(0.5, 2000))
    feed.feed(np.full(100, 2000, dtype=np.int16))
    feed.end()

    audio = record(feed, listen_from=0)

    assert len(audio) == 0.5 * RATE // FRAME * FRAME + 100
    assert np.all(audio > 0.05)


def test_a_feed_with_no_speech_gives_nothing():
    feed = AudioFeed()
    feed.feed(seconds(2.0, 0))

    audio = record(feed, listen_from=0)

    assert len(audio) == 0
    assert audio.dtype == np.float32
