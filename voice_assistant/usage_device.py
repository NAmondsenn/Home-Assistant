"""
Usage Device Module.

What Nova's calls to Claude cost, as a device of its own in Home Assistant: the brain's,
rather than a satellite's, since the brain is the one machine which makes them. It is
announced the same way as each satellite's device - MQTT discovery, through the broker
the Mosquitto add-on runs - and is a NovaDevice in all but what it shows, so it connects,
reconnects, says it's offline when Nova stops and sends only what's changed in just the
same way.

It has seven sensors, all read from UsageLog.summary(): what's been spent today and this
month, the tokens used over the last 24 hours, 7 days and 30 days, the spending by hour
and by day for the charts, and which models did the work. Home Assistant can only read
them - there's nothing to press or set, so it listens for nothing but its own restart,
after which the announcement is sent again.

Counts, costs, model names and the kinds of request are all that's sent, as in
docs/decisions/0005: never anything said to Nova, nor its answers. The log keeps none of
those, and the summary has no place for them.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from voice_assistant.ha_device import HA_STATUS, QOS, UNKNOWN, NovaDevice
from voice_assistant.usage import UsageLog

# Logger setup
logger = logging.getLogger(__name__)

# The device's name, and so the start of each sensor's entity id in Home Assistant.
NAME = "Nova usage"

# Which of the summary's windows each token sensor shows, by its control's id.
TOKEN_WINDOWS = (("tokens_24h", "24h"), ("tokens_7d", "7d"), ("tokens_30d", "30d"))

# Each sensor's control id and name in Home Assistant, the entity id it's to have and its icon.
# The entity ids are set here rather than left for Home Assistant to work out from the
# names: renaming a sensor later would otherwise change its entity id and leave its
# history behind under the old one.
SENSORS = (("spent_today", "Spent today", "sensor.nova_usage_spent_today", "mdi:cash-clock"),
           ("spent_month", "Spent this month", "sensor.nova_usage_spent_this_month", "mdi:calendar-month"),
           ("tokens_24h", "Tokens 24 hours", "sensor.nova_usage_tokens_24_hours", "mdi:text-recognition"),
           ("tokens_7d", "Tokens 7 days", "sensor.nova_usage_tokens_7_days", "mdi:text-recognition"),
           ("tokens_30d", "Tokens 30 days", "sensor.nova_usage_tokens_30_days", "mdi:text-recognition"),
           ("spend_history", "Spend history", "sensor.nova_usage_spend_history", "mdi:chart-line"),
           ("models", "Models", "sensor.nova_usage_models", "mdi:robot-outline"))

# The money sensors' unit - Home Assistant takes a monetary sensor only with a currency code.
CURRENCY = "GBP"


class UsageDevice(NovaDevice):
    """The brain's device in Home Assistant, showing what Nova's Claude calls cost."""

    def __init__(self, host: str, port: int, username: str, password: str, usage: UsageLog,
                 client_factory=None, hostname: Optional[str] = None):
        """
        Args:
            host: Home Assistant's broker - its own address on the home network.
            port: The broker's port, MQTT's usual 1883.
            username: Nova's Home Assistant user, or nothing for a broker which lets
                anyone in.
            password: MQTT_PASSWORD.
            usage: The log of Claude calls, whose summary the sensors show.
            client_factory: Makes the MQTT client from its client id - only replaced by
                the tests.
            hostname: The machine whose name goes in the id, if it isn't this one.
        """
        # No controls: nothing about how Nova is, and nothing Home Assistant can do.
        super().__init__(host, port, username, password, None, name=NAME, client_factory=client_factory,
                         hostname=hostname)
        self.usage = usage
        base = f"nova/{self.node_id}"
        self._topics = {control: f"{base}/{control}" for control, _, _, _ in SENSORS}

    @classmethod
    def from_config(cls, config, password: Optional[str], usage: UsageLog) -> Optional["UsageDevice"]:
        """
        The device mqtt.host in config.yaml sets up, or None if it doesn't name a broker.

        Args:
            config: Configs loaded from config.yaml.
            password: MQTT_PASSWORD.
            usage: The log of Claude calls, whose summary the sensors show.
        """
        settings = config.section("mqtt")
        host = (settings.get("host") or "").strip()
        if not host:
            return None
        if not password:
            logger.warning("mqtt.host is set but MQTT_PASSWORD isn't, in ~/.config/nova/.env, "
                           "so Home Assistant's broker will most likely turn Nova's usage away")
        return cls(host, int(settings.get("port") or 1883), (settings.get("username") or "").strip(),
                   password or "", usage)

    def discovery(self) -> Dict:
        """
        The announcement which makes the usage device in Home Assistant, with its sensors.

        Returns:
            Home Assistant's device-based discovery message, with each sensor by its id.
        """
        controls = {}
        for control, name, entity_id, icon in SENSORS:
            controls[control] = {"p": "sensor", "name": name, "state_topic": self._topics[control],
                                 "json_attributes_topic": f"{self._topics[control]}/attributes",
                                 "default_entity_id": entity_id, "icon": icon,
                                 "unique_id": f"{self.node_id}_{control}", "qos": QOS}
        for control in ("spent_today", "spent_month"):
            controls[control].update({"device_class": "monetary", "unit_of_measurement": CURRENCY})
        return {"dev": {"ids": [self.node_id], "name": self.name, "mf": "Nova", "mdl": "Nova brain"},
                "o": {"name": "Nova"},
                "availability_topic": self._availability,
                "cmps": controls}

    def _subscriptions(self) -> List[Tuple[str, int]]:
        """Only Home Assistant saying it's back, so the announcement is sent again."""
        return [(HA_STATUS, QOS)]

    def _on_message(self, client, userdata, message):
        """paho's callback for a message - only Home Assistant being back means anything."""
        if message.topic == HA_STATUS:
            super()._on_message(client, userdata, message)

    def _state(self) -> Dict[str, str]:
        """What each state topic, and each one's attributes, should say now."""
        # Only read: summary() gives its cached dict, which is also what the dashboard gets.
        summary = self.usage.summary()
        windows = summary["windows"]
        top = next(iter(summary["models"]["7d"]), None)
        # Each sensor's state, and the attributes which go with it - JSON, as Home Assistant
        # takes them. The charts and tables read the attributes, whose lists are too long for a state.
        sensors = {
            "spent_today": (summary["today_pounds"],
                            {key: summary[key] for key in ("daily_limit_pounds", "requests_24h", "cache_share_24h")}),
            "spent_month": (summary["month_pounds"],
                            {key: summary[key] for key in ("month_projection_pounds", "kinds_month")}),
            "spend_history": (summary["today_pounds"],
                              {key: summary[key] for key in ("hourly_pounds", "daily_pounds", "daily_limit_pounds")}),
            "models": (UNKNOWN if top is None else top, {"models": summary["models"]}),
        }
        for control, window in TOKEN_WINDOWS:
            sensors[control] = (windows[window]["output"],
                                {key: windows[window][key] for key in ("total", "pounds", "requests")})
        state = {}
        for control, (value, attributes) in sensors.items():
            state[self._topics[control]] = str(value)
            state[f"{self._topics[control]}/attributes"] = json.dumps(attributes)
        return state
