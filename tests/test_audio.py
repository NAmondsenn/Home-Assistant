"""
Audio Manager Test
Checks what's handed to the speaker, using a fake in place of PyAudio, so nothing
is ever actually played. Needs numpy installed (as it is on the Pi), and is
skipped anywhere it isn't.
"""

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import audio as audio_module
from voice_assistant.audio import AudioManager


class FakeStream:
    """
    Stands in for a PyAudio output stream, keeping everything written to it. Holds
    12000 frames - a quarter of a second at 48kHz - as the Pi's shared output does.
    """

    def __init__(self, buffer_frames=12000):
        self.buffer_frames = buffer_frames
        self.written = b""

    def get_write_available(self):
        return self.buffer_frames

    def write(self, data):
        self.written += data

    def stop_stream(self):
        pass

    def close(self):
        pass


class FakePyAudio:
    """Stands in for PyAudio, keeping every stream it opens."""

    def __init__(self):
        self.streams = []

    def open(self, **kwargs):
        self.streams.append(FakeStream())
        return self.streams[-1]

    def terminate(self):
        pass


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(audio_module.pyaudio, "PyAudio", FakePyAudio)
    return AudioManager(sample_rate=48000)


def test_a_short_sound_is_followed_by_a_full_buffer_of_silence(manager):
    # Closing the stream on the Pi throws away whatever is still buffered rather
    # than playing it - which was the whole of the 0.14s listening blip, since it
    # fits in the buffer with room to spare. With a full buffer of silence after
    # it, only the silence is left to be thrown away.
    blip = np.full(6816, 0.5, dtype=np.float32)
    manager.play(blip)

    written = np.frombuffer(manager.pa.streams[0].written, dtype=np.int16)
    assert len(written) == 6816 + 12000
    assert np.all(written[:6816] != 0)
    assert np.all(written[6816:] == 0)
