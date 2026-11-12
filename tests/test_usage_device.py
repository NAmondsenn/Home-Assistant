"""
Usage Device Test
Checks what Nova's Claude calls cost shows in Home Assistant as a device for the brain,
with its seven sensors under entity ids which are fixed from the start, that they follow
the usage log's summary, that the device takes no commands, and that nothing but counts,
costs, model names and request kinds is sent - with stand-ins for paho-mqtt's client and
the usage log.
"""

import json

import pytest

from test_ha_device import FakeClient
from voice_assistant import ha_device
from voice_assistant.usage_device import UsageDevice

BASE = "nova/nova_usage_nova"
DISCOVERY = "homeassistant/device/nova_usage_nova/config"

SUMMARY = {
    "today_pounds": 0.07,
    "month_pounds": 1.5,
    "month_projection_pounds": 4.2,
    "daily_limit_pounds": 1.0,
    "requests_24h": 12,
    "cache_share_24h": 0.8,
    "windows": {"24h": {"output": 1200, "total": 90000, "pounds": 0.07, "requests": 12},
                "7d": {"output": 8000, "total": 600000, "pounds": 0.5, "requests": 80},
                "30d": {"output": 30000, "total": 2500000, "pounds": 1.5, "requests": 300}},
    "hourly_pounds": [["2026-11-12T09:00:00+00:00", 0.01], ["2026-11-12T10:00:00+00:00", 0.06]],
    "daily_pounds": [["2026-11-11", 0.2], ["2026-11-12", 0.07]],
    "kinds_month": {"chat": 1.0, "tools": 0.5},
    "models": {"24h": {"claude-haiku": 1200}, "7d": {"claude-sonnet": 6000, "claude-haiku": 2000},
               "30d": {"claude-sonnet": 20000, "claude-haiku": 10000}},
}


class FakeUsage:
    """Stands in for the UsageLog, with the summary it's given."""

    def __init__(self, summary):
        self.current = summary

    def summary(self):
        return self.current


@pytest.fixture
def usage():
    return FakeUsage(json.loads(json.dumps(SUMMARY)))


@pytest.fixture
def started(usage):
    """A UsageDevice for usage, started, with the client it made - not yet connected."""
    made = []

    def factory(client_id):
        made.append(FakeClient(client_id))
        return made[-1]

    device = UsageDevice("broker.local", 1883, "nova", "secret", usage, client_factory=factory, hostname="nova")
    device.start()
    yield device, made[0]
    device.stop()


def _config(client):
    [(_, payload, _, retained)] = client.sent(DISCOVERY)
    assert retained
    return json.loads(payload)


def test_discovery_lists_usage_sensors(started):
    device, client = started
    assert client.client_id == "nova_usage_nova" and device.identifier == "nova_usage_nova"
    client.connects()
    config = _config(client)
    assert config["dev"]["ids"] == ["nova_usage_nova"]
    assert (config["dev"]["name"], config["dev"]["mdl"]) == ("Nova usage", "Nova brain")
    assert config["availability_topic"] == f"{BASE}/availability"

    controls = config["cmps"]
    assert set(controls) == {"spent_today", "spent_month", "tokens_24h", "tokens_7d", "tokens_30d",
                             "spend_history", "models"}
    for control, settings in controls.items():
        assert settings["p"] == "sensor"
        assert settings["unique_id"] == f"nova_usage_nova_{control}"
        # Read-only: nothing here for Home Assistant to press or set.
        assert "command_topic" not in settings
        assert settings["state_topic"] == f"{BASE}/{control}"
        assert settings["json_attributes_topic"] == f"{BASE}/{control}/attributes"
    assert {s["name"] for s in controls.values()} == {
        "Spent today", "Spent this month", "Tokens 24 hours", "Tokens 7 days", "Tokens 30 days",
        "Spend history", "Models"}
    for control in ("spent_today", "spent_month"):
        assert controls[control]["device_class"] == "monetary"
        assert controls[control]["unit_of_measurement"] == "GBP"


def test_entity_ids_are_pinned(started):
    device, client = started
    client.connects()
    pinned = {control: settings["default_entity_id"] for control, settings in _config(client)["cmps"].items()}
    # Renaming one later loses its chart history, so they're set rather than left to be worked out.
    assert pinned == {
        "spent_today": "sensor.nova_usage_spent_today",
        "spent_month": "sensor.nova_usage_spent_this_month",
        "tokens_24h": "sensor.nova_usage_tokens_24_hours",
        "tokens_7d": "sensor.nova_usage_tokens_7_days",
        "tokens_30d": "sensor.nova_usage_tokens_30_days",
        "spend_history": "sensor.nova_usage_spend_history",
        "models": "sensor.nova_usage_models",
    }


def test_state_from_summary(started):
    device, client = started
    client.connects()
    sent = client.latest()
    assert sent[f"{BASE}/spent_today"] == "0.07"
    assert json.loads(sent[f"{BASE}/spent_today/attributes"]) == {
        "daily_limit_pounds": 1.0, "requests_24h": 12, "cache_share_24h": 0.8}
    assert sent[f"{BASE}/spent_month"] == "1.5"
    assert json.loads(sent[f"{BASE}/spent_month/attributes"]) == {
        "month_projection_pounds": 4.2, "kinds_month": {"chat": 1.0, "tools": 0.5}}
    assert sent[f"{BASE}/tokens_7d"] == "8000"
    assert json.loads(sent[f"{BASE}/tokens_7d/attributes"]) == {"total": 600000, "pounds": 0.5, "requests": 80}
    assert sent[f"{BASE}/tokens_24h"] == "1200" and sent[f"{BASE}/tokens_30d"] == "30000"
    assert sent[f"{BASE}/spend_history"] == "0.07"
    assert json.loads(sent[f"{BASE}/spend_history/attributes"]) == {
        "hourly_pounds": SUMMARY["hourly_pounds"], "daily_pounds": SUMMARY["daily_pounds"],
        "daily_limit_pounds": 1.0}
    # The 7-day top model is the state; all the windows' are the attributes.
    assert sent[f"{BASE}/models"] == "claude-sonnet"
    assert json.loads(sent[f"{BASE}/models/attributes"]) == {"models": SUMMARY["models"]}
    assert sent[f"{BASE}/availability"] == "online"


def test_state_follows_the_summary_and_is_sent_only_when_it_changes(started, usage):
    device, client = started
    client.connects()
    count = len(client.published)
    device.publish_state()
    assert len(client.published) == count

    usage.current["today_pounds"] = 0.09
    device.publish_state()
    assert client.sent(f"{BASE}/spent_today")[-1][1] == "0.09"
    assert len(client.sent(f"{BASE}/spent_month")) == 1


def test_models_is_unknown_before_any_call(started, usage):
    device, client = started
    usage.current["models"]["7d"] = {}
    usage.current["cache_share_24h"] = None
    client.connects()
    assert client.latest()[f"{BASE}/models"] == "None"
    assert json.loads(client.latest()[f"{BASE}/spent_today/attributes"])["cache_share_24h"] is None


def test_the_summary_is_left_as_it_was(started, usage):
    device, client = started
    before = json.dumps(usage.current, sort_keys=True)
    client.connects()
    assert json.dumps(usage.current, sort_keys=True) == before


def test_subscribes_to_ha_status_only(started):
    device, client = started
    client.connects()
    assert client.subscribed == [("homeassistant/status", 1)]


def test_it_announces_again_when_home_assistant_restarts_and_takes_no_commands(started):
    device, client = started
    client.connects()
    assert len(client.sent(DISCOVERY)) == 1
    client.delivers("homeassistant/status", "online")
    assert len(client.sent(DISCOVERY)) == 2

    count = len(client.published)
    for topic in (f"{BASE}/stop", f"{BASE}/restart", f"{BASE}/speak", f"{BASE}/spent_today/set",
                  "nova/nova_nova/stop"):
        client.delivers(topic, "PRESS")
    assert len(client.published) == count


def _keys(value):
    """Every key in a nested dict, however deep."""
    if not isinstance(value, dict):
        return set()
    keys = set(value)
    for item in value.values():
        keys |= _keys(item)
    return keys


def test_no_words_reach_ha(started, usage):
    device, client = started
    # What a request said and what Nova answered are words which could be anywhere in the log; they
    # must not turn up in what's sent, whatever the summary holds besides.
    usage.current["said"] = "turn the lights off"
    usage.current["answered"] = "Done, the lights are off"
    client.connects()
    sent = " ".join(payload for topic, payload, _, _ in client.published if topic != DISCOVERY)
    assert "lights" not in sent
    # Every key sent is one of the summary's, and only a number or a model's name is a state.
    for topic, payload, _, _ in client.published:
        if topic.endswith("/attributes"):
            assert _keys(json.loads(payload)) <= _keys(SUMMARY)
        elif not topic.endswith("/availability") and topic != DISCOVERY:
            assert payload in ("None", "claude-sonnet") or float(payload) >= 0


def test_a_broken_summary_is_logged_once_and_doesnt_stop_it(started, usage, caplog):
    device, client = started
    client.connects()

    def broken():
        raise RuntimeError("no log")

    usage.summary = broken
    with caplog.at_level("ERROR", logger=ha_device.__name__):
        device.publish_state()
        device.publish_state()
    # Only the errors: whether the logger's INFO lines are kept depends on what ran before.
    assert len([r for r in caplog.records if r.name == ha_device.__name__ and r.levelname == "ERROR"]) == 1


def test_its_off_without_a_broker(make_config, usage):
    assert UsageDevice.from_config(make_config(mqtt={"host": ""}), "pw", usage) is None


def test_its_id_comes_from_the_brains_hostname(monkeypatch, make_config, usage):
    monkeypatch.setattr(ha_device.socket, "gethostname", lambda: "nova.lan")
    settings = {"host": " 192.168.1.20 ", "port": 1883, "username": "nova", "name": "Nova"}
    device = UsageDevice.from_config(make_config(mqtt=settings), "pw", usage)
    assert device.identifier == "nova_usage_nova" and (device.host, device.port) == ("192.168.1.20", 1883)
    assert device.username == "nova" and device.password == "pw" and device.name == "Nova usage"
