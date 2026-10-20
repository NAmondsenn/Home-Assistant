"""
Echo Test
Checks what the assistant plays is lined up with what the microphone hears, and
taken back out of it. The tests which use speexdsp itself only run where it's
installed, as it is on the Pi; the rest run anywhere numpy and soxr are.
"""

import numpy as np
import pytest

pytest.importorskip("soxr")

from voice_assistant import echo as echo_module
from voice_assistant.echo import RATE, EchoCanceller, PlayedAudio, Samples, Speex, make_echo_canceller, measure_delay

try:
    Speex()
    HAVE_SPEEX = True
except OSError:
    HAVE_SPEEX = False
needs_speex = pytest.mark.skipif(not HAVE_SPEEX, reason="needs speexdsp, as on the Pi")


def _music(seconds, seed=0):
    """Something music-like to play: noise shaped towards the low end, with gaps."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 1, int(seconds * RATE))
    shaped = np.convolve(noise, np.ones(8) / 8, mode="same")
    envelope = 0.5 + 0.5 * np.sin(np.arange(len(noise)) / RATE * 2 * np.pi * 2)
    return (shaped * envelope * 6000).astype(np.int16)


def _echo_of(played, delay, gain=0.4, seed=1):
    """What a microphone hears of something played: later, quieter, a little reverb, some noise."""
    heard = np.zeros(len(played), dtype=np.float64)
    heard[delay:] += gain * played[:len(played) - delay]
    heard[delay + 120:] += 0.15 * played[:len(played) - delay - 120]
    heard += np.random.default_rng(seed).normal(0, 30, len(played))
    return np.clip(heard, -32768, 32767).astype(np.int16)


# Keeping what was played and heard

def test_samples_are_read_back_by_position_with_silence_either_side():
    samples = Samples(seconds=0.01)
    samples.add(np.arange(1, 101, dtype=np.int16), start_time=None)
    samples.add(np.arange(101, 201, dtype=np.int16), start_time=None)
    # Only the last 160 are kept.
    assert list(samples.read(150, 10)) == list(range(151, 161))
    assert list(samples.read(195, 10)) == [196, 197, 198, 199, 200, 0, 0, 0, 0, 0]
    assert not np.any(samples.read(0, 10))


def test_each_streams_clock_is_fitted_to_its_timestamps():
    # The Pi's microphone ran 2,876 parts per million fast, with jittery timestamps.
    samples = Samples()
    rng = np.random.default_rng(0)
    for block in range(80):
        heard_at = 500.0 + block * 2000 / (RATE * 1.002876) + rng.normal(0, 0.0017)
        samples.add(np.zeros(2000, dtype=np.int16), heard_at)
    samples.fit_clock()
    assert samples.clock[1] == pytest.approx(RATE * 1.002876, rel=2e-4)
    assert samples.position_at(samples.time_at(80000)) == pytest.approx(80000)


def test_a_reopened_stream_starts_its_clock_again():
    samples = Samples()
    for block in range(60):
        samples.add(np.zeros(2000, dtype=np.int16), 10.0 + block * 0.125)
    samples.fit_clock()
    samples.add(np.zeros(2000, dtype=np.int16), 25.0)
    assert samples.clock is None and len(samples._anchors) == 1


def test_samples_can_be_read_between_positions():
    samples = Samples()
    samples.add(np.array([0, 100, 200, 300], dtype=np.int16), None)
    assert list(samples.read_at(np.array([0.5, 1.25, 2.0]))) == [50.0, 125.0, 200.0]


# Measuring the gap

@pytest.mark.parametrize("delay", [0, 880, 2320, 6000])
def test_the_gap_between_playing_and_hearing_is_measured(delay):
    # On the Pi it was 55ms one run and 145ms the next.
    most = int(0.4 * RATE)
    played = _music(2.0)
    heard = _echo_of(played, delay)
    count = int(1.5 * RATE)
    assert measure_delay(heard[-count:], played[len(played) - count - most:], most) == delay


def test_silence_isnt_measured():
    most = int(0.4 * RATE)
    assert measure_delay(np.zeros(1000, dtype=np.int16), np.zeros(1000 + most, dtype=np.int16), most) is None


def test_nothing_in_common_isnt_measured():
    most = int(0.4 * RATE)
    assert measure_delay(_music(1.0, seed=5), _music(1.4, seed=6), most) is None


class FakeSpeex:
    """Stands in for speexdsp, taking out exactly what it's given, so lining up shows."""

    frame = 256

    def __init__(self):
        self.resets = 0

    def cancel(self, heard, played):
        return (heard.astype(np.int32) - played).clip(-32768, 32767).astype(np.int16)

    def reset(self):
        self.resets += 1


def _canceller(monkeypatch, speex):
    monkeypatch.setattr(echo_module.threading, "Thread", lambda **kwargs: type("T", (), {"start": lambda self: None})())
    if isinstance(speex, FakeSpeex):
        # The stand-in takes out exactly what it's given, sample for sample, so a
        # margin for an adaptive filter to absorb would only throw it out.
        monkeypatch.setattr(echo_module, "MARGIN", 0)
    return EchoCanceller(PlayedAudio(speaker_rate=RATE), speex=speex)


def _play_and_hear(canceller, played, delay, heard_rate=RATE, block=2000):
    """
    Plays something through the canceller's speaker, and has its microphone hear it
    `delay` samples later through a clock running at `heard_rate`. Returns what was
    passed on.
    """
    heard_clock = np.arange(len(played)) / heard_rate
    played_clock = np.arange(len(played)) / RATE
    echo_of_played = np.interp(heard_clock - delay / RATE, played_clock, played.astype(np.float64), left=0)
    heard = echo_of_played.astype(np.int16)
    out = []
    for start in range(0, len(played), block):
        canceller.played.played(played[start:start + block].astype(np.float32) / 32767, 100.0 + start / RATE)
        out.append(canceller.process(heard[start:start + block], 100.0 + start / heard_rate))
        if start and start % (RATE // 2) == 0:
            canceller.played.fit_clock()
            canceller.heard.fit_clock()
            if start % (2 * RATE) == 0:
                canceller.measure()
    return heard, np.concatenate(out)


def test_what_was_played_is_lined_up_and_taken_out(monkeypatch):
    canceller = _canceller(monkeypatch, FakeSpeex())
    played = _music(16.0)
    heard, out = _play_and_hear(canceller, played, delay=2000)
    # Before it's lined up, nothing is taken out.
    assert np.array_equal(out[:RATE], heard[:RATE])
    assert canceller.delay is not None and abs(canceller.delay - 2000) < 40
    last = slice(-2 * RATE, -RATE)
    assert np.sqrt(np.mean(out[last].astype(np.float64) ** 2)) < 0.1 * np.sqrt(np.mean(heard[last].astype(np.float64) ** 2))


def test_a_microphone_on_a_faster_clock_is_followed(monkeypatch):
    # The two drift apart by 46 samples a second - in under a second, more than a
    # canceller can follow by itself.
    canceller = _canceller(monkeypatch, FakeSpeex())
    played = _music(16.0)
    heard, out = _play_and_hear(canceller, played, delay=2000, heard_rate=RATE * 1.002876)
    last = slice(-2 * RATE, -RATE)
    assert np.sqrt(np.mean(out[last].astype(np.float64) ** 2)) < 0.15 * np.sqrt(np.mean(heard[last].astype(np.float64) ** 2))


def test_the_gap_is_the_middle_of_measurements_which_agree(monkeypatch):
    # On the Pi, measurements a couple of seconds apart differed by a few milliseconds.
    canceller = _canceller(monkeypatch, FakeSpeex())
    found = iter([1650, 3000, 1610, 1700, 1500, 1648])
    monkeypatch.setattr(echo_module, "measure_delay", lambda *args: next(found))
    monkeypatch.setattr(canceller, "_played_positions", lambda *args: np.zeros(10))
    for _ in range(3):
        canceller.measure()
    assert canceller.delay is None
    canceller.measure()
    # The middle of 1650, 3000, 1610 and 1700, three of which agree.
    assert canceller.delay == 1675 - echo_module.MARGIN
    # A stray measurement doesn't move it, nor do small changes.
    canceller.measure()
    canceller.measure()
    assert canceller.delay == 1675 - echo_module.MARGIN


def test_without_speexdsp_the_microphone_is_left_alone(make_config, monkeypatch):
    def missing(*args, **kwargs):
        raise OSError("libspeexdsp.so.1: cannot open shared object file")

    monkeypatch.setattr(echo_module, "Speex", missing)
    assert make_echo_canceller(make_config(audio={"echo_cancellation": True}), 48000) is None
    assert make_echo_canceller(make_config(audio={"echo_cancellation": False}), 48000) is None


# With speexdsp itself

@needs_speex
def test_speexdsp_takes_most_of_an_echo_out():
    speex = Speex()
    played = _music(6.0)
    heard = _echo_of(played, delay=0)
    out = np.concatenate([speex.cancel(heard[i:i + 256], played[i:i + 256])
                          for i in range(0, len(played) - 255, 256)])
    # Once it has learnt the room - the last two seconds - the echo is far quieter.
    last = slice(-2 * RATE, None)
    before = np.sqrt(np.mean(heard[last].astype(np.float64) ** 2))
    after = np.sqrt(np.mean(out[last].astype(np.float64) ** 2))
    assert 20 * np.log10(before / after) > 15


@needs_speex
def test_the_whole_canceller_finds_the_gap_and_cancels(monkeypatch):
    canceller = _canceller(monkeypatch, Speex())
    played = _music(24.0)
    heard, out = _play_and_hear(canceller, played, delay=2320, heard_rate=RATE * 1.002876)
    last = slice(-2 * RATE, None)
    before = np.sqrt(np.mean(heard[last].astype(np.float64) ** 2))
    after = np.sqrt(np.mean(out[-len(heard[last]):].astype(np.float64) ** 2))
    assert canceller.delay is not None and 20 * np.log10(before / after) > 15
