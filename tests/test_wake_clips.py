"""
Wake Clips Test
Checks that the seconds around a wake are kept as they should be, and that nothing can
go wrong with them in a way the wake word would feel - with a fake microphone, or the
real one over a fake stream where how it treats a late or a waiting read is the point.
Needs numpy and soxr installed (as they are on the Pi), and is skipped anywhere they
aren't.
"""

import calendar
import errno
import inspect
import io
import logging
import os
import stat
import threading
import time
import wave

import pytest
import yaml

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import microphone as microphone_module
from voice_assistant import paths, storage, wake_clips
from voice_assistant.microphone import Microphone
from voice_assistant.wake_clips import WakeClips

RATE = 16000
HEARD_AT = 10 * RATE
BEFORE = 2 * RATE
AFTER = RATE


def not_muted(start):
    return False


class Mute:
    """Stands in for the satellite's mute, noting what it was asked about."""

    def __init__(self):
        self.on = False
        self.asked = []

    def __call__(self, start):
        self.asked.append(start)
        return self.on


def samples(start, end):
    """What the fake microphone heard between two positions: each sample is its own position."""
    return (np.arange(start, end) % 32768).astype(np.int16)


def wait_for(condition, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


def finish():
    """Waits for whatever is keeping a clip to be done."""
    for thread in threading.enumerate():
        if thread.name == "wake-clip":
            thread.join(timeout=5)
            assert not thread.is_alive()


def clips_in(folder):
    return sorted(os.listdir(folder))


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def audio_of(path):
    with wave.open(str(path), "rb") as file:
        assert (file.getnchannels(), file.getsampwidth(), file.getframerate()) == (1, 2, RATE)
        return np.frombuffer(file.readframes(file.getnframes()), np.int16)


class FakeReader:
    def __init__(self, microphone, position):
        self.microphone = microphone
        self.position = position

    def read(self, count):
        self.microphone.called_from.add(threading.current_thread().name)
        self.microphone.reading.set()
        assert self.microphone.released.wait(timeout=5)
        if self.microphone.broken is not None:
            raise self.microphone.broken
        # A real reader whose start has gone from the buffer starts later than it was
        # asked to, and says so only by how far it has got - see Microphone._read.
        self.position += self.microphone.skips
        heard = samples(self.position, self.position + count)
        self.position += count
        return heard


class FakeMicrophone:
    """Has already heard well past the wake, unless a test says otherwise."""

    def __init__(self):
        self.position = HEARD_AT + 5 * RATE
        self.starts = []
        self.called_from = set()
        self.reading = threading.Event()
        self.released = threading.Event()
        self.released.set()
        self.broken = None
        self.skips = 0

    def listen(self, start=None):
        self.called_from.add(threading.current_thread().name)
        self.starts.append(start)
        return FakeReader(self, start)


@pytest.fixture
def microphone():
    return FakeMicrophone()


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "clips"


@pytest.fixture
def clips(microphone, folder):
    return WakeClips(microphone, not_muted, folder=str(folder))


@pytest.fixture
def nothing_masked():
    """Has modes depend on what the code asks for, not on the umask it happens to run with."""
    before = os.umask(0)
    yield
    os.umask(before)


@pytest.fixture
def real_microphone(monkeypatch):
    """The real Microphone over a stream which never hears anything by itself."""
    opened = []

    class Stream:
        def __init__(self, **settings):
            opened.append(self)

        def start(self):
            pass

        def close(self, ignore_errors=False):
            pass

    monkeypatch.setattr(microphone_module.sd, "InputStream", Stream)
    made = Microphone(sample_rate=RATE, buffer_seconds=4.0, stall_timeout=0.05)
    made.opened = opened
    yield made
    made.close()


def test_the_settings_keep_nothing_until_switched_on():
    with open(paths.DEFAULTS_FILE) as file:
        assert yaml.safe_load(file)["wake_clips"] == {"keep": False, "most": 400}


def test_a_clip_is_kept_with_the_recordings_outside_the_project():
    assert wake_clips.FOLDER == os.path.join(paths.DATA_DIR, "voice-samples", "wake", "clips")


def test_one_wake_keeps_two_seconds_before_it_and_one_after(clips, microphone, folder, nothing_masked):
    clips.keep(HEARD_AT, 0.73)
    finish()

    (name,) = clips_in(folder)
    assert microphone.starts == [HEARD_AT - BEFORE]
    assert np.array_equal(audio_of(folder / name), samples(HEARD_AT - BEFORE, HEARD_AT + AFTER))
    assert len(audio_of(folder / name)) == 3 * RATE
    assert mode(folder / name) == 0o600
    assert mode(folder) == 0o700


@pytest.mark.parametrize("zone, name", [("Europe/London", "20260701-130501-0.73.wav"),
                                        ("UTC", "20260701-120501-0.73.wav")])
def test_a_clip_is_named_for_the_wakes_local_time_and_score(clips, folder, monkeypatch, zone, name):
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        moment = calendar.timegm((2026, 7, 1, 12, 5, 1))
        monkeypatch.setattr(wake_clips.time, "time", lambda: moment)
        clips.keep(HEARD_AT, 0.7262)
        finish()
    finally:
        monkeypatch.undo()
        time.tzset()

    assert clips_in(folder) == [name]


def test_the_time_is_the_wakes_not_the_moment_it_was_written(clips, microphone, folder, monkeypatch):
    moment = calendar.timegm((2026, 7, 1, 12, 5, 1))
    monkeypatch.setattr(wake_clips.time, "time", lambda: moment)
    microphone.released.clear()
    clips.keep(HEARD_AT, 0.9)
    assert microphone.reading.wait(timeout=5)
    monkeypatch.setattr(wake_clips.time, "time", lambda: moment + 60)
    microphone.released.set()
    finish()

    expected = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
    assert clips_in(folder) == [f"{expected}-0.90.wav"]


def test_a_wake_just_after_starting_keeps_what_there_is(clips, microphone, folder):
    heard_at = RATE // 2
    clips.keep(heard_at, 0.9)
    finish()

    (name,) = clips_in(folder)
    assert microphone.starts == [0]
    assert np.array_equal(audio_of(folder / name), samples(0, heard_at + AFTER))


def test_a_clip_ends_a_second_after_the_wake_not_when_the_wake_was_handed_over(clips, microphone, folder):
    # Waits for the second after the wake, then goes no further - however soon it starts.
    microphone.position = HEARD_AT
    clips.keep(HEARD_AT, 0.9)
    assert not wait_for(lambda: microphone.starts, seconds=0.1)
    microphone.position = HEARD_AT + AFTER
    finish()

    (name,) = clips_in(folder)
    assert np.array_equal(audio_of(folder / name), samples(HEARD_AT - BEFORE, HEARD_AT + AFTER))


def test_keep_returns_while_the_read_is_still_blocked(clips, microphone, folder):
    microphone.released.clear()
    clips.keep(HEARD_AT, 0.9)

    # Here, with the read held up, is the proof: the wake word carries on meanwhile.
    assert microphone.reading.wait(timeout=5)
    assert not folder.exists() or clips_in(folder) == []

    microphone.released.set()
    finish()
    assert len(clips_in(folder)) == 1


def test_nothing_touches_the_disk_or_the_microphone_on_the_listening_loop(clips, microphone, folder, monkeypatch):
    used_from = set()
    real_folder = storage.private_folder
    monkeypatch.setattr(storage, "private_folder",
                        lambda path: (used_from.add(threading.current_thread().name), real_folder(path)))
    clips.keep(HEARD_AT, 0.9)
    finish()

    assert used_from == {"wake-clip"}
    assert microphone.called_from == {"wake-clip"}


def test_the_thread_is_a_daemon_so_it_cant_hold_up_shutting_down(clips, microphone):
    microphone.released.clear()
    clips.keep(HEARD_AT, 0.9)
    assert microphone.reading.wait(timeout=5)

    (thread,) = [thread for thread in threading.enumerate() if thread.name == "wake-clip"]
    assert thread.daemon
    microphone.released.set()
    finish()


def test_a_second_wake_while_the_first_is_being_kept_isnt_kept(clips, microphone, folder, monkeypatch):
    moment = 1_800_000_000
    monkeypatch.setattr(wake_clips.time, "time", lambda: moment)
    microphone.released.clear()
    clips.keep(HEARD_AT, 0.7)
    assert microphone.reading.wait(timeout=5)

    clips.keep(HEARD_AT + RATE, 0.8)
    microphone.released.set()
    finish()

    assert microphone.starts == [HEARD_AT - BEFORE]
    (name,) = clips_in(folder)
    assert name.endswith("-0.70.wav")

    # And it only holds up those: the next wake is kept as usual.
    clips.keep(HEARD_AT + 2 * RATE, 0.8)
    finish()
    assert len(clips_in(folder)) == 2


def test_at_most_nothing_more_is_kept_and_it_warns_once(microphone, folder, caplog):
    folder.mkdir()
    for number in range(2):
        (folder / f"clip-{number}.wav").write_bytes(b"")
    # Not clips, so they don't count towards the limit.
    (folder / "notes.txt").write_bytes(b"")
    clips = WakeClips(microphone, not_muted, folder=str(folder), most=3)
    with caplog.at_level(logging.WARNING, logger="voice_assistant.wake_clips"):
        clips.keep(HEARD_AT, 0.7)
        finish()
        assert len(clips_in(folder)) == 4
        assert caplog.records == []

        for number in range(3):
            clips.keep(HEARD_AT + number * RATE, 0.8)
            finish()

    assert len(clips_in(folder)) == 4
    assert len(microphone.starts) == 1
    (warning,) = caplog.records
    assert warning.levelno == logging.WARNING
    assert "3" in warning.getMessage()


def test_nothing_is_deleted_at_most(microphone, folder):
    folder.mkdir()
    (folder / "the-oldest.wav").write_bytes(b"still here")
    clips = WakeClips(microphone, not_muted, folder=str(folder), most=1)
    clips.keep(HEARD_AT, 0.8)
    finish()

    assert clips_in(folder) == ["the-oldest.wav"]
    assert (folder / "the-oldest.wav").read_bytes() == b"still here"


def test_a_new_folder_is_made_private_and_so_are_those_made_above_it(microphone, tmp_path, nothing_masked):
    folder = tmp_path / "voice-samples" / "wake" / "clips"
    WakeClips(microphone, not_muted, folder=str(folder)).keep(HEARD_AT, 0.8)
    finish()

    assert len(clips_in(folder)) == 1
    assert mode(tmp_path / "voice-samples") == 0o700
    assert mode(tmp_path / "voice-samples" / "wake") == 0o700
    assert mode(folder) == 0o700


def test_a_folder_already_there_is_made_private_but_not_those_above_it(microphone, tmp_path):
    above = tmp_path / "above"
    folder = above / "clips"
    folder.mkdir(parents=True)
    os.chmod(above, 0o755)
    os.chmod(folder, 0o755)
    WakeClips(microphone, not_muted, folder=str(folder)).keep(HEARD_AT, 0.8)
    finish()

    assert mode(folder) == 0o700
    assert mode(above) == 0o755
    assert len(clips_in(folder)) == 1


def test_a_folder_opened_up_since_the_last_clip_is_made_private_again(clips, folder):
    clips.keep(HEARD_AT, 0.8)
    finish()
    os.chmod(folder, 0o755)
    clips.keep(HEARD_AT + RATE, 0.8)
    finish()

    assert mode(folder) == 0o700


@pytest.mark.parametrize("folder", ["clips", "nova/wake/clips"])
def test_a_relative_folder_works(microphone, tmp_path, monkeypatch, folder):
    # NOVA_DATA can be relative, and a bare name has nothing above it to make.
    monkeypatch.chdir(tmp_path)
    WakeClips(microphone, not_muted, folder=folder).keep(HEARD_AT, 0.8)
    finish()

    assert len(clips_in(tmp_path / folder)) == 1


def test_making_it_touches_nothing_so_a_bad_folder_cant_stop_the_satellite_starting(microphone, tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_bytes(b"")
    WakeClips(microphone, not_muted, folder=str(blocked / "clips"))

    assert blocked.read_bytes() == b""


def test_a_failing_write_is_logged_and_the_next_wake_works(clips, folder, monkeypatch, caplog):
    real_open = wake_clips.wave.open

    def fail(*args):
        monkeypatch.setattr(wake_clips.wave, "open", real_open)
        raise OSError("The card has gone read-only")

    monkeypatch.setattr(wake_clips.wave, "open", fail)
    clips.keep(HEARD_AT, 0.7)
    finish()

    assert "The card has gone read-only" in caplog.text
    assert clips_in(folder) == []
    clips.keep(HEARD_AT + RATE, 0.8)
    finish()
    assert len(clips_in(folder)) == 1


def test_a_full_disk_leaves_no_part_of_a_clip_and_the_next_wake_works(clips, folder, monkeypatch, caplog):
    real_fdopen = os.fdopen

    class FullDisk(io.FileIO):
        def write(self, data):
            super().write(bytes(data)[:100])
            raise OSError(errno.ENOSPC, "No space left on device")

    def fill_up(descriptor, mode):
        monkeypatch.setattr(wake_clips.os, "fdopen", real_fdopen)
        return FullDisk(descriptor, "wb")

    monkeypatch.setattr(wake_clips.os, "fdopen", fill_up)
    clips.keep(HEARD_AT, 0.7)
    finish()

    assert "No space left on device" in caplog.text
    assert clips_in(folder) == []
    clips.keep(HEARD_AT + RATE, 0.8)
    finish()
    assert len(clips_in(folder)) == 1


def test_a_raising_reader_is_logged_and_the_next_wake_works(clips, microphone, folder, caplog):
    microphone.broken = RuntimeError("The reader has gone wrong")
    clips.keep(HEARD_AT, 0.7)
    finish()

    assert "The reader has gone wrong" in caplog.text
    microphone.broken = None
    clips.keep(HEARD_AT + RATE, 0.8)
    finish()
    assert len(clips_in(folder)) == 1


def test_a_folder_which_cant_be_made_is_logged_and_the_next_wake_works(microphone, tmp_path, caplog):
    blocked = tmp_path / "a-file"
    blocked.write_bytes(b"")
    clips = WakeClips(microphone, not_muted, folder=str(blocked / "clips"))
    clips.keep(HEARD_AT, 0.7)
    finish()

    assert microphone.starts == []
    assert "a-file" in caplog.text
    blocked.unlink()
    clips.keep(HEARD_AT + RATE, 0.8)
    finish()
    assert len(clips_in(tmp_path / "a-file" / "clips")) == 1


def test_a_thread_which_cant_start_is_logged_and_the_next_wake_works(clips, folder, monkeypatch, caplog):
    real_thread = wake_clips.threading.Thread

    def fail(*args, **kwargs):
        monkeypatch.setattr(wake_clips.threading, "Thread", real_thread)
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(wake_clips.threading, "Thread", fail)
    clips.keep(HEARD_AT, 0.7)

    assert "can't start new thread" in caplog.text
    clips.keep(HEARD_AT + RATE, 0.8)
    finish()
    assert len(clips_in(folder)) == 1


def test_a_name_already_there_is_left_alone(clips, folder, monkeypatch, caplog):
    # The hour the clocks go back happens twice, so a name can come round again.
    moment = 1_800_000_000
    monkeypatch.setattr(wake_clips.time, "time", lambda: moment)
    clips.keep(HEARD_AT, 0.7)
    finish()
    (name,) = clips_in(folder)
    before = (folder / name).read_bytes()

    clips.keep(HEARD_AT + RATE, 0.7)
    finish()

    assert clips_in(folder) == [name]
    assert (folder / name).read_bytes() == before
    assert "File exists" in caplog.text


def test_a_dead_microphone_gives_up_rather_than_waiting_for_ever(clips, microphone, folder, monkeypatch, caplog):
    monkeypatch.setattr(wake_clips, "GIVE_UP_SECONDS", 0.05)
    microphone.position = HEARD_AT
    clips.keep(HEARD_AT, 0.7)
    finish()

    # Audio from long after the wake isn't the wake's: nothing was read, and nothing kept.
    assert microphone.starts == []
    assert clips_in(folder) == []
    assert "microphone" in caplog.text
    microphone.position = HEARD_AT + 5 * RATE
    clips.keep(HEARD_AT + RATE, 0.8)
    finish()
    assert len(clips_in(folder)) == 1


def test_a_dead_microphone_is_only_ever_reopened_by_the_main_loop(real_microphone, folder, monkeypatch):
    # A read which has to wait reopens a microphone it finds quiet, which only the main
    # loop may do - see WakeClips._arrives.
    monkeypatch.setattr(wake_clips, "GIVE_UP_SECONDS", 0.2)
    beats = []
    monkeypatch.setattr(microphone_module.watchdog, "beat", lambda: beats.append(1))
    heard_at = real_microphone.position
    WakeClips(real_microphone, not_muted, folder=str(folder)).keep(heard_at, 0.7)
    finish()

    assert len(real_microphone.opened) == 1
    # Nor does it say the main loop is working, which might not be true.
    assert beats == []


def test_audio_which_has_already_gone_isnt_kept_in_its_place(real_microphone, folder, caplog):
    # Held up long enough for the wake to drop out of the microphone's few seconds, the
    # read would quietly start later. That's the room some time after the wake, and
    # nobody has said the wake word in it, so it's nothing the user agreed to keep.
    real_microphone._append(samples(0, 3 * RATE))
    heard_at = real_microphone.position
    real_microphone._append(samples(3 * RATE, 8 * RATE))
    WakeClips(real_microphone, not_muted, folder=str(folder)).keep(heard_at, 0.7)
    finish()

    assert clips_in(folder) == []
    assert "too late" in caplog.text


def test_a_read_which_started_late_isnt_kept(clips, microphone, folder, caplog):
    microphone.skips = RATE
    clips.keep(HEARD_AT, 0.7)
    finish()

    assert clips_in(folder) == []
    assert "too late" in caplog.text


def test_a_mute_before_the_last_sample_arrives_keeps_nothing(microphone, folder, caplog):
    mute = Mute()
    clips = WakeClips(microphone, mute, folder=str(folder))
    microphone.position = HEARD_AT + AFTER - 1
    with caplog.at_level(logging.INFO, logger="voice_assistant.wake_clips"):
        clips.keep(HEARD_AT, 0.7)
        assert not wait_for(lambda: microphone.starts, seconds=0.1)
        mute.on = True
        microphone.position = HEARD_AT + AFTER
        finish()

    # Not read at all, and all that's said of it is that.
    assert microphone.starts == []
    assert clips_in(folder) == []
    assert [record.getMessage() for record in caplog.records] == ["Wake clip not kept, muted"]


def test_a_mute_during_the_read_keeps_nothing(microphone, folder, caplog):
    mute = Mute()
    clips = WakeClips(microphone, mute, folder=str(folder))
    microphone.released.clear()
    with caplog.at_level(logging.INFO, logger="voice_assistant.wake_clips"):
        clips.keep(HEARD_AT, 0.7)
        assert microphone.reading.wait(timeout=5)
        mute.on = True
        microphone.released.set()
        finish()

    assert clips_in(folder) == []
    assert [record.getMessage() for record in caplog.records] == ["Wake clip not kept, muted"]


@pytest.mark.parametrize("heard_at, start", [(HEARD_AT, HEARD_AT - BEFORE), (RATE // 2, 0)])
def test_the_mute_is_asked_about_all_of_the_clip_before_reading_and_before_writing(microphone, folder, heard_at, start):
    mute = Mute()
    WakeClips(microphone, mute, folder=str(folder)).keep(heard_at, 0.7)
    finish()

    assert mute.asked == [start, start]
    assert len(clips_in(folder)) == 1


def test_a_mute_which_cant_be_told_keeps_nothing(microphone, folder, caplog):
    def broken(start):
        raise RuntimeError("Can't tell")

    WakeClips(microphone, broken, folder=str(folder)).keep(HEARD_AT, 0.7)
    finish()

    assert microphone.starts == []
    assert clips_in(folder) == []
    assert "Can't tell" in caplog.text


def test_the_wait_is_counted_from_the_wake_not_from_when_the_thread_got_to_it(clips, microphone, folder, monkeypatch,
                                                                              caplog):
    monkeypatch.setattr(wake_clips, "GIVE_UP_SECONDS", 0.05)
    real_folder = storage.private_folder

    def slowly(path):
        monkeypatch.setattr(storage, "private_folder", real_folder)
        time.sleep(0.1)
        real_folder(path)

    monkeypatch.setattr(storage, "private_folder", slowly)
    clips.keep(HEARD_AT, 0.7)
    finish()

    assert microphone.starts == []
    assert clips_in(folder) == []
    assert "microphone" in caplog.text


def test_a_stream_reopened_after_a_stall_is_too_late_to_finish_a_clip():
    # The main loop's read reopens a quiet stream after this long, and what it hears from
    # then on isn't the second after the wake.
    stall_timeout = inspect.signature(Microphone).parameters["stall_timeout"].default
    assert AFTER / RATE < wake_clips.GIVE_UP_SECONDS < stall_timeout


def test_a_real_microphone_gives_the_clip_its_own_samples(real_microphone, folder):
    real_microphone._append(samples(0, 3 * RATE))
    heard_at = real_microphone.position
    real_microphone._append(samples(3 * RATE, heard_at + AFTER))
    WakeClips(real_microphone, not_muted, folder=str(folder)).keep(heard_at, 0.7)
    finish()

    (name,) = clips_in(folder)
    assert np.array_equal(audio_of(folder / name), samples(heard_at - BEFORE, heard_at + AFTER))
