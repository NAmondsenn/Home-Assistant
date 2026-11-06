"""Tests for recording until the user stops, from a feed of audio."""

import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

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


def test_time_at_counts_back_from_the_piece_a_position_is_in(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(recording, "time", SimpleNamespace(monotonic=lambda: now[0]))
    feed = AudioFeed()
    # With nothing fed there's no arrival to go by, so it's now.
    assert feed.time_at(0) == 100.0

    now[0] = 101.0
    feed.feed(np.zeros(8000, dtype=np.int16))
    now[0] = 102.0
    feed.feed(np.zeros(8000, dtype=np.int16))

    # The whole of a piece was heard before it was sent, so a position in it is counted back from its arrival.
    assert feed.time_at(8000) == 101.0
    assert feed.time_at(4000) == pytest.approx(100.75)
    assert feed.time_at(12000) == pytest.approx(101.75)
    # One not yet fed, such as a mark made as the feed ends, goes by the last arrival.
    assert feed.time_at(20000) == 102.0


def test_a_feed_hands_the_recording_over_as_its_made():
    feed = AudioFeed()
    feed.feed(seconds(0.3, 0))
    feed.feed(seconds(0.6, 2000))
    feed.feed(seconds(1.0, 0))
    heard = []

    audio = record(feed, listen_from=0, heard=heard.append)

    # Everything up to the moment the user was found to be talking, then each frame as it
    # comes - adding up to exactly the recording.
    assert len(heard[0]) > FRAME and all(len(piece) == FRAME for piece in heard[1:])
    assert np.array_equal(np.concatenate(heard), audio)

    # And nothing at all when they never talk.
    silent = AudioFeed()
    silent.feed(seconds(2.0, 0))
    nothing = []
    assert len(record(silent, listen_from=0, heard=nothing.append)) == 0
    assert nothing == []


def test_a_recording_from_a_feed_marks_the_timeline_by_when_the_audio_arrived(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(recording, "time", SimpleNamespace(monotonic=lambda: now[0]))
    marks = {}

    class Timeline:
        def mark(self, stage, at=None):
            marks[stage] = at

    # Quiet, then speech which starts 0.1s before the end of the first piece, and stops at the end of the second.
    feed = AudioFeed()
    feed.feed(np.concatenate([seconds(0.3, 0), np.full(1600, 2000, dtype=np.int16)]))
    now[0] = 11.0
    feed.feed(np.full(8000, 2000, dtype=np.int16))
    now[0] = 12.0
    feed.feed(seconds(1.0, 0))

    record(feed, listen_from=0, timeline=Timeline())

    assert set(marks) == {"recording_started", "speech_started", "speech_ended", "recording_stopped"}
    assert marks["speech_started"] == pytest.approx(9.9)
    assert marks["speech_ended"] == pytest.approx(11.0)
