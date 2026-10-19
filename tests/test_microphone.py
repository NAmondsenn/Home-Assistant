"""
Microphone Test
Checks the shared microphone stream - each reader keeping its own place, getting
going again after the microphone goes quiet, and when a recording should stop -
using a fake in place of the real input stream, so no sound card is needed. Needs
numpy and soxr installed (as they are on the Pi), and is skipped anywhere they aren't.
"""

import threading
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import microphone as microphone_module
from voice_assistant.microphone import FRAME, RATE, Microphone


class FakeInputStream:
    """
    Stands in for sounddevice.InputStream. Captures nothing by itself - the test
    hands it audio through feed(), which calls back just as PortAudio would.
    """

    opened = []
    # How many of the next attempts to open a stream should fail, as they do while
    # the microphone is unplugged.
    failures = 0

    def __init__(self, callback, **settings):
        if FakeInputStream.failures:
            FakeInputStream.failures -= 1
            raise OSError("No such device")
        self.callback = callback
        self.settings = settings
        self.closed = False
        FakeInputStream.opened.append(self)

    def start(self):
        pass

    def close(self, ignore_errors=False):
        self.closed = True

    def feed(self, samples):
        """Delivers 16-bit sample values as one captured block."""
        self.callback(as_captured(samples).reshape(-1, 1), len(samples), None, None)


def as_captured(samples):
    """
    16-bit sample values as the float32 audio a stream captures, nudged so they
    come back out of the conversion to 16-bit exactly.
    """
    return (np.asarray(samples, dtype=np.float32) + 0.5) / 32767


def feed_frames(stream, frames, value):
    """Delivers some 30ms frames of one steady sample value."""
    for _ in range(frames):
        stream.feed(np.full(FRAME, value))


@pytest.fixture
def streams(monkeypatch):
    FakeInputStream.opened = []
    FakeInputStream.failures = 0
    monkeypatch.setattr(microphone_module.sd, "InputStream", FakeInputStream)
    return FakeInputStream.opened


def wait_for(condition, timeout=2.0):
    """Waits for something another thread is doing to happen."""
    give_up_at = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < give_up_at, "timed out waiting"
        time.sleep(0.005)


# Reading

def test_readers_each_keep_their_own_place(streams):
    microphone = Microphone(sample_rate=RATE)
    first = microphone.listen()
    streams[0].feed(np.arange(0, 480))
    second = microphone.listen()
    streams[0].feed(np.arange(480, 960))

    assert list(first.read(960)) == list(range(0, 960))
    assert list(second.read(480)) == list(range(480, 960))


def test_audio_heard_before_now_can_still_be_read(streams):
    microphone = Microphone(sample_rate=RATE)
    streams[0].feed(np.arange(0, 1000))

    reader = microphone.listen(start=microphone.position - 300)
    assert list(reader.read(300)) == list(range(700, 1000))


def test_reading_waits_for_audio_not_yet_heard(streams):
    microphone = Microphone(sample_rate=RATE)
    reader = microphone.listen()
    threading.Timer(0.05, streams[0].feed, args=(np.arange(480),)).start()

    assert list(reader.read(480)) == list(range(480))


def test_the_buffer_wraps_around_without_mixing_anything_up(streams):
    microphone = Microphone(sample_rate=RATE, buffer_seconds=0.1)  # 1600 samples
    reader = microphone.listen()
    streams[0].feed(np.arange(0, 1000))
    assert list(reader.read(1000)) == list(range(0, 1000))

    streams[0].feed(np.arange(1000, 2000))
    assert list(reader.read(1000)) == list(range(1000, 2000))


def test_a_reader_left_too_far_behind_skips_to_the_oldest_audio(streams):
    microphone = Microphone(sample_rate=RATE, buffer_seconds=0.1)  # 1600 samples
    reader = microphone.listen()
    for start in range(0, 3000, 500):
        streams[0].feed(np.arange(start, start + 500))

    assert list(reader.read(100)) == list(range(1400, 1500))
    assert reader.position == 1500


def test_the_input_buffer_holds_more_than_one_burst_from_the_pi(streams):
    # The Pi's shared input hands audio over 125ms at a time. A buffer of just that
    # overflowed at the slightest delay, losing about a fifth of everything heard.
    Microphone(sample_rate=48000)
    assert streams[0].settings["latency"] >= 0.25


def test_captured_audio_is_kept_at_16khz(streams):
    microphone = Microphone(sample_rate=48000)
    # A second of audio at 48kHz, in the 30ms blocks the stream delivers.
    for _ in range(round(1 / 0.03)):
        streams[0].feed(np.full(1440, 1000))

    # The resampler holds a few milliseconds back until it hears what follows.
    assert 15500 < microphone.position <= 16000


def test_full_scale_audio_is_clipped_rather_than_wrapping_around(streams):
    # Resampling a jump from full scale negative to full scale positive overshoots
    # past 1.0, and converting that to 16-bit without clipping wrapped it round to
    # the opposite extreme - a loud click in what the wake word model was hearing.
    microphone = Microphone(sample_rate=48000)
    step = np.concatenate([np.full(24000, -1.0), np.full(24000, 1.0)]).astype(np.float32)
    for start in range(0, len(step), 1440):
        streams[0].callback(step[start:start + 1440].reshape(-1, 1), 1440, None, None)

    heard = microphone.listen(start=0).read(microphone.position)
    first_positive = int(np.argmax(heard > 0))
    assert np.all(heard[:first_positive] <= 0) and np.all(heard[first_positive:] > 0)


# Recovering

def test_a_microphone_which_goes_quiet_is_reopened(streams):
    # Unplugging the microphone used to leave the assistant deaf until it was
    # restarted, since nothing noticed the stream had stopped delivering audio.
    microphone = Microphone(sample_rate=RATE, stall_timeout=0.1)
    reader = microphone.listen()

    def plug_back_in():
        wait_for(lambda: len(streams) == 2)
        streams[1].feed(np.arange(480))
    threading.Thread(target=plug_back_in, daemon=True).start()

    assert list(reader.read(480)) == list(range(480))
    wait_for(lambda: streams[0].closed)

    # A late block from the old stream is thrown away rather than mixed in.
    position = microphone.position
    streams[0].feed(np.arange(480))
    assert microphone.position == position


def test_starts_without_a_microphone_and_picks_it_up_later(streams):
    FakeInputStream.failures = 1
    microphone = Microphone(sample_rate=RATE, stall_timeout=0.05)
    assert streams == []

    reader = microphone.listen()

    def plug_in():
        wait_for(lambda: len(streams) == 1)
        streams[0].feed(np.arange(480))
    threading.Thread(target=plug_in, daemon=True).start()

    assert list(reader.read(480)) == list(range(480))


# Recording

class FakeDetector:
    """Takes any frame above 1000 to be speech, so a test decides what's speech by what it feeds."""

    threshold = 0.5

    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def speech_probability(self, frame):
        return 1.0 if frame.mean() > 1000 else 0.0


class FakeTimeline:
    def __init__(self):
        self.marks = {}

    def mark(self, stage, at=None):
        self.marks[stage] = at


SPEECH, QUIET = 2000, 0


def record(microphone, **kwargs):
    """record_until_silence with short, round settings, all in whole 30ms frames."""
    settings = dict(silence_duration=10 * FRAME / RATE, speech_timeout=20 * FRAME / RATE,
                    timeout=100 * FRAME / RATE, min_speech=4 * FRAME / RATE)
    settings.update(kwargs)
    return microphone.record_until_silence(FakeDetector(), **settings)


def test_recording_stops_once_the_user_goes_quiet(streams):
    microphone = Microphone(sample_rate=RATE)
    listen_from = microphone.position
    feed_frames(streams[0], 5, QUIET)
    feed_frames(streams[0], 20, SPEECH)
    feed_frames(streams[0], 100, QUIET)

    timeline = FakeTimeline()
    audio = record(microphone, listen_from=listen_from, timeline=timeline)

    # Up to the end of the speech, and the silence which ended it.
    assert len(audio) == (5 + 20 + 10) * FRAME
    assert {"recording_started", "speech_started", "speech_ended", "recording_stopped"} <= set(timeline.marks)


def test_a_recording_keeps_the_moment_before_listening_started(streams):
    # "Hey Nova what time is it", said in one breath, has usually reached "what"
    # by the time the wake word is detected.
    microphone = Microphone(sample_rate=RATE)
    feed_frames(streams[0], 10, SPEECH)
    listen_from = microphone.position
    feed_frames(streams[0], 20, SPEECH)
    feed_frames(streams[0], 100, QUIET)

    audio = record(microphone, listen_from=listen_from, keep_before=10 * FRAME / RATE)

    assert len(audio) == (10 + 20 + 10) * FRAME
    assert np.all(audio[:10 * FRAME] > 0.05)


def test_speech_from_before_listening_started_does_not_start_the_recording(streams):
    # The end of the wake word is in the moment kept from before it was detected,
    # and mustn't count as the user going on to say something.
    microphone = Microphone(sample_rate=RATE)
    feed_frames(streams[0], 10, SPEECH)
    listen_from = microphone.position
    feed_frames(streams[0], 100, QUIET)

    audio = record(microphone, listen_from=listen_from, keep_before=10 * FRAME / RATE)
    assert len(audio) == 0


def test_a_sound_too_short_to_be_speech_does_not_start_the_recording(streams):
    # Like the listening sound, which the microphone hears as well.
    microphone = Microphone(sample_rate=RATE)
    listen_from = microphone.position
    feed_frames(streams[0], 3, SPEECH)
    feed_frames(streams[0], 100, QUIET)

    assert len(record(microphone, listen_from=listen_from)) == 0


def test_recording_stops_at_the_timeout_if_the_user_keeps_talking(streams):
    microphone = Microphone(sample_rate=RATE)
    listen_from = microphone.position
    feed_frames(streams[0], 120, SPEECH)

    assert len(record(microphone, listen_from=listen_from, timeout=50 * FRAME / RATE)) == 50 * FRAME
