"""Tests for what the satellite does while it can't reach its brain."""

import os
import time
import wave

from voice_assistant import fallback
from voice_assistant.fallback import AlarmCopy, FallbackRinger, Phrase, StateCopy

NOW = 1729587600.0


class FakePlayback:
    """Finishes only when stopped."""

    def __init__(self):
        self.stopped = False

    def done(self):
        return self.stopped

    def wait(self, timeout=None):
        time.sleep(min(timeout or 0.01, 0.01))
        return self.stopped

    def stop(self):
        self.stopped = True


class FakeSpeaker:
    def __init__(self):
        self.calls = []
        self.playbacks = []

    def play_file(self, filename, volume=1.0, kind="reply", cache=True, start=0.0, length=None):
        self.calls.append({"filename": filename, "volume": volume, "kind": kind, "cache": cache})
        playback = FakePlayback()
        self.playbacks.append(playback)
        return playback


def entry(id="alarm-1@1729587600", due=NOW, sound="alarm_sound.wav"):
    return {"id": id, "due": due, "kind": "alarm", "sound": sound}


def make(tmp_path, volume=0.4):
    copy = AlarmCopy(str(tmp_path / "copy.json"))
    state = StateCopy(str(tmp_path / "state.json"))
    state.update({"volumes": {"general": 0.5, "alarm": volume, "music": 0.3}, "muted": False})
    speaker = FakeSpeaker()
    return copy, state, speaker, FallbackRinger(speaker, "/sounds", copy, state)


def wait_for(condition, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_an_alarm_due_while_away_rings_until_dismissed(tmp_path):
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry()])
    ringer.check(NOW - 1)
    assert not ringer.ringing

    ringer.check(NOW + 1)
    assert wait_for(lambda: speaker.calls)
    assert ringer.ringing
    assert speaker.calls[0] == {"filename": "/sounds/alarm_sound.wav", "volume": 0.4,
                                "kind": "announcement", "cache": True}

    # Looped - each time the sound finishes, it's played again.
    speaker.playbacks[0].stop()
    assert wait_for(lambda: len(speaker.calls) >= 2)

    # Not rung a second time by the next check.
    ringer.check(NOW + 2)
    assert len({call["filename"] for call in speaker.calls}) == 1
    assert copy.due(NOW + 2) == []

    ringer.dismiss()
    assert not ringer.ringing
    assert all(p.stopped for p in speaker.playbacks)
    count = len(speaker.calls)
    time.sleep(0.05)
    assert len(speaker.calls) == count


def test_an_alarm_long_missed_isnt_rung(tmp_path):
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry()])
    ringer.check(NOW + fallback.GRACE + 1)
    assert speaker.calls == []
    assert not ringer.ringing
    assert copy.due(NOW + fallback.GRACE) != []


def test_the_away_report_says_what_rang_and_what_was_dismissed(tmp_path):
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry("alarm-1@1", NOW), entry("timer-3@1", NOW + 500)])
    ringer.check(NOW)
    assert wait_for(lambda: ringer.ringing)
    ringer.dismiss()
    ringer.check(NOW + 500)
    assert copy.away_report() == {"rang": ["alarm-1@1", "timer-3@1"], "dismissed": ["alarm-1@1"]}

    ringer.dismiss()
    copy.clear_report()
    assert copy.away_report() == {"rang": [], "dismissed": []}


def test_the_copy_lasts_through_a_restart(tmp_path):
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry("alarm-1@1", NOW), entry("alarm-2@1", NOW + 100)])
    ringer.check(NOW)
    ringer.dismiss()

    again = AlarmCopy(str(tmp_path / "copy.json"))
    assert [e["id"] for e in again.due(NOW + 100)] == ["alarm-2@1"]
    assert again.away_report() == {"rang": ["alarm-1@1"], "dismissed": ["alarm-1@1"]}

    restored = StateCopy(str(tmp_path / "state.json"))
    assert restored.volume("alarm") == 0.4
    assert restored.muted is False
    restored.update({"volumes": {"alarm": 0.2}, "muted": True})
    assert StateCopy(str(tmp_path / "state.json")).muted is True


def test_the_phrase_is_the_default_until_the_brain_sends_one(tmp_path):
    speaker = FakeSpeaker()
    phrase = Phrase(str(tmp_path / "phrase.wav"), "/sounds/cant_reach_server.wav")
    phrase.play(speaker, 0.7)
    assert speaker.calls[-1]["filename"] == "/sounds/cant_reach_server.wav"
    assert speaker.calls[-1]["volume"] == 0.7

    phrase.replace(b"\x00\x00" * 100, 22050)
    phrase.play(speaker, 0.7)
    assert speaker.calls[-1]["filename"] == str(tmp_path / "phrase.wav")
    with wave.open(str(tmp_path / "phrase.wav")) as file:
        assert (file.getframerate(), file.getnchannels(), file.getsampwidth()) == (22050, 1, 2)
        assert file.getnframes() == 100
    assert os.listdir(tmp_path) == ["phrase.wav"]
