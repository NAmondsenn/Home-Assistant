"""
Home Assistant Registry Module.

Reads Home Assistant's entity, device and area registries, and which entities are
exposed to Assist, over its WebSocket API - every five minutes, into a snapshot the
rest of Nova looks things up in (decision 0008, answer 1).

The REST API has none of this: it knows the states, not what an entity is, which device
it belongs to, what the user calls it or which room it's in. Reading it here, rather
than keeping a list of entity ids in code or config, means a new lock, a renamed light
or a moved device is known about within five minutes with nothing changed in Nova
(decision 7) - and the guard on what Nova may work (see ha_guard.py) can go by
what a thing is, not by what it's called.

The registry isn't read for every request: a snapshot is at most five minutes old,
and while Home Assistant can't be reached there's no snapshot at all, so whatever
needs one has to cope without.
"""

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Dict, List, Optional, Tuple

from voice_assistant import network

logger = logging.getLogger(__name__)

# The buttons HASS.Agent gives a PC, by what each does, and which are safe by voice.
# Locking only makes things safer, so it's allowed; shutting down is not.
PC_KINDS = ("shutdown", "restart", "sleep", "monitorsleep", "lock")
PC_SAFE = ("sleep", "monitorsleep", "lock")

# Other names for the same button: "Reboot" is HASS.Agent's restart on some versions.
KIND_NAMES = {"reboot": "restart"}

# Whose devices are HASS.Agent's. Read from the device's maker, never from the
# windows_pc_ in its entity ids, which the user can rename whenever they like - and
# Nova's own MQTT device, whose maker is "Nova", has a Restart button too.
HASS_AGENT_MAKER = "HASS.Agent"


# Seconds the registry's own socket allows each of a read's two calls - the lists, then
# every entity in full - which are a lot of entities (over a thousand, through a proxy)
# and not what a voice command waits on.
REGISTRY_TIMEOUT = 30.0


class HomeSocketError(Exception):
    """Home Assistant's WebSocket couldn't be used - no network, a refusal or a bad answer."""

    def __init__(self, message: str, code: Optional[str] = None):
        """
        Args:
            message: What went wrong.
            code: Home Assistant's own code for an answer which wasn't a success, e.g.
                "config_not_found" - None for anything else, a timeout say.
        """
        super().__init__(message)
        self.code = code


class HomeSocket:
    """One-off conversations with Home Assistant's WebSocket API."""

    def __init__(self, url: str, token: str, timeout: float = 3.0):
        """
        Args:
            url: Home Assistant's address, e.g. "http://homeassistant.local:8123/" -
                "https" is reached as "wss".
            token: A long-lived access token.
            timeout: Seconds to allow once connected - the whole exchange and the close
                together, counted from before connecting and then the connection is cut off.
                Connecting itself can't be interrupted, only noticed once it is over:
                websocket-client allows its own timeout to each step (the TCP connect, TLS
                and the upgrade reply), and the system's resolver the name lookup.
        """
        scheme, _, rest = url.strip().rstrip("/").partition("://")
        self.url = ("wss" if scheme == "https" else "ws") + "://" + rest + "/api/websocket"
        self._token = token
        self._timeout = timeout

    def call(self, messages: List[Dict]) -> List[Any]:
        """
        Signs in, sends each message, and closes.

        Args:
            messages: What to ask, e.g. [{"type": "config/area_registry/list"}]. Each is
                given the next id, from 1.

        Returns:
            The result of each answer, in order.

        Raises:
            HomeSocketError: With no network (nothing is tried), a refused sign-in, an
                answer which wasn't a success, a timeout, or the library missing or failing.
        """
        if not network.online():
            raise HomeSocketError("there's no network")
        try:
            import websocket
        except ImportError as error:
            raise HomeSocketError(f"websocket-client isn't installed: {error}")

        deadline = time.monotonic() + self._timeout
        try:
            connection = websocket.create_connection(self.url, timeout=self._timeout)
        except websocket.WebSocketTimeoutException as error:
            raise HomeSocketError(f"timed out connecting: {error}")
        except (websocket.WebSocketException, OSError) as error:
            raise HomeSocketError(f"couldn't connect: {error}")

        # An answer trickling in is bounded by no socket timeout, which only limits each
        # read - so at the deadline the connection is aborted, which wakes a blocked read.
        cut_off = None
        try:
            cut_off = threading.Timer(max(deadline - time.monotonic(), 0), self._abort, args=(connection,))
            cut_off.daemon = True
            cut_off.start()
            return self._converse(connection, messages, deadline)
        except websocket.WebSocketTimeoutException as error:
            raise HomeSocketError(f"timed out: {error}")
        except (websocket.WebSocketException, OSError, ValueError, RuntimeError) as error:
            if time.monotonic() >= deadline:
                raise HomeSocketError(f"timed out: {error}")
            raise HomeSocketError(f"failed: {error}")
        finally:
            if cut_off is not None:
                cut_off.cancel()
            try:
                # Without waiting for Home Assistant's close frame, which takes up to 3 seconds.
                connection.close(timeout=0)
            except Exception:
                pass

    @staticmethod
    def _abort(connection):
        try:
            connection.abort()
        except Exception:
            pass

    @staticmethod
    def _receive(connection, deadline: float) -> Dict:
        """
        The next message, waiting no longer than the call's deadline has left.

        Raises:
            HomeSocketError: At the deadline, or for a frame which isn't one message.
        """
        left = deadline - time.monotonic()
        if left <= 0:
            raise HomeSocketError("timed out")
        if hasattr(connection, "settimeout"):
            connection.settimeout(left)
        message = json.loads(connection.recv())
        # Home Assistant only sends a list of messages to a client which asks for that,
        # which Nova doesn't - so anything but one message is a bad answer, said as one
        # rather than left to fail as an AttributeError further on.
        if not isinstance(message, dict):
            raise HomeSocketError(f"expected a message, got {type(message).__name__}")
        return message

    def _converse(self, connection, messages: List[Dict], deadline: float) -> List[Any]:
        """Does the talking of call() on a connection, which call() then closes."""
        first = self._receive(connection, deadline)
        if first.get("type") != "auth_required":
            raise HomeSocketError(f"expected auth_required, got {first.get('type')}")
        connection.send(json.dumps({"type": "auth", "access_token": self._token}))
        answer = self._receive(connection, deadline)
        if answer.get("type") != "auth_ok":
            raise HomeSocketError(f"sign-in refused ({answer.get('type')})")

        results = []
        for number, message in enumerate(messages, start=1):
            connection.send(json.dumps({**message, "id": number}))
            while True:
                answer = self._receive(connection, deadline)
                # Anything else, such as an event, isn't the answer to this.
                if answer.get("type") == "result" and answer.get("id") == number:
                    break
            if not answer.get("success"):
                error = answer.get("error")
                raise HomeSocketError(f"{message.get('type')} failed: {error}",
                                      code=error.get("code") if isinstance(error, dict) else None)
            results.append(answer.get("result"))
        return results


def _letters(name: Optional[str]) -> str:
    """A name lower-cased with everything but letters taken out - "Monitor Sleep" is "monitorsleep"."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


@dataclass(frozen=True)
class Entry:
    """One entity in the registry."""
    entity_id: str
    platform: str
    device_id: Optional[str]
    # Its own area, else its device's.
    area_id: Optional[str]
    unique_id: str
    original_name: Optional[str]
    aliases: Tuple[str, ...]
    # Whether it's exposed to Assist.
    exposed: bool
    # The registry's own id for it, e.g. "4b1e...", which a device action gives in place of
    # its entity id - "" if it isn't known.
    registry_id: str = ""


def button_kind(entry: Entry) -> str:
    """
    What a HASS.Agent button does.

    Args:
        entry: The button.

    Returns:
        "shutdown", "restart", "sleep", "monitorsleep", "lock" or "other" - from its
        original name, else the end of its unique id.
    """
    last = entry.unique_id.rsplit("_", 1)[-1] if entry.unique_id else ""
    for name in (entry.original_name, last):
        kind = KIND_NAMES.get(_letters(name), _letters(name))
        if kind in PC_KINDS:
            return kind
    return "other"


@dataclass(frozen=True)
class Snapshot:
    """Home Assistant's registries as they were at read_at."""
    entries: Dict[str, Entry]
    # Device id to its "name" (the user's own, if they gave one), "maker", "model" and "area_id".
    devices: Dict[str, Dict]
    # Area id to its name.
    areas: Dict[str, str]
    read_at: float

    def platform(self, entity_id: str) -> Optional[str]:
        """The integration the entity is from, e.g. "mqtt", or None if it isn't known."""
        entry = self.entries.get(entity_id)
        return entry.platform if entry else None

    def aliases(self, entity_id: str) -> Tuple[str, ...]:
        """The other names the user gave the entity."""
        entry = self.entries.get(entity_id)
        return entry.aliases if entry else ()

    def exposed(self, entity_id: str) -> bool:
        """Whether the entity is exposed to Assist - False if it isn't known."""
        entry = self.entries.get(entity_id)
        return entry.exposed if entry else False

    def area_name(self, entity_id: str) -> Optional[str]:
        """The name of the area the entity is in, or None."""
        entry = self.entries.get(entity_id)
        return self.areas.get(entry.area_id) if entry and entry.area_id else None

    def entity_for_registry_id(self, registry_id: str) -> Optional[str]:
        """The entity id of the entity the registry gives this id, in any case, or None if it gives none."""
        return self._by_registry_id.get(registry_id.lower())

    @cached_property
    def _by_registry_id(self) -> Dict[str, str]:
        return {entry.registry_id.lower(): entry.entity_id for entry in self.entries.values() if entry.registry_id}

    def on_device(self, device_id: str) -> List[str]:
        """The entity ids of the device's entities."""
        return [entry.entity_id for entry in self.entries.values() if entry.device_id == device_id]

    def device_name(self, device_id: str) -> Optional[str]:
        """The device's name - the user's own, if they gave one - or None if it isn't known."""
        return (self.devices.get(device_id) or {}).get("name")

    def wake_buttons(self) -> List[str]:
        """The entity ids of the Wake-on-LAN integration's buttons, each of which wakes one machine."""
        return [entry.entity_id for entry in self.entries.values()
                if entry.platform == "wake_on_lan" and entry.entity_id.startswith("button.")]

    @cached_property
    def hass_agent_commands(self) -> Dict[str, Tuple[str, str]]:
        """
        Each entity of a HASS.Agent device, by entity id - its buttons, and the switches,
        lights and the like a command can be made as instead, and its sensors. Worked out
        once a snapshot, which is never changed, as the guard looks it up on the voice
        path for every button and switch there is.

        Returns:
            {entity id: (device id, kind)}, the kind as button_kind gives it.
        """
        agents = {device_id for device_id, device in self.devices.items()
                  if (device.get("maker") or "").startswith(HASS_AGENT_MAKER)}
        return {entry.entity_id: (entry.device_id, button_kind(entry)) for entry in self.entries.values()
                if entry.device_id in agents}

    def hass_agent_buttons(self) -> Dict[str, Tuple[str, str]]:
        """
        Each button of a HASS.Agent device, by entity id - those of hass_agent_commands.

        Returns:
            {entity id: (device id, kind)}, the kind as button_kind gives it.
        """
        return {entity_id: found for entity_id, found in self.hass_agent_commands.items()
                if entity_id.startswith("button.")}


class HomeRegistry:
    """Keeps a Snapshot of Home Assistant's registries, read again every few minutes."""

    def __init__(self, socket: HomeSocket, every: float = 300.0, retry: float = 30.0):
        """
        Args:
            socket: How to reach Home Assistant.
            every: Seconds between reads.
            retry: Seconds before trying again after one fails.
        """
        self._socket = socket
        self._every = every
        self._retry = retry
        self._snapshot: Optional[Snapshot] = None
        self._failing = False
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def read(self) -> Snapshot:
        """
        Reads everything, once.

        Returns:
            The snapshot.

        Raises:
            HomeSocketError: If Home Assistant couldn't be asked.
            ValueError, KeyError, TypeError: If an answer was the wrong shape.
        """
        registry, exposed, devices, areas = self._socket.call([
            {"type": "config/entity_registry/list"},
            {"type": "homeassistant/expose_entity/list"},
            {"type": "config/device_registry/list"},
            {"type": "config/area_registry/list"},
        ])
        # The list has no aliases, so each entity is asked for in full.
        full = {}
        if registry:
            full, = self._socket.call([{"type": "config/entity_registry/get_entries",
                                        "entity_ids": [item["entity_id"] for item in registry]}])
        exposed = exposed["exposed_entities"]

        device_areas = {}
        device_info = {}
        for device in devices:
            device_areas[device["id"]] = device.get("area_id")
            device_info[device["id"]] = {"name": device.get("name_by_user") or device.get("name"),
                                         "maker": device.get("manufacturer"), "model": device.get("model"),
                                         "area_id": device.get("area_id")}
        entries = {}
        for item in registry:
            entity_id = item["entity_id"]
            aliases = ((full or {}).get(entity_id) or {}).get("aliases") or []
            entries[entity_id] = Entry(
                entity_id=entity_id, platform=item.get("platform"), device_id=item.get("device_id"),
                area_id=item.get("area_id") or device_areas.get(item.get("device_id")),
                unique_id=str(item.get("unique_id") or ""), original_name=item.get("original_name"),
                aliases=tuple(alias for alias in aliases if alias is not None),
                exposed=bool((exposed.get(entity_id) or {}).get("conversation")),
                registry_id=str(item.get("id") or ""))
        return Snapshot(entries=entries, devices=device_info,
                        areas={area["area_id"]: area["name"] for area in areas}, read_at=time.time())

    def refresh(self) -> bool:
        """
        Reads once and keeps the snapshot - or drops it, if that fails.

        Returns:
            Whether it worked. A failure of any kind is logged the first time only, until a read works.
        """
        try:
            self._snapshot = self.read()
        except Exception as error:
            # Any failure drops the snapshot: the guard must fail shut, never go by one
            # which can't be refreshed. An answer of a shape nobody foresaw is one too, and
            # mustn't end the thread - so it's logged with its traceback, the first time.
            self._snapshot = None
            if not self._failing:
                known = isinstance(error, (HomeSocketError, ValueError, KeyError, TypeError))
                logger.warning(f"Couldn't read Home Assistant's registry: {error}",
                               exc_info=None if known else True)
            self._failing = True
            return False
        if self._failing:
            logger.info("Home Assistant's registry is being read again")
        self._failing = False
        return True

    def snapshot(self) -> Optional[Snapshot]:
        """The latest snapshot - None until a read works, and while the latest one is failing."""
        return self._snapshot

    def start(self):
        """Starts the daemon thread which reads straight away, then again every few minutes."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ha-registry")
        self._thread.start()

    def stop(self):
        """Stops the thread, waiting briefly for a read already under way."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        while not self._stopping.is_set():
            worked = self.refresh()
            self._stopping.wait(self._every if worked else self._retry)
