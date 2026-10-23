"""Tests for what the satellite does while it can't reach its brain."""

import os
import threading
import time
import wave

from voice_assistant import fallback
from voice_assistant.fallback import AlarmCopy, FallbackRinger, Phrase, StateCopy

NOW = 1729587600.0


class FakePlayback:
    """Finishes only when stopped."""

    def __init__(self):
        self.stopped = False

    # A property, as the speaker's own Playback has it.
    @property
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


def test_quietening_stops_the_ringing_without_dismissing_it(tmp_path):
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry()])
    ringer.check(NOW)
    assert wait_for(lambda: speaker.calls)
    # What the brain is told is still ringing, to take over.
    assert ringer.ringing_ids() == ["alarm-1@1729587600"]

    ringer.quiet()
    assert not ringer.ringing and ringer.ringing_ids() == []
    assert all(p.stopped for p in speaker.playbacks)
    # The brain is told it rang but wasn't dismissed, so it takes the ringing over.
    assert copy.away_report() == {"rang": ["alarm-1@1729587600"], "dismissed": []}


def test_a_ring_is_known_of_by_whatever_asks_as_it_starts(tmp_path, monkeypatch):
    # Anything may ask what's ringing, from any thread - the satellite as the wake word
    # is heard or the brain comes back to take it over, and the end-to-end tests from
    # their own. Asked between the ring's thread being made and it starting, the
    # ringer used to forget it: the alarm rang on with nothing knowing, so the brain
    # would be told nothing was ringing and never take it over, and quietening it for
    # the brain would then stop it for good.
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry()])
    start = threading.Thread.start
    answers = []

    def asked_as_it_starts(thread):
        if thread.name.startswith("ring-"):
            asking = threading.Thread(target=lambda: answers.append(ringer.ringing_ids()))
            start(asking)
            # It has until the ring starts to ask. A ringer which lets it in then
            # answers at once; one which doesn't keeps it waiting its turn until this
            # runs out.
            asking.join(timeout=0.5)
        start(thread)

    with monkeypatch.context() as patched:
        patched.setattr(threading.Thread, "start", asked_as_it_starts)
        ringer.check(NOW)
    assert wait_for(lambda: speaker.calls and answers)
    # Ringing, and known to be - by whatever asked as well.
    assert ringer.ringing_ids() == ["alarm-1@1729587600"]
    assert answers == [["alarm-1@1729587600"]]

    ringer.quiet()
    assert all(playback.stopped for playback in speaker.playbacks)


def test_an_alarm_already_ringing_isnt_rung_again_when_the_brain_lists_it_anew(tmp_path):
    # The brain, catching up, sends a copy without an alarm it only knows it missed, so
    # the copy forgets it rang it - then one listing it due again, once it has taken it
    # over. Lost before it said so, it left the copy to ring the alarm a second time,
    # over the first - and quietening them left one playing.
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry()])
    ringer.check(NOW)
    assert wait_for(lambda: speaker.calls)
    copy.replace([])
    copy.replace([entry(due=NOW + 30)])

    ringer.check(NOW + 31)

    assert ringer.ringing_ids() == ["alarm-1@1729587600"]
    assert len(speaker.calls) == 1
    # Marked as rung all the same, so it isn't checked again.
    assert copy.due(NOW + 31) == []
    ringer.quiet()
    assert all(playback.stopped for playback in speaker.playbacks)


def test_a_ring_going_as_the_satellite_stops_carries_on_once_it_starts_again(tmp_path):
    # Its watchdog, a crash or the power. Knowing only that the alarm had rung, the
    # copy rang it no more - and the brain, told just that, neither rang it nor said
    # it had been missed.
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry()])
    ringer.check(NOW)
    assert wait_for(lambda: speaker.calls)
    # Stopping quietens it, but leaves it going.
    ringer.quiet()

    again, speaker = AlarmCopy(str(tmp_path / "copy.json")), FakeSpeaker()
    ringer = FallbackRinger(speaker, "/sounds", again, state)
    ringer.resume(NOW + 60)

    assert wait_for(lambda: speaker.calls)
    assert ringer.ringing_ids() == ["alarm-1@1729587600"]
    # Still rung, so not rung twice.
    ringer.check(NOW + 61)
    assert len(speaker.calls) == 1
    ringer.quiet()


def test_a_ring_which_has_stopped_for_good_isnt_carried_on(tmp_path):
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry("alarm-1@1", NOW), entry("alarm-2@1", NOW + 100), entry("alarm-3@1", NOW + 200)])
    # Dismissed...
    ringer.check(NOW)
    assert wait_for(lambda: ringer.ringing)
    ringer.dismiss()
    # ...handed to the brain, which says it has it...
    ringer.check(NOW + 100)
    assert wait_for(lambda: ringer.ringing)
    ringer.quiet()
    copy.taken_over(["alarm-2@1"])
    # ...and left going as the satellite stopped, but for longer than it would ring.
    ringer.check(NOW + 200)
    assert wait_for(lambda: ringer.ringing)
    ringer.quiet()

    again, speaker = AlarmCopy(str(tmp_path / "copy.json")), FakeSpeaker()
    ringer = FallbackRinger(speaker, "/sounds", again, state)
    ringer.resume(NOW + 200 + fallback.RING_LIMIT)

    assert not ringer.ringing and speaker.calls == []
    assert again.going() == []


def test_a_ring_which_rings_out_isnt_carried_on(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "RING_LIMIT", 0.05)
    copy, state, speaker, ringer = make(tmp_path)
    copy.replace([entry()])
    ringer.check(NOW)
    assert wait_for(lambda: speaker.calls)
    assert wait_for(lambda: not ringer.ringing)

    ringer.resume(NOW)
    assert not ringer.ringing and copy.going() == []


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

    # Only what was sent is forgotten - not what has happened since.
    sent = copy.away_report()
    ringer.dismiss()
    copy.clear_report(sent)
    assert copy.away_report() == {"rang": [], "dismissed": ["timer-3@1"]}


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
