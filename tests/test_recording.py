"""Tests for recording until the user stops, from a feed of audio."""

import logging
import threading
import time

import numpy as np

from voice_assistant import config, protocol, recording
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


def test_a_feed_drops_what_comes_past_its_cap(caplog):
    # A sender which doesn't stop mustn't grow the feed for ever - the satellite's own
    # limit is the only other guard.
    feed = AudioFeed(max_seconds=1.0)
    with caplog.at_level(logging.WARNING, logger=recording.__name__):
        feed.feed(np.full(int(0.6 * RATE), 1, dtype=np.int16))
        feed.feed(np.full(int(0.6 * RATE), 2, dtype=np.int16))
        feed.feed(np.full(int(0.6 * RATE), 3, dtype=np.int16))
    feed.end()

    assert feed.position == RATE
    kept = feed.listen(start=0).read(2 * RATE)
    assert len(kept) == RATE
    # The piece which crossed the cap keeps what fits, and the one after it is dropped.
    assert (kept[:int(0.6 * RATE)] == 1).all() and (kept[int(0.6 * RATE):] == 2).all()
    # Said once, not for each piece.
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_the_cap_leaves_room_for_the_longest_request_and_its_pre_roll():
    # A feed holds a request which is the longest the settings allow, with the most
    # pre-roll a wake may say is at its start (in samples: a message's audio is 16-bit).
    longest = config.LIMITS[("thresholds", "recording_timeout")].high
    assert recording.MAX_FEED_SECONDS >= longest + protocol.MAX_AUDIO // 2 / RATE


def test_audio_fed_after_the_feed_ended_is_ignored():
    feed = AudioFeed()
    feed.feed(np.full(1600, 1, dtype=np.int16))
    feed.end()
    feed.feed(np.full(1600, 2, dtype=np.int16))

    assert feed.position == 1600
    assert (feed.listen(start=0).read(3200) == 1).all()


def test_audio_fed_after_the_feed_stalled_is_ignored():
    # A stalled feed counts as ended, and a reader has already been told so.
    feed = AudioFeed(stall_seconds=0.05)
    feed.feed(np.full(1600, 1, dtype=np.int16))
    reader = feed.listen(start=0)
    assert len(reader.read(3200)) == 1600

    feed.feed(np.full(1600, 2, dtype=np.int16))

    assert feed.position == 1600
