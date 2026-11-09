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
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests

np = pytest.importorskip("numpy")
pytest.importorskip("soxr")

from voice_assistant import brain as brain_module
from voice_assistant import clock as clock_module
from voice_assistant import memory as memory_module
from voice_assistant import ntfy as ntfy_module
from voice_assistant import paths, protocol
from voice_assistant import routines as routines_module
from voice_assistant import session as session_module
from voice_assistant import settings_page as settings_page_module
from voice_assistant.batteries import Battery, BatteryChoices, BatteryWatcher
from voice_assistant.config import Config
from voice_assistant.displays import Displays
from voice_assistant.google_calendar import GoogleCalendar
from voice_assistant.ha_registry import HomeRegistry
from voice_assistant.media_watch import MediaWatcher
from voice_assistant.ntfy import Alert
from voice_assistant.recording import RATE
from voice_assistant.remote import RemoteSatellite
from voice_assistant.satellite_passwords import SatellitePasswords
from voice_assistant.vad import LoudnessDetector
from voice_assistant.volume import VolumeControl

BEDROOM = "the-bedroom-satellites-password"
KITCHEN = "the-kitchen-satellites-password"
HALL = "the-hall-satellites-password"

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
        self.speed = 1.0

    def set_speed(self, speed):
        self.speed = speed

    def stream(self, message, on_start=None, remember=True, speed=None):
        self.said.append(message)

        def pieces():
            if on_start:
                on_start()
            # Quieter the faster it's set, so what's said at each speed can be told apart.
            yield np.full(2205, 0.5 / self.speed, np.float32)
        return pieces()

    def close(self):
        self.closed = True


class FakeTranscription:
    def __init__(self, text=""):
        self.text = text
        self.cancelled = False

    def add(self, audio):
        pass

    def finish(self, audio, **how):
        return {"text": self.text}

    def cancel(self):
        self.cancelled = True


class FakeSpeechToText:
    """Stands in for Nemotron, hearing in each recording the next of the texts the test gives."""

    loaded = True
    hung = False

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
        # What announce and alert return, as the real ones do - whether it was said.
        self.said = True
        self.volume, self.music_player, self.actions, self.home = (object() for _ in range(4))
        self.closed = threading.Event()
        # Never stuck, unless a test says it is.
        self.moved_at = float("inf")
        self.activity = "idle"
        self.muted = False
        # How many times Stop was pressed for it.
        self.stopped = 0

    def send_state(self):
        longest = self.brain.config.section("thresholds").get("recording_timeout", 15.0)
        return self.satellite.set_state({"general": 0.5, "alarm": 0.8, "music": 0.4}, False, longest)

    def run(self):
        while self.satellite.connected and not self.closed.is_set():
            time.sleep(0.01)
        self.close()

    def close(self):
        if not self.closed.is_set():
            self.closed.set()
            self.happened.append(("closed", self.satellite.name, self.number))

    def announce(self, message, sound="announcement_sound.wav", boost=False, still_wanted=None,
                 start=0.0, length=None, level=1.0, ringing=False):
        self.announced.append(message)
        self.rung = getattr(self, "rung", []) + [ringing]
        return self.said

    def alert(self, alert):
        self.alerts.append(alert.headline)
        return self.said

    def run_routine(self, routine):
        self.routines.append(routine["name"])

    def stop_what_rings_and_speaks(self):
        self.stopped += 1


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
    monkeypatch.setattr(brain_module, "_speech_to_text", lambda config, tts: FakeSpeechToText())
    happened = []
    monkeypatch.setattr(brain_module, "Session", lambda brain, satellite: FakeSession(brain, satellite, happened))
    monkeypatch.setattr(brain_module, "Displays", lambda: Displays(str(tmp_path / "displays.json")))
    made = []

    def make(**sections):
        # The battery marks come from the settings with no fallback of the brain's own, as a real Config
        # always has config.yaml's - which a FakeConfig has only if it's given them.
        sections.setdefault("batteries", Config(path=str(tmp_path / "none.yaml"), quiet=True).section("batteries"))
        # One announcement a round, and no pauses, so ringing never sleeps in a test.
        config = make_config(assistant={"name": "Nova", "wake_word": "Hey Nova"}, backend={"port": 0},
                             timers={"announcements": 1, "repeat_gap": 0}, **sections)
        made.append(brain_module.Brain(config))
        return made[-1]

    yield make
    for brain in made:
        brain.stop()
    # For a brain which listened (see serving), stopped for good: the thread which listened
    # has ended, and every session with it - each satellite's own thread forgets its
    # session as the brain lets it go. One a test put in by hand has no such thread.
    for brain in made:
        listening = getattr(brain, "listening", None)
        if listening is not None:
            listening.join(5)
            assert not listening.is_alive(), "The thread which listened for satellites outlived stop()"
            assert wait_for(lambda: not brain.sessions), f"stop() left sessions: {sorted(brain.sessions)}"


@pytest.fixture
def brain(make_brain):
    return make_brain()


def serving(brain):
    """Has the brain listen for satellites, on a thread of its own, as run() does on the main one."""
    brain.listening = threading.Thread(target=brain.serve, daemon=True)
    brain.listening.start()
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


def test_a_stop_signal_just_as_the_brain_starts_listening_leaves_nothing_for_stop_to_wait_on(brain, monkeypatch):
    # The signal's SystemExit can land anywhere in serve(). Landing after the server was
    # noted but before it ran, it left stop() calling shutdown() on a loop which never
    # started - and that waits for ever.
    def signalled(message, *args, **kwargs):
        if message.startswith("Listening for satellites"):
            raise SystemExit(0)

    monkeypatch.setattr(brain_module.logger, "info", signalled)
    with pytest.raises(SystemExit):
        brain.serve()

    # Taken away first, so that a failing test can't hang the fixture's own stop().
    stuck, brain._server = brain._server, None
    assert stuck is None


def test_a_satellite_which_connects_gets_its_state_alarms_and_phrase(brain, connect):
    brain.clock.set_timer(600, satellite="bedroom")
    brain.clock.set_timer(900, satellite="kitchen")
    serving(brain)
    satellite = connect(brain)
    assert wait_for(lambda: satellite.messages("away_settled"))

    # And, once it has said what happened while it was away, its copy as that left it and
    # that the brain has it.
    assert [message["type"] for message, _ in satellite.received] == ["state", "alarms", "phrase", "alarms",
                                                                      "away_settled"]
    # Its volumes and mute, as its session has them.
    assert satellite.messages("state")[0] == {"type": "state", "volumes": {"general": 0.5, "alarm": 0.8, "music": 0.4},
                                             "muted": False, "longest_request": 15.0}
    # Its own timers, and nobody else's, to ring while it can't reach the brain.
    entries = satellite.messages("alarms")[0]["entries"]
    assert entries == brain.clock.copy_for("bedroom") and len(entries) == 1
    # "I can't reach my server", as the brain's Piper says it.
    message, payload = satellite.received[2]
    assert brain.tts.said[0] == "Sorry, I can't reach my server right now."
    assert message["rate"] == 22050
    assert np.array_equal(np.frombuffer(payload, "<i2"), np.full(2205, 16383))
    assert connected(brain).satellite.hostname == "bedroom-pi"


def test_a_second_connection_replaces_the_first(brain, connect, caplog):
    serving(brain)
    first = connect(brain)
    old = connected(brain)
    with caplog.at_level(logging.INFO, logger="voice_assistant.brain"):
        second = connect(brain)
        assert wait_for(lambda: brain.session_for("bedroom") not in (None, old))
        new = brain.session_for("bedroom")

        # The first is let go, its session closed before the second's was made - both
        # would be the same device in Home Assistant, with the same MQTT client id.
        assert first.gone.wait(5)
        assert old.happened == [("made", "bedroom", 1), ("closed", "bedroom", 1), ("made", "bedroom", 2)]
        # Going, it leaves the second's session be - checked once it has gone
        # altogether, which being let go doesn't promise.
        assert wait_for(lambda: any(record.message == "The bedroom satellite went" for record in caplog.records))
    assert brain.session_for("bedroom") is new and not new.closed.is_set()
    assert wait_for(lambda: second.messages("phrase"))
    assert not second.gone.is_set()


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


# Satellites added, and given new passwords, on the settings page

def test_a_satellite_added_on_the_page_is_let_in_with_no_restart(brain, connect):
    serving(brain)
    password = brain.add_satellite("study")

    connect(brain, "study", password)
    assert connected(brain, "study") is not None
    assert brain.satellites() == ["bedroom", "kitchen", "study"]
    # Only ever its Pi's machine name, which setup.sh gives the brain for it.
    with pytest.raises(ValueError, match="machine name"):
        brain.add_satellite("The Study")
    assert brain.satellites() == ["bedroom", "kitchen", "study"]


def test_a_new_password_lets_go_of_the_satellite_and_refuses_the_old_one(brain, connect, passwords):
    brain.clock.set_alarm(7, 30, satellite="bedroom")
    serving(brain)
    old = connect(brain)
    session = connected(brain)

    password = brain.renew_password("bedroom")

    # Let go, rather than kept until it next connects, as it proved the old one.
    assert old.gone.wait(5) and session.closed.is_set()
    assert wait_for(lambda: brain.session_for("bedroom") is None and "bedroom" not in brain.sessions)
    with pytest.raises(protocol.Refused):
        connect(brain, password=BEDROOM)
    # One which proved the old password just before, and whose session is made just
    # after, isn't let stay either.
    late = SimpleNamespace(name="bedroom", connected=False, close=lambda: None)
    assert brain._start_session(late, BEDROOM) is None and "bedroom" not in brain.sessions

    connect(brain, password=password)
    connected(brain)
    # Still the default, with its alarm.
    assert passwords.names() == ["bedroom", "kitchen"] and password != BEDROOM
    assert len(brain.clock.overview(satellite="bedroom")["alarms"]) == 1
    with pytest.raises(ValueError):
        brain.renew_password("study")


def test_a_new_password_is_shown_once_and_never_logged(brain, connect, passwords, caplog, tmp_path, monkeypatch):
    # Through the page itself, as the brain runs it, so what the brain logs is checked too.
    monkeypatch.setattr(paths, "CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(paths, "SECRETS_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(settings_page_module, "STOP_CHECK", 0.02)
    login = "you@example.com"
    page = settings_page_module.SettingsPage([login], port=0, brain=brain)
    page.start()

    def ask(path, body=None, **headers):
        request = urllib.request.Request(f"http://127.0.0.1:{page.port}{path}",
                                         data=None if body is None else json.dumps(body).encode())
        request.add_header("Tailscale-User-Login", login)
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    serving(brain)
    connect(brain)
    connected(brain)
    try:
        with caplog.at_level(logging.DEBUG):
            status, added_headers, added = ask("/api/satellites/add", {"name": "study"}, **{"X-Nova": "page"})
            added = json.loads(added)
            first = passwords.get("study")
            _, renewed_headers, renewed = ask("/api/satellites/password", {"name": "bedroom"}, **{"X-Nova": "page"})
            renewed = json.loads(renewed)
            # Only from the page itself, as every change.
            assert ask("/api/satellites/add", {"name": "hall"})[0] == 403
            assert ask("/api/satellites/password", {"name": "study"})[0] == 403
            # Refused, with nothing to show.
            refusals = [ask(path, {"name": name}, **{"X-Nova": "page"})
                        for path, name in [("/api/satellites/add", "study"), ("/api/satellites/add", "The Hall"),
                                           ("/api/satellites/password", "hall")]]
            state = ask("/api/state")[2].decode()
            assert wait_for(lambda: brain.session_for("bedroom") is None)

        assert status == 200 and added == {"success": True, "message": added["message"], "password": first}
        assert renewed == {"success": True, "message": renewed["message"], "password": passwords.get("bedroom")}
        assert renewed["password"] != BEDROOM
        # Kept by no cache.
        assert added_headers["Cache-Control"] == renewed_headers["Cache-Control"] == "no-store"
        assert [json.loads(body) for _, _, body in refusals] == [
            {"success": False, "message": "There's already a satellite called study."},
            {"success": False, "message": "A satellite's name is its Pi's machine name: lower-case letters, "
                                          "digits and hyphens, not starting or ending with a hyphen."},
            {"success": False, "message": "There's no satellite called hall."}]
        assert passwords.names() == ["bedroom", "kitchen", "study"]
        # What was done is logged, and who did it - never either password.
        assert f"Settings page: {login} added the study satellite" in caplog.text
        assert f"Settings page: {login} gave the bedroom satellite a new password" in caplog.text
        for password in (first, renewed["password"]):
            assert password not in caplog.text
            assert password not in state and password not in json.dumps(page.state())
    finally:
        page.stop()


# Satellites removed, or made the default

def _rings_on(brain, name, session):
    """Steps the clock to each of the satellite's alarms, checking it rings on the session given."""
    alarms = brain.clock.overview(satellite=name)["alarms"]
    assert alarms
    brain.clock.alarm_ring_duration = 0
    for alarm in alarms:
        before = len(session.announced)
        brain.clock._tick(alarm["due_at"] + 1)
        assert len(session.announced) > before
        brain.clock.dismiss()


def test_removing_a_connected_satellite_empties_its_copy_first(brain, connect):
    brain.clock.set_alarm(7, 0, repeat="weekdays", satellite="kitchen")
    serving(brain)
    kitchen = connect(brain, "kitchen", KITCHEN)
    session = connected(brain, "kitchen")
    assert wait_for(lambda: kitchen.copy())

    assert brain.remove_satellite("kitchen") is True

    # Emptied before it's let go, so it never rings its old alarms from its copy.
    assert kitchen.gone.wait(5) and session.closed.is_set()
    assert kitchen.copy() == []
    assert brain.session_for("kitchen") is None and "kitchen" not in brain.sessions
    assert brain.satellites() == ["bedroom"]
    assert brain.remove_satellite("kitchen") is None


def test_removing_one_thats_away_moves_its_alarms_and_warns(brain, connect, caplog):
    brain.clock.set_alarm(7, 0, repeat="weekdays", satellite="kitchen")
    serving(brain)
    bedroom = connect(brain)
    session = connected(brain)

    with caplog.at_level(logging.WARNING, logger="voice_assistant.brain"):
        brain.remove_satellite("kitchen")

    assert brain.clock.overview(satellite="kitchen")["alarms"] == []
    assert [alarm["name"] for alarm in brain.clock.overview(satellite="bedroom")["alarms"]] == ["7:00 AM alarm, every weekday"]
    assert wait_for(lambda: bedroom.copy() == brain.clock.copy_for("bedroom") and bedroom.copy())
    assert {entry["kind"] for entry in bedroom.copy()} == {"alarm"}
    # Wherever it is, it still has its copy - which nothing here can empty.
    assert "The kitchen satellite was removed while it wasn't connected" in caplog.text
    _rings_on(brain, "bedroom", session)


def test_removing_one_which_couldnt_be_sent_the_empty_copy_says_so(brain, connect, monkeypatch, caplog):
    # Connected, so "while it wasn't connected" would be wrong.
    serving(brain)
    connect(brain, "kitchen", KITCHEN)
    session = connected(brain, "kitchen")
    monkeypatch.setattr(session.satellite, "send_alarms", lambda entries: False)

    with caplog.at_level(logging.WARNING, logger="voice_assistant.brain"):
        assert brain.remove_satellite("kitchen") is False

    assert "The kitchen satellite was removed but couldn't be sent an empty copy, so it still has a copy" in caplog.text
    assert "wasn't connected" not in caplog.text


def test_removing_the_default_makes_the_next_one_default(brain, connect):
    brain.clock.set_alarm(6, 30, satellite="bedroom")
    serving(brain)
    connect(brain, "kitchen", KITCHEN)
    kitchen = connected(brain, "kitchen")

    brain.remove_satellite("bedroom")

    assert brain.default_satellite() == "kitchen"
    assert len(brain.clock.overview(satellite="kitchen")["alarms"]) == 1
    _rings_on(brain, "kitchen", kitchen)


def test_removing_the_last_satellite_keeps_its_alarms_for_the_next(brain, connect, passwords):
    brain.clock.set_alarm(6, 30, satellite="bedroom")
    brain.clock.set_timer(600, satellite="kitchen")
    brain.remove_satellite("kitchen")
    brain.remove_satellite("bedroom")

    # On none, so not rung anywhere - until there's a satellite to ring them.
    assert brain.default_satellite() is None
    assert brain.clock.satellites() == {None}
    assert not brain._unowned_given

    passwords.add("hall", HALL)
    serving(brain)
    hall = connect(brain, "hall", HALL)
    session = connected(brain, "hall")
    assert brain.clock.satellites() == {"hall"}
    assert wait_for(lambda: hall.copy() == brain.clock.copy_for("hall"))
    assert {entry["kind"] for entry in hall.copy()} == {"alarm", "timer"}
    _rings_on(brain, "hall", session)


def test_routines_on_a_removed_satellite_run_on_the_default(brain, connect):
    routine = brain.routines.save({**MORNING, "satellite": "kitchen"})["routine"]
    serving(brain)
    connect(brain)
    bedroom = connected(brain)

    brain.remove_satellite("kitchen")

    assert brain.routines.get(routine["id"])["satellite"] is None
    assert brain.run_routine(brain.routines.get(routine["id"])) is True
    assert bedroom.routines == ["Good morning"]


def test_a_routine_isnt_saved_on_a_satellite_gone_from_the_file(brain, passwords):
    # The page checks too, but a removal can land between its check and the save.
    passwords.remove("kitchen")

    assert brain.routines.save({**MORNING, "satellite": "kitchen"}) == {
        "success": False, "message": "There's no satellite called kitchen."}
    assert brain.routines.save({**MORNING, "satellite": "bedroom"})["success"]


def test_a_removed_satellites_volumes_are_kept(brain, passwords):
    volume = VolumeControl(path=session_module._kept_for("volume", "kitchen"))
    volume.set("music", 20)
    muted = session_module._kept_for("microphone", "kitchen")
    with open(muted, "w") as file:
        file.write('{"muted": true}')

    brain.remove_satellite("kitchen")
    passwords.add("kitchen", KITCHEN)

    # Added again, it has them back - they're by its name.
    assert VolumeControl(path=session_module._kept_for("volume", "kitchen")).percent("music") == 20
    assert json.loads(open(muted).read()) == {"muted": True}


def test_a_removal_on_the_command_line_is_handled_the_same(brain, connect, passwords):
    brain.clock.set_alarm(7, 0, repeat="weekdays", satellite="kitchen")
    routine = brain.routines.save({**MORNING, "satellite": "kitchen"})["routine"]
    serving(brain)
    bedroom_satellite = connect(brain)
    bedroom = connected(brain)
    kitchen = connect(brain, "kitchen", KITCHEN)
    connected(brain, "kitchen")
    assert wait_for(lambda: kitchen.copy())

    # As backend.py's remove-satellite does, in a process of its own.
    SatellitePasswords(passwords.path).remove("kitchen")
    brain._shut_out_removed()

    assert kitchen.gone.wait(5) and kitchen.copy() == []
    assert brain.session_for("kitchen") is None
    assert len(brain.clock.overview(satellite="bedroom")["alarms"]) == 1
    assert wait_for(lambda: bedroom_satellite.copy() == brain.clock.copy_for("bedroom") and bedroom_satellite.copy())
    assert brain.routines.get(routine["id"])["satellite"] is None
    _rings_on(brain, "bedroom", bedroom)


def test_a_removal_moves_to_the_default_it_read_the_removal_from(brain, passwords, monkeypatch):
    # The file goes missing just after the removal is read from it. Read again for
    # the default, it would say there are none, and the alarm would ring nowhere.
    brain.clock.set_alarm(7, 0, satellite="kitchen")
    names = passwords.names
    monkeypatch.setattr(passwords, "names", lambda missing_ok=True: names() if not missing_ok else [])

    brain.remove_satellite("kitchen")

    assert brain.clock.satellites() == {"bedroom"}


def test_a_fresh_brain_with_no_satellites_file_logs_no_error(make_brain, passwords, caplog):
    # No satellite has been added yet, so there's no file - nothing to be alarmed by.
    os.remove(passwords.path)

    with caplog.at_level(logging.INFO, logger="voice_assistant.brain"):
        brain = make_brain()
        brain.serve = lambda: None
        brain.run()

    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "No satellites have been added yet" in caplog.text

    # With alarms on a satellite, though, it was likely given the wrong config folder.
    brain.clock.set_alarm(7, 0, satellite="kitchen")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="voice_assistant.brain"):
        again = make_brain()
        again.serve = lambda: None
        again.run()
    assert "is the config folder the right one?" in caplog.text
    assert again.clock.satellites() == {"kitchen"}


def test_a_removal_while_the_brain_was_down_is_caught_at_start(make_brain, connect, passwords, tmp_path, caplog):
    # remove-satellite run with the brain stopped, or across an update.
    first = make_brain()
    first.clock.set_alarm(7, 0, repeat="weekdays", satellite="kitchen")
    first.clock.set_timer(600, satellite="kitchen")
    routine = first.routines.save({**MORNING, "satellite": "kitchen"})["routine"]
    # Kept for the hall, the default then, while it was away - nothing else is the hall's.
    bins = first.routines.save({**MORNING, "name": "Bins", "at": "21:30"})["routine"]
    first.routines.missed(bins, "hall", time.time() - 60)
    first.stop()
    passwords.remove("kitchen")

    brain = make_brain()
    brain.serve = lambda: None
    with caplog.at_level(logging.WARNING, logger="voice_assistant.brain"):
        brain.run()

    assert brain.clock.satellites() == {"bedroom"}
    assert brain.routines.get(routine["id"])["satellite"] is None
    assert brain.routines.missed_satellites() == {"bedroom"}
    # Wherever it is, it still has its copy, as for a removal the brain saw.
    assert "The kitchen satellite was removed while the brain was down, so it still has a copy" in caplog.text

    # Kept so: the next brain rings them on the bedroom.
    again = make_brain()
    serving(again)
    connect(again)
    _rings_on(again, "bedroom", connected(again))


def test_a_removal_is_handled_once(brain, passwords, monkeypatch):
    # The server's poll and remove_satellite both look, maybe at once.
    brain.clock.set_alarm(7, 0, satellite="kitchen")
    handled = []
    on_removed = brain._on_removed
    monkeypatch.setattr(brain, "_on_removed", lambda name, names: (handled.append(name), on_removed(name, names)))
    # Each, having read how the file was last seen and which satellites there were,
    # waits for the other to read them too - so both would take the removal for new.
    # One at a time, they can't.
    seen, names = threading.Barrier(2), threading.Barrier(2)

    def wait(barrier):
        try:
            barrier.wait(0.5)
        except threading.BrokenBarrierError:
            pass

    class Watched(brain_module.Brain):
        @property
        def _passwords_seen(self):
            value = self.__dict__["seen"]
            wait(seen)
            return value

        @_passwords_seen.setter
        def _passwords_seen(self, value):
            self.__dict__["seen"] = value

        @property
        def _names_seen(self):
            value = self.__dict__["names"]
            wait(names)
            return value

        @_names_seen.setter
        def _names_seen(self, value):
            self.__dict__["names"] = value

    brain.__dict__["seen"], brain.__dict__["names"] = brain._passwords_seen, brain._names_seen
    brain.__class__ = Watched
    passwords.remove("kitchen")
    threads = [threading.Thread(target=brain._shut_out_removed) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert handled == ["kitchen"]
    assert len(brain.clock.overview(satellite="bedroom")["alarms"]) == 1


class _HeldSatellite:
    """
    A satellite whose connected is held, on the thread sending everyone's copies, until
    let go - so a copy made before it was removed arrives after.
    """

    def __init__(self, name):
        self.name = name
        self.sent = []
        self.letting_on = threading.Event()
        self.reached = threading.Event()

    @property
    def connected(self):
        if threading.current_thread().name == "on-change":
            self.reached.set()
            self.letting_on.wait(5)
        return True

    def send_alarms(self, entries):
        self.sent.append(entries)
        return True

    def close(self):
        pass


def test_no_real_copy_reaches_a_removed_satellite(brain):
    brain.clock.set_alarm(7, 0, satellite="kitchen")
    satellite = _HeldSatellite("kitchen")
    brain.sessions["kitchen"] = FakeSession(brain, satellite, [])
    # Something changed on the clock just before, and its copies are on their way.
    sending = threading.Thread(target=brain._send_alarms, name="on-change", daemon=True)
    sending.start()
    assert satellite.reached.wait(5)

    brain.remove_satellite("kitchen")
    satellite.letting_on.set()
    sending.join(5)

    assert satellite.sent == [[]]


def test_routines_kept_for_a_removed_satellite_run_on_the_default(brain, connect, passwords):
    routine = brain.routines.save({**MORNING, "satellite": "kitchen"})["routine"]
    brain._run_timed(routine, time.time())
    assert brain.routines.missed_satellites() == {"kitchen"}

    brain.remove_satellite("kitchen")
    assert brain.routines.missed_satellites() == {"bedroom"}

    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    brain._catch_up_routines()
    assert wait_for(lambda: bedroom.routines == ["Good morning"])


def _seed_doubled(tmp_path):
    """The same 7am weekday alarm on the bedroom and the kitchen, as older files can have it."""
    clock = clock_module.Clock(config=None, start=False)
    due = clock._next_occurrence(7, 0, "weekdays")
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 3, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": "weekdays", "due_at": due, "satellite": "bedroom"},
        {"id": 2, "hour": 7, "minute": 0, "repeat": "weekdays", "due_at": due, "satellite": "kitchen"}]}))


def test_an_alarm_both_had_rings_once_once_one_is_removed(make_brain, connect, tmp_path):
    _seed_doubled(tmp_path)
    brain = make_brain()
    serving(brain)
    connect(brain)
    connected(brain)

    brain.remove_satellite("kitchen")

    assert len(brain.clock.overview(satellite="bedroom")["alarms"]) == 1
    brain.clock.alarm_ring_duration = 0
    brain.clock._tick(brain.clock.overview(satellite="bedroom")["alarms"][0]["due_at"] + 1)
    assert len(brain.clock._ringing) == 1


def test_an_alarm_both_had_isnt_doubled_by_the_start_sweep(make_brain, passwords, tmp_path):
    _seed_doubled(tmp_path)
    passwords.remove("kitchen")
    brain = make_brain()
    brain.serve = lambda: None
    brain.run()
    assert len(brain.clock.overview()["alarms"]) == 1 and brain.clock.satellites() == {"bedroom"}


def test_removing_the_last_satellite_while_the_brain_was_down_keeps_everything(make_brain, connect, passwords):
    first = make_brain()
    first.clock.set_alarm(7, 0, repeat="weekdays", satellite="kitchen")
    first.clock.set_timer(600, satellite="bedroom")
    first.stop()
    passwords.remove("kitchen")
    passwords.remove("bedroom")

    brain = make_brain()
    brain.serve = lambda: None
    brain.run()

    # On none, for the next satellite added.
    assert brain.clock.satellites() == {None} and len(brain.clock.overview()["alarms"]) == 1
    assert not brain._unowned_given

    passwords.add("hall", HALL)
    again = make_brain()
    serving(again)
    connect(again, "hall", HALL)
    hall = connected(again, "hall")
    assert again.clock.satellites() == {"hall"}
    _rings_on(again, "hall", hall)


def test_a_missing_or_broken_satellites_file_is_never_every_satellite_removed(make_brain, passwords, tmp_path):
    # Moved by hand, say, or the wrong config folder: nothing is moved on it, as
    # moving it can't be undone.
    first = make_brain()
    first.clock.set_alarm(7, 0, repeat="weekdays", satellite="kitchen")
    first.clock.set_timer(600, satellite="bedroom")
    routine = first.routines.save({**MORNING, "satellite": "kitchen"})["routine"]
    first.stop()
    os.remove(passwords.path)

    brain = make_brain()
    brain.serve = lambda: None
    brain.run()
    assert brain.clock.satellites() == {"bedroom", "kitchen"}

    passwords.add("bedroom", BEDROOM)
    passwords.add("kitchen", KITCHEN)
    running = make_brain()
    with open(passwords.path, "w") as file:
        file.write("{not json")
    running._shut_out_removed()
    running._give_out_orphans()
    os.remove(passwords.path)
    running._shut_out_removed()
    running._give_out_orphans()

    assert running.clock.satellites() == {"bedroom", "kitchen"}
    assert running.routines.get(routine["id"])["satellite"] == "kitchen"


def test_the_start_sweep_waits_for_a_removal_being_handled(brain):
    # Reading which satellites there are part way through a removal, it could give
    # what's on none to the satellite being removed.
    swept = threading.Event()
    with brain._removing:
        threading.Thread(target=lambda: (brain._give_out_orphans(), swept.set()), daemon=True).start()
        assert not swept.wait(0.2)
    assert swept.wait(5)


def test_the_servers_poll_doesnt_wait_for_a_removal_being_handled(brain, monkeypatch):
    # Nothing in it may stop the brain listening.
    monkeypatch.setattr(brain_module.watchdog, "beat", lambda at=None: None)
    polled = threading.Event()
    with brain._removing:
        threading.Thread(target=lambda: (brain_module._Server.service_actions(SimpleNamespace(brain=brain)),
                                         polled.set()), daemon=True).start()
        assert polled.wait(5)


def test_a_removal_from_the_command_line_doesnt_stop_the_brain_letting_others_in(brain, connect, passwords,
                                                                                 monkeypatch):
    # Handling it sends the satellite its empty copy and closes its session, which can
    # take as long as the stream's timeout - during which the server's poll, if it did
    # that itself, would let nobody in.
    serving(brain)
    kitchen = connect(brain, "kitchen", KITCHEN)
    session = connected(brain, "kitchen")
    sending, release = threading.Event(), threading.Event()

    def send_slowly(entries):
        sending.set()
        release.wait(5)
        return True

    monkeypatch.setattr(session.satellite, "send_alarms", send_slowly)
    passwords.remove("kitchen")
    assert sending.wait(5)

    # Shaking hands is all it asks of the poll - its session waits for the removal.
    joining = threading.Thread(target=connect, args=(brain,), daemon=True)
    joining.start()
    joining.join(1)
    shook_hands = not joining.is_alive()
    release.set()
    joining.join(5)
    assert shook_hands
    assert kitchen.gone.wait(5)


def test_a_write_for_a_removed_satellite_is_refused_at_once(brain, passwords):
    # A turn still in flight, or the page's alarm or Undo, just after the removal -
    # before the brain has looked, too. Set, it would ring nowhere until a restart.
    SatellitePasswords(passwords.path).remove("kitchen")

    assert brain.clock.set_alarm(7, 0, satellite="kitchen") == {
        "success": False, "message": "That satellite's gone, so it can't ring there."}
    assert brain.clock.set_timer(600, satellite="kitchen")["success"] is False
    assert brain.clock.satellites() == set()
    assert brain.clock.set_alarm(7, 0, satellite="bedroom")["success"]


def test_a_satellite_removed_as_it_reconnects_loses_its_copy(brain, passwords):
    # Online moments before, so the page's question said nothing of a copy.
    sent = []
    removed = SimpleNamespace(name="kitchen", connected=True, close=lambda: None,
                              send_alarms=lambda entries: sent.append(entries) or True)
    passwords.remove("kitchen")
    brain._shut_out_removed()

    assert brain._start_session(removed, KITCHEN) is None
    assert sent == [[]]

    # One merely given a new password keeps it - it's still that satellite.
    passwords.renew("bedroom")
    renewed = SimpleNamespace(name="bedroom", connected=True, close=lambda: None,
                              send_alarms=lambda entries: sent.append(entries) or True)
    assert brain._start_session(renewed, BEDROOM) is None
    assert sent == [[]]


def test_a_last_connection_which_wont_close_doesnt_stop_the_next(brain, caplog):
    closed = []
    old = FakeSession(brain, SimpleNamespace(name="bedroom", connected=True, close=lambda: closed.append("old")), [])

    def fail():
        raise RuntimeError("stuck")

    old.close = fail
    brain.sessions["bedroom"] = old
    new = SimpleNamespace(name="bedroom", connected=False, close=lambda: None)

    assert brain._start_session(new) is not None
    assert closed == ["old"] and "Couldn't let go of the bedroom satellite" in caplog.text


def test_make_default_puts_it_first(brain, connect, passwords):
    serving(brain)
    connect(brain)
    connect(brain, "kitchen", KITCHEN)
    bedroom, kitchen = connected(brain), connected(brain, "kitchen")

    brain.make_default("kitchen")

    assert brain.default_satellite() == "kitchen" and passwords.names() == ["kitchen", "bedroom"]
    # Its password is kept, so it stays connected.
    assert passwords.get("kitchen") == KITCHEN and brain.session_for("kitchen") is kitchen
    brain.run_routine({"name": "Good morning", "satellite": None})
    assert kitchen.routines == ["Good morning"] and bedroom.routines == []
    with pytest.raises(ValueError):
        brain.make_default("study")


def test_a_new_password_is_given_even_if_letting_go_fails(brain, connect, passwords, caplog):
    # It's saved by then, so the old one is refused: lost here, the satellite couldn't
    # connect until it was given another.
    serving(brain)
    connect(brain)
    session = connected(brain)

    close = session.close
    failed = []

    def fail():
        if not failed:
            failed.append(True)
            raise RuntimeError("stuck")
        close()

    session.close = fail
    password = brain.renew_password("bedroom")

    assert password == passwords.get("bedroom") and password != BEDROOM
    # Let go all the same.
    assert wait_for(lambda: brain.session_for("bedroom") is None)
    assert "Couldn't let go of the bedroom satellite" in caplog.text and password not in caplog.text


def test_a_satellite_is_let_go_outside_the_pages_save_lock(brain, connect, tmp_path, monkeypatch):
    # Letting go stops its device in Home Assistant and its music, which every other
    # save on the page would wait for.
    monkeypatch.setattr(paths, "CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(paths, "SECRETS_FILE", str(tmp_path / ".env"))
    page = settings_page_module.SettingsPage(["you@example.com"], port=0, brain=brain)
    serving(brain)
    connect(brain)
    connect(brain, "kitchen", KITCHEN)
    held = []
    for session in (connected(brain), connected(brain, "kitchen")):
        session.close = lambda close=session.close, name=session.satellite.name: (
            held.append((name, page._saving.locked())), close())

    assert page.new_password("bedroom", "you@example.com")["success"]
    assert page.remove_satellite("kitchen", "you@example.com") == {"success": True, "message": "Removed kitchen."}
    assert {name for name, _ in held} == {"bedroom", "kitchen"} and not any(locked for _, locked in held)


def test_the_page_says_when_a_removed_satellite_still_has_its_copy(brain, connect, tmp_path, monkeypatch):
    # The page's question is asked from what it last heard, up to twenty seconds
    # old - so a Pi shut down just before is asked about as connected.
    monkeypatch.setattr(paths, "CONFIG_FILE", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(paths, "SECRETS_FILE", str(tmp_path / ".env"))
    page = settings_page_module.SettingsPage(["you@example.com"], port=0, brain=brain)
    brain.passwords.add("hall", HALL)
    serving(brain)
    connect(brain, "kitchen", KITCHEN)
    connected(brain, "kitchen")

    assert brain.remove_satellite("hall") is False
    assert page.remove_satellite("bedroom", "you@example.com") == {
        "success": True, "message": "Removed bedroom. It wasn't connected, so it still has a copy of its alarms "
                                    "for the next week - wipe it or connect it before switching it on."}
    assert page.remove_satellite("kitchen", "you@example.com") == {"success": True, "message": "Removed kitchen."}


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
    # Said as ringing, so no sound switch silences it.
    assert set(session.rung) == {True}
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


def test_a_satellite_has_the_copy_listing_what_the_brain_took_over_before_its_told_its_settled(make_brain, connect,
                                                                                             tmp_path):
    # Told it's settled, the satellite stops ringing what it was and lets its copy ring it
    # again, as due from when the brain began ringing it - which only the copy made after
    # the report says. Sent after, by the thread sending copies - held up here, as by a
    # satellite which has stalled - a brain lost in between left it silent on both sides.
    now = time.time()
    (tmp_path / "alarms.json").write_text(json.dumps({"next_id": 2, "alarms": [
        {"id": 1, "hour": 7, "minute": 0, "repeat": None, "due_at": now - 30, "satellite": "bedroom"}]}))
    brain = make_brain()
    brain.clock.alarm_ring_duration = 0
    held_up = threading.Event()
    send_alarms = brain._send_alarms

    def stalled():
        held_up.wait(1)
        send_alarms()

    brain._send_alarms = stalled
    serving(brain)
    alarm = f"alarm-1@{int(now - 30)}"
    try:
        satellite = connect(brain, away={"rang": [alarm], "dismissed": [], "ringing": [alarm]})
        assert wait_for(lambda: satellite.messages("away_settled"))
        sent = [message for message, _ in satellite.received]
        settled = [message["type"] for message in sent].index("away_settled")
        assert any(alarm in [entry["id"] for entry in message["entries"]]
                   for message in sent[:settled] if message["type"] == "alarms")
    finally:
        held_up.set()


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


def test_how_long_a_satellite_took_to_be_caught_up_is_logged(brain, connect, caplog):
    # From its hello, as all of it counts against the satellite's SETTLE_WAIT.
    serving(brain)
    with caplog.at_level(logging.INFO, logger="voice_assistant.brain"):
        satellite = connect(brain)
        assert wait_for(lambda: satellite.messages("away_settled"))
        assert wait_for(lambda: any("caught up" in record.message for record in caplog.records))

    logged = [record for record in caplog.records if "caught up" in record.message]
    assert logged[0].levelno == logging.INFO
    assert re.fullmatch(r"The bedroom satellite was caught up \d+\.\d\d seconds after its hello", logged[0].message)


def test_a_satellite_isnt_told_its_settled_when_what_it_said_cant_be_saved(make_brain, connect, tmp_path,
                                                                           monkeypatch):
    now = time.time()
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 2, "timers": [
        {"id": 1, "due_at": now - 60, "duration": 600, "label": None, "satellite": "bedroom"}]}))
    brain = make_brain()
    save_json = clock_module.storage.save_json

    def full(path, data):
        if path == clock_module.TIMERS_FILE:
            raise OSError("No space left on device")
        save_json(path, data)

    monkeypatch.setattr(clock_module.storage, "save_json", full)
    serving(brain)
    satellite = connect(brain, away={"rang": [f"timer-1@{int(now - 60)}"], "dismissed": [], "ringing": []})

    # Let go, to ring on and say it all again.
    assert satellite.gone.wait(5)
    assert satellite.messages("phrase") and not satellite.messages("away_settled")


def test_a_satellite_which_says_nothing_of_while_it_was_away_is_let_go_to_say_it_again(make_brain, connect,
                                                                                     tmp_path, monkeypatch):
    # Its report was late, so the brain counted it as caught up, and said what it had
    # missed - letting go of it - while the satellite might still be ringing it. When
    # the report came again, nothing was left to take the ringing over.
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 2, "timers": [
        {"id": 1, "due_at": time.time() - 60, "duration": 600, "label": None, "satellite": "bedroom"}]}))
    brain = make_brain()
    monkeypatch.setattr(brain_module, "AWAY_WAIT", 0.1)
    serving(brain)
    satellite = connect(brain, away=None)

    # Let go, to connect again and say it - not told it's settled, nor anything said.
    assert satellite.gone.wait(5)
    assert satellite.messages("phrase") and not satellite.messages("away_settled")
    brain.clock._tick(time.time())
    assert "bedroom" not in brain._caught_up
    assert len(brain.clock._missed) == 1


def _real_sessions(monkeypatch):
    """Has the brain make real Sessions, with speech told apart by how loud it is and no music player."""
    monkeypatch.setattr(brain_module, "Session", session_module.Session)
    monkeypatch.setattr(session_module, "make_detector", lambda config: LoudnessDetector())
    monkeypatch.setattr(session_module, "make_player", lambda config, speaker, volume, spotify_speaker=None: None)


def test_a_satellite_which_goes_mid_turn_leaves_the_brain_working(brain, connect, monkeypatch):
    # The real session this time.
    _real_sessions(monkeypatch)
    serving(brain)
    satellite = connect(brain)
    first = connected(brain)

    # Woken, and gone part way through what the user says.
    satellite.say("wake", talked_over=False)
    satellite.send_audio(np.full(RATE, 8000, np.int16))
    assert wait_for(lambda: brain.stt.made)
    satellite.go()
    assert wait_for(lambda: "bedroom" not in brain.sessions and brain.stt.made[0].cancelled)

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

    _real_sessions(monkeypatch)
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
    # The second's device starts as its session runs, once it has caught up.
    assert wait_for(lambda: devices[1].connected)
    assert not devices[0].connected


def test_alerts_go_to_the_default_satellite(brain, connect):
    serving(brain)
    connect(brain, "kitchen", KITCHEN)
    kitchen = connected(brain, "kitchen")
    # The bedroom, the first satellite added, isn't there to say it - which ntfy is
    # told, so it keeps the alert for "any notifications?".
    assert brain._alert(Alert("homelab", "Backup failed", "Error 5.")) is False

    connect(brain)
    bedroom = connected(brain)
    assert brain._alert(Alert("homelab", "Disk nearly full", "")) is True
    assert bedroom.alerts == ["Disk nearly full"] and kitchen.alerts == []


def test_a_routine_runs_on_its_own_satellite_or_the_default(brain, connect):
    serving(brain)
    connect(brain)
    connect(brain, "kitchen", KITCHEN)
    bedroom, kitchen = connected(brain), connected(brain, "kitchen")

    brain.run_routine({"name": "Good morning", "satellite": None})
    brain.run_routine({"name": "Dinner time", "satellite": "kitchen"})
    assert bedroom.routines == ["Good morning"] and kitchen.routines == ["Dinner time"]


def test_stop_silences_what_rings_and_what_nova_says_on_each_satellite(brain, connect, passwords):
    # The settings page's Stop is Home Assistant's, on every satellite with something
    # to stop: the bedroom's timer is ringing and the kitchen's Nova is talking. The
    # hall has neither, so is left alone.
    passwords.add("hall", HALL)
    serving(brain)
    connect(brain)
    connect(brain, "kitchen", KITCHEN)
    connect(brain, "hall", HALL)
    bedroom, kitchen, hall = connected(brain), connected(brain, "kitchen"), connected(brain, "hall")
    assert not brain.stoppable()

    brain.clock.set_timer(60, satellite="bedroom")
    brain.clock._tick(time.time() + 61)
    kitchen.activity = "speaking"
    assert brain.stoppable()

    assert brain.stop_everywhere() == ["bedroom", "kitchen"]
    assert (bedroom.stopped, kitchen.stopped, hall.stopped) == (1, 1, 0)


def test_stop_counts_what_rings_on_no_satellite_as_the_defaults(brain, connect):
    # What was set before there were satellites rings on the default, so it is the
    # default's to stop - until it is given to it.
    serving(brain)
    connect(brain)
    connect(brain, "kitchen", KITCHEN)
    bedroom, kitchen = connected(brain), connected(brain, "kitchen")
    brain.clock.set_timer(60)
    brain.clock._tick(time.time() + 61)
    assert brain.clock.is_ringing() and not brain.clock.is_ringing("bedroom")

    assert brain.stoppable()
    assert brain.stop_everywhere() == ["bedroom"]

    assert not brain.clock.is_ringing()
    assert (bedroom.stopped, kitchen.stopped) == (1, 0)
    assert not brain.stoppable()


def test_a_stop_for_what_nova_says_alone_leaves_the_defaults_missed_timers(brain, connect):
    # Dismissing on the default, with nothing ringing there, would let go of what it missed
    # unheard - so it's only done when something rings.
    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    brain.clock._record_unacknowledged({"id": 1, "due_at": time.time() - 60, "duration": 600, "label": None,
                                        "satellite": "bedroom"})
    bedroom.activity = "speaking"

    assert brain.stop_everywhere() == ["bedroom"]
    assert bedroom.stopped == 1
    assert brain.clock.missed()["message"].startswith("You missed 10 minute timer")


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


def handshake_with(brain, satellite, paused=False):
    """
    Runs the brain's side of the handshake over a real connection, with satellite
    run at the far end.

    Args:
        brain: The brain.
        satellite: Called with the far end's socket, which it closes when done.
        paused: Whether the brain only gets to the connection once satellite is done,
            as a paused brain does - rather than at the same time.

    Returns:
        What the brain's handshake returned.
    """
    # A pair, rather than a loopback connection, so a hang-up has arrived by the time close returns.
    far, near = socket.socketpair()
    far.settimeout(5)
    near.settimeout(brain_module.SOCKET_TIMEOUT)
    thread = threading.Thread(target=satellite, args=(far,), daemon=True)
    thread.start()
    if paused:
        thread.join(5)
    try:
        return brain._admit(brain_module.Stream(near), "the satellite's address")
    finally:
        thread.join(5)
        near.close()


def gives_up_waiting(far):
    """A satellite which said hello to a paused brain, waited for an answer, and gave up before the brain got to it."""
    with far, far.makefile("rwb") as stream:
        protocol.send(stream, "hello", name="bedroom", hostname="bedroom-pi", sample_rate=48000,
                      unclean=False, nonce="0" * 32)


def test_a_satellite_which_gave_up_waiting_for_the_brain_isnt_blamed_on_its_password(brain, caplog):
    # The brain was paused for the release checklist's stopped-brain test while the
    # satellite tried to reconnect: the kernel took the connection, the satellite said
    # hello, waited its SOCKET_TIMEOUT for an answer, and hung up - several times over.
    # The brain only got to each once it carried on, with its hello and hang-up both there.
    for _ in range(3):
        assert handshake_with(brain, gives_up_waiting, paused=True) is None
    gave_up = [record for record in caplog.records if "gave up waiting" in record.getMessage()]
    assert len(gave_up) == 1
    assert gave_up[0].levelno == logging.WARNING and "bedroom" in gave_up[0].getMessage()
    assert not [line for line in caplog.messages if "password" in line]


def test_a_satellite_which_hung_up_on_the_challenge_most_likely_has_the_wrong_password(brain, caplog):
    refused = []

    def wrong_password(far):
        # It hangs up as soon as the brain can't prove it knows the password it has.
        with far, far.makefile("rwb") as stream:
            try:
                protocol.greet(stream, "bedroom", "bedroom-pi", "not-the-bedrooms-password", 48000, False)
            except protocol.Refused:
                refused.append(True)

    assert handshake_with(brain, wrong_password) is None
    assert refused
    assert [line for line in caplog.messages if "bedroom" in line and "most likely" in line and "password" in line]
    assert not [line for line in caplog.messages if "gave up" in line]


def test_a_satellite_which_gave_up_waiting_is_let_in_when_it_tries_again(brain, connect, caplog):
    serving(brain)
    assert handshake_with(brain, gives_up_waiting, paused=True) is None
    connect(brain)
    connected(brain)
    assert "bedroom" not in brain._why_turned_away
    # So if it gives up again another time, that's said again.
    assert handshake_with(brain, gives_up_waiting, paused=True) is None
    assert len([line for line in caplog.messages if "gave up waiting" in line]) == 2


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
    caught_up = len(satellite.messages("alarms"))
    assert wait_for(lambda: len(satellite.messages("alarms")) >= caught_up + 2)


def test_a_change_on_the_clock_never_waits_for_a_satellites_copy(brain, connect):
    # The clock tells of a change on whichever thread made it - its own, which rings
    # everything, among them - so it mustn't wait while a copy goes to a satellite which
    # has stalled, for up to the stream's timeout.
    serving(brain)
    satellite = connect(brain)
    connected(brain)
    with brain._sending_alarms:
        setting = threading.Thread(target=brain.clock.set_timer, args=(600,), kwargs={"satellite": "bedroom"},
                                   daemon=True)
        setting.start()
        setting.join(1)
        assert not setting.is_alive()
    # It's sent once the satellite takes it.
    assert wait_for(lambda: satellite.copy() == brain.clock.copy_for("bedroom") and satellite.copy())


def test_the_newest_copy_reaches_a_satellite_last(brain, connect):
    # Changes made while a copy is on its way are sent after it, never before.
    serving(brain)
    satellite = connect(brain)
    connected(brain)
    held, release = threading.Event(), threading.Event()

    def stalled():
        # A copy on its way to a satellite which has stalled - for a second at most, so
        # nothing here can wait for good.
        with brain._sending_alarms:
            held.set()
            release.wait(1)

    threading.Thread(target=stalled, daemon=True).start()
    assert held.wait(5)
    for minutes in range(1, 6):
        brain.clock.set_timer(60 * minutes, satellite="bedroom")
    release.set()
    assert wait_for(lambda: satellite.copy() is not None and len(satellite.copy()) == 5)
    time.sleep(0.1)
    assert satellite.copy() == brain.clock.copy_for("bedroom")


def test_the_copies_stop_being_sent_once_the_brain_stops(brain):
    def sending():
        return {thread for thread in threading.enumerate() if thread.name == "alarm-copies"}

    before = sending()
    serving(brain)
    assert wait_for(lambda: sending() - before)
    ours = sending() - before
    brain.stop()
    assert wait_for(lambda: not ours & sending())


def test_a_stop_just_as_the_copies_wake_still_ends_their_thread(brain):
    # stop() set its wake-up after the thread had looked whether it was stopped and before
    # it cleared it, so the thread slept on until ALARMS_RESENT.
    class StoppedAsItsCleared(threading.Event):
        def clear(self):
            if not brain._stopped.is_set():
                brain.stop()
            super().clear()

    brain._alarms_changed = StoppedAsItsCleared()
    brain._alarms_changed.set()
    sending = threading.Thread(target=brain._send_alarm_copies, daemon=True)
    sending.start()
    sending.join(2)
    assert not sending.is_alive()


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


def _display_threads():
    return [thread.name for thread in threading.enumerate() if thread.name in ("display", "display-server")]


def _running(brain):
    """Runs the brain on a thread of its own, giving what's set once run() has returned."""
    done = threading.Event()
    threading.Thread(target=lambda: (brain.run(), done.set()), daemon=True).start()
    assert wait_for(lambda: brain.port)
    return done


def test_display_server_off_by_default(brain):
    assert isinstance(brain.displays, Displays) and brain.display_server is None
    done = _running(brain)
    try:
        # Nothing listens for clocks, and with none to serve, nothing reads Home Assistant for them either.
        assert _display_threads() == []
    finally:
        brain.stop()
    assert done.wait(5)


def test_display_server_starts_and_stops(make_brain):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    held.close()
    brain = make_brain(display={"port": port})
    assert brain.display_server.port == port
    assert (brain.display_server.displays, brain.display_server.feed) == (brain.displays, brain.display_feed)
    # Not until run(), as the settings page isn't.
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()

    done = _running(brain)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/display", timeout=5) as response:
        assert response.status == 200
    assert sorted(_display_threads()) == ["display", "display-server"]

    brain.stop()
    assert done.wait(5)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    assert wait_for(lambda: not _display_threads())


def test_a_display_port_in_use_leaves_the_feed_stopped_and_the_brain_working(make_brain, caplog):
    # Another program already on the port: the feed used to poll Home Assistant for
    # clocks which could never connect, for as long as the brain ran.
    held = socket.socket()
    held.bind(("0.0.0.0", 0))
    held.listen()
    port = held.getsockname()[1]
    caplog.set_level(logging.INFO)
    try:
        brain = make_brain(display={"port": port})
        done = _running(brain)
        assert wait_for(lambda: "brain is ready" in caplog.text)
        assert f"Couldn't start the clock displays' server on port {port}" in caplog.text
        assert _display_threads() == [] and not brain.display_server.serving
        # Still listening for its satellites.
        socket.create_connection(("127.0.0.1", brain.port), timeout=1).close()
        brain.stop()
        assert done.wait(5)
    finally:
        held.close()


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

    def move_satellite(self, from_satellite, to_satellite):
        return 0

    def satellites(self):
        return set()


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


def test_a_reminder_missed_too_long_ago_to_say_goes_to_the_phone(make_brain, connect, tmp_path):
    # The brain was off as it came due, and its satellite away for hours after - so it's
    # kept for "what did I miss" rather than said, and sent as one nobody answered.
    due = time.time() - 60
    (tmp_path / "timers.json").write_text(json.dumps({"next_id": 2, "timers": [
        {"id": 1, "due_at": due, "duration": 600, "label": "take the bins out", "satellite": "bedroom"}]}))
    brain = make_brain()
    brain.ntfy = FakeNtfy()
    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    brain.clock._tick(due + 5 * 3600)
    assert brain.ntfy.sent == [("reminders", "Reminder nobody answered", "Take the bins out")]
    assert bedroom.announced == []


def test_each_topic_is_read_once_for_each_send(brain):
    # So one cleared on the settings page between checking it and sending to it is never sent to as "".
    class Cleared(FakeNtfy):
        def __getattribute__(self, name):
            topic = object.__getattribute__(self, name)
            if name in ("reminders", "restarts"):
                setattr(self, name, "")
            return topic

    brain.ntfy = Cleared()
    brain._unanswered({"label": "take the bins out"}, "clock", satellite="bedroom")
    brain._tell_restarted("bedroom")
    assert [topic for topic, _, _ in brain.ntfy.sent] == ["reminders", "homelab"]


def test_a_routine_runs_by_itself_when_its_due(brain, monkeypatch):
    monkeypatch.setattr(brain_module, "ROUTINE_CHECK", 0.01)
    due, ran = [[{"name": "Good morning"}]], []

    def next_due():
        if not due:
            brain.running = False
            return []
        return due.pop(0)

    brain.running = True
    brain.routines = SimpleNamespace(due=next_due, take_missed=lambda satellite: [])
    brain.run_routine = lambda routine: ran.append(routine["name"]) or True
    brain._run_routines_when_due()

    assert wait_for(lambda: ran)
    assert ran == ["Good morning"]


MORNING = {"name": "Good morning", "at": "7:00", "steps": ["what's the weather"]}


def test_a_routine_due_while_its_satellite_is_away_runs_once_its_back(make_brain, connect):
    # It gave up, and Routines had already marked it as run for the day.
    brain = make_brain()
    routine = brain.routines.save(MORNING)["routine"]
    brain._run_timed(routine, time.time() - 1800)

    # Through a restart of the brain, too.
    brain = make_brain()
    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    brain._catch_up_routines()
    assert wait_for(lambda: bedroom.routines == ["Good morning"])
    assert bedroom.announced == []
    brain._catch_up_routines()
    time.sleep(0.05)
    assert bedroom.routines == ["Good morning"]


def test_a_routine_more_than_an_hour_late_is_said_to_have_been_skipped(brain, connect):
    routine = brain.routines.save(MORNING)["routine"]
    brain._run_timed(routine, time.time() - brain_module.MISSED_ROUTINE_GRACE - 1)

    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    brain._catch_up_routines()
    assert wait_for(lambda: bedroom.announced)
    assert bedroom.announced == ["Your 7 AM routine didn't run - I couldn't reach the speaker."]
    assert bedroom.routines == []


def test_several_routines_skipped_are_said_together(brain, connect):
    for routine in (MORNING, {"name": "Bins", "at": "21:30", "steps": ["remind me about the bins"]}):
        brain._run_timed(brain.routines.save(routine)["routine"], time.time() - 2 * brain_module.MISSED_ROUTINE_GRACE)

    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    brain._catch_up_routines()
    assert wait_for(lambda: bedroom.announced)
    assert bedroom.announced == ["Your 7 AM and 9:30 PM routines didn't run - I couldn't reach the speaker."]


def test_routines_said_to_have_been_skipped_are_kept_until_its_said(brain, connect):
    # Its satellite going part way through the notice lost it: they'd been let go of first.
    routine = brain.routines.save(MORNING)["routine"]
    brain._run_timed(routine, time.time() - brain_module.MISSED_ROUTINE_GRACE - 1)
    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    bedroom.said = False

    brain._catch_up_routines()
    assert wait_for(lambda: brain.routines.missed_satellites() == {"bedroom"})
    assert bedroom.announced == ["Your 7 AM routine didn't run - I couldn't reach the speaker."]

    # Said next time - once.
    bedroom.said = True
    brain._catch_up_routines()
    assert wait_for(lambda: len(bedroom.announced) == 2)
    brain._catch_up_routines()
    time.sleep(0.05)
    assert len(bedroom.announced) == 2 and brain.routines.missed_satellites() == set()
    assert bedroom.routines == []


def test_a_notice_of_skipped_routines_which_keeps_failing_is_handed_back_three_times_at_most(brain, caplog):
    # Handed back on every failure, it was tried again at every check for as long as its
    # satellite stayed connected - with a chime each time the chime got through.
    routine = brain.routines.save(MORNING)["routine"]
    brain._run_timed(routine, time.time() - brain_module.MISSED_ROUTINE_GRACE - 1)
    tries = []

    def fail(message):
        tries.append(message)
        raise OSError("the satellite refused it")

    session = SimpleNamespace(announce=fail)
    for _ in range(3):
        brain._tell_skipped(session, "bedroom", brain.routines.take_missed("bedroom"))
        assert brain.routines.missed_satellites() == {"bedroom"}
    with caplog.at_level(logging.WARNING, logger="voice_assistant.brain"):
        brain._tell_skipped(session, "bedroom", brain.routines.take_missed("bedroom"))
    assert brain.routines.missed_satellites() == set() and len(tries) == 4
    assert "Gave up telling the bedroom satellite" in caplog.text


def skipped(*times):
    """What's said of routines at these times, e.g. "7:00", which didn't run while their satellite was away."""
    return brain_module._skipped_message([{"at": at} for at in times])


@pytest.mark.parametrize("at, said", [("7:00", "7 AM"), ("19:00", "7 PM"), ("0:00", "midnight"), ("12:00", "noon"),
                                      ("7:05", "7:05 AM"), ("21:30", "9:30 PM")])
def test_a_skipped_routines_time_is_said_with_am_or_pm(at, said):
    # On the hour it said "7 o'clock", which is 7 in the morning or the evening.
    assert skipped(at) == f"Your {said} routine didn't run - I couldn't reach the speaker."


def test_two_skipped_routines_at_one_time_are_routines_not_a_routine():
    assert skipped("7:00", "7:00") == "Your 7 AM routines didn't run - I couldn't reach the speaker."
    assert skipped("7:00", "7:00", "21:30") == "Your 7 AM and 9:30 PM routines didn't run - I couldn't reach the speaker."


def test_a_routine_deleted_while_its_satellite_was_away_is_let_go(brain, connect):
    routine = brain.routines.save(MORNING)["routine"]
    brain._run_timed(routine, time.time())
    brain.routines.remove(routine["id"])

    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    brain._catch_up_routines()
    time.sleep(0.05)
    assert bedroom.routines == [] and bedroom.announced == []


def test_run_routine_says_whether_it_ran(brain, connect):
    assert brain.run_routine({"name": "Good morning", "satellite": None}) is False
    serving(brain)
    connect(brain)
    connected(brain)
    assert brain.run_routine({"name": "Good morning", "satellite": None}) is True


def test_a_routine_which_didnt_run_is_warned_of_without_its_name_when_transcripts_are_off(brain, caplog):
    # Its name is the user's own words, so the line holding it goes when transcripts are
    # switched off - and with it the warning, from the log and the page's problems.
    with caplog.at_level(logging.INFO, logger="voice_assistant.brain"):
        brain.run_routine({"name": "Good morning", "satellite": None})

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.getMessage() for record in warnings] == ["A routine didn't run, as its satellite isn't connected"]
    assert not getattr(warnings[0], "transcript", False)
    named = [record for record in caplog.records if getattr(record, "transcript", False)]
    assert [record.getMessage() for record in named] == [
        "The Good morning routine didn't run, as its satellite isn't connected"]


# Batteries

HOME_ASSISTANT = {"url": "http://home-assistant.test:8123"}


@pytest.fixture
def home_assistant_token(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "a-token")


def test_the_brain_watches_batteries_when_home_assistant_is_set_up(make_brain, home_assistant_token, tmp_path,
                                                                   monkeypatch):
    made = []
    monkeypatch.setattr(brain_module, "BatteryWatcher",
                        lambda *args, **kwargs: made.append(kwargs) or BatteryWatcher(*args, **kwargs))
    brain = make_brain(home_assistant=HOME_ASSISTANT,
                       batteries={"mobile_marks": [7], "other_marks": [30, 7], "check_every": 10, "stale_after": 99})
    assert isinstance(brain.batteries, BatteryWatcher) and brain.home is not None
    assert brain.batteries.stale_after == 99 and made[-1]["check_every"] == 10
    # The settings come from config.yaml's batteries section, kept in the brain's own data folder.
    phone = Battery(id="sensor.phone_battery_level", name="Phone", level=50, changed=0, device="d1", kind="mobile",
                    phone=True, computer=False, charging=False)
    mac = Battery(id="sensor.mac_battery_level", name="Mac", level=50, changed=0, device="d2", kind="other",
                  phone=False, computer=True, charging=False)
    assert (brain.batteries.choices.default_marks(phone), brain.batteries.choices.default_marks(mac)) == ([7], [30, 7])
    brain.batteries.choices.set_also_send_when_said(False)
    assert BatteryChoices(str(tmp_path / "batteries.json"), [5], [15, 5]).also_send_when_said is False
    # Not running until run() starts it.
    assert not [thread for thread in threading.enumerate() if thread.name == "batteries"]


def test_the_brain_has_no_battery_watcher_without_home_assistant(make_brain, monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    assert brain.batteries is None and brain.home is None
    assert make_brain().batteries is None


def test_the_brain_watches_whats_playing_when_home_assistant_is_set_up(make_brain, home_assistant_token):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    assert isinstance(brain.media, MediaWatcher) and brain.media._home is brain.home
    assert brain.media._on_change == brain._send_states
    assert brain.media._every == 3.0
    # Not running until run() starts it.
    assert brain.media._thread is None


def test_the_brain_has_no_media_watcher_without_home_assistant(make_brain, monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    assert make_brain(home_assistant=HOME_ASSISTANT).media is None
    assert make_brain().media is None


class _StateCounter:
    """Stands in for a session, counting the states it's had sent."""

    def __init__(self, name):
        self.satellite = SimpleNamespace(name=name, close=lambda: None)
        self.sent = 0

    def send_state(self):
        self.sent += 1

    def close(self):
        pass


def test_a_change_in_whats_playing_sends_every_satellite_its_state(make_brain, home_assistant_token):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    sessions = [_StateCounter("bedroom"), _StateCounter("kitchen")]
    brain.sessions.update({"bedroom": sessions[0], "kitchen": sessions[1]})
    assert brain.connected_sessions() == sessions
    brain.media._on_change()
    brain.media._on_change()
    assert [session.sent for session in sessions] == [2, 2]


def test_a_satellite_joining_or_leaving_sends_the_others_their_state(brain, connect):
    # Each satellite's view of what's playing leaves out every connected satellite's speaker and device.
    brain.media = MediaWatcher(SimpleNamespace(playing_media=lambda: []), on_change=brain._send_states)
    serving(brain)
    bedroom = connect(brain)
    connected(brain)
    assert wait_for(lambda: len(bedroom.messages("state")) == 1)
    kitchen = connect(brain, "kitchen", KITCHEN)
    connected(brain, "kitchen")
    assert wait_for(lambda: len(bedroom.messages("state")) == 2)
    # The one joining has its state once, as it's caught up.
    assert len(kitchen.messages("state")) == 1
    kitchen.go()
    assert wait_for(lambda: brain.session_for("kitchen") is None)
    assert wait_for(lambda: len(bedroom.messages("state")) == 3)


def test_a_removed_satellite_sends_the_others_their_state(brain, connect, passwords):
    brain.media = MediaWatcher(SimpleNamespace(playing_media=lambda: []), on_change=brain._send_states)
    serving(brain)
    bedroom = connect(brain)
    connect(brain, "kitchen", KITCHEN)
    connected(brain)
    connected(brain, "kitchen")
    assert wait_for(lambda: len(bedroom.messages("state")) == 2)
    brain.remove_satellite("kitchen")
    assert wait_for(lambda: len(bedroom.messages("state")) == 3)


def test_without_a_media_watcher_a_satellite_joining_sends_the_others_nothing(brain, connect):
    serving(brain)
    bedroom = connect(brain)
    connected(brain)
    connect(brain, "kitchen", KITCHEN)
    connected(brain, "kitchen")
    time.sleep(0.2)
    assert len(bedroom.messages("state")) == 1


def test_the_media_watcher_runs_while_the_brain_does_and_stops_with_it(make_brain, home_assistant_token):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    # Nothing here may reach a real Home Assistant.
    brain.home.batteries = lambda timeout=None: []
    brain.home.people = lambda: []
    brain.home.playing_media = lambda: []
    brain.registry.refresh = lambda: True
    running = []

    def serve():
        running.append(any(thread.name == "media" and thread.is_alive() for thread in threading.enumerate()))

    brain.serve = serve
    brain.run()
    assert running == [True]
    assert not any(thread.name == "media" for thread in threading.enumerate())


def test_the_brain_has_the_weather_only_with_home_assistant(make_brain, home_assistant_token, monkeypatch,
                                                             tmp_path):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    assert brain.weather.home is brain.home
    # Which weather entity is used, with several, is kept in the brain's own data folder.
    assert brain.weather.choices.path == str(tmp_path / "weather.json")
    assert brain.weather.choices.get("entity") is None

    monkeypatch.delenv("HA_TOKEN")
    assert make_brain(home_assistant=HOME_ASSISTANT).weather is None
    assert make_brain().weather is None


def test_the_brain_has_the_lists_only_with_home_assistant(make_brain, home_assistant_token, monkeypatch,
                                                           tmp_path):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    assert brain.lists.home is brain.home
    # The main list, once one's chosen, is kept in the brain's own data folder.
    assert brain.lists.choices.path == str(tmp_path / "lists.json")
    assert brain.lists.choices.get("main") is None

    monkeypatch.delenv("HA_TOKEN")
    assert make_brain(home_assistant=HOME_ASSISTANT).lists is None
    assert make_brain().lists is None


def test_the_brain_reads_the_registry_when_home_assistant_is_set_up(make_brain, home_assistant_token, monkeypatch):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    assert isinstance(brain.registry, HomeRegistry)
    assert brain.home.registry is brain.registry
    # The registry's whole read gets longer than the voice path's calls do.
    assert brain.registry._socket is not brain.home.socket
    assert brain.registry._socket.url == brain.home.socket.url
    assert brain.registry._socket._timeout == 30.0 and brain.home.socket._timeout == 3.0
    # Not running until run() starts it.
    assert brain.registry._thread is None

    monkeypatch.delenv("HA_TOKEN")
    assert make_brain(home_assistant=HOME_ASSISTANT).registry is None
    assert make_brain().registry is None


def test_the_brain_reads_the_calendar_from_google_once_its_keys_are_set(make_brain, monkeypatch):
    # Google's, not Home Assistant's - so it's there without Home Assistant, and not without Google.
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "made-up-client.apps.example.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "made-up-client-secret")
    brain = make_brain()
    assert isinstance(brain.calendar, GoogleCalendar) and brain.home is None
    assert brain.calendar.sign_in.path == paths.GOOGLE_TOKEN_FILE

    monkeypatch.delenv("GOOGLE_CLIENT_SECRET")
    assert make_brain(home_assistant=HOME_ASSISTANT).calendar is None


@pytest.mark.parametrize("fails", [False, True])
def test_the_calendars_are_read_once_as_the_brain_starts(make_brain, monkeypatch, caplog, fails):
    # So their names - the activities, "when's gym tomorrow?" - are known before anything's asked.
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "made-up-client.apps.example.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "made-up-client-secret")
    brain = make_brain()
    read = threading.Event()

    def calendars():
        read.set()
        if fails:
            raise requests.ConnectionError("Max retries exceeded with url: .../calendars/calendar-personal/events")
        return []

    brain.calendar.calendars = calendars
    # Serves until the read, in its own thread, is done.
    brain.serve = lambda: [thread.join(5) for thread in threading.enumerate() if thread.name == "calendars"]
    with caplog.at_level(logging.INFO):
        brain.run()
    assert read.is_set()
    if fails:
        # Only logged - with no calendar's id - and the brain runs on.
        assert any("calendar" in record.getMessage() and record.levelno == logging.WARNING
                   for record in caplog.records)
        assert "calendar-personal" not in caplog.text


def test_a_battery_warning_is_said_on_the_default_satellite(brain, connect):
    # With the bedroom, the first satellite added, away, there's nothing to say it.
    assert brain._say_battery("Your phone's battery is down to 5%.") is False
    serving(brain)
    connect(brain, "kitchen", KITCHEN)
    kitchen = connected(brain, "kitchen")
    assert brain._say_battery("Your phone's battery is down to 5%.") is False

    connect(brain)
    bedroom = connected(brain)
    assert brain._say_battery("Your MacBook Pro's battery is down to 15%.") is True
    assert bedroom.announced == ["Your MacBook Pro's battery is down to 15%."] and kitchen.announced == []


def test_saying_a_battery_warning_never_raises(brain, connect, caplog):
    serving(brain)
    connect(brain)
    bedroom = connected(brain)

    def fail(message, *args, **kwargs):
        raise OSError("the satellite went mid-sentence")

    bedroom.announce = fail
    with caplog.at_level(logging.WARNING):
        assert brain._say_battery("Your phone's battery is down to 5%.") is False
    assert "battery warning" in caplog.text


def test_warnings_go_to_the_phone_through_ntfy_and_wait_for_ntfys_quiet_hours(make_brain, home_assistant_token):
    brain = make_brain(home_assistant=HOME_ASSISTANT,
                       ntfy={"url": "http://ntfy.test", "phone": "my-phone", "quiet_from": "22:00",
                             "quiet_until": "06:30"})
    sent = []
    brain.ntfy.send = lambda topic, message, title="": sent.append((topic, message, title)) or True
    assert brain.batteries._send("It's low.", "Battery low") is True
    assert sent == [("my-phone", "It's low.", "Battery low")]
    assert brain.batteries._quiet == brain.ntfy.quiet


def test_without_ntfy_warnings_are_only_said_and_quiet_hours_are_still_ntfys(make_brain, home_assistant_token):
    brain = make_brain(home_assistant=HOME_ASSISTANT, ntfy={"quiet_from": "22:00", "quiet_until": "06:30"})
    assert brain.ntfy is None and brain.batteries._send("It's low.", "Battery low") is False
    brain._night._now = lambda: datetime(2026, 10, 20, 22, 30)
    assert brain.batteries._quiet()
    brain._night._now = lambda: datetime(2026, 10, 20, 6, 45)
    assert not brain.batteries._quiet()
    # And with ntfy but nowhere to send to, the same.
    brain = make_brain(home_assistant=HOME_ASSISTANT, ntfy={"url": "http://ntfy.test", "phone": ""})
    assert brain.batteries._send("It's low.", "Battery low") is False


def test_a_night_changed_with_no_ntfy_reaches_the_battery_watcher(make_brain, home_assistant_token):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    brain._night._now = lambda: datetime(2026, 10, 20, 22, 30)
    assert brain.ntfy is None and not brain.batteries._quiet()

    assert brain.apply_settings({("ntfy", "quiet_from"): "22:00"}) == []
    assert brain.batteries._quiet()


def test_a_topic_saved_with_no_ntfy_running_waits_for_the_restart(make_brain, home_assistant_token):
    # Nothing is sent to it until Nova restarts with an ntfy server - but the night is
    # still used, by the battery watcher.
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    assert brain.ntfy is None
    assert brain.apply_settings({("ntfy", "phone"): "my-phone", ("ntfy", "restarts"): "homelab",
                                 ("ntfy", "quiet_from"): "22:00"}) == [("ntfy", "phone"), ("ntfy", "restarts")]
    assert brain._night.quiet_from == "22:00"


def test_a_phone_topic_set_on_the_page_gets_battery_warnings_straight_away(make_brain, home_assistant_token,
                                                                         monkeypatch):
    brain = make_brain(home_assistant=HOME_ASSISTANT, ntfy={"url": "http://ntfy.test", "phone": ""})
    posted = []
    brain.ntfy._session = SimpleNamespace(post=lambda url, json=None, **kwargs: posted.append(json) or
                                          SimpleNamespace(raise_for_status=lambda: None))
    monkeypatch.setattr(ntfy_module.network, "online", lambda: True)

    assert brain.apply_settings({("ntfy", "phone"): "my-phone"}) == []
    assert brain.batteries._send("It's low.", "Battery low") is True
    assert [(body["topic"], body["message"]) for body in posted] == [("my-phone", "It's low.")]

    assert brain.apply_settings({("ntfy", "phone"): ""}) == []
    assert brain.batteries._send("It's low.", "Battery low") is False
    assert len(posted) == 1


def test_the_brain_sends_each_satellite_its_longest_request(brain, connect):
    serving(brain)
    bedroom, kitchen = connect(brain), connect(brain, "kitchen", KITCHEN)
    connected(brain)
    connected(brain, "kitchen")
    # As each connects.
    assert wait_for(lambda: bedroom.messages("state") and kitchen.messages("state"))
    assert bedroom.messages("state")[0]["longest_request"] == 15.0

    # And to each connected when it's saved, with no restart.
    assert brain.apply_settings({("thresholds", "recording_timeout"): 20.0}) == []
    assert wait_for(lambda: len(bedroom.messages("state")) == len(kitchen.messages("state")) == 2)
    assert bedroom.messages("state")[1]["longest_request"] == kitchen.messages("state")[1]["longest_request"] == 20.0


@pytest.mark.parametrize("name", ["wake_word_confidence", "media_wake_word_confidence"])
def test_a_wake_word_confidence_saved_is_sent_to_each_satellite(brain, connect, monkeypatch, name):
    # The real session, which works out the value it sends.
    _real_sessions(monkeypatch)
    serving(brain)
    bedroom, kitchen = connect(brain), connect(brain, "kitchen", KITCHEN)
    connected(brain)
    connected(brain, "kitchen").home = SimpleNamespace(area="Kitchen", device_id="devkitchen")
    assert wait_for(lambda: bedroom.messages("state") and kitchen.messages("state"))
    # A film in the kitchen only.
    brain.media = SimpleNamespace(playing_in=lambda area, devices, speakers: ["Kitchen TV"] if area == "Kitchen" else [])

    assert brain.apply_settings({("thresholds", name): 0.6}) == []
    assert wait_for(lambda: len(bedroom.messages("state")) == len(kitchen.messages("state")) == 2)
    sent = {"wake_word_confidence": (0.6, 0.75), "media_wake_word_confidence": (0.5, 0.6)}[name]
    assert (bedroom.messages("state")[1]["wake_confidence"], kitchen.messages("state")[1]["wake_confidence"]) == sent


def test_a_voice_check_saved_is_sent_to_each_satellite(brain, connect, monkeypatch):
    _real_sessions(monkeypatch)
    serving(brain)
    bedroom, kitchen = connect(brain), connect(brain, "kitchen", KITCHEN)
    connected(brain)
    connected(brain, "kitchen")
    assert wait_for(lambda: bedroom.messages("state") and kitchen.messages("state"))
    assert bedroom.messages("state")[0]["voice_check"] == kitchen.messages("state")[0]["voice_check"] == 0.2

    assert brain.apply_settings({("thresholds", "voice_check"): 0.5}) == []
    assert wait_for(lambda: len(bedroom.messages("state")) == len(kitchen.messages("state")) == 2)
    assert bedroom.messages("state")[1]["voice_check"] == kitchen.messages("state")[1]["voice_check"] == 0.5


def test_other_settings_saved_send_the_satellites_nothing(brain, connect):
    serving(brain)
    bedroom = connect(brain)
    connected(brain)
    assert wait_for(lambda: bedroom.messages("state"))

    assert brain.apply_settings({("thresholds", "silence_duration"): 1.0}) == []
    time.sleep(0.2)
    assert len(bedroom.messages("state")) == 1


def test_the_cant_reach_phrase_is_made_again_at_the_new_speed(brain, connect):
    # Each satellite says it while it can't reach the brain, so it should sound as Nova now does.
    serving(brain)
    bedroom, kitchen = connect(brain), connect(brain, "kitchen", KITCHEN)
    connected(brain)
    connected(brain, "kitchen")
    assert wait_for(lambda: len(bedroom.messages("phrase")) == len(kitchen.messages("phrase")) == 1)

    assert brain.apply_settings({("voice", "speed"): 1.25}) == []
    assert brain.tts.speed == 1.25 and brain.tts.said.count(brain_module.FALLBACK_PHRASE) == 2
    assert wait_for(lambda: len(bedroom.messages("phrase")) == len(kitchen.messages("phrase")) == 2)


def test_a_satellite_connecting_as_the_speed_changes_ends_up_with_the_new_phrase(brain, connect):
    # It's caught up on its own thread while the page's save makes the phrase again on
    # another - so either could send first, but never the old one last.
    serving(brain)
    old = brain.phrase
    with brain._sending_phrase:
        kitchen = connect(brain, "kitchen", KITCHEN)
        assert wait_for(lambda: kitchen.messages("alarms") and brain.session_for("kitchen") is not None)
        changing = threading.Thread(target=brain.apply_settings, args=({("voice", "speed"): 1.25},))
        changing.start()
        assert wait_for(lambda: brain.tts.said.count(brain_module.FALLBACK_PHRASE) == 2)
        time.sleep(0.05)
        assert not kitchen.messages("phrase")
    changing.join(5)
    assert brain.phrase != old
    assert wait_for(lambda: kitchen.messages("phrase"))
    assert wait_for(lambda: [payload for message, payload in kitchen.received
                             if message["type"] == "phrase"][-1] == brain.phrase[0])


def test_a_speed_piper_couldnt_take_waits_for_a_restart(brain, caplog):
    def refuse(speed):
        raise OSError("Piper's gone")

    brain.tts.set_speed = refuse
    assert brain.apply_settings({("voice", "speed"): 1.25}) == [("voice", "speed")]
    assert "so it's used once Nova restarts" in caplog.text


def test_a_speed_in_use_is_said_to_be_though_getting_piper_ready_failed(brain):
    # set_speed changes the speed first, so it can fail after Piper speaks at it.
    old = brain.phrase

    def half(speed):
        brain.tts.speed = speed
        raise OSError("The old Piper wouldn't stop")

    brain.tts.set_speed = half
    # And Piper says nothing for the phrase, so the one each satellite has is kept.
    brain.tts.stream = lambda message, on_start=None, remember=True, speed=None: iter([])
    assert brain.apply_settings({("voice", "speed"): 1.25}) == []
    assert brain.phrase == old is not None


def test_the_battery_watcher_runs_while_the_brain_does_and_stops_with_it(brain, home_assistant_token, make_brain):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    # Nothing here may reach a real Home Assistant.
    brain.home.batteries = lambda timeout=None: []
    brain.home.people = lambda: []
    brain.registry.refresh = lambda: True
    running = []

    def serve():
        running.append(any(thread.name == "batteries" and thread.is_alive() for thread in threading.enumerate()))

    brain.serve = serve
    brain.run()
    assert running == [True]
    assert not any(thread.name == "batteries" for thread in threading.enumerate())


def test_the_registry_is_read_while_the_brain_runs_and_stops_with_it(make_brain, home_assistant_token):
    brain = make_brain(home_assistant=HOME_ASSISTANT)
    # Nothing here may reach a real Home Assistant.
    brain.home.batteries = lambda timeout=None: []
    brain.home.people = lambda: []
    brain.registry.refresh = lambda: True
    running = []

    def serve():
        running.append(any(thread.name == "ha-registry" and thread.is_alive() for thread in threading.enumerate()))

    brain.serve = serve
    brain.run()
    assert running == [True]
    assert not any(thread.name == "ha-registry" for thread in threading.enumerate())


def test_a_warning_or_alert_not_said_to_the_end_counts_as_not_said(brain, connect):
    serving(brain)
    connect(brain)
    bedroom = connected(brain)
    bedroom.said = False
    assert brain._say_battery("Your phone's battery is down to 5%.") is False
    assert brain._alert(Alert("homelab", "Disk nearly full", "")) is False


# The speech-to-text the brain loads, with its second model for what Nemotron misses.

@pytest.fixture
def loading(monkeypatch, tmp_path):
    """Stands in for Nemotron and Piper's "Please" as speech-to-text is loaded, from the test's own folder."""
    from voice_assistant import nemotron as nemotron_module
    from voice_assistant import speech_to_text as speech_to_text_module

    monkeypatch.setattr(paths, "PROJECT_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(nemotron_module, "Nemotron", lambda folder: SimpleNamespace(folder=folder))
    monkeypatch.setattr(speech_to_text_module, "make_please", lambda tts: None)


def test_without_its_second_model_the_brain_still_hears(loading, make_config, tmp_path, caplog):
    config = make_config(backend={"model": "models/nemotron", "fallback_model": "models/parakeet"})
    with caplog.at_level(logging.WARNING, logger="voice_assistant.brain"):
        stt = brain_module._speech_to_text(config, FakePiper())

    assert stt.loaded and stt.nemotron.folder == str(tmp_path / "models/nemotron")
    assert stt.fallback is None
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith("The second speech model couldn't be loaded, so Nova will hear lone words "
                                  "again by itself: ")


def test_the_brain_loads_its_second_model_from_the_project(loading, make_config, tmp_path, monkeypatch):
    from voice_assistant import parakeet as parakeet_module

    monkeypatch.setattr(parakeet_module, "Parakeet", lambda path: SimpleNamespace(path=path))
    config = make_config(backend={"model": "models/nemotron", "fallback_model": "models/parakeet"})
    assert brain_module._speech_to_text(config, FakePiper()).fallback.path == str(tmp_path / "models/parakeet")


def test_an_empty_fallback_model_switches_the_second_model_off(loading, make_config, caplog):
    config = make_config(backend={"model": "models/nemotron", "fallback_model": ""})
    with caplog.at_level(logging.WARNING, logger="voice_assistant.brain"):
        assert brain_module._speech_to_text(config, FakePiper()).fallback is None
    assert not [record for record in caplog.records if record.levelno == logging.WARNING]
