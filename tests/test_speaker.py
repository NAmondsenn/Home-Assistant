"""
Speaker Test
Checks the one output stream every sound goes through - mixing, stopping part way,
silencing a kind of sound while the user talks, and not hanging on a dead speaker -
using a fake in place of the real output stream, so nothing is played. Needs numpy
and soxr installed (as they are on the Pi), and is skipped anywhere they aren't.
"""

import time
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import speaker as speaker_module
from voice_assistant.speaker import Speaker

RATE = 48000


class FakeOutputStream:
    """
    Stands in for sounddevice.OutputStream. Plays nothing by itself - the test asks
    for audio through pull(), just as PortAudio calls back when it needs more.
    """

    opened = []
    # How many of the next attempts to open a stream should fail.
    failures = 0

    def __init__(self, callback, **settings):
        if FakeOutputStream.failures:
            FakeOutputStream.failures -= 1
            raise OSError("No such device")
        self.callback = callback
        self.settings = settings
        self.latency = 0.05
        self.closed = False
        FakeOutputStream.opened.append(self)

    def start(self):
        pass

    def close(self, ignore_errors=False):
        self.closed = True

    def pull(self, frames=480, both=False):
        """What the speaker would be given next - the left side, or both."""
        out = np.zeros((frames, self.settings["channels"]), dtype=np.float32)
        self.callback(out, frames, None, None)
        return out if both else out[:, 0]


@pytest.fixture
def streams(monkeypatch):
    FakeOutputStream.opened = []
    FakeOutputStream.failures = 0
    monkeypatch.setattr(speaker_module.sd, "OutputStream", FakeOutputStream)
    return FakeOutputStream.opened


def test_a_sound_is_played_at_its_volume(streams):
    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play(np.full(480, 0.5), volume=0.5)

    assert np.allclose(streams[0].pull(), 0.25)
    assert playback.wait(timeout=1)
    assert not np.any(streams[0].pull())


def test_sounds_playing_together_are_mixed(streams):
    speaker = Speaker(sample_rate=RATE)
    speaker.play(np.full(480, 0.25))
    speaker.play(np.full(480, 0.5))
    assert np.allclose(streams[0].pull(), 0.75)


def test_a_stopped_sound_goes_quiet_straight_away(streams):
    # Saying "stop" to an alarm used to wait for its 34 second sound to finish.
    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play(np.full(RATE * 34, 0.5), kind="announcement")
    streams[0].pull()

    started = time.monotonic()
    playback.stop()
    assert playback.wait() and time.monotonic() - started < 0.1
    assert not np.any(streams[0].pull())


def test_a_silenced_kind_is_stopped_and_refused_until_allowed(streams):
    # The wake word silences timers and alarms while the user talks, so the next
    # round can't start in the gap.
    speaker = Speaker(sample_rate=RATE)
    ringing = speaker.play(np.full(RATE, 0.5), kind="announcement")
    speaker.silence("announcement")

    assert ringing.done
    assert speaker.play(np.full(RATE, 0.5), kind="announcement").done
    reply = speaker.play(np.full(480, 0.5), kind="reply")
    assert np.allclose(streams[0].pull(), 0.5) and reply.wait(timeout=1)

    speaker.allow("announcement")
    assert not speaker.play(np.full(RATE, 0.5), kind="announcement").done


def test_waiting_on_a_speaker_that_has_died_doesnt_wait_forever(streams, monkeypatch):
    monkeypatch.setattr(speaker_module, "STALL_SECONDS", 0.05)
    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play(np.full(480, 0.5))

    started = time.monotonic()
    assert playback.wait()
    assert time.monotonic() - started < 1


def test_a_speaker_which_stopped_taking_audio_is_reopened(streams, monkeypatch):
    speaker = Speaker(sample_rate=RATE)
    speaker._last_callback -= speaker_module.STALL_SECONDS + 1

    speaker.play(np.full(480, 0.5))
    assert len(streams) == 2
    assert np.allclose(streams[1].pull(), 0.5)


def test_without_a_speaker_a_sound_finishes_straight_away(streams):
    FakeOutputStream.failures = 2
    speaker = Speaker(sample_rate=RATE)
    assert speaker.play(np.full(RATE, 0.5)).done


def test_sound_files_are_resampled_and_kept_once_loaded(streams, tmp_path, monkeypatch):
    soundfile = pytest.importorskip("soundfile")
    path = str(tmp_path / "beep.wav")
    soundfile.write(path, np.full(22050, 0.5, dtype=np.float32), 22050)
    speaker = Speaker(sample_rate=RATE)

    playback = speaker.play_file(path)
    assert abs(playback.duration - 1) < 0.01

    reads = []
    monkeypatch.setattr(speaker_module.sf, "read", lambda *args, **kwargs: reads.append(args))
    speaker.play_file(path)
    assert reads == []


def test_a_missing_sound_file_plays_nothing(streams, tmp_path):
    assert Speaker(sample_rate=RATE).play_file(str(tmp_path / "missing.wav")) is None


def test_part_of_a_sound_file_can_be_played_as_if_it_looped(streams):
    # An alarm's bursts carry on through its sound, wrapping round at the end.
    speaker = Speaker(sample_rate=RATE)
    sound = np.arange(RATE * 2, dtype=np.float32)
    assert list(speaker._section(sound, 0.5, 0.25)) == list(sound[RATE // 2:RATE // 2 + RATE // 4])
    wrapped = speaker._section(sound, 1.75, 0.5)
    assert list(wrapped) == list(sound[RATE * 7 // 4:]) + list(sound[:RATE // 4])
    assert list(speaker._section(sound, 2.5, None)) == list(sound[RATE // 2:])


def test_a_streamed_sound_plays_each_piece_as_it_arrives(streams):
    # A reply used to be synthesised whole, and written to the SD card, before any
    # of it played.
    import queue
    arriving = queue.Queue()

    def pieces():
        while (piece := arriving.get()) is not None:
            yield piece

    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play_stream(pieces())
    assert not np.any(streams[0].pull())

    arriving.put(np.full(480, 0.5, dtype=np.float32))
    time.sleep(0.05)
    assert np.allclose(streams[0].pull(), 0.5)
    # Catching up with what's arrived so far isn't the end of it.
    assert not np.any(streams[0].pull()) and not playback.done

    arriving.put(np.full(480, 0.25, dtype=np.float32))
    arriving.put(None)
    time.sleep(0.05)
    assert np.allclose(streams[0].pull(), 0.25)
    streams[0].pull()
    assert playback.wait(timeout=1)


def test_stopping_a_streamed_sound_stops_whatever_is_making_it(streams):
    closed = []

    def pieces():
        try:
            while True:
                yield np.full(480, 0.5, dtype=np.float32)
                time.sleep(0.01)
        finally:
            closed.append(True)

    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play_stream(pieces())
    time.sleep(0.05)
    playback.stop()
    time.sleep(0.05)
    assert closed == [True]


def test_a_silenced_streamed_sound_never_starts_its_source(streams):
    started = []

    def pieces():
        started.append(True)
        yield np.full(480, 0.5, dtype=np.float32)

    speaker = Speaker(sample_rate=RATE)
    speaker.silence("announcement")
    assert speaker.play_stream(pieces(), kind="announcement").wait(timeout=1)
    assert started == []


def test_a_streamed_sound_is_resampled_without_gaps(streams):
    # Resampling each piece on its own would leave a click at every join.
    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play_stream(iter([np.full(2205, 0.5, dtype=np.float32)] * 10), sample_rate=22050)
    while playback.streaming:
        time.sleep(0.01)

    assert abs(playback.duration - 1) < 0.01
    middle = np.concatenate(playback._take(RATE))[1000:-1000]
    assert np.allclose(middle, 0.5, atol=0.01)


def test_mono_sounds_play_on_both_sides_and_stereo_keeps_its_sides(streams):
    speaker = Speaker(sample_rate=RATE)
    speaker.play(np.full(480, 0.5))
    assert np.allclose(streams[0].pull(both=True), 0.5)

    stereo = np.column_stack([np.full(480, 0.25), np.full(480, -0.25)]).astype(np.float32)
    speaker.play_stream(iter([stereo]))
    time.sleep(0.05)
    out = streams[0].pull(both=True)
    assert np.allclose(out[:, 0], 0.25) and np.allclose(out[:, 1], -0.25)


def test_a_paused_sound_keeps_its_place(streams):
    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play(np.arange(960, dtype=np.float32) / 1000)
    first = streams[0].pull()
    playback.paused = True
    assert not np.any(streams[0].pull()) and not playback.done
    playback.paused = False
    assert np.allclose(streams[0].pull(), np.arange(480, 960) / 1000)
    assert np.allclose(first, np.arange(480) / 1000)


def test_a_long_stream_is_only_read_a_little_ahead(streams):
    # A song decoded all at once would be a hundred megabytes in memory.
    read = []

    def pieces():
        for n in range(100):
            read.append(n)
            yield np.zeros(RATE // 10, dtype=np.float32)

    speaker = Speaker(sample_rate=RATE)
    playback = speaker.play_stream(pieces(), most_ahead=0.5)
    time.sleep(0.2)
    assert len(read) <= 8
    streams[0].pull(RATE // 2)
    time.sleep(0.2)
    assert 8 < len(read) <= 14
    playback.stop()


def test_the_times_it_ran_dry_are_counted(streams):
    # Running dry crackles, and is otherwise invisible - see dropouts.py.
    speaker = Speaker(sample_rate=RATE)
    out = np.zeros((480, 2), dtype=np.float32)
    streams[0].callback(out, 480, None, SimpleNamespace(output_underflow=True))
    streams[0].callback(out, 480, None, SimpleNamespace(output_underflow=False))
    assert speaker.ran_dry.total == 1
