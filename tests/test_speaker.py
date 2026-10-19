"""
Speaker Test
Checks the one output stream every sound goes through - mixing, stopping part way,
silencing a kind of sound while the user talks, and not hanging on a dead speaker -
using a fake in place of the real output stream, so nothing is played. Needs numpy
and soxr installed (as they are on the Pi), and is skipped anywhere they aren't.
"""

import time

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

    def pull(self, frames=480):
        """What the speaker would be given next."""
        out = np.zeros((frames, 1), dtype=np.float32)
        self.callback(out, frames, None, None)
        return out[:, 0]


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
    assert abs(len(playback.audio) - RATE) < 100

    reads = []
    monkeypatch.setattr(speaker_module.sf, "read", lambda *args, **kwargs: reads.append(args))
    speaker.play_file(path)
    assert reads == []


def test_a_missing_sound_file_plays_nothing(streams, tmp_path):
    assert Speaker(sample_rate=RATE).play_file(str(tmp_path / "missing.wav")) is None
