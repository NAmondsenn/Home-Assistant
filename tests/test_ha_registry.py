"""Tests for reading Home Assistant's registry over its WebSocket API."""

import json
import logging
import time

import pytest
import websocket

from voice_assistant import ha_registry, network
from voice_assistant.ha_registry import (Entry, HomeRegistry, HomeSocket, HomeSocketError, Snapshot,
                                         button_kind)


class FakeConnection:
    """Stands in for a websocket connection, answering auth_required and then each message by its type."""

    def __init__(self, answers=None, sign_in="auth_ok"):
        self.answers = answers or {}
        self.sign_in = sign_in
        self.sent = []
        self.closed = False
        self._queue = [json.dumps({"type": "auth_required"})]

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if message["type"] == "auth":
            self._queue.append(json.dumps({"type": self.sign_in}))
            return
        # An event comes first, which isn't an answer, to be skipped.
        self._queue.append(json.dumps({"type": "event", "id": message["id"]}))
        answer = self.answers.get(message["type"], {"success": True, "result": message["type"]})
        self._queue.append(json.dumps({"type": "result", "id": message["id"], **answer}))

    def recv(self):
        return self._queue.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def connect(monkeypatch):
    """Has create_connection hand out the given connection, noting the address."""
    monkeypatch.setattr(network, "online", lambda: True)

    def use(connection):
        urls = []

        def create(url, timeout=None):
            urls.append(url)
            return connection
        monkeypatch.setattr(websocket, "create_connection", create)
        return urls
    return use


def test_a_call_signs_in_then_asks_each_thing_in_turn(connect):
    connection = FakeConnection()
    connect(connection)
    results = HomeSocket("http://ha.test:8123", "token").call([{"type": "one"}, {"type": "two"}])
    assert results == ["one", "two"]
    assert connection.sent == [{"type": "auth", "access_token": "token"},
                               {"type": "one", "id": 1}, {"type": "two", "id": 2}]
    assert connection.closed


def test_a_refused_sign_in_or_a_failed_answer_raises(connect):
    refused = FakeConnection(sign_in="auth_invalid")
    connect(refused)
    with pytest.raises(HomeSocketError):
        HomeSocket("http://ha.test:8123", "token").call([{"type": "one"}])
    assert refused.closed

    failed = FakeConnection({"one": {"success": False, "error": {"code": "unauthorized"}}})
    connect(failed)
    with pytest.raises(HomeSocketError):
        HomeSocket("http://ha.test:8123", "token").call([{"type": "one"}])
    assert failed.closed


def test_a_timeout_raises_and_closes(connect):
    class Slow(FakeConnection):
        def recv(self):
            raise websocket.WebSocketTimeoutException("too slow")

    slow = Slow()
    connect(slow)
    with pytest.raises(HomeSocketError):
        HomeSocket("http://ha.test:8123", "token").call([{"type": "one"}])
    assert slow.closed


def test_the_address_is_home_assistants_own_websocket():
    assert HomeSocket("http://homeassistant.local:8123/", "t").url == "ws://homeassistant.local:8123/api/websocket"
    assert HomeSocket("https://home.example.test", "t").url == "wss://home.example.test/api/websocket"


def test_with_no_network_nothing_is_tried(monkeypatch):
    monkeypatch.setattr(network, "online", lambda: False)

    def create(*args, **kwargs):
        raise AssertionError("tried the network")
    monkeypatch.setattr(websocket, "create_connection", create)
    with pytest.raises(HomeSocketError):
        HomeSocket("http://ha.test:8123", "token").call([{"type": "one"}])


# What the live answers look like: the list has no aliases, which get_entries gives in full.
ENTITIES = [
    {"entity_id": "light.bedroom_light", "platform": "hue", "device_id": "dev_lamp", "area_id": None,
     "unique_id": "lamp-1", "original_name": "Bedroom light"},
    {"entity_id": "lock.front_door", "platform": "zwave_js", "device_id": None, "area_id": "hall",
     "unique_id": "lock-1", "original_name": "Front door"},
    {"entity_id": "button.desk_shutdown", "platform": "mqtt", "device_id": "dev_pc", "area_id": None,
     "unique_id": "abc_shutdown", "original_name": "Shutdown"},
    {"entity_id": "button.desk_restart", "platform": "mqtt", "device_id": "dev_pc", "area_id": None,
     "unique_id": "abc_restart", "original_name": "Restart"},
    {"entity_id": "button.desk_sleep", "platform": "mqtt", "device_id": "dev_pc", "area_id": None,
     "unique_id": "abc_sleep", "original_name": "Sleep"},
    {"entity_id": "button.desk_monitor", "platform": "mqtt", "device_id": "dev_pc", "area_id": None,
     "unique_id": "abc_monitorsleep", "original_name": "Monitor Sleep"},
    {"entity_id": "button.desk_lock", "platform": "mqtt", "device_id": "dev_pc", "area_id": None,
     "unique_id": "abc_lock", "original_name": "Lock"},
    {"entity_id": "button.desk_launch", "platform": "mqtt", "device_id": "dev_pc", "area_id": None,
     "unique_id": "abc_launchha", "original_name": "Launch HA"},
    {"entity_id": "button.nova_restart", "platform": "mqtt", "device_id": "dev_nova", "area_id": None,
     "unique_id": "nova_restart", "original_name": "Restart"},
]
DEVICES = [
    {"id": "dev_lamp", "name": "Lamp", "name_by_user": "Reading lamp", "manufacturer": "Signify", "model": "Bulb",
     "area_id": "bedroom"},
    {"id": "dev_pc", "name": "Desk", "name_by_user": None, "manufacturer": "HASS.Agent Team", "model": "Agent",
     "area_id": "study"},
    {"id": "dev_nova", "name": "Nova", "name_by_user": None, "manufacturer": "Nova", "model": "Satellite",
     "area_id": None},
]
AREAS = [{"area_id": "bedroom", "name": "Bedroom", "aliases": []},
         {"area_id": "hall", "name": "Hall", "aliases": []},
         {"area_id": "study", "name": "Study", "aliases": []}]
EXPOSED = {"exposed_entities": {"light.bedroom_light": {"conversation": True},
                                "lock.front_door": {"conversation": False}}}


class FakeSocket:
    """Stands in for HomeSocket, answering from the live answers' shapes and keeping each call."""

    def __init__(self, aliases=None, fail=None):
        self.calls = []
        self.aliases = aliases if aliases is not None else {"light.bedroom_light": ["main light"]}
        self.fail = fail

    def call(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise self.fail
        answers = {"config/entity_registry/list": ENTITIES, "homeassistant/expose_entity/list": EXPOSED,
                   "config/device_registry/list": DEVICES, "config/area_registry/list": AREAS,
                   "config/entity_registry/get_entries": {
                       entity["entity_id"]: {**entity, "aliases": self.aliases.get(entity["entity_id"], [])}
                       for entity in ENTITIES}}
        return [answers[message["type"]] for message in messages]


def test_the_registry_is_read_into_a_snapshot():
    socket = FakeSocket()
    snapshot = HomeRegistry(socket).read()
    assert [[m["type"] for m in call] for call in socket.calls] == [
        ["config/entity_registry/list", "homeassistant/expose_entity/list", "config/device_registry/list",
         "config/area_registry/list"],
        ["config/entity_registry/get_entries"]]
    assert socket.calls[1][0]["entity_ids"] == [entity["entity_id"] for entity in ENTITIES]

    assert snapshot.aliases("light.bedroom_light") == ("main light",)
    assert snapshot.exposed("light.bedroom_light") and not snapshot.exposed("lock.front_door")
    assert not snapshot.exposed("light.unknown")
    # The light is in its device's area, the lock in its own.
    assert snapshot.area_name("light.bedroom_light") == "Bedroom"
    assert snapshot.area_name("lock.front_door") == "Hall"
    assert snapshot.area_name("light.unknown") is None
    assert snapshot.platform("lock.front_door") == "zwave_js" and snapshot.platform("light.unknown") is None
    assert snapshot.devices["dev_lamp"] == {"name": "Reading lamp", "maker": "Signify", "model": "Bulb",
                                            "area_id": "bedroom"}
    assert snapshot.devices["dev_pc"]["name"] == "Desk"
    assert snapshot.areas["hall"] == "Hall"
    assert snapshot.on_device("dev_lamp") == ["light.bedroom_light"]
    assert snapshot.read_at > 0


def test_hass_agents_buttons_are_found_by_their_device_and_kind():
    snapshot = HomeRegistry(FakeSocket()).read()
    assert snapshot.hass_agent_buttons() == {
        "button.desk_shutdown": ("dev_pc", "shutdown"), "button.desk_restart": ("dev_pc", "restart"),
        "button.desk_sleep": ("dev_pc", "sleep"), "button.desk_monitor": ("dev_pc", "monitorsleep"),
        "button.desk_lock": ("dev_pc", "lock"), "button.desk_launch": ("dev_pc", "other")}
    # Nova's own Restart button isn't one of them.
    assert "button.nova_restart" not in snapshot.hass_agent_buttons()


def test_a_buttons_kind_is_read_from_its_name_else_its_unique_id():
    def kind(original_name, unique_id=""):
        return button_kind(Entry("button.x", "mqtt", "d", None, unique_id, original_name, (), True))
    assert kind("MonitorSleep") == "monitorsleep" and kind("Shut down") == "shutdown"
    assert kind("Reboot") == "restart"
    assert kind(None, "pc_monitorsleep") == "monitorsleep"
    assert kind("Launch HA", "pc_launchha") == "other" and kind(None, "") == "other"
    assert ha_registry.PC_SAFE == ("sleep", "monitorsleep", "lock")


def test_a_none_alias_is_dropped():
    snapshot = HomeRegistry(FakeSocket(aliases={"light.bedroom_light": [None, "main light"]})).read()
    assert snapshot.aliases("light.bedroom_light") == ("main light",)


def test_a_wrong_shaped_answer_is_dropped_by_refresh():
    class Odd(FakeSocket):
        def call(self, messages):
            return [None for _ in messages]
    registry = HomeRegistry(Odd())
    with pytest.raises(TypeError):
        registry.read()
    assert registry.refresh() is False and registry.snapshot() is None


def test_any_failure_drops_the_snapshot_and_the_thread_carries_on(caplog):
    socket = FakeSocket()
    registry = HomeRegistry(socket, every=60, retry=0.01)
    assert registry.refresh() is True and registry.snapshot() is not None
    # An answer of a shape nobody foresaw: exposed_entities is a list.
    odd = HomeRegistry.read
    HomeRegistry.read = lambda self: (_ for _ in ()).throw(AttributeError("'list' has no attribute 'get'"))
    try:
        with caplog.at_level(logging.WARNING, logger=ha_registry.logger.name):
            assert registry.refresh() is False and registry.snapshot() is None
            assert caplog.records[-1].exc_info
            registry.start()
            deadline = time.time() + 5
            passes = []
            registry.refresh = lambda: passes.append(1) or False
            while len(passes) < 3 and time.time() < deadline:
                time.sleep(0.01)
            assert len(passes) >= 3 and registry._thread.is_alive()
    finally:
        registry.stop()
        HomeRegistry.read = odd


def test_a_call_has_one_deadline_for_the_whole_exchange(connect):
    class Slow(FakeConnection):
        def recv(self):
            time.sleep(0.03)
            return super().recv()

    slow = Slow()
    connect(slow)
    # Each answer is quicker than the timeout, but the five of them together aren't.
    with pytest.raises(HomeSocketError):
        HomeSocket("http://ha.test:8123", "token", timeout=0.1).call([{"type": "one"}, {"type": "two"}])
    assert slow.closed


def test_no_snapshot_until_a_read_works_and_none_while_reads_fail(caplog):
    socket = FakeSocket(fail=HomeSocketError("there's no network"))
    registry = HomeRegistry(socket)
    assert registry.snapshot() is None
    with caplog.at_level(logging.INFO, logger=ha_registry.logger.name):
        assert registry.refresh() is False and registry.refresh() is False
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        assert registry.snapshot() is None

        socket.fail = None
        assert registry.refresh() is True
        assert isinstance(registry.snapshot(), Snapshot)
        assert any("being read again" in r.getMessage() for r in caplog.records)

        # A fresh failure after that is logged again, and drops the snapshot.
        socket.fail = HomeSocketError("down")
        registry.refresh()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
        assert registry.snapshot() is None


def test_the_thread_reads_at_once_then_retries_sooner_after_a_failure():
    socket = FakeSocket(fail=HomeSocketError("down"))
    registry = HomeRegistry(socket, every=60, retry=0.01)
    registry.start()
    try:
        deadline = time.time() + 5
        while len(socket.calls) < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert len(socket.calls) >= 3
        assert registry._thread.name == "ha-registry"
    finally:
        registry.stop()
    assert registry._thread is None
