"""
Home Assistant Device Module.

Nova as a device of its own in Home Assistant, the way the Windows PC already is
through HASS.Agent: announced to Home Assistant's MQTT broker - the Mosquitto add-on -
through MQTT discovery, with eleven controls. What Nova's doing, its three volumes,
the next alarm, the timers running, Stop, Restart, Speak for automations ("the
washing's done"), whether it's reaching its backend, and a microphone mute. Its room
is whichever area it's given in Home Assistant - see docs/decisions/0005.

Home Assistant is told how Nova is set and what it's doing, and nothing else - never
anything said to it, nor its answers. The whole device shows as unavailable while
Nova isn't running: the broker says "offline" for it if its connection drops.

It logs in as a Home Assistant user of its own, with MQTT_PASSWORD from
~/.config/nova/.env. paho-mqtt does the talking, and is only imported once it's
needed, so Nova runs without it until mqtt.host is set.
"""

import json
import logging
import math
import queue
import re
import socket
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional

from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Where Home Assistant looks for devices announcing themselves, and where it says
# it's back after a restart - when the announcement is sent again.
DISCOVERY_PREFIX = "homeassistant"
HA_STATUS = "homeassistant/status"

# Home Assistant's own words for each state, and "None" for one which isn't known -
# a backend that isn't set up, or no alarm.
ONLINE, OFFLINE = "online", "offline"
ON, OFF = "ON", "OFF"
UNKNOWN = "None"

# What Nova can be doing, as main.py has it.
STATUSES = ("idle", "listening", "thinking", "speaking")

# Nova's volumes, each with its control's id, its name in Home Assistant and its icon.
VOLUMES = (("general", "volume", "Volume", "mdi:volume-high"),
           ("alarm", "alarm_volume", "Alarm volume", "mdi:alarm"),
           ("music", "music_volume", "Music volume", "mdi:music"))

# How often what Nova's doing is looked at, in seconds. Only what's changed is sent.
PUBLISH_EVERY = 1

# Everything's sent until the broker says it has it, rather than once and hoped for -
# a Stop pressed in Home Assistant mustn't go missing.
QOS = 1


@dataclass
class DeviceControls:
    """
    What Home Assistant can see and do of Nova, each a function main.py gives it.

    The ones which read how Nova is - status, volumes, clock, backend and muted - are
    called every second on the device's own state thread, and on paho's as it connects,
    so they must return at once. The commands - set_volume, set_muted, stop, restart
    and speak - are carried out one at a time, in the order Home Assistant sent them,
    on a thread of their own. A slow one holds up only the commands behind it, never
    the connection - but it does hold them up, so a speak which waited until the words
    had been said would keep a Stop pressed meanwhile waiting too.
    """
    # What Nova's doing: one of STATUSES.
    status: Callable[[], str]
    # Each volume's level, as a percentage, by its category - "general", "alarm" and "music".
    volumes: Callable[[], Dict[str, int]]
    set_volume: Callable[[str, int], None]
    # The timers and alarms set, as Clock.overview has them.
    clock: Callable[[], Dict]
    # Whether Nova's reaching its backend, or None if it doesn't use one.
    backend: Callable[[], Optional[bool]]
    muted: Callable[[], bool]
    set_muted: Callable[[bool], None]
    # What "Hey Nova, stop" does: silences anything ringing, and stops Nova talking.
    stop: Callable[[], None]
    restart: Callable[[], None]
    # Says a message out loud, once any conversation has finished.
    speak: Callable[[str], None]


def _slug(text: str) -> str:
    """An MQTT id made from text - "Nova Kitchen_kitchen-pi" as "nova_kitchen_kitchen_pi"."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _time(timestamp: float) -> str:
    """A time as Home Assistant's timestamp sensors take it - with its time zone."""
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _percent(payload: str) -> Optional[int]:
    """A volume Home Assistant sent, between 0 and 100, or None if it isn't a number."""
    try:
        value = float(payload)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(100, round(value)))


def _paho_client(client_id: str):
    """A paho-mqtt client, with the callbacks of its version 2."""
    # Imported only here, so Nova - and its tests - run without paho-mqtt until MQTT is
    # set up.
    import paho.mqtt.client as mqtt
    return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


class NovaDevice:
    """Nova's device in Home Assistant, kept up to date over MQTT."""

    def __init__(self, host: str, port: int, username: str, password: str, controls: DeviceControls,
                 name: str = "Nova", node_id: Optional[str] = None, client_factory=None):
        """
        Args:
            host: Home Assistant's broker - its own address on the home network, e.g.
                "192.168.1.20". Its web address only passes on web requests.
            port: The broker's port, MQTT's usual 1883.
            username: Nova's Home Assistant user, or nothing for a broker which lets
                anyone in.
            password: MQTT_PASSWORD.
            controls: What Home Assistant sees and does of Nova.
            name: What the device is called in Home Assistant.
            node_id: Its MQTT id - from name and the machine's own name unless given,
                so a second Nova is a second device rather than taking over the first.
            client_factory: Makes the MQTT client from its client id - only replaced by
                the tests.
        """
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.controls = controls
        self.name = name
        self.node_id = node_id or _slug(f"{name}_{socket.gethostname().split('.')[0]}")
        self._client_factory = client_factory

        base = f"nova/{self.node_id}"
        self._availability = f"{base}/availability"
        self._status = f"{base}/status"
        self._volume = {category: f"{base}/volume/{category}" for category, _, _, _ in VOLUMES}
        self._next_alarm = f"{base}/next_alarm"
        self._timers = f"{base}/timers"
        self._timers_attributes = f"{base}/timers/attributes"
        self._backend = f"{base}/backend"
        self._muted = f"{base}/muted"
        self._stop, self._restart, self._speak = f"{base}/stop", f"{base}/restart", f"{base}/speak"
        self._discovery_topic = f"{DISCOVERY_PREFIX}/device/{self.node_id}/config"

        self._client = None
        self._connected = False
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Home Assistant's commands, waiting their turn - see _queue().
        self._commands = queue.Queue()
        # What each state topic was last sent, so only what's changed is sent again.
        self._lock = threading.Lock()
        self._published: Dict[str, str] = {}
        # Whether a problem has been logged since things last worked, so an outage is
        # logged once rather than at every try.
        self._complained = False
        self._state_failing = False

    @classmethod
    def from_config(cls, config, password: Optional[str], controls: DeviceControls) -> Optional["NovaDevice"]:
        """
        The device mqtt.host in config.yaml sets up, or None if it doesn't name a broker.

        Args:
            config: Configs loaded from config.yaml.
            password: MQTT_PASSWORD.
            controls: What Home Assistant sees and does of Nova.
        """
        settings = config.section("mqtt")
        host = (settings.get("host") or "").strip()
        if not host:
            return None
        if not password:
            logger.warning("mqtt.host is set but MQTT_PASSWORD isn't, in ~/.config/nova/.env, "
                           "so Home Assistant's broker will most likely turn Nova away")
        return cls(host, int(settings.get("port") or 1883), (settings.get("username") or "").strip(),
                   password or "", controls, name=(settings.get("name") or "").strip() or "Nova")

    @property
    def identifier(self) -> str:
        """What Home Assistant knows Nova's device by, among its MQTT identifiers."""
        return self.node_id

    def discovery(self) -> Dict:
        """
        The announcement which makes Nova a device in Home Assistant, with its controls.

        Returns:
            Home Assistant's device-based discovery message, with each control by its id.
        """
        controls = {
            "status": {"p": "sensor", "name": "Status", "state_topic": self._status, "device_class": "enum",
                       "options": list(STATUSES), "icon": "mdi:account-voice"},
        }
        for category, control, name, icon in VOLUMES:
            controls[control] = {"p": "number", "name": name, "state_topic": self._volume[category],
                                 "command_topic": f"{self._volume[category]}/set", "min": 0, "max": 100, "step": 1,
                                 "mode": "slider", "unit_of_measurement": "%", "icon": icon}
        controls.update({
            "next_alarm": {"p": "sensor", "name": "Next alarm", "state_topic": self._next_alarm,
                           "device_class": "timestamp", "icon": "mdi:alarm"},
            "timers": {"p": "sensor", "name": "Timers", "state_topic": self._timers,
                       "json_attributes_topic": self._timers_attributes, "icon": "mdi:timer-outline"},
            "stop": {"p": "button", "name": "Stop", "command_topic": self._stop, "icon": "mdi:stop"},
            "restart": {"p": "button", "name": "Restart", "command_topic": self._restart, "device_class": "restart",
                        "entity_category": "config"},
            "speak": {"p": "notify", "name": "Speak", "command_topic": self._speak},
            "backend": {"p": "binary_sensor", "name": "Backend", "state_topic": self._backend,
                        "device_class": "connectivity", "payload_on": ON, "payload_off": OFF,
                        "entity_category": "diagnostic"},
            "muted": {"p": "switch", "name": "Microphone muted", "state_topic": self._muted,
                      "command_topic": f"{self._muted}/set", "payload_on": ON, "payload_off": OFF,
                      "icon": "mdi:microphone-off"},
        })
        for control, settings in controls.items():
            settings["unique_id"] = f"{self.node_id}_{control}"
            settings["qos"] = QOS
        return {"dev": {"ids": [self.node_id], "name": self.name, "mf": "Nova", "mdl": "Raspberry Pi"},
                "o": {"name": "Nova"},
                "availability_topic": self._availability,
                "cmps": controls}

    # Connecting

    def start(self):
        """
        Connects to the broker in the background, and keeps Home Assistant up to date
        from then on. A broker which can't be reached is tried again and again, without
        holding Nova up.
        """
        if self._client is not None:
            return
        try:
            client = (self._client_factory or _paho_client)(self.node_id)
        except ImportError as e:
            logger.error(f"Nova can't be in Home Assistant without paho-mqtt ({e}) - "
                         f"install requirements.txt again")
            return
        if self.username:
            client.username_pw_set(self.username, self.password or None)
        # Sent by the broker for Nova if its connection drops, so the device shows as
        # unavailable rather than idle when Nova's gone.
        client.will_set(self._availability, OFFLINE, qos=QOS, retain=True)
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        # Kept before connecting, since a broker close by can answer before
        # loop_start() has even returned, and the announcement goes through it.
        self._client = client
        try:
            client.connect_async(self.host, self.port)
        except (ValueError, OSError) as e:
            # paho checks the address before it tries in the background - a port of 0
            # or below, say - so trying again wouldn't help, and Nova carries on without
            # its device rather than stopping as it starts.
            logger.error(f"Nova can't be in Home Assistant with its broker at {self.host}:{self.port} ({e}) - "
                         f"mqtt in config.yaml needs checking")
            self._client = None
            return
        # paho's own thread, which connects - and connects again whenever the
        # connection's lost.
        client.loop_start()
        self._thread = threading.Thread(target=self._keep_publishing, name="ha-device", daemon=True)
        self._thread.start()
        threading.Thread(target=self._carry_out, name="ha-device-commands", daemon=True).start()

    def stop(self):
        """Says Nova's offline, and disconnects."""
        self._stopping.set()
        client, self._client = self._client, None
        if client is None:
            return
        # Ends the commands' thread once it's carried out those already waiting - not
        # waited for, since one may be a message still to be said.
        self._commands.put(None)
        connected, self._connected = self._connected, False
        try:
            # The broker only sends the last will for a connection which drops, not
            # one closed like this.
            if connected:
                client.publish(self._availability, OFFLINE, qos=QOS, retain=True)
            client.disconnect()
            # Waits for "offline" to be sent. Without a connection there's nothing to
            # wait for, and waiting could take up to paho's 5 seconds for a broker which
            # isn't answering - its thread finishes by itself once disconnected.
            if connected:
                client.loop_stop()
        except Exception as e:
            logger.warning(f"Couldn't disconnect from Home Assistant's broker cleanly: {e}")

    def _complain(self, message: str):
        """Logs a problem with the connection, once until it's working again."""
        if not self._complained:
            logger.warning(message)
        self._complained = True

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """paho's callback for the broker answering a connection - accepted or turned away."""
        try:
            # Too late - "offline" may already have been sent.
            if self._stopping.is_set():
                return
            if reason_code.is_failure:
                self._complain(f"Home Assistant's broker turned Nova away ({reason_code}), so trying again")
                return
            logger.info(f"Nova's in Home Assistant, through its broker at {self.host}")
            self._complained = False
            client.subscribe([(f"{self._volume[category]}/set", QOS) for category, _, _, _ in VOLUMES] +
                             [(f"{self._muted}/set", QOS), (self._stop, QOS), (self._restart, QOS),
                              (self._speak, QOS), (HA_STATUS, QOS)])
            self._announce()
            # Everything's sent again, since the broker may have restarted and lost
            # what it was keeping - and only then is Nova shown as there.
            with self._lock:
                self._published.clear()
                self._connected = True
            self.publish_state()
            client.publish(self._availability, ONLINE, qos=QOS, retain=True)
        except Exception as e:
            logger.error(f"Couldn't set Nova up in Home Assistant: {e}")

    def _on_connect_fail(self, client, userdata):
        """paho's callback for a broker which couldn't be reached at all."""
        self._complain(f"Couldn't reach Home Assistant's broker at {self.host}:{self.port}, so trying again")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        """paho's callback for the connection ending - lost, or closed by stop()."""
        self._connected = False
        if not self._stopping.is_set():
            self._complain(f"Lost Home Assistant's broker ({reason_code}), so trying again")

    def _announce(self):
        """Tells Home Assistant about Nova's device and its controls."""
        client = self._client
        if client is not None:
            client.publish(self._discovery_topic, json.dumps(self.discovery()), qos=QOS, retain=True)

    # Nova's state, to Home Assistant

    def _state(self) -> Dict[str, str]:
        """What each state topic should say now."""
        controls = self.controls
        status = controls.status()
        state = {self._status: status if status in STATUSES else UNKNOWN}
        volumes = controls.volumes()
        for category, _, _, _ in VOLUMES:
            if category in volumes:
                state[self._volume[category]] = str(round(volumes[category]))
        overview = controls.clock()
        alarms = overview.get("alarms") or []
        state[self._next_alarm] = _time(min(alarm["due_at"] for alarm in alarms)) if alarms else UNKNOWN
        timers = overview.get("timers") or []
        state[self._timers] = str(len(timers))
        # When each one ends, and not its name: a reminder's is the user's own words -
        # "take my tablets reminder" - which would be kept by the broker and in Home
        # Assistant's history. Home Assistant takes attributes only as a JSON object,
        # so the list is in one.
        state[self._timers_attributes] = json.dumps({"timers": [{"ends": _time(timer["due_at"])} for timer in timers]})
        backend = controls.backend()
        state[self._backend] = UNKNOWN if backend is None else (ON if backend else OFF)
        state[self._muted] = ON if controls.muted() else OFF
        return state

    def publish_state(self):
        """
        Sends Home Assistant whatever's changed since it was last sent - each kept by
        the broker, so Home Assistant has it however long ago that was.
        """
        client = self._client
        if client is None or not self._connected:
            return
        with self._lock:
            try:
                state = self._state()
            except Exception as e:
                if not self._state_failing:
                    logger.error(f"Couldn't tell Home Assistant how Nova is: {e}")
                self._state_failing = True
                return
            self._state_failing = False
            for topic, payload in state.items():
                if self._published.get(topic) != payload:
                    client.publish(topic, payload, qos=QOS, retain=True)
                    self._published[topic] = payload

    def _keep_publishing(self):
        """Keeps Home Assistant up to date, until stopped."""
        while not self._stopping.wait(PUBLISH_EVERY):
            self.publish_state()

    # Home Assistant's commands, to Nova

    def _on_message(self, client, userdata, message):
        """paho's callback for a message from Home Assistant - a command, or it being back."""
        try:
            self._command(message.topic, message.payload.decode("utf-8", "replace").strip(), message.retain)
        except Exception as e:
            logger.error(f"Couldn't do what Home Assistant asked, on {message.topic}: {e}")

    def _command(self, topic: str, payload: str, retained: bool):
        """
        Checks one of Home Assistant's commands, and queues it to be carried out.

        Args:
            topic: Where it came in.
            payload: What it said.
            retained: Whether the broker had kept it, rather than it being sent just now.
        """
        if topic == HA_STATUS:
            if payload == ONLINE:
                self._announce()
            return
        # Home Assistant never has the broker keep a command, so one that's kept is left
        # over from something else - and would act every time Nova connected, which
        # for Restart would be over and over.
        if retained:
            return

        if topic == self._stop:
            logger.info("Stop, from Home Assistant")
            self._queue("Stop", self.controls.stop)
        elif topic == self._restart:
            logger.info("Restart, from Home Assistant")
            self._queue("Restart", self.controls.restart)
        elif topic == self._speak:
            if payload:
                logger.info(f"Home Assistant asked to say: '{payload}'", extra=TRANSCRIPT)
                self._queue("Speak", self.controls.speak, payload)
        elif topic == f"{self._muted}/set":
            if payload in (ON, OFF):
                logger.info(f"Microphone {'muted' if payload == ON else 'unmuted'} from Home Assistant")
                self._queue("Microphone muted", self._set_muted, payload == ON)
        else:
            for category, _, name, _ in VOLUMES:
                if topic == f"{self._volume[category]}/set":
                    percent = _percent(payload)
                    if percent is not None:
                        self._queue(name, self._set_volume, category, percent)

    def _queue(self, what: str, action: Callable, *args):
        """
        Hands a command to the commands' own thread, rather than carrying it out on
        paho's, which keeps the connection alive - the music's volume goes through
        Spotify, which can take seconds.

        Args:
            what: The control it came from, as Home Assistant names it.
            action: What carries it out.
            *args: What action is given.
        """
        self._commands.put((what, action, args))

    def _carry_out(self):
        """Carries out Home Assistant's commands one at a time, in the order they came, until stopped."""
        while True:
            command = self._commands.get()
            if command is None:
                return
            what, action, args = command
            try:
                action(*args)
            except Exception as e:
                logger.error(f"Home Assistant's {what} failed: {e}")

    def _set_volume(self, category: str, percent: int):
        # Sent straight back, so Home Assistant's slider doesn't wait to catch up.
        self.controls.set_volume(category, percent)
        self.publish_state()

    def _set_muted(self, muted: bool):
        self.controls.set_muted(muted)
        self.publish_state()
