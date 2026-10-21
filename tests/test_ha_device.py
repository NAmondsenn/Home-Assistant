"""
Home Assistant Device Test
Checks Nova appears in Home Assistant as a device of its own with its eleven
controls, that its state is sent only when it changes, and that Home Assistant's
commands reach Nova while nothing anyone says goes the other way - with stand-ins
for paho-mqtt's client, the broker and Nova itself.
"""

import json
import logging
import threading
from datetime import datetime

import pytest

from voice_assistant import ha_device
from voice_assistant.ha_device import DeviceControls, NovaDevice

BASE = "nova/nova_test"
DISCOVERY = "homeassistant/device/nova_test/config"
DUE = 1_800_000_000.0


class Reason:
    """Stands in for paho's ReasonCode."""

    def __init__(self, name="Success", failure=False):
        self.name, self.is_failure = name, failure

    def __str__(self):
        return self.name


class Message:
    """Stands in for paho's MQTTMessage."""

    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload.encode(), retain


class FakeClient:
    """
    Stands in for paho-mqtt's client, keeping what's published and subscribed to,
    and lets the test play the broker - connecting, failing to, and delivering
    Home Assistant's messages.
    """

    def __init__(self, client_id):
        self.client_id = client_id
        self.published = []
        self.subscribed = []
        self.will = self.login = self.address = None
        self.looping = self.disconnected = False
        self.on_connect = self.on_connect_fail = self.on_disconnect = self.on_message = None

    def username_pw_set(self, username, password=None):
        self.login = (username, password)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect_async(self, host, port=1883, keepalive=60):
        self.address = (host, port)

    def loop_start(self):
        self.looping = True

    def loop_stop(self):
        self.looping = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscribed.extend(topic if isinstance(topic, list) else [(topic, qos)])

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    # The broker's side

    def connects(self, refused=False):
        self.on_connect(self, None, None, Reason("Not authorized", True) if refused else Reason(), None)

    def cant_connect(self):
        self.on_connect_fail(self, None)

    def drops(self):
        self.on_disconnect(self, None, None, Reason("Unspecified error", True), None)

    def delivers(self, topic, payload, retain=False):
        self.on_message(self, None, Message(topic, payload, retain))

    def sent(self, topic):
        return [published for published in self.published if published[0] == topic]

    def latest(self):
        """What each topic was last sent."""
        return {topic: payload for topic, payload, _, _ in self.published}


class FakeNova:
    """Stands in for the rest of Nova, keeping what Home Assistant asked it to do."""

    def __init__(self):
        self.activity = "idle"
        self.levels = {"general": 60, "alarm": 80, "music": 40}
        self.overview = {"timers": [], "alarms": []}
        self.reachable = True
        self.is_muted = False
        self.calls = []
        self.spoken = threading.Event()
        self.broken = False

    def controls(self):
        return DeviceControls(status=lambda: self.activity, volumes=lambda: dict(self.levels),
                              set_volume=lambda category, percent: self.calls.append(("set_volume", category, percent)),
                              clock=lambda: self.overview, backend=lambda: self.reachable,
                              muted=lambda: self.is_muted, set_muted=lambda muted: self.calls.append(("set_muted", muted)),
                              stop=self._stop, restart=lambda: self.calls.append(("restart",)), speak=self._speak)

    def _stop(self):
        if self.broken:
            raise RuntimeError("the speaker's gone")
        self.calls.append(("stop",))

    def _speak(self, message):
        self.calls.append(("speak", message))
        self.spoken.set()


@pytest.fixture
def nova():
    return FakeNova()


@pytest.fixture
def started(nova):
    """A NovaDevice for nova, started, with the client it made - not yet connected."""
    made = []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    device = NovaDevice("broker.local", 1883, "nova", "secret", nova.controls(), node_id="nova_test",
                        client_factory=factory)
    device.start()
    yield device, made[0]
    device.stop()


def _complaints(caplog):
    return [record for record in caplog.records if record.name == ha_device.__name__
            and record.levelno >= logging.WARNING]


def test_nova_announces_itself_with_its_eleven_controls(started):
    device, client = started
    assert client.client_id == "nova_test" and device.identifier == "nova_test"
    assert client.address == ("broker.local", 1883) and client.login == ("nova", "secret") and client.looping
    # Nothing's sent until it's connected, and from then on the broker says it's
    # offline for it if the connection drops.
    assert client.published == [] and client.will == (f"{BASE}/availability", "offline", 1, True)

    client.connects()
    [(_, payload, _, retained)] = client.sent(DISCOVERY)
    config = json.loads(payload)
    assert retained
    # Home Assistant's device for Nova is found by exactly these identifiers.
    assert config["dev"]["ids"] == ["nova_test"]
    assert (config["dev"]["name"], config["dev"]["mf"], config["dev"]["mdl"]) == ("Nova", "Nova", "Raspberry Pi")
    assert config["o"]["name"] == "Nova" and config["availability_topic"] == f"{BASE}/availability"
    controls = config["cmps"].values()
    assert {control["name"] for control in controls} == {
        "Status", "Volume", "Alarm volume", "Music volume", "Next alarm", "Timers", "Stop", "Restart", "Speak",
        "Backend", "Microphone muted"}
    assert len({control["unique_id"] for control in controls}) == 11
    assert client.sent(f"{BASE}/availability") == [(f"{BASE}/availability", "online", 1, True)]
    assert {f"{BASE}/volume/alarm/set", f"{BASE}/muted/set", f"{BASE}/stop", f"{BASE}/restart", f"{BASE}/speak",
            "homeassistant/status"} <= {topic for topic, _ in client.subscribed}

    # Home Assistant says it's online again after it restarts, and is told again.
    client.delivers("homeassistant/status", "online")
    assert len(client.sent(DISCOVERY)) == 2
    client.delivers("homeassistant/status", "offline")
    assert len(client.sent(DISCOVERY)) == 2


def test_state_is_published_only_when_it_changes(started, nova):
    device, client = started
    client.connects()
    state = client.latest()
    assert {topic: state[topic] for topic in state if topic != DISCOVERY} == {
        f"{BASE}/status": "idle", f"{BASE}/volume/general": "60", f"{BASE}/volume/alarm": "80",
        f"{BASE}/volume/music": "40", f"{BASE}/next_alarm": "None", f"{BASE}/timers": "0",
        f"{BASE}/timers/attributes": json.dumps({"timers": []}), f"{BASE}/backend": "ON", f"{BASE}/muted": "OFF",
        f"{BASE}/availability": "online"}
    assert all(qos == 1 and retained for _, _, qos, retained in client.published)

    before = len(client.published)
    device.publish_state()
    assert len(client.published) == before

    nova.levels["alarm"] = 55
    nova.activity = "listening"
    device.publish_state()
    assert sorted(client.published[before:]) == [(f"{BASE}/status", "listening", 1, True),
                                                  (f"{BASE}/volume/alarm", "55", 1, True)]

    # After a dropped connection all of it is sent again, in case the broker itself
    # restarted and lost what it was keeping.
    client.drops()
    before = len(client.published)
    device.publish_state()
    assert len(client.published) == before
    client.connects()
    assert {topic for topic, _, _, _ in client.published[before:]} == set(state)


def test_the_next_alarm_and_the_timers_are_sent_as_home_assistant_reads_them(started, nova):
    device, client = started
    nova.overview = {"timers": [{"id": 1, "name": "pizza timer", "due_at": DUE},
                                {"id": 2, "name": "call mum reminder", "due_at": DUE + 600}],
                     "alarms": [{"id": 3, "name": "7am alarm", "due_at": DUE + 3600}]}
    client.connects()
    state = client.latest()

    # A time with its time zone, which is what Home Assistant's timestamp sensors take.
    next_alarm = datetime.fromisoformat(state[f"{BASE}/next_alarm"])
    assert next_alarm.tzinfo is not None and next_alarm.timestamp() == DUE + 3600
    assert state[f"{BASE}/timers"] == "2"
    # Home Assistant only takes attributes as a JSON object.
    timers = json.loads(state[f"{BASE}/timers/attributes"])["timers"]
    assert [timer["name"] for timer in timers] == ["pizza timer", "call mum reminder"]
    assert datetime.fromisoformat(timers[1]["ends"]).timestamp() == DUE + 600

    # With no backend set up, whether it's reached isn't known.
    nova.reachable = None
    nova.overview = {"timers": [], "alarms": []}
    device.publish_state()
    state = client.latest()
    assert (state[f"{BASE}/backend"], state[f"{BASE}/next_alarm"], state[f"{BASE}/timers"]) == ("None", "None", "0")


def test_home_assistants_commands_reach_nova(started, nova):
    device, client = started
    client.connects()
    client.delivers(f"{BASE}/volume/alarm/set", "40")
    client.delivers(f"{BASE}/muted/set", "ON")
    client.delivers(f"{BASE}/stop", "PRESS")
    client.delivers(f"{BASE}/restart", "PRESS")
    client.delivers(f"{BASE}/speak", "Washing's done")
    assert nova.spoken.wait(2)
    assert nova.calls == [("set_volume", "alarm", 40), ("set_muted", True), ("stop",), ("restart",),
                          ("speak", "Washing's done")]

    # What a command changed is sent straight back, so Home Assistant's slider and
    # switch don't wait to catch up.
    before = len(client.published)
    nova.is_muted = True
    client.delivers(f"{BASE}/muted/set", "ON")
    assert client.published[before:] == [(f"{BASE}/muted", "ON", 1, True)]


def test_junk_commands_are_ignored(started, nova, caplog):
    device, client = started
    client.connects()
    for junk in ("abc", "nan", "inf", ""):
        client.delivers(f"{BASE}/volume/general/set", junk)
    client.delivers(f"{BASE}/volume/general/set", "150")
    client.delivers(f"{BASE}/volume/music/set", "-5")
    client.delivers(f"{BASE}/volume/loud/set", "50")
    client.delivers(f"{BASE}/muted/set", "maybe")
    client.delivers(f"{BASE}/speak", "   ")
    # Home Assistant never has the broker keep a command, so one that's kept is left
    # over from something else - and would act every time Nova connected, restarting
    # it over and over.
    client.delivers(f"{BASE}/restart", "PRESS", retain=True)
    assert nova.calls == [("set_volume", "general", 100), ("set_volume", "music", 0)]

    # A control which fails is logged, rather than let loose on paho's own thread.
    nova.broken = True
    client.delivers(f"{BASE}/stop", "PRESS")
    assert any("the speaker's gone" in record.getMessage() for record in _complaints(caplog))


def test_a_broker_which_cant_be_reached_doesnt_stop_nova(started, caplog):
    device, client = started
    # start() has already returned, leaving paho to keep trying in the background.
    assert client.looping
    for _ in range(5):
        client.cant_connect()
    complaints = _complaints(caplog)
    assert len(complaints) == 1 and "broker.local" in complaints[0].getMessage()
    device.publish_state()
    assert client.published == []

    # Once it's reached, Nova's in Home Assistant as usual.
    client.connects()
    assert client.sent(DISCOVERY)

    # Losing it again is said once more, as is a password the broker turns away.
    client.drops()
    client.cant_connect()
    client.connects(refused=True)
    complaints = _complaints(caplog)
    assert len(complaints) == 2 and "Unspecified error" in complaints[1].getMessage()
    client.connects()
    client.connects(refused=True)
    complaints = _complaints(caplog)
    assert len(complaints) == 3 and "Not authorized" in complaints[2].getMessage()


def test_without_paho_nova_starts_all_the_same(monkeypatch, nova, caplog):
    def missing(client_id):
        raise ImportError("No module named 'paho'")

    monkeypatch.setattr(ha_device, "_paho_client", missing)
    device = NovaDevice("broker.local", 1883, "nova", "secret", nova.controls(), node_id="nova_test")
    device.start()
    device.publish_state()
    device.stop()
    assert "paho-mqtt" in _complaints(caplog)[0].getMessage()


def test_its_off_without_a_broker(make_config, nova):
    settings = {"host": "", "port": 1883, "username": "", "name": "Nova"}
    assert NovaDevice.from_config(make_config(mqtt=settings), "pw", nova.controls()) is None


def test_its_ids_come_from_its_name_and_the_machine(monkeypatch, make_config, nova, caplog):
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "home-assistant")
    settings = {"host": " 192.168.1.20 ", "port": 1883, "username": "nova", "name": "Nova"}
    device = NovaDevice.from_config(make_config(mqtt=settings), "pw", nova.controls())
    assert device.identifier == "nova_home_assistant" and (device.host, device.port) == ("192.168.1.20", 1883)
    assert _complaints(caplog) == []

    # A second Nova, named for its room, is a second device.
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "kitchen-pi.local")
    device = NovaDevice.from_config(make_config(mqtt={**settings, "name": "Nova Kitchen"}), None, nova.controls())
    assert device.identifier == "nova_kitchen_kitchen_pi"
    assert "MQTT_PASSWORD" in _complaints(caplog)[0].getMessage()


def test_stopping_says_its_offline(started):
    device, client = started
    client.connects()
    device.stop()
    assert client.published[-1] == (f"{BASE}/availability", "offline", 1, True)
    assert client.disconnected and not client.looping


def test_nothing_said_is_published(started, nova):
    device, client = started
    client.connects()
    client.delivers(f"{BASE}/speak", "Washing's done")
    assert nova.spoken.wait(2)
    nova.activity = "speaking"
    device.publish_state()
    client.delivers("homeassistant/status", "online")

    # Only how Nova is set and what it's doing - nothing anyone said, nor its answers.
    assert {topic for topic, _, _, _ in client.published} <= {
        DISCOVERY, f"{BASE}/availability", f"{BASE}/status", f"{BASE}/volume/general", f"{BASE}/volume/alarm",
        f"{BASE}/volume/music", f"{BASE}/next_alarm", f"{BASE}/timers", f"{BASE}/timers/attributes",
        f"{BASE}/backend", f"{BASE}/muted"}
    assert not any("Washing" in payload for _, payload, _, _ in client.published)
