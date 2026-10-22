"""Tests for recording until the user stops, from a feed of audio."""

import threading
import time

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


def test_a_recording_follows_audio_fed_from_another_thread():
    feed = AudioFeed()

    def feeder():
        for value in [2000] * 5 + [0] * 10:
            feed.feed(seconds(0.1, value))
            time.sleep(0.01)

    thread = threading.Thread(target=feeder)
    thread.start()
    audio = record(feed, listen_from=0)
    thread.join()

    assert 0.5 * RATE < len(audio) < 1.5 * RATE
    assert np.all(audio[:int(0.4 * RATE)] > 0.05)


def test_end_wakes_a_reader_already_waiting():
    feed = AudioFeed()
    feed.feed(seconds(0.5, 2000))
    threading.Timer(0.2, feed.end).start()

    started = time.monotonic()
    audio = record(feed, listen_from=0)

    assert time.monotonic() - started < 1.5
    assert len(audio) == 0.5 * RATE // FRAME * FRAME


def test_a_feed_which_stalls_counts_as_ended():
    feed = AudioFeed(stall_seconds=0.2)
    feed.feed(seconds(0.5, 2000))

    started = time.monotonic()
    audio = record(feed, listen_from=0)

    assert time.monotonic() - started < 1.0
    assert len(audio) == 0.5 * RATE // FRAME * FRAME
