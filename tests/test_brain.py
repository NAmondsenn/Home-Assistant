"""
Brain Test
Checks Nova's brain: each satellite let in, given a session of its own, and sent its
state, its copy of the timers and alarms, and the phrase; what rings, alerts and
routines sent to the satellite they belong to; and the brain's own start, stop and
restart notice - over real connections on this machine, with fake satellites which
speak the real protocol, and stand-ins for the sessions, Piper and Nemotron. Needs
numpy and soxr installed (as they are on the Pi), and is skipped anywhere they aren't.
"""

import json
import logging
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import brain as brain_module
from voice_assistant import clock as clock_module
from voice_assistant import memory as memory_module
from voice_assistant import paths, protocol
from voice_assistant import routines as routines_module
from voice_assistant import session as session_module
from voice_assistant.ntfy import Alert
from voice_assistant.recording import RATE
from voice_assistant.remote import RemoteSatellite
from voice_assistant.satellite_passwords import SatellitePasswords
from voice_assistant.vad import LoudnessDetector

BEDROOM = "the-bedroom-satellites-password"
KITCHEN = "the-kitchen-satellites-password"

# What a satellite says as it connects when nothing rang or was dismissed while it was away.
NOTHING_AWAY = {"rang": [], "dismissed": []}


def wait_for(condition, seconds=5):
    for _ in range(int(seconds / 0.005)):
        if condition():
            return True
        time.sleep(0.005)
    return False


class FakePiper:
    """Stands in for Piper, keeping what it was asked to say, which it says as a tenth of a second at half volume."""

    sample_rate = 22050

    def __init__(self):
        self.said = []
        self.closed = False

    def stream(self, message, on_start=None, remember=True):
        self.said.append(message)

        def pieces():
            if on_start:
                on_start()
            yield np.full(2205, 0.5, np.float32)
        return pieces()

    def close(self):
        self.closed = True


class FakeTranscription:
    def __init__(self, text=""):
        self.text = text
        self.cancelled = False

    def add(self, audio):
        pass

    def finish(self, audio):
        return {"text": self.text}

    def cancel(self):
        self.cancelled = True


class FakeSpeechToText:
    """Stands in for Nemotron, hearing in each recording the next of the texts the test gives."""

    loaded = True

    def __init__(self):
        self.texts = []
        self.made = []

    def listen(self):
        self.made.append(FakeTranscription(self.texts.pop(0) if self.texts else ""))
        return self.made[-1]


class FakeSession:
    """
    Stands in for a satellite's session, keeping what it was asked to say and run, and
    noting in happened - which every session shares - when each was made and closed.
    """

    def __init__(self, brain, satellite, happened):
        self.brain = brain
        self.satellite = satellite
        self.happened = happened
        self.number = sum(1 for event in happened if event[0] == "made") + 1
        happened.append(("made", satellite.name, self.number))
        self.announced, self.alerts, self.routines = [], [], []
        self.volume, self.music_player, self.actions, self.home = (object() for _ in range(4))
        self.closed = threading.Event()
        # Never stuck, unless a test says it is.
        self.moved_at = float("inf")

    def send_state(self):
        return self.satellite.set_state({"general": 0.5, "alarm": 0.8, "music": 0.4}, False)

    def run(self):
        while self.satellite.connected and not self.closed.is_set():
            time.sleep(0.01)
        self.close()

    def close(self):
        if not self.closed.is_set():
            self.closed.set()
            self.happened.append(("closed", self.satellite.name, self.number))

    def announce(self, message, sound="announcement_sound.wav", boost=False, still_wanted=None,
                 start=0.0, length=None, level=1.0):
        self.announced.append(message)

    def alert(self, alert):
        self.alerts.append(alert.headline)

    def run_routine(self, routine):
        self.routines.append(routine["name"])


class FakeNtfy:
    """Stands in for ntfy, keeping what was sent."""

    def __init__(self):
        self.reminders, self.restarts = "reminders", "homelab"
        self.sent = []

    def send_soon(self, topic, message, title="", priority=0):
        self.sent.append((topic, title, message))

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def passwords(tmp_path, monkeypatch):
    """The satellites the brain lets in - the bedroom first, so it's the default - kept in the test's own folder."""
    kept = SatellitePasswords(str(tmp_path / "satellites.json"))
    kept.add("bedroom", BEDROOM)
    kept.add("kitchen", KITCHEN)
    monkeypatch.setattr(brain_module, "SatellitePasswords", lambda: kept)
    return kept


@pytest.fixture
def make_brain(tmp_path, monkeypatch, make_config, passwords):
    """
    Makes brains which keep everything in the test's own folder, with stand-ins for the
    sessions, Piper and Nemotron, stopping them after the test.
    """
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(clock_module, "TIMERS_FILE", str(tmp_path / "timers.json"))
    monkeypatch.setattr(clock_module, "ALARMS_FILE", str(tmp_path / "alarms.json"))
    monkeypatch.setattr(memory_module, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(routines_module, "ROUTINES_FILE", str(tmp_path / "routines.json"))
    monkeypatch.setattr(brain_module, "RUNNING_MARK", str(tmp_path / "running"))
    # Short, so a satellite which has gone is noticed - and let go - quickly, and so
    # is the brain being stopped.
    monkeypatch.setattr(brain_module, "SOCKET_TIMEOUT", 0.1)
    monkeypatch.setattr(brain_module, "STOP_CHECK", 0.02)
    monkeypatch.setattr(brain_module, "TextToSpeech", lambda config: FakePiper())
    monkeypatch.setattr(brain_module, "_speech_to_text", lambda config: FakeSpeechToText())
    happened = []
    monkeypatch.setattr(brain_module, "Session", lambda brain, satellite: FakeSession(brain, satellite, happened))
    made = []

    def make(**sections):
        # One announcement a round, and no pauses, so ringing never sleeps in a test.
        config = make_config(assistant={"name": "Nova", "wake_word": "Hey Nova"}, backend={"port": 0},
                             timers={"announcements": 1, "repeat_gap": 0}, **sections)
        made.append(brain_module.Brain(config))
        return made[-1]

    yield make
    for brain in made:
        brain.stop()


@pytest.fixture
def brain(make_brain):
    return make_brain()


def serving(brain):
    """Has the brain listen for satellites, on a thread of its own, as run() does on the main one."""
    threading.Thread(target=brain.serve, daemon=True).start()
    assert wait_for(lambda: brain.port)


class Satellite:
    """
    A satellite at the far end of a real connection to the brain: it shakes hands as
    satellite_link does, says what happened while it was away, keeps everything the
    brain sends, and plays as a satellite would - each of its own sounds and each
    stream finished as soon as it has all arrived.
    """

    def __init__(self, brain, name="bedroom", password=BEDROOM, unclean=False, away=NOTHING_AWAY):
        self.socket = socket.create_connection(("127.0.0.1", brain.port), timeout=5)
        self.stream = self.socket.makefile("rwb")
        self.lock = threading.Lock()
        self.received = []
        # Set once the brain has let it go.
        self.gone = threading.Event()
        try:
            protocol.greet(self.stream, name, f"{name}-pi", password, 48000, unclean)
        except BaseException:
            # As satellite_link does, so the brain sees it hang up.
            self.socket.close()
            raise
        self.socket.settimeout(None)
        if away is not None:
            self.say("away", **away)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                message, payload = protocol.receive(self.stream)
                self.received.append((message, payload))
                if message["type"] in ("play_sound", "stream_end"):
                    self.say("played", id=message["id"])
        except (OSError, ValueError, protocol.ProtocolError):
            pass
        finally:
            self.gone.set()

    def say(self, kind, payload=b"", **fields):
        with self.lock:
            protocol.send(self.stream, kind, payload, **fields)

    def send_audio(self, audio):
        for begin in range(0, len(audio), 1600):
            self.say("audio", audio[begin:begin + 1600].astype("<i2").tobytes())

    def messages(self, kind):
        return [message for message, _ in self.received if message["type"] == kind]

    def copy(self):
        """The last copy of its timers and alarms the brain sent, or None."""
        sent = self.messages("alarms")
        return sent[-1]["entries"] if sent else None

    def go(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


@pytest.fixture
def connect():
    """Connects satellites to a brain, letting them all go after the test."""
    made = []

    def connect(brain, *args, **kwargs):
        made.append(Satellite(brain, *args, **kwargs))
        return made[-1]

    yield connect
    for satellite in made:
        satellite.go()


def connected(brain, name="bedroom"):
    """The satellite's session, once it has connected and caught up with what happened while it was away."""
    assert wait_for(lambda: brain.session_for(name) is not None and name in brain._caught_up)
    return brain.session_for(name)


# Satellites connecting

def test_whatever_sees_the_brain_listening_knows_its_port(brain):
    # As test_end_to_end's make_brain does, connecting a satellite straight after: the
    # port was set from the socket only once the brain was seen listening, so could
    # still be 0, the port it was asked for.
    seen = []

    class Watched(brain_module.Brain):
        def __setattr__(self, name, value):
            if name == "_server" and value is not None:
                seen.append(self.port)
            super().__setattr__(name, value)

    brain.__class__ = Watched
    serving(brain)

    assert seen == [brain.port] and brain.port != 0


def test_a_satellite_which_connects_gets_its_state_alarms_and_phrase(brain, connect):
    brain.clock.set_timer(600, satellite="bedroom")
    brain.clock.set_timer(900, satellite="kitchen")
    serving(brain)
    satellite = connect(brain)
    assert wait_for(lambda: satellite.messages("away_settled"))

    # And, once it has said what happened while it was away, that the brain has it.
    assert [message["type"] for message, _ in satellite.received] == ["state", "alarms", "phrase", "away_settled"]
    # Its volumes and mute, as its session has them.
    assert satellite.messages("state")[0] == {"type": "state", "volumes": {"general": 0.5, "alarm": 0.8, "music": 0.4},
                                             "muted": False}
    # Its own timers, and nobody else's, to ring while it can't reach the brain.
    entries = satellite.messages("alarms")[0]["entries"]
    assert entries == brain.clock.copy_for("bedroom") and len(entries) == 1
    # "I can't reach my server", as the brain's Piper says it.
    message, payload = satellite.received[2]
    assert brain.tts.said[0] == "Sorry, I can't reach my server right now."
    assert message["rate"] == 22050
    assert np.array_equal(np.frombuffer(payload, "<i2"), np.full(2205, 16383))
    assert connected(brain).satellite.hostname == "bedroom-pi"


def test_a_second_connection_replaces_the_first(brain, connect):
    serving(brain)
    first = connect(brain)
    old = connected(brain)
    second = connect(brain)
    assert wait_for(lambda: brain.session_for("bedroom") not in (None, old))
    new = brain.session_for("bedroom")

    # The first is let go, its session closed before the second's was made - both
    # would be the same device in Home Assistant, with the same MQTT client id.
    assert first.gone.wait(5)
    assert old.happened == [("made", "bedroom", 1), ("closed", "bedroom", 1), ("made", "bedroom", 2)]
    # Going, it leaves the second's session be.
    assert brain.session_for("bedroom") is new and not new.closed.is_set()
    assert second.messages("phrase") and not second.gone.is_set()


def test_a_satellite_removed_while_connected_is_let_go(brain, connect, passwords):
    # Its password was only checked as it connected, so it stayed for as long as it
    # stayed connected - and removing a satellite is meant to shut it out.
    serving(brain)
    bedroom, kitchen = connect(brain), connect(brain, "kitchen", KITCHEN)
    connected(brain)
    connected(brain, "kitchen")

    # As remove-satellite does, run by itself.
    passwords.remove("kitchen")
    assert kitchen.gone.wait(5)
    assert wait_for(lambda: brain.session_for("kitchen") is None and "kitchen" not in brain.sessions)
    # Every other is left be.
    assert brain.session_for("bedroom") is not None and not bedroom.gone.is_set()


def test_a_satellite_removed_while_its_session_is_made_isnt_let_stay(brain, passwords):
    # The bad order: the brain checks which satellites are still let in while this one's
    # session is being made, so it isn't there to be let go - and, the file seen, that
    # is never checked again. Its session must be refused when it's made instead.
    removed = SimpleNamespace(name="kitchen", connected=False, close=lambda: None)
    passwords.remove("kitchen")
    brain._shut_out_removed()
    assert brain.session_for("kitchen") is None

    assert brain._start_session(removed) is None

    assert brain.session_for("kitchen") is None and "kitchen" not in brain.sessions
    assert brain._caught_up == set()


def test_a_satellite_still_let_in_gets_its_session(brain, passwords):
    kept = SimpleNamespace(name="kitchen", connected=False, close=lambda: None)

    session = brain._start_session(kept)

    assert session is not None and brain.sessions["kitchen"] is session


def test_a_timer_rings_on_its_satellite_or_waits_for_its_copy(brain, connect):
    serving(brain)
    brain.clock.set_timer(60, satellite="bedroom")
    brain.clock.set_timer(60, label="the pizza", satellite="kitchen")
    bedroom = connect(brain)
    kitchen = connect(brain, "kitchen", KITCHEN)
    connected(brain, "kitchen")
    assert wait_for(lambda: kitchen.copy())
    kitchen.go()
    assert wait_for(lambda: brain.session_for("kitchen") is None)
    session = connected(brain)

    brain.clock._tick(time.time() + 61)
    # Rung on the satellite it was set from, which needn't be told which that is - in
    # as many rounds as a tick a minute on brings.
    assert session.announced and set(session.announced) == {"Timer for 1 minute."}
    # The kitchen isn't there, so the brain says nothing of its reminder: the kitchen
    # rings it from its own copy.
    assert [entry["kind"] for entry in kitchen.copy()] == ["timer"]
    # The bedroom's copy is brought up to date as its timer starts ringing.
    assert wait_for(lambda: bedroom.copy() == brain.clock.copy_for("bedroom"))


def test_what_was_dismissed_while_away_doesnt_ring_again(brain, connect):
    serving(brain)
    brain.clock.set_timer(60, satellite="bedroom")
    first = connect(brain)
    assert wait_for(lambda: first.messages("alarms"))
    timer = first.messages("alarms")[0]["entries"][0]["id"]
    first.go()
    assert wait_for(lambda: brain.session_for("bedroom") is None)

    # The brain was out of reach as the timer came due, so the satellite rang it from
    # its copy, and the user dismissed it there.
    back = connect(brain, away={"rang": [timer], "dismissed": [timer]})
    session = connected(brain)
    brain.clock._tick(time.time() + 61)

    assert session.announced == [] and not brain.clock.is_ringing()
    # It's gone from the satellite's copy too.
    assert wait_for(lambda: back.copy() == [])


def test_what_came_due_while_the_brain_was_off_waits_for_its_satellite(make_brain, connect, tmp_path):
    # Both timers came due while the brain was off. The kitchen rang its own from its
    # copy, so only the bedroom's is worth a "While I was off".
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 3, "timers": [
        {"id": 1, "due_at": now - 60, "duration": 600, "label": None, "satellite": "bedroom"},
        {"id": 2, "due_at": now - 90, "duration": 900, "label": None, "satellite": "kitchen"}]}))
    brain = make_brain()
    serving(brain)
    brain.clock._tick(time.time())

    connect(brain, "kitchen", KITCHEN, away={"rang": [f"timer-2@{int(now - 90)}"], "dismissed": []})
    kitchen = connected(brain, "kitchen")
    brain.clock._tick(time.time())
    connect(brain)
    bedroom = connected(brain)
    brain.clock._tick(time.time())

    assert kitchen.announced == []
    assert len(bedroom.announced) == 1 and bedroom.announced[0].startswith("While I was off: your 10 minutes timer.")


def test_what_a_satellite_was_still_ringing_as_the_brain_came_back_rings_on(make_brain, connect, tmp_path):
    # The brain restarted as the alarm came due - a crash, the watchdog or an update -
    # so the satellite rang it from its copy, and was still ringing it as it reached the
    # brain again. It stops then, for the brain to take over - which only rang what it
    # was ringing itself, so the alarm went quiet half a minute in, and was never said.
    now = time.time()
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": None, "due_at": now - 30, "satellite": "bedroom"}]}))
    brain = make_brain()
    brain.clock.alarm_ring_duration = 0
    serving(brain)
    alarm = f"alarm-1@{int(now - 30)}"

    connect(brain, away={"rang": [alarm], "dismissed": [], "ringing": [alarm]})
    session = connected(brain)
    assert brain.clock.is_ringing()
    assert len(session.announced) == 1 and session.announced[0].endswith("Time to get up.")
    # And not said to have been missed as well.
    brain.clock._tick(time.time())
    assert len(session.announced) == 1


def test_a_satellite_is_told_once_what_happened_while_it_was_away_is_saved(brain, connect, tmp_path,
                                                                          monkeypatch):
    # It rings on what it was still ringing, and keeps its report, until then - so a
    # brain lost before it has saved them can't lose them.
    brain.clock.set_timer(60, satellite="bedroom")
    timer = brain.clock.copy_for("bedroom")[0]["id"]
    saved = []
    send = RemoteSatellite.send

    def sending(satellite, kind, payload=b"", /, **fields):
        if kind == "away_settled":
            saved.append(json.loads((tmp_path / "timers.json").read_text())["timers"])
        return send(satellite, kind, payload, **fields)

    monkeypatch.setattr(RemoteSatellite, "send", sending)
    serving(brain)
    satellite = connect(brain, away={"rang": [timer], "dismissed": [timer]})

    assert wait_for(lambda: satellite.messages("away_settled"))
    assert satellite.messages("away_settled") == [{"type": "away_settled"}]
    # The timer dismissed there was already gone from the brain's file.
    assert saved == [[]]


def test_a_satellite_isnt_told_its_report_is_settled_when_applying_it_fails(brain, connect, monkeypatch):
    def failing(*args, **kwargs):
        raise RuntimeError("A fault in catching up")

    monkeypatch.setattr(brain.clock, "apply_away", failing)
    serving(brain)
    satellite = connect(brain)

    # Its session ends there, and it's let go.
    assert satellite.gone.wait(5)
    assert satellite.messages("phrase") and not satellite.messages("away_settled")


def test_a_satellite_which_says_nothing_of_while_it_was_away_isnt_told_its_settled(brain, connect, monkeypatch):
    monkeypatch.setattr(brain_module, "AWAY_WAIT", 0.1)
    serving(brain)
    satellite = connect(brain, away=None)
    connected(brain)

    # Let go, so it has heard everything the brain sent it.
    brain.stop()
    assert satellite.gone.wait(5)
    assert satellite.messages("phrase") and not satellite.messages("away_settled")


def test_a_satellite_which_goes_mid_turn_leaves_the_brain_working(brain, connect, monkeypatch):
    # The real session this time, with speech told apart by how loud it is.
    monkeypatch.setattr(brain_module, "Session", session_module.Session)
    monkeypatch.setattr(session_module, "make_detector", lambda config: LoudnessDetector())
    monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume: None)
    serving(brain)
    satellite = connect(brain)
    first = connected(brain)

    # Woken, and gone part way through what the user says.
    satellite.say("wake", talked_over=False)
    satellite.send_audio(np.full(RATE, 8000, np.int16))
    assert wait_for(lambda: brain.stt.made)
    satellite.go()
    assert wait_for(lambda: brain.session_for("bedroom") is None)
    assert brain.stt.made[0].cancelled and "bedroom" not in brain.sessions

    # Back again, and answered as ever.
    back = connect(brain)
    assert connected(brain) is not first
    brain.stt.texts = ["What time is it?"]
    back.say("wake", talked_over=False)
    back.send_audio(np.concatenate([np.zeros(round(0.3 * RATE)), np.full(RATE // 2, 8000), np.zeros(RATE)]))
    assert wait_for(lambda: back.messages("allow"))
    assert brain.tts.said[-1].startswith("It's ")


class FakeDevice:
    """Stands in for a session's device in Home Assistant, connected to the broker from start until stop."""

    def __init__(self):
        self.connected = False

    def start(self):
        self.connected = True

    def stop(self):
        self.connected = False


def test_a_satellite_replaced_while_catching_up_leaves_no_device_behind(brain, connect, monkeypatch):
    # The first connection was still waiting to hear what happened while it was away
    # when the second replaced it. Its device was started once it stopped waiting, and
    # stayed connected for good - with the same MQTT client id as the second's, so the
    # two knocked each other off Home Assistant.
    devices = []

    def make_device(*args, **kwargs):
        devices.append(FakeDevice())
        return devices[-1]

    monkeypatch.setattr(brain_module, "Session", session_module.Session)
    monkeypatch.setattr(session_module, "make_detector", lambda config: LoudnessDetector())
    monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume: None)
    monkeypatch.setattr(session_module, "NovaDevice", SimpleNamespace(from_config=make_device))
    monkeypatch.setattr(brain_module, "AWAY_WAIT", 0.5)
    serving(brain)
    first = connect(brain, away=None)
    assert wait_for(lambda: brain.session_for("bedroom") is not None)
    replaced = brain.session_for("bedroom")
    connect(brain)
    assert connected(brain) is not replaced

    assert first.gone.wait(5)
    assert len(devices) == 2
    assert not devices[0].connected and devices[1].connected


def test_alerts_go_to_the_default_satellite(brain, connect):
    serving(brain)
    connect(brain, "kitchen", KITCHEN)
    kitchen = connected(brain, "kitchen")
    # The bedroom, the first satellite added, isn't there to say it.
    brain._alert(Alert("homelab", "Backup failed", "Error 5."))

    connect(brain)
    bedroom = connected(brain)
    brain._alert(Alert("homelab", "Disk nearly full", ""))
    assert bedroom.alerts == ["Disk nearly full"] and kitchen.alerts == []


def test_a_routine_runs_on_its_own_satellite_or_the_default(brain, connect):
    serving(brain)
    connect(brain)
    connect(brain, "kitchen", KITCHEN)
    bedroom, kitchen = connected(brain), connected(brain, "kitchen")

    brain.run_routine({"name": "Good morning", "satellite": None})
    brain.run_routine({"name": "Dinner time", "satellite": "kitchen"})
    assert bedroom.routines == ["Good morning"] and kitchen.routines == ["Dinner time"]


def test_an_unclean_satellite_is_told_to_the_phone(brain, connect):
    brain.ntfy = FakeNtfy()
    serving(brain)
    clean = connect(brain)
    assert wait_for(lambda: clean.messages("phrase"))
    assert brain.ntfy.sent == []

    # Started again without having stopped properly - a crash, the watchdog, or the power.
    connect(brain, unclean=True)
    assert wait_for(lambda: brain.ntfy.sent)
    assert brain.ntfy.sent == [("homelab", "Nova's bedroom satellite restarted",
                                "Nova's bedroom satellite stopped unexpectedly, and has started again.")]

    # And not again straight away, if it keeps happening.
    again = connect(brain, unclean=True)
    assert wait_for(lambda: again.messages("phrase"))
    assert len(brain.ntfy.sent) == 1


def test_a_satellite_with_the_wrong_password_is_logged_once_by_name(brain, connect, caplog):
    # It hangs up as soon as the brain can't prove it knows its password, so all the
    # brain sees is the connection closing - every time it tries again.
    serving(brain)
    for _ in range(3):
        with pytest.raises(protocol.Refused):
            connect(brain, password="not-the-bedrooms-password")
    time.sleep(0.5)
    # Most likely - it also hangs up on a brain too slow to answer it.
    assert len([line for line in caplog.messages
                if "bedroom" in line and "password" in line and "most likely" in line]) == 1


def test_whats_unowned_goes_to_the_default_before_the_clock_starts(brain):
    # Set before there were satellites, when the Pi was all of Nova.
    brain.clock.set_timer(600)
    brain.serve = lambda: None
    brain.run()
    assert len(brain.clock.overview(satellite="bedroom")["timers"]) == 1


def test_whats_unowned_goes_to_the_default_once_there_is_one(make_brain, passwords, connect):
    passwords.remove("bedroom")
    passwords.remove("kitchen")
    brain = make_brain()
    brain.clock.set_timer(600)
    serving(brain)

    passwords.add("bedroom", BEDROOM)
    satellite = connect(brain)
    connected(brain)
    assert len(brain.clock.overview(satellite="bedroom")["timers"]) == 1
    assert wait_for(lambda: satellite.copy()) and len(satellite.copy()) == 1


def test_each_satellites_copy_is_sent_again_now_and_then(brain, connect, monkeypatch):
    # A repeating alarm's next week comes into the copy a day at a time, with nothing
    # having changed to send it.
    monkeypatch.setattr(brain_module, "ALARMS_RESENT", 0.05)
    serving(brain)
    satellite = connect(brain)
    connected(brain)
    threading.Thread(target=brain._send_alarms_hourly, daemon=True).start()
    assert wait_for(lambda: len(satellite.messages("alarms")) >= 3)


def test_listening_for_satellites_tells_the_watchdog_the_brain_is_working(brain, monkeypatch):
    beats = []
    monkeypatch.setattr(brain_module.watchdog, "beat", lambda at=None: beats.append(at))
    serving(brain)
    assert wait_for(lambda: len(beats) >= 2)
    # With no session to be stuck, it's working now.
    assert time.monotonic() - beats[-1] < 1


def test_a_session_stuck_part_way_through_a_turn_stops_the_watchdog(brain, connect, monkeypatch):
    # Its satellite stayed connected and pinging, so the brain looked healthy while
    # every wake word there got silence. Listening for satellites goes on regardless,
    # so it tells the watchdog when the stillest session last moved - see Session.moved_at.
    beats = []
    monkeypatch.setattr(brain_module.watchdog, "beat", lambda at=None: beats.append(at))
    serving(brain)
    connect(brain)
    session = connected(brain)
    session.moved_at = time.monotonic() - 1000
    told = len(beats)
    assert wait_for(lambda: len(beats) > told + 1)
    assert beats[-1] == session.moved_at


def test_the_settings_page_is_given_the_brain_to_look_after_every_satellite_by(make_brain, make_config):
    brain = make_brain(settings_page={"port": 8765, "allow": ["you@example.com"]})
    assert brain.settings_page.brain is brain
    assert brain.satellites() == ["bedroom", "kitchen"]


def test_a_restart_stops_the_brain_cleanly_and_says_it_was_asked_for(brain, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr(brain_module, "RESTART_DELAY", 0)
    done = threading.Event()
    threading.Thread(target=lambda: (brain.run(), done.set()), daemon=True).start()
    assert wait_for(lambda: brain.port)
    assert not brain.restarting
    assert brain.restart() == "Restarting - give it half a minute."
    assert done.wait(5) and brain.restarting
    # A clean stop, so the next start isn't taken for a crash.
    assert not os.path.exists(brain_module.RUNNING_MARK)


def test_a_restart_outside_systemd_is_left_to_the_user(brain, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert brain.restart() == "Nova isn't running as a service here, so restart it yourself."
    assert not brain.restarting


# The brain's own start and stop

class Recorder:
    """Stands in for the clock, a session or Piper, noting the order they're let go in."""

    def __init__(self, name, closed):
        self.name = name
        self.closed = closed
        self.satellite = SimpleNamespace(close=lambda: None)

    def stop(self):
        self.closed.append(self.name)

    def close(self):
        self.closed.append(self.name)

    def start(self):
        pass

    def assign_unowned(self, satellite):
        return 0


@pytest.fixture
def stopping(brain):
    """A brain whose clock, one session and Piper note the order they're let go in."""
    brain.closed = []
    brain.clock = Recorder("clock", brain.closed)
    brain.sessions["bedroom"] = Recorder("bedroom", brain.closed)
    brain.tts = Recorder("tts", brain.closed)
    return brain


def test_a_stop_signal_ends_the_brain_and_everything_is_closed_once_in_order(stopping):
    # The signal handler used to close everything itself, in the middle of whatever
    # the main loop was doing.
    stopping.serve = stopping.shutdown

    with pytest.raises(SystemExit) as stopped:
        stopping.run()

    assert stopped.value.code == 0
    assert stopping.closed == ["clock", "bedroom", "tts"]


def test_a_second_stop_signal_doesnt_cut_the_cleanup_short(stopping):
    def stop_twice():
        try:
            stopping.shutdown()
        finally:
            assert stopping.shutdown() is None

    stopping.serve = stop_twice
    with pytest.raises(SystemExit):
        stopping.run()
    assert stopping.closed == ["clock", "bedroom", "tts"]


def test_the_brain_needs_none_of_the_satellites_audio_libraries(monkeypatch):
    # The brain has no microphone or speaker, so no PortAudio for sounddevice to load.
    # Imported afresh, with every module of Nova's put back as it was afterwards.
    import importlib
    import sys

    before = set(sys.modules)
    for name in [name for name in sys.modules if name.partition(".")[0] in ("voice_assistant", "backend")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    try:
        importlib.import_module("backend")
    finally:
        for name in set(sys.modules) - before:
            if name.partition(".")[0] in ("voice_assistant", "backend"):
                del sys.modules[name]


def test_importing_the_brain_writes_nothing():
    # Logging used to be set up on import, creating logs/ and writing to it.
    import backend  # noqa: F401

    assert all("backend.log" not in getattr(handler, "baseFilename", "")
               for handler in logging.getLogger().handlers)


def test_a_start_after_a_run_which_didnt_stop_properly_is_told_to_the_phone(brain):
    brain.ntfy = FakeNtfy()
    # The first start has nothing to tell.
    brain._note_start()
    assert brain.ntfy.sent == []

    # Started again without having stopped properly - a crash, or the watchdog.
    brain._note_start()
    assert brain.ntfy.sent == [("homelab", "Nova restarted", "Nova stopped unexpectedly, and has started again.")]

    # And not again straight away, if it keeps happening.
    brain._note_start()
    assert len(brain.ntfy.sent) == 1


def test_a_start_after_stopping_properly_isnt_told(brain):
    brain.ntfy = FakeNtfy()
    brain._note_start()
    brain._clean_up()
    brain._note_start()
    assert brain.ntfy.sent == []


def test_only_a_reminder_nobody_answered_goes_to_the_phone(brain):
    brain.ntfy = FakeNtfy()
    brain._unanswered({"label": "take the bins out"}, "clock", satellite="bedroom")
    # A plain timer, and an alarm, have nothing more to say there.
    brain._unanswered({"label": None}, "clock")
    brain._unanswered({"label": "wake up"}, "alarm")
    assert brain.ntfy.sent == [("reminders", "Reminder nobody answered", "Take the bins out")]


def test_a_routine_runs_by_itself_when_its_due(brain, monkeypatch):
    monkeypatch.setattr(brain_module, "ROUTINE_CHECK", 0.01)
    due, ran = [[{"name": "Good morning"}]], []

    def next_due():
        if not due:
            brain.running = False
            return []
        return due.pop(0)

    brain.running = True
    brain.routines = SimpleNamespace(due=next_due)
    brain.run_routine = lambda routine: ran.append(routine["name"])
    brain._run_routines_when_due()

    assert wait_for(lambda: ran)
    assert ran == ["Good morning"]
