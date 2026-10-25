"""
Home Assistant Module.

Works the lights, and reads the calendar, through Home Assistant.

Nova signs in as a Home Assistant user of its own, with a long-lived access token,
and asks for things in plain words through Home Assistant's own Assist - "turn off
the kitchen lights" - rather than naming devices itself. Assist only knows about
the devices exposed to it (Settings > Voice assistants > Expose), so that list is
what Assist can reach, enforced by Home Assistant rather than by anything Claude is
told.

Some things are never passed on to Assist, by what Home Assistant says they are and
under any name it knows them by - their name, the words of their entity id, or an
other name from the registry (see ha_registry.py):
- every lock, valve and alarm panel, and every cover which is a door, garage door,
  gate or window (see GUARDED) - Assist's own "turn off" unlocks a lock;
- everything of the Proxmox integration, so "turn on PVE2 shut down" is refused even
  if it's been exposed;
- HASS.Agent's buttons on the PC, bar sleep, monitor sleep and lock - through Assist,
  shutting the PC down or restarting it would be done with no yes.
Nor are the words for unlocking or opening anything (see SENSITIVE).

A request naming one of those is refused whole, even when a harmless thing's name
holds the guarded one's, as Assist reads a room before a name: "turn off the bedroom
front door" can be a lock called "front door" in the bedroom. So a light sentence can
be refused for its light's name - "turn on the front door light", with a lock called
"front door". That's then done by Nova's own lights instead (see switch_lights), which
only ever work lights, rather than refused for a lock nobody asked about.

Nor is anything passed on before Assist's own sentence debugging has said what it would
act on (see _assist_targets). A request is refused if any of that is guarded, and isn't
sent at all if Assist would act on nothing the guard can see. Without the registry or
the debugging, only Nova's own light sentences go to Assist, with the checks of what
they name - decision 2's wider rule is off until both answer again.

Nor is a script or scene run - by Assist, by Claude (see run_script) or from the settings
page - before what it does has been checked: a script's configuration walked whole, and a
scene's members, for anything above (see works_guarded). One whose contents can't be seen
is refused too, and so is every one without the registry, which has the key to each
script's configuration. Claude only runs those exposed to Assist, as Assist would.

What Assist doesn't understand - "dim the lights" - goes to Claude, which works the
lights through Home Assistant's own services instead, by light or by room, with a
brightness, colour or warmth. Those services are only ever given lights, by entity
id or by area - but any light, exposed to Assist or not, as exposing only limits
what Assist does.

The settings page names devices itself, as there's nothing to mishear: it lists the
lights, to switch and dim, and the scripts and scenes, to run - but nothing else,
and still no script or scene whose name says it locks or opens anything, nor any it
wouldn't run.

Every service Nova calls goes through _call_service, which only calls those in
SERVICES, so nothing else in Nova can reach a lock, a cover or Home Assistant itself.
Nova's token is an administrator's, which could do anything in Home Assistant, so
this is the only guard on what Nova's own calls reach.

Knowing what's in which room - the room Nova's in, and the lights' - and the batteries
take an administrator's token, as Home Assistant only answers templates for one. With
an ordinary user's, Nova goes through Assist alone, the settings page has the lights
without their rooms, and Nova can't tell the batteries - nor see what a script does, so
it runs none.

The calendar is whichever calendars Home Assistant has, such as a Google Calendar
added through its Google Calendar integration.
"""

import json
import logging
import math
import re
from datetime import date, datetime, time as day_time, timedelta
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import requests

from voice_assistant import network
from voice_assistant.clock import spoken_clock_time
from voice_assistant.ha_registry import PC_SAFE, HomeRegistry, HomeSocket, HomeSocketError, Snapshot
from voice_assistant.logging_setup import TRANSCRIPT

# Logger setup
logger = logging.getLogger(__name__)

# Seconds to wait for Home Assistant before giving up. It's on the local network,
# so anything slower than this means it's down.
REQUEST_TIMEOUT = 5

# What's said when Home Assistant can't be asked - by the lights and calendar, and by
# the battery questions, so it's the same sentence wherever it comes up.
CANT_REACH = "I can't reach Home Assistant right now."

# Home Assistant's own built-in Assist, which matches requests against the exposed
# devices by itself. Named, rather than left to the default, in case the default
# has been set to a language model - which would be slower, and less predictable.
DEFAULT_AGENT = "conversation.home_assistant"

# Requests which could let someone in are refused by voice however well they were
# heard - a misheard word, or a TV in the background, mustn't be able to unlock the
# front door. Checked here, in code, not left to Claude. "Turn on the front door
# light" is still fine; "open the front door" isn't.
SENSITIVE = re.compile(r"\b(?:un)?lock|\bgarage\b|\bgates?\b|\bdisarm|\barm the\b|\balarm system\b"
                       r"|\b(?:open|close|shut)\b.*\bdoors?\b")

# What's said when a request is refused, by its words or by what it names.
REFUSED = "I don't lock, unlock or open anything by voice."

# Locking the PC - HASS.Agent's button.windows_pc_lock - is the one exception to the
# words above: it only makes things safer (the user's call), and it's a button, not a
# lock, so only the word would refuse it. "Unlock my PC" is still refused. It excuses
# words alone - SENSITIVE's and the lock kind's - never a guarded thing's name, so a
# shutdown button the user has called "PC lock" is still refused by it.
PC_LOCK = re.compile(r"\block (?:my |the )?(?:pc|computer)\b|\b(?:pc|computer) lock\b")

# What's never worked by voice, by what Home Assistant says it is rather than what it's
# called: every lock, valve and alarm panel, and any cover which is a door, garage door,
# gate or window - "doors, garages, gates and windows never", the user's call. Blinds,
# curtains, shades and other covers may open and close. Each comes with the words
# Assist takes as all of them - "open the windows" opens every window in Nova's room.
GUARDED = {"lock": ("lock", "locks", "door", "doors"), "valve": ("valve", "valves"),
           "alarm_control_panel": ("alarm", "alarms", "security")}
GUARDED_COVERS = {"door": ("door", "doors"), "garage": ("garage", "garages"), "gate": ("gate", "gates"),
                  "window": ("window", "windows")}
# The Proxmox integration's platform in the registry. Everything of it is guarded, as
# its buttons and switches shut down, restart and stop the machines Nova itself runs on.
PROXMOX = "proxmoxve"

# Why a script or scene isn't run: it works something guarded, or what it does can't be
# seen. They're told apart in what's said, as "I don't lock, unlock or open anything" is
# no answer for a script whose contents Home Assistant won't show.
WORKS_GUARDED = "works something guarded"
UNSEEN = "can't be seen"

# The domains whose actions, given a whole area, floor or label - or "all", for every
# entity - could reach something guarded there: homeassistant's reach anything, a door is a
# cover, the PC's shutdown and Proxmox's are buttons, Proxmox's switches stop the machines
# Nova runs on, and the scripts and scenes in an area aren't known, so can't be walked.
# light.turn_off for an area only ever reaches the lights.
AREA_WIDE = ("homeassistant", "cover", "button", "switch", "script", "scene", *GUARDED)
AREA_TARGETS = ("area_id", "floor_id", "label_id")

# What a step of a script calls its action, old and new - Home Assistant still takes all three.
ACTION_KEYS = ("action", "service", "service_template")

# Actions which hand what they're given on to something the guard doesn't stand in front of,
# so what they'd do can't be checked - by domain or in full. Any of the conversation domain's
# gives a sentence to Assist inside Home Assistant, as conversation.process with "unlock the
# front door" does, and mqtt.publish could press HASS.Agent's commands by their topic.
PASSED_ON = ("conversation", "mqtt.publish")

# The script domain's own actions. Every other script.<name> is a script's own action, named
# by its key - see _walk.
SCRIPT_SERVICES = ("turn_on", "turn_off", "toggle", "reload")

# What could be an entity id in a script's configuration, e.g. "lock.front_door" - or an
# action's name, which reads as one: "lock.unlock" is the lock domain's, so guarded too.
ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")

# Every service Nova calls, by domain, and nothing else - no lock, cover, valve, alarm
# panel, hassio or homeassistant service. Each is only ever given entities in its own
# domain, and only the lights' an area as well, which light.turn_on and light.turn_off
# take as just the lights in it. A cover service added here would need each cover
# checked with _guarded too, as a door's as much a cover as a blind is.
SERVICES = {"light": ("turn_on", "turn_off"), "script": ("turn_on",), "scene": ("turn_on",)}
BY_AREA = ("light",)

# How far "dim" and "brighten" move a light, in points of brightness. 30 is steps that
# can be told apart across a room - 10 isn't - and a light at 40% takes two to go dark.
DIM_STEP = 30
# The dimmest "dim" leaves a light at. It never switches one off: a dim lamp is still
# there to find in the dark, and an off one is a light the user asked to switch off.
DIM_FLOOR = 5
# What lights_called takes "lights" or "lamp" on its own to mean: those in Nova's room.
GENERIC_LIGHT_WORDS = ("light", "lights", "lamp", "lamps")

# What Assist's sentence debugging says it would do with a sentence, without doing it:
# whether it matched, the intent, e.g. "HassTurnOff", and every entity it would act on.
AssistMatch = NamedTuple("AssistMatch", [("matched", bool), ("intent", Optional[str]), ("targets", List[str])])

# What's said of a request which isn't given to Assist, as what Assist would act on
# can't be told - nor, then, whether it may.
DIDNT_UNDERSTAND = "Home Assistant didn't understand that."

# The error codes Assist gives when it matched nothing at all - no sentence it knows,
# or none of the devices exposed to it - as opposed to understanding and failing, which
# is "failed_to_handle". Only the first kind is worth trying Claude on: "dim the
# lights" has no sentence of Assist's own, but Claude can set a brightness.
NOT_UNDERSTOOD = ("no_intent_match", "no_valid_targets")

# Finds Nova's device by the identifier it registered with over MQTT, and renders its
# id and its area's name, e.g. "da94...|Bedroom" - or just "|" if there's no such device.
# A device has no entity id of its own, so this walks the devices of every entity there
# is. Asked for by the identifier, never by an entity name, since the user can rename
# entities, or move the device to another area, in Home Assistant whenever they like.
FIND_DEVICE_TEMPLATE = """
{%- set found = namespace(device='', area='') -%}
{%- for device in states | map(attribute='entity_id') | map('device_id') | select('string') | unique -%}
  {%- if ('mqtt', 'IDENTIFIER') in (device_attr(device, 'identifiers') or []) -%}
    {%- set found.device = device -%}
    {%- set found.area = area_name(device) or '' -%}
  {%- endif -%}
{%- endfor -%}
{{ found.device }}|{{ found.area }}
"""

# Renders every light as JSON in the shape of /api/states, plus the name of its area,
# which that doesn't have - e.g. [{"entity_id": "light.lamp", "area": "Bedroom", "state":
# "on", "attributes": {"brightness": 102, ...}}]. All the lights at once, as asking for
# each one's area would be a request each. Attributes a light hasn't got, such as the
# brightness of one that's off, come out as null. A light with no area of its own is in
# its device's, which area_name works out.
LIGHTS_TEMPLATE = """
{%- set found = namespace(lights=[]) -%}
{%- for light in states.light -%}
  {%- set attributes = light.attributes -%}
  {%- set found.lights = found.lights + [{
    'entity_id': light.entity_id,
    'area': area_name(light.entity_id),
    'state': light.state,
    'attributes': {
      'friendly_name': attributes.get('friendly_name'),
      'brightness': attributes.get('brightness'),
      'supported_color_modes': (attributes.get('supported_color_modes') or []) | sort,
      'color_mode': attributes.get('color_mode'),
      'hs_color': attributes.get('hs_color'),
      'color_temp_kelvin': attributes.get('color_temp_kelvin'),
      'min_color_temp_kelvin': attributes.get('min_color_temp_kelvin'),
      'max_color_temp_kelvin': attributes.get('max_color_temp_kelvin')
    }
  }] -%}
{%- endfor -%}
{{ found.lights | tojson }}
"""

# Renders every battery sensor as JSON, with what its device says of it - e.g.
# [{"entity_id": "sensor.s25_ultra_battery_level", "state": "46", "unit": "%", "changed":
# 1761200000.0, "friendly_name": "S25 Ultra Battery level", "device": "abc123", "device_name":
# "SM-S938B", "device_name_by_user": "s25-ultra", "maker": "samsung", "model": "SM-S938B",
# "charging": "discharging", "app": true}]. Only what Home Assistant has is rendered: whether the state is
# a level, and in percent, is left to batteries(). A sensor made by a template has no device,
# so those come out as null. The charging entity is the device's binary_sensor of device class
# battery_charging if it has one, else its sensor whose id ends in battery_state - the phone has
# the second, saying "charging", "discharging" or "full".
BATTERIES_TEMPLATE = """
{%- set found = namespace(batteries=[]) -%}
{%- for battery in states.sensor | selectattr('attributes.device_class', 'eq', 'battery') -%}
  {%- set device = device_id(battery.entity_id) -%}
  {%- set charging = namespace(state=none, fallback=none) -%}
  {%- if device -%}
    {%- for entity in device_entities(device) -%}
      {%- if is_state_attr(entity, 'device_class', 'battery_charging') and entity.startswith('binary_sensor.') -%}
        {%- set charging.state = states(entity) -%}
      {%- elif entity.startswith('sensor.') and entity.endswith('battery_state') -%}
        {%- set charging.fallback = states(entity) -%}
      {%- endif -%}
    {%- endfor -%}
  {%- endif -%}
  {%- set found.batteries = found.batteries + [{
    'entity_id': battery.entity_id,
    'state': battery.state,
    'unit': battery.attributes.get('unit_of_measurement'),
    'changed': battery.last_reported | as_timestamp,
    'friendly_name': battery.attributes.get('friendly_name'),
    'device': device,
    'device_name': device_attr(device, 'name') if device else none,
    'device_name_by_user': device_attr(device, 'name_by_user') if device else none,
    'maker': device_attr(device, 'manufacturer') if device else none,
    'model': device_attr(device, 'model') if device else none,
    'charging': charging.state if charging.state is not none else charging.fallback,
    'app': battery.entity_id in integration_entities('mobile_app')
  }] -%}
{%- endfor -%}
{{ found.batteries | tojson }}
"""

# Renders every person as JSON - e.g. [{"entity_id": "person.nathaniel", "name": "Nathaniel",
# "state": "home", "devices": ["abc123"]}] - with the device ids of the trackers which say
# where they are.
# A tracker with no device, such as one made in YAML, has none to name and is left out.
PEOPLE_TEMPLATE = """
{%- set found = namespace(people=[]) -%}
{%- for person in states.person -%}
  {%- set devices = namespace(ids=[]) -%}
  {%- for tracker in person.attributes.get('device_trackers') or [] -%}
    {%- set device = device_id(tracker) -%}
    {%- if device -%}
      {%- set devices.ids = devices.ids + [device] -%}
    {%- endif -%}
  {%- endfor -%}
  {%- set found.people = found.people + [{
    'entity_id': person.entity_id,
    'name': person.name,
    'state': person.state,
    'devices': devices.ids
  }] -%}
{%- endfor -%}
{{ found.people | tojson }}
"""

# What the settings page may switch, and run, by entity id. A light can't let anyone
# in, whatever it's called; a script or scene could do anything, so its name is
# checked against SENSITIVE as well, and what it does with works_guarded.
LIGHT = re.compile(r"light\.[a-z0-9_]+")
RUNNABLE = re.compile(r"(script|scene)\.[a-z0-9_]+")

# Most events said for one day before the rest are only counted.
MOST_EVENTS_SAID = 5

# From this many days, what's on is given as an overview - which days there's uni
# or work, and the rest by name - rather than every time of every day. A week said
# in full, as "uni from 11:30 AM until 1 PM and from 2:30 PM until 4:30 PM" a day
# at a time, took 52 seconds; as an overview, about 18. A fortnight, read out
# whole, once took three and a half minutes. "What about Friday?" still gets that
# day in full.
OVERVIEW_FROM_DAYS = 3

# Most things other than the activities named in an overview.
MOST_OTHERS_IN_OVERVIEW = 5

# How far ahead to look for one thing on the calendar - "when's my MOT?" - and the
# most matches said.
SEARCH_DAYS = 90
MOST_FOUND = 3


class NotAllowed(ValueError):
    """A service call SERVICES doesn't allow, which nothing in Nova should ever make."""


class HomeAssistant:
    """One Home Assistant, reached over its REST API."""

    def __init__(self, url: str, token: str, agent: str = DEFAULT_AGENT, calendars: Optional[List[str]] = None,
                 activities: Optional[Dict[str, List[str]]] = None, registry: Optional[HomeRegistry] = None):
        """
        Args:
            url: Its address, e.g. "http://homeassistant.local:8123".
            token: A long-lived access token for Nova's own Home Assistant user.
            agent: The Assist agent requests go to.
            calendars: The calendars to read, e.g. ["calendar.personal"], or None
                for all of them.
            activities: The regular things in the user's life and what they're called
                on the calendar - {"work": ["Shift"], "uni": ["CSC"]} - for "what time
                do I have work?". An event with a word in its name starting with one of
                them is that activity. Just work, called "work", if not given.
            registry: Where to look up what Home Assistant's entities are, or None -
                see ha_registry.py.
        """
        self.url = url.rstrip("/")
        # Its WebSocket, for what the REST API can't say - see ha_registry.py.
        self.socket = HomeSocket(self.url, token)
        self.registry = registry
        self.agent = agent
        self.calendars = list(calendars or [])
        self.activities = {name.lower(): [called.lower() for called in calls]
                           for name, calls in (activities or {"work": ["work"]}).items()}
        # Which device in Home Assistant Nova is, and the name of the area it's in, e.g.
        # "Bedroom" - found by find_device, and None until then, or if it isn't there.
        self.device_id: Optional[str] = None
        self.area: Optional[str] = None
        # Whether looking for it last failed, or found no such device - see find_device.
        self._lookup_failing = False
        self._device_missing = False
        # Whether Home Assistant last turned a template down - see _template.
        self._templates_refused = False
        # Why decision 2's wider rule is off, or None while it's on - see _wider_rule.
        self._wider_off: Optional[str] = None
        # The scripts whose configuration last couldn't be read - see _script_config - and
        # those found too deep to walk, each said once.
        self._unreadable: Set[str] = set()
        self._too_deep: Set[str] = set()
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    @classmethod
    def from_config(cls, config, token: Optional[str],
                    registry: Optional[HomeRegistry] = None) -> Optional["HomeAssistant"]:
        """
        The Home Assistant config.yaml points at, or None if it isn't set up.

        Args:
            config: Configs loaded from config.yaml - home_assistant.url and friends.
            token: HA_TOKEN from .env.
            registry: The brain's HomeRegistry, shared by every HomeAssistant.

        Returns:
            A HomeAssistant, or None without both an address and a token.
        """
        settings = config.section("home_assistant") if config else {}
        url = (settings.get("url") or "").strip()
        if not url:
            return None
        if not token:
            logger.warning("home_assistant.url is set but HA_TOKEN isn't in .env, so Home Assistant is off")
            return None
        logger.info(f"Home Assistant at {url}")
        return cls(url, token, calendars=settings.get("calendars"), activities=settings.get("activities"),
                   registry=registry)

    def _snapshot(self) -> Optional[Snapshot]:
        """The latest read of Home Assistant's registry, or None if there's none to go by."""
        return self.registry.snapshot() if self.registry else None

    def _request(self, method: str, path: str, timeout: Optional[float] = None, **kwargs) -> requests.Response:
        # Home Assistant is on the local network, so it's still asked with only the
        # internet down - but with no network at all, there's no reaching it. Found
        # with the Pi's cable out: "turn on the lamp" waited 20 seconds for its name
        # to fail to be looked up before saying so.
        if not network.online():
            raise requests.ConnectionError("No network connection")
        timeout = REQUEST_TIMEOUT if timeout is None else timeout
        response = self._session.request(method, self.url + path, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response

    def _template(self, template: str, timeout: Optional[float] = None) -> str:
        """
        Has Home Assistant render a template - the only way to ask it what's in which
        room, for Nova's own and the lights'.

        Args:
            template: The template, in Home Assistant's Jinja.
            timeout: Seconds to wait for Home Assistant, or None for REQUEST_TIMEOUT.

        Returns:
            What it rendered, as plain text.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked, or turned
                the template down, as it does for a token which isn't an administrator's.
        """
        # Nova's user needn't be an administrator, and templates are asked for every
        # half a minute while Nova's device is looked for, and with every request to
        # Claude - so being turned down is said once, until a template's rendered again.
        try:
            text = self._request("POST", "/api/template", timeout=timeout, json={"template": template}).text
        except requests.HTTPError as e:
            if _refused(e):
                if not self._templates_refused:
                    logger.warning(f"Home Assistant won't tell Nova what's in which room ({e.response.status_code}) "
                                   f"- HA_TOKEN needs to be an administrator's for Nova's room, Claude's lights "
                                   f"and the batteries, so until then Nova goes through Assist alone")
                self._templates_refused = True
            raise
        if self._templates_refused:
            logger.info("Home Assistant tells Nova what's in which room now")
        self._templates_refused = False
        return text

    @staticmethod
    def _failure_message(error: Exception) -> str:
        """What to say when Home Assistant couldn't be asked, depending on why."""
        if isinstance(error, NotAllowed):
            return "I can't do that."
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 401:
            return "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return CANT_REACH
        return "Home Assistant had a problem with that."

    def _states(self) -> List[Dict]:
        """
        Every entity's state, as /api/states has them - read afresh each time, so a lock
        added a minute ago is refused too.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
            ValueError: If it didn't answer with a list of states, each with its attributes
                a dict. Those aren't taken for none: a door cover would then have no device
                class, and pass for a blind.
        """
        states = self._request("GET", "/api/states").json()
        if not isinstance(states, list) or not all(
                isinstance(state, dict) and isinstance(state.get("attributes", {}), dict) for state in states):
            raise ValueError("Home Assistant's states aren't a list of states")
        return states

    def command(self, request: str, wider: bool = False, only: Optional[Sequence[str]] = None) -> Dict:
        """
        Asks Home Assistant's Assist to do something, in plain words - once Assist has
        said what that would act on, and none of it is guarded.

        Decision 2's wider rule - Assist being given any of its own sentences, not only
        Nova's light sentences - needs both the registry and Assist's sentence
        debugging. Without either, only light sentences go to Assist, with the checks
        of what they name, until both answer again: it fails shut, not open.

        Args:
            request: e.g. "turn off the kitchen lights".
            wider: Whether it's any sentence of Assist's, which is only sent on the wider
                rule - rather than one of Nova's own light sentences.
            only: The intents Assist may act on it with, e.g. ("HassListAddItem",), or
                None for any.

        Returns:
            Dict with 'success' and 'message' - Assist's own reply - and 'chime' for
            something done, since the lights changing speaks for itself. 'not_understood'
            is set too when Assist matched nothing, as opposed to failing at something
            it did understand - and when it isn't asked to act, as what it would act on
            can't be told. 'refused' is set when Nova wouldn't ask Assist at all, by the
            words, by what they name or by what Assist would act on - which includes a
            script or scene, named or acted on, that works_guarded refuses.
        """
        refused = {"success": False, "message": REFUSED, "refused": True}
        not_understood = {"success": False, "message": DIDNT_UNDERSTAND, "not_understood": True}
        if _sensitive_words(request):
            logger.warning(f"Refused a sensitive Home Assistant request: '{request}'", extra=TRANSCRIPT)
            return refused

        asked = {"text": request, "language": "en", "agent_id": self.agent}
        if self.device_id:
            # Assist works out "the lights" from the area the device it's talking to is
            # in, so Nova's room is what's switched when no room is named.
            asked["device_id"] = self.device_id

        try:
            # Nothing goes to Assist if what's there can't be read.
            states = self._states()
            snapshot = self._snapshot()
            if snapshot is None:
                self._wider_rule("there's no read of Home Assistant's registry to go by")
                if wider:
                    logger.info(f"Not sent to Assist, which is only given light sentences just now: '{request}'",
                                extra=TRANSCRIPT)
                    return not_understood
            if _names_guarded(request, states, snapshot):
                logger.warning(f"Refused a Home Assistant request naming a lock, door or the like: '{request}'",
                               extra=TRANSCRIPT)
                return refused

            # Told the same device as Assist, which find_device can change meanwhile.
            match, unanswered = self._assist_targets(request, asked.get("device_id"))
            if snapshot is not None:
                self._wider_rule(unanswered)
            if match is None and wider:
                logger.info(f"Not sent to Assist, which is only given light sentences just now: '{request}'",
                            extra=TRANSCRIPT)
                return not_understood
            if match is not None:
                # A match with no targets acts on what the guard can't see - a script
                # which isn't exposed, or an automation's own sentence trigger. A light
                # sentence never has none: every light it means is a target, by its
                # room or by its name.
                if not match.matched or (only is not None and match.intent not in only) or not match.targets:
                    what = match.intent or ("something it can't show" if match.matched else "nothing")
                    logger.info(f"Not sent to Assist, which reads it as {what} on {match.targets}: '{request}'",
                                extra=TRANSCRIPT)
                    return not_understood
                by_id = {state.get("entity_id"): state for state in states}
                barred = [target for target in match.targets if _target_refused(target, by_id.get(target), snapshot)]
                if barred:
                    logger.warning(f"Refused a Home Assistant request Assist would act on {barred} with: '{request}'",
                                   extra=TRANSCRIPT)
                    return refused
            # A script or scene is only run once what it does has been checked - one Assist
            # would act on, and one the request names, as a light sentence can while the
            # debugging isn't answering: "turn on the porch light", with a script called
            # "Porch light" which unlocks the front door.
            runs = [target for target in (match.targets if match else []) if RUNNABLE.fullmatch(target)]
            runs += [entity for entity in _runnables_named(request, states, snapshot) if entity not in runs]
            if self._why_not_run(runs, states, snapshot):
                logger.warning(f"Refused a Home Assistant request running {runs}, which work something guarded or "
                               f"can't be checked: '{request}'", extra=TRANSCRIPT)
                return refused
            reply = self._request("POST", "/api/conversation/process", json=asked).json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}

        response = reply.get("response") or {}
        said = ((response.get("speech") or {}).get("plain") or {}).get("speech") or ""
        kind = response.get("response_type")
        logger.info(f"Home Assistant: {kind} - '{said}'", extra=TRANSCRIPT)
        if kind == "action_done":
            return {"success": True, "message": said or "Done.", "chime": True}
        if kind == "query_answer":
            return {"success": True, "message": said}
        failed = {"success": False, "message": said or DIDNT_UNDERSTAND}
        if kind == "error" and (response.get("data") or {}).get("code") in NOT_UNDERSTOOD:
            failed["not_understood"] = True
        return failed

    def _assist_targets(self, request: str,
                        device_id: Optional[str] = None) -> Tuple[Optional[AssistMatch], Optional[str]]:
        """
        What Assist would act on for a request, without it acting - from the WebSocket
        API's own sentence debugging, which matches a sentence as Assist would.

        Args:
            request: e.g. "turn off the TV".
            device_id: The device Assist is told it's talking to, or None - the one
                /api/conversation/process is told, so "the lights" are the same room's.

        Returns:
            (what it would do, None) once the debugging has answered: AssistMatch(True,
            the intent, the entity ids it would act on) for a match, AssistMatch(True,
            None, []) for one whose intent and targets can't be seen - an automation's
            own sentence, say - and AssistMatch(False, None, []) for none. Or (None, why
            not) if it didn't answer, or answered without a result for the sentence - and
            when requests go to an agent other than Home Assistant's own, which the
            debugging doesn't speak for.
        """
        if self.agent != DEFAULT_AGENT:
            return None, f"requests go to {self.agent}, which Assist's sentence debugging doesn't speak for"
        asked = {"type": "conversation/agent/homeassistant/debug", "sentences": [request], "language": "en"}
        if device_id:
            asked["device_id"] = device_id
        try:
            answer = self.socket.call([asked])[0]
        except HomeSocketError as error:
            return None, f"Assist's sentence debugging didn't answer ({error})"
        results = answer.get("results") if isinstance(answer, dict) else None
        if not isinstance(results, list) or not results or not (results[0] is None or isinstance(results[0], dict)):
            return None, "Assist's sentence debugging answered without a result for the sentence"

        found = results[0]
        # A sentence which fits one of Assist's but names nothing there - "pause the tv",
        # with no media player called that - is answered in full, with "match": false,
        # and can still carry targets. Only "match": true is a match.
        if found is None or found.get("match") is not True:
            return AssistMatch(False, None, []), None
        # An automation's own sentence matches with no intent or targets, as what the
        # automation does can't be told without running it.
        if found.get("source") == "trigger":
            logger.info("Assist's sentence debugging matched an automation's own sentence, which isn't sent")
            return AssistMatch(True, None, []), None
        intent = found.get("intent")
        name = intent.get("name") if isinstance(intent, dict) else None
        targets = found.get("targets")
        if not isinstance(name, str) or not name or not isinstance(targets, dict):
            logger.warning(f"Assist's sentence debugging gave a match whose intent or targets can't be read "
                           f"(intent {intent!r}, targets {type(targets).__name__}), so it isn't sent")
            return AssistMatch(True, None, []), None
        return AssistMatch(True, name, list(targets)), None

    def _wider_rule(self, off: Optional[str]):
        """
        Notes whether decision 2's wider rule is on, saying so when it goes off and when
        it's back on.

        Args:
            off: Why it's off, e.g. "there's no read of Home Assistant's registry to go
                by" - or None, for the registry and the sentence debugging both answering.
        """
        # Two requests at once can each say the same change, which does no harm.
        if off and not self._wider_off:
            logger.warning(f"The wider rule is off, as {off}: only light sentences go to Assist until it's back")
        elif not off and self._wider_off:
            logger.info("The wider rule is back on: Home Assistant's registry and Assist's sentence debugging "
                        "both answer")
        self._wider_off = off

    def find_device(self, identifier: str) -> bool:
        """
        Looks up which device in Home Assistant is Nova, and which area it's in, which
        Home Assistant decides - the user can move it between rooms there at any time.

        Args:
            identifier: What Nova registered itself under over MQTT, e.g.
                "nova_home_assistant".

        Returns:
            Whether the device was found. If Home Assistant couldn't be asked, what was
            known before is kept.
        """
        # Asked again and again while Nova runs - every half a minute until it's found,
        # see session.py - so each problem is logged once, when it starts, rather than
        # every time: Home Assistant being down, or it not having Nova's device.
        template = FIND_DEVICE_TEMPLATE.replace("IDENTIFIER", identifier)
        try:
            text = self._template(template)
        except requests.RequestException as e:
            # Turned down for want of an administrator's token, which _template has
            # said already - Home Assistant's there, so the lookup isn't failing.
            if _refused(e):
                return False
            if not self._lookup_failing:
                logger.error(f"Couldn't ask Home Assistant which device Nova is, so trying again: {e}")
            self._lookup_failing = True
            return False
        if self._lookup_failing:
            logger.info("Home Assistant's answering again")
        self._lookup_failing = False

        # Home Assistant answers with plain text, not JSON.
        device_id, _, area = text.strip().partition("|")
        if not device_id and not self._device_missing:
            logger.warning(f"Home Assistant has no device '{identifier}' for Nova yet, so Nova's room isn't "
                           f"known - it's there once Home Assistant has taken in Nova's MQTT announcement")
        self._device_missing = not device_id
        self.device_id = device_id or None
        if (area or None) != self.area:
            logger.info(f"Nova is in the {area}, as Home Assistant has it" if area
                        else "Nova isn't in a room, as Home Assistant has it")
        self.area = area or None
        return self.device_id is not None

    # For the settings page

    def overview(self) -> Dict:
        """
        The lights, and the scripts and scenes run() would run, for the settings page -
        each by its entity id and name. The lights are in the order of _in_order, with
        Nova's own room first; the scripts and scenes are in name order.

        Returns:
            Dict with 'success', and 'lights' and 'routines', or a 'message'. Each light
            is as lights() has it - in no area if Home Assistant won't say, without an
            administrator's token - and 'dimmable' as well: whether it does more than
            switch on and off.
        """
        try:
            states = self._states()
            try:
                lights = self.lights()
            except requests.HTTPError as e:
                if not _refused(e):
                    raise
                # The lights are among the states too, just without their rooms - so the
                # page still has them, as it did before Nova knew about rooms.
                lights = [_light(state) for state in states if LIGHT.fullmatch(state.get("entity_id") or "")]
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        snapshot = self._snapshot()
        # Each script's configuration is read once a draw, however many others run it.
        configs: Dict[str, Optional[Dict]] = {}
        routines = []
        for state in states:
            entity = state.get("entity_id") or ""
            name = (state.get("attributes") or {}).get("friendly_name") or entity
            # Only what run() would run, so the page never offers a button which can't work -
            # and none without the registry, which has the key to each script's contents.
            if (RUNNABLE.fullmatch(entity) and not _sensitive(entity, name)
                    and not self.works_guarded(entity, states, snapshot, configs=configs)):
                routines.append({"id": entity, "name": name, "kind": entity.split(".")[0]})
        return {"success": True,
                "lights": [{**light, "dimmable": any(mode != "onoff" for mode in light["colour_modes"])}
                           for light in self._in_order(lights)],
                "routines": sorted(routines, key=lambda routine: routine["name"].lower())}

    def lights(self, timeout: Optional[float] = None) -> List[Dict]:
        """
        Every light there is, with the room it's in and how it's set.

        Args:
            timeout: Seconds to wait for Home Assistant, or None for REQUEST_TIMEOUT.

        Returns:
            One dict for each, with "id" (e.g. "light.lamp"), "name", "area" (its name,
            e.g. "Bedroom", or None), "on", "reachable", "brightness" (percent, or None
            if it's off or can't dim), "colour_modes" (what it can do, e.g.
            ["color_temp", "hs"]), "min_kelvin" and "max_kelvin" (the warmest and
            coolest white it makes, or None) and how it's set now: "hue" (0 to 360)
            and "saturation" (percent) if it's set to a colour, or "kelvin" if it's set
            to a white. Whichever it isn't set to is None. And "aliases", the other names
            the user gave it in Home Assistant, e.g. ["main light"] - [] if it has none,
            or Nova can't read the registry.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked, or turned
                the template down - see _template.
            ValueError: If it didn't answer with the lights.
        """
        found = self._template(LIGHTS_TEMPLATE, timeout=timeout)
        snapshot = self._snapshot()
        return [_light(state, snapshot.aliases(state["entity_id"]) if snapshot else ()) for state in json.loads(found)]

    def batteries(self, timeout: Optional[float] = None) -> List[Dict]:
        """
        Every battery there is that Home Assistant can give a level for, with what its
        device is and whether it's charging.

        Args:
            timeout: Seconds to wait for Home Assistant, or None for REQUEST_TIMEOUT.

        Returns:
            One dict for each sensor of device class battery that reads in percent and
            has a level, with "id" (e.g. "sensor.s25_ultra_battery_level"), "name" (the
            name the user gave the device, else its own name, else the sensor's friendly
            name), "level" (a float), "changed" (when Home Assistant last heard the
            level, in epoch seconds, even if the level did not move), "device" (its id,
            or None), "maker" and "model" (or None), "charging" (its device's charging
            state as Home Assistant has it, e.g. "discharging", or None) and "app"
            (whether the sensor belongs to the Home Assistant app - a Mac's does too, so
            it doesn't tell a phone from anything else alone). A sensor which is
            unavailable, unknown, not a number, not in percent or outside 0 to 100 is
            left out - never taken as 0%, which would be a warning for nothing.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked, or turned
                the template down - see _template.
            ValueError: If it didn't answer with JSON. An answer of the wrong shape
                raises KeyError or TypeError - callers catch batteries.HOME_FAILURES,
                which has all three.
        """
        found = []
        for state in json.loads(self._template(BATTERIES_TEMPLATE, timeout=timeout)):
            if state["unit"] != "%":
                continue
            try:
                level = float(state["state"])
            except ValueError:
                continue
            # float() reads "nan" and "inf", which no battery means, and some integrations
            # say -1 or 255 for "don't know" - a -1 would be a warning for nothing.
            if not math.isfinite(level) or not 0 <= level <= 100:
                continue
            found.append({"id": state["entity_id"],
                          "name": state["device_name_by_user"] or state["device_name"] or state["friendly_name"]
                                  or state["entity_id"],
                          "level": level, "changed": state["changed"], "device": state["device"],
                          "maker": state["maker"], "model": state["model"], "charging": state["charging"],
                          "app": state["app"]})
        return found

    def people(self, timeout: Optional[float] = None) -> List[Dict]:
        """
        Everyone Home Assistant has as a person, and where they are.

        Args:
            timeout: Seconds to wait for Home Assistant, or None for REQUEST_TIMEOUT.

        Returns:
            One dict for each, with "id" (e.g. "person.nathaniel"), "name" (e.g.
            "Nathaniel", else the id), "state" (e.g. "home", "not_home" or the name of
            a zone) and "devices" (the ids of the devices of the trackers which tell
            where they are).

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked, or turned
                the template down - see _template.
            ValueError: If it didn't answer with JSON. An answer of the wrong shape
                raises KeyError or TypeError - callers catch batteries.HOME_FAILURES,
                which has all three.
        """
        return [{"id": person["entity_id"], "name": person["name"] or person["entity_id"],
                 "state": person["state"], "devices": person["devices"]}
                for person in json.loads(self._template(PEOPLE_TEMPLATE, timeout=timeout))]

    def _in_order(self, lights: List[Dict]) -> List[Dict]:
        """
        The lights with Nova's own room's first, then the other rooms' by name, and last
        any in no room - each room's by name. Where Nova is matters most, and the order
        is the same every time, rather than whichever Home Assistant happens to list first.
        """
        def order(light: Dict):
            area = light["area"]
            return area is None or area != self.area, area is None, (area or "").lower(), light["name"].lower()

        return sorted(lights, key=order)

    def describe_lights(self, timeout: Optional[float] = None) -> str:
        """
        The lights, and the room Nova is in, as something for Claude to read, so that
        "the lights" can mean that room's and "is the lamp on?" can be answered.

        Args:
            timeout: Seconds to wait for Home Assistant, or None for REQUEST_TIMEOUT. A
                reply which takes longer counts as the lights being unreadable.

        Returns:
            e.g. "Nova is in the Bedroom. Lights: Bedroom Light, also main light
            (light.bedroom_light, Bedroom) on at 40%, 2700K; Lamp (light.lamp, Bedroom)
            off" - or "Nova's
            room isn't known." to begin with, when it isn't. "" if the lights couldn't
            be read.
        """
        # Read with every request to Claude, so nothing here may lose the whole turn -
        # a light Home Assistant gives oddly is the lights being unreadable, as much as
        # Home Assistant being down is.
        try:
            lights = self._in_order(self.lights(timeout=timeout))
        except Exception as e:
            # Being turned down for want of an administrator's token is said once, by
            # _template, rather than with every request.
            if not _refused(e):
                logger.error(f"Couldn't read the lights ({type(e).__name__}): {e}")
            return ""
        room = f"Nova is in the {self.area}." if self.area else "Nova's room isn't known."
        if not lights:
            return f"{room} There are no lights."
        return f"{room} Lights: " + "; ".join(_light_said(light) for light in lights)

    def lights_called(self, words: str, lights: List[Dict]) -> Optional[List[Dict]]:
        """
        The lights some words mean, for "dim the main light" or "brighten the bedroom
        lights" - worked out here, as there's no Assist sentence for dimming.

        Args:
            words: What the user called them, e.g. "the main light". A leading "all",
                "the" or "my" is ignored, and so are case and punctuation.
            lights: The lights, as lights() has them.

        Returns:
            The lights named, whether by name or by another name; else, for "lights" or
            "lamp" alone, those in Nova's room - none if that isn't known; else those in
            the room named, e.g. "bedroom lights". None if the words mean nothing Nova
            knows.
        """
        said = _plain(words)
        while said.split(" ")[0] in ("all", "the", "my") and " " in said:
            said = said.split(" ", 1)[1]
        if not said:
            return None
        # Before the generic words, so that a light the user calls "Lamp" is the lamp
        # rather than every light in the room - "brighten the lamp" with a ceiling
        # light in the room too.
        named = [light for light in lights if said in [_plain(name) for name in [light["name"], *light["aliases"]]]]
        if named:
            return named
        if said in GENERIC_LIGHT_WORDS:
            return [light for light in lights if self.area and light["area"] == self.area]
        room = said.rsplit(" ", 1)[0] if said.rsplit(" ", 1)[-1] in GENERIC_LIGHT_WORDS else None
        if room:
            in_room = [light for light in lights if light["area"] and _plain(light["area"]) == room]
            return in_room or None
        return None

    def dim(self, direction: str, words: str) -> Dict:
        """
        Makes lights dimmer or brighter by a step, from where each one is now - "dim the
        lights" for a lamp at 80% and a ceiling light at 40% is 50% and 10%. Never
        switches a light off: a light at the floor stays there, and one that's off
        stays off when dimmed.

        Args:
            direction: "down" or "up".
            words: The lights, as the user called them - see lights_called.

        Returns:
            Dict with 'success' and a 'message', and a 'chime' for success when
            something changed, since the lights changing speaks for itself. A
            'not_understood' of True when Nova doesn't know the lights, so that Claude
            is asked.
        """
        try:
            lights = self._in_order(self.lights())
            found = self.lights_called(words, lights)
            if found is None or (not found and self.area):
                return {"success": False, "message": "I don't know a light called that.", "not_understood": True}
            if not found:
                return {"success": False, "message": "Which room?"}

            # Said rather than "as dim as they go", which would tell the user a lamp
            # switched off at the wall is as bright as it goes while the room is dark.
            if not any(light["reachable"] for light in found):
                return _out_of_reach(found)

            by_brightness: Dict[int, List[str]] = {}
            for light in found:
                new = _stepped(light, direction)
                if new is not None:
                    by_brightness.setdefault(new, []).append(light["id"])
            if not by_brightness:
                dimmable = [light for light in found if light["reachable"] and _dims(light)]
                if not dimmable and any(light["colour_modes"] and not _dims(light) for light in found):
                    return {"success": True, "message": "Those lights only switch on and off."}
                return {"success": True,
                        "message": "They're as dim as they go." if direction == "down" else "They're as bright as they go."}
            for brightness, ids in by_brightness.items():
                logger.info(f"Lights dimmed {direction}: {ids} to {brightness}%")
                self._call_service("light", "turn_on", {"entity_id": ids, "brightness_pct": brightness})
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": "Done.", "chime": True}

    def switch_lights(self, on: bool, words: str, brightness: Optional[int] = None) -> Dict:
        """
        Switches lights on or off, or sets how bright they are, by what the user called
        them - for a light sentence the guard wouldn't give Assist, as "turn on the front
        door light" is with a lock called "front door". Only ever lights, by entity id.

        Args:
            on: Whether to switch them on, or off. Off ignores the brightness.
            words: The lights, as the user called them - see lights_called.
            brightness: How bright, from 1 to 100 percent, or None to leave it as it is.

        Returns:
            Dict with 'success' and a 'message', and a 'chime' for success, since the
            lights changing speaks for itself. A 'not_understood' of True when Nova
            doesn't know the lights, so that Claude is asked.
        """
        try:
            found = self.lights_called(words, self._in_order(self.lights()))
            if found is None or (not found and self.area):
                return {"success": False, "message": "I don't know a light called that.", "not_understood": True}
            if not found:
                return {"success": False, "message": "Which room?"}
            # Rather than "Done." for a lamp switched off at the wall, with nothing done.
            if not any(light["reachable"] for light in found):
                return _out_of_reach(found)
            data = {"entity_id": [light["id"] for light in found]}
            if on and brightness is not None:
                data["brightness_pct"] = max(1, min(100, int(brightness)))
            service = "turn_on" if on else "turn_off"
            logger.info(f"Lights {service}: {data}")
            self._call_service("light", service, data)
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": "Done.", "chime": True}

    def set_light(self, entity_id: str, on: Optional[bool] = None, brightness: Optional[int] = None,
                  hue: Optional[float] = None, saturation: Optional[float] = None,
                  kelvin: Optional[float] = None) -> Dict:
        """
        Switches a light on or off, or sets how bright it is, or what colour, from the
        settings page.

        Args:
            entity_id: The light, e.g. "light.kitchen".
            on: Whether to switch it on or off, unless something else is given.
            brightness: How bright, from 1 to 100 percent - which switches it on.
            hue: A colour's hue, from 0 to 360 degrees. Needs a saturation too.
            saturation: How much of a colour, from 0 to 100 percent.
            kelvin: A white's colour temperature, e.g. 2700 for a warm one, kept to
                the warmest and coolest the light makes. Ignored if there's a colour,
                as Home Assistant answers a request for both with an error.

        Returns:
            Dict with 'success' and a 'message'.
        """
        if not LIGHT.fullmatch(entity_id or ""):
            return {"success": False, "message": "That isn't a light."}
        if (hue is None) != (saturation is None):
            return {"success": False, "message": "A colour needs its hue and its saturation."}
        data = {"entity_id": entity_id}
        if brightness is not None:
            data["brightness_pct"] = max(1, min(100, int(brightness)))
        if hue is not None:
            data["hs_color"] = [round(max(0, min(360, hue))), round(max(0, min(100, saturation)))]
        try:
            if kelvin is not None and hue is None:
                # The light's own range is asked of Home Assistant rather than taken
                # from the page, e.g. 9000K on a light which stops at 6500K is 6500K -
                # sent as it is, Home Assistant would refuse it.
                attributes = self._request("GET", f"/api/states/{entity_id}").json().get("attributes") or {}
                warmest = attributes.get("min_color_temp_kelvin")
                coolest = attributes.get("max_color_temp_kelvin")
                if isinstance(warmest, (int, float)) and isinstance(coolest, (int, float)):
                    kelvin = max(warmest, min(coolest, kelvin))
                data["color_temp_kelvin"] = round(kelvin)
            changes = brightness is not None or hue is not None or kelvin is not None
            self._call_service("light", "turn_on" if on or changes else "turn_off", data)
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": "Done."}

    def set_lights(self, on: bool, light: Optional[str] = None, area: Optional[str] = None,
                   brightness: Optional[int] = None, colour: Optional[str] = None,
                   warmth: Optional[int] = None) -> Dict:
        """
        Works the lights through Home Assistant's own services, for what Claude was asked.
        Only ever lights: a light is checked to be one, and an area is only passed to
        light.turn_on and light.turn_off, which do nothing to a lock in it.

        Args:
            on: Whether to switch them on, or off. Off ignores everything else.
            light: One light's entity id, e.g. "light.lamp".
            area: A room's name, e.g. "Bedroom", for all the lights in it. Ignored if
                a light is given. If neither is, it's the room Nova is in.
            brightness: How bright, from 1 to 100 percent.
            colour: A colour by name, e.g. "blue".
            warmth: A white's colour temperature in kelvin, e.g. 2700 for a warm one.

        Returns:
            Dict with 'success' and a 'message', and a 'chime' for success, since the
            lights changing speaks for itself.
        """
        try:
            if light:
                if not LIGHT.fullmatch(light):
                    return {"success": False, "message": "That isn't a light."}
                data = {"entity_id": light}
            else:
                area = area or self.area
                if not area:
                    return {"success": False, "message": "Which room?"}
                area_id = self._area_id(area)
                if area_id is None:
                    return {"success": False, "message": f"I don't know a room called {area}."}
                data = {"area_id": area_id}

            if on:
                if brightness is not None:
                    data["brightness_pct"] = max(1, min(100, int(brightness)))
                # Home Assistant only knows the CSS colours, which are one word in lower
                # case - "lightblue", where Claude may well say "Light Blue".
                named = (colour or "").strip().lower().replace(" ", "")
                if named:
                    data["color_name"] = named
                if warmth is not None:
                    data["color_temp_kelvin"] = int(warmth)
            service = "turn_on" if on else "turn_off"
            logger.info(f"Lights {service}: {data}")
            self._call_service("light", service, data)
        except (requests.RequestException, NotAllowed) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": "Done.", "chime": True}

    def _call_service(self, domain: str, service: str, data: Dict):
        """
        Calls a Home Assistant service - the only place Nova does, so what it may
        call is decided here alone, by SERVICES.

        Args:
            domain: The service's domain, e.g. "light".
            service: The service, e.g. "turn_on".
            data: What it's given: its targets, by entity_id - or area_id, for the
                lights - and anything else it takes, e.g. {"brightness_pct": 30}.

        Raises:
            NotAllowed: If SERVICES doesn't have the service, or it's given anything
                outside its own domain, or nothing to act on.
            requests.RequestException: If Home Assistant couldn't be asked.
        """
        entities = data.get("entity_id")
        entities = [entities] if isinstance(entities, str) else list(entities or [])
        targets = {key for key in data if key in ("entity_id", "area_id", "device_id", "floor_id", "label_id")}
        allowed_targets = {"entity_id", "area_id"} if domain in BY_AREA else {"entity_id"}
        if (service not in SERVICES.get(domain, ()) or not targets or targets - allowed_targets
                or not all(isinstance(entity, str) and re.fullmatch(rf"{domain}\.[a-z0-9_]+", entity)
                           for entity in entities)):
            logger.warning(f"Refused to call {domain}.{service} with {data}")
            raise NotAllowed(f"{domain}.{service}")
        self._request("POST", f"/api/services/{domain}/{service}", json=data)

    def _area_id(self, name: str) -> Optional[str]:
        """
        The id Home Assistant gives an area, e.g. "living_room" for "Living room", which
        its services want rather than the name.

        Args:
            name: The area's name, in any case.

        Returns:
            Its id, or None if there's no such area.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked, or turned
                the template down - see _template.
        """
        # The name is put into the template as a quoted string, so that however it was
        # made up it can't be taken for more of the template.
        template = "{{ area_id(" + json.dumps(name) + ") }}"
        found = self._template(template).strip()
        return None if found in ("", "None") else found

    # Scripts and scenes

    def run(self, entity_id: str) -> Dict:
        """
        Runs a script, or sets a scene, from the settings page - never one whose name
        says it locks or opens anything, nor one works_guarded refuses. Both are looked
        at again first, in case it's been renamed or changed since the page was drawn.

        Args:
            entity_id: e.g. "script.movie_night" or "scene.relax".

        Returns:
            Dict with 'success' and a 'message'.
        """
        found = RUNNABLE.fullmatch(entity_id or "")
        if not found:
            return {"success": False, "message": "That isn't a script or a scene."}
        try:
            states = self._states()
            state = next((state for state in states if state.get("entity_id") == entity_id), {})
            name = (state.get("attributes") or {}).get("friendly_name") or entity_id
            why = WORKS_GUARDED if _sensitive(entity_id, name) else self._why_not_run([entity_id], states,
                                                                                     self._snapshot())
            if why:
                logger.warning(f"Refused a Home Assistant {found[1]} from the settings page, as it {why}: '{name}'")
                return {"success": False, "message": "Nova doesn't run anything which locks, unlocks or opens."
                        if why == WORKS_GUARDED else f"Nova can't see what {name} does, so it doesn't run it."}
            self._call_service(found[1], "turn_on", {"entity_id": entity_id})
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": f"Started {name}." if found[1] == "script" else f"Set {name}."}

    def run_script(self, name: str) -> Dict:
        """
        Runs a script, or sets a scene, by what the user called it - for Claude, for "do
        my goodnight thing". Only one exposed to Assist, as Assist itself would be held
        to, and never one whose name says it locks or opens anything, nor one
        works_guarded refuses.

        Args:
            name: Its name or an other name, as the user said it, e.g. "my goodnight
                routine" - see _script_name.

        Returns:
            Dict with 'success' and a 'message': what was done, why it wasn't, or which
            of several was meant.
        """
        # Which are exposed, and the key to each script's configuration, are the registry's.
        snapshot = self._snapshot()
        if snapshot is None:
            return {"success": False, "message": "I can't tell which scripts can be run by voice just now."}
        try:
            states = self._states()
            wanted = _script_name(name)
            found = {}
            for state in states:
                entity = str(state.get("entity_id") or "")
                called = str((state.get("attributes") or {}).get("friendly_name") or entity)
                if RUNNABLE.fullmatch(entity) and wanted in {_script_name(other)
                                                             for other in [called, *snapshot.aliases(entity)]}:
                    found[entity] = called
            if not wanted or not found:
                return {"success": False, "message": "I haven't got a script or scene called that."}
            if len(found) > 1:
                names = sorted(found.values(), key=str.lower)
                return {"success": False, "message": f"Which one, {', '.join(names[:-1])} or {names[-1]}?"}
            (entity, called), = found.items()
            kind = entity.partition(".")[0]
            if not snapshot.exposed(entity):
                return {"success": False, "message": f"{called} isn't exposed to Assist, so I don't run it by voice."}
            why = WORKS_GUARDED if _sensitive(entity, called) else self._why_not_run([entity], states, snapshot)
            if why:
                logger.warning(f"Refused to run {entity} for Claude, as it {why}")
                return {"success": False, "message": REFUSED if why == WORKS_GUARDED
                        else f"I can't see what {called} does, so I don't run it by voice."}
            logger.info(f"Running {entity} for Claude")
            self._call_service(kind, "turn_on", {"entity_id": entity})
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": f"Started {called}." if kind == "script" else f"Set {called}."}

    def works_guarded(self, entity_id: str, states: List[Dict], snapshot: Optional[Snapshot], *,
                      configs: Optional[Dict[str, Optional[Dict]]] = None) -> bool:
        """
        Whether a script or scene works anything never worked by voice (see _guarded), or
        anything that can't be checked - so it's refused alike by Assist, by Claude and
        from the settings page.

        A scene is checked by its members. A script's configuration is read from Home
        Assistant's script editor, by the registry's key for it, and walked whole: every
        entity it names, every device it acts on, every action given a whole area (see
        AREA_WIDE), and every script and scene it runs - by entity id, or by a script's own
        action, named by its key - checked the same way. A template is left to the user.
        What can't be seen past is refused: an automation, an action that hands its words
        on (see PASSED_ON), an entity given by the registry's own id for it, a scene with no
        members, a configuration that can't be read or is too deep to walk, and anything
        without the registry.

        Args:
            entity_id: e.g. "script.goodnight".
            states: Every entity's state, as /api/states has them.
            snapshot: The registry, or None if it can't be read.
            configs: The script configurations read so far - see _why_not_run.

        Returns:
            Whether it's to be refused. Why is _why_not_run's, which this shares.
        """
        return self._why_not_run([entity_id], states, snapshot, configs) is not None

    def _why_not_run(self, entity_ids: Sequence[str], states: List[Dict], snapshot: Optional[Snapshot],
                     configs: Optional[Dict[str, Optional[Dict]]] = None) -> Optional[str]:
        """
        Why some scripts and scenes aren't to be run, if they aren't - see works_guarded.
        Each is walked on its own, so that a script another has walked already is still
        checked for itself - but each script's configuration is read once.

        Args:
            entity_ids: e.g. ["script.goodnight", "scene.relax"].
            states: Every entity's state, as /api/states has them.
            snapshot: The registry, or None if it can't be read.
            configs: The script configurations read so far, by entity id - None for a
                configuration that can't be read - which this adds to. Shared by the caller
                for a settings page draw, and otherwise None, to read them afresh.

        Returns:
            WORKS_GUARDED if any of them works something guarded, else UNSEEN if any of
            them does what can't be checked, else None.
        """
        by_id = {state.get("entity_id"): state for state in states}
        configs = {} if configs is None else configs
        reasons = []
        for entity_id in entity_ids:
            # One that wasn't there a moment ago can't be checked - it could be new since.
            if entity_id not in by_id:
                reasons.append(UNSEEN)
                continue
            try:
                reasons.append(self._walk(entity_id, by_id, snapshot, set(), configs))
            except RecursionError:
                # A configuration nested deeper than Python will walk or decode, or a long
                # enough chain of scripts each running the next, can't be checked - and
                # mustn't take the request, or the settings page's draw, with it.
                if entity_id not in self._too_deep:
                    logger.warning(f"Can't see what {entity_id} does, as it goes too deep to follow, so it isn't "
                                   f"run by voice or from the settings page")
                self._too_deep.add(entity_id)
                reasons.append(UNSEEN)
        return _worst(reasons)

    def _walk(self, part, by_id: Dict[str, Dict], snapshot: Optional[Snapshot], checked: Set[str],
              configs: Dict[str, Optional[Dict]]) -> Optional[str]:
        """
        Why a piece of a script's configuration isn't to be run, if it isn't. It's walked
        whole, keys as well as values - scene.apply names each thing as a key - and so is
        every script, scene or group it names, each once: a script which runs itself, or
        two which run each other, are each read once rather than for ever.

        Args:
            part: Any piece of it - a dict, a list or a string - or an entity id, e.g.
                "script.goodnight", to check that and whatever it runs.
            by_id: Every entity's state, by entity id.
            snapshot: The registry, or None if it can't be read.
            checked: The scripts, scenes and groups walked already, which this adds to.
            configs: The script configurations read so far - see _why_not_run.

        Returns:
            WORKS_GUARDED if any of it works something guarded, else UNSEEN if any of it
            can't be checked, else None.

        Raises:
            RecursionError: For a configuration, or a chain of scripts, too deep to follow.
        """
        if isinstance(part, dict):
            return _worst([_step_refused(part, by_id, snapshot)]
                          + [self._walk(item, by_id, snapshot, checked, configs)
                             for pair in part.items() for item in pair])
        if isinstance(part, list):
            return _worst([self._walk(item, by_id, snapshot, checked, configs) for item in part])
        found = []
        # A template is left out: it's the user's own, and they exposed what it's in (0008,
        # "My calls").
        for entity in _pieces(part):
            if not ENTITY.fullmatch(entity):
                continue
            domain, _, name = entity.partition(".")
            if domain == "script" and (name not in SCRIPT_SERVICES or entity in by_id):
                if entity in checked:
                    continue
                checked.add(entity)
                # Home Assistant gives each script an action of its own, named by its key -
                # the registry's unique id for it - which stays as it was when the script's
                # entity id is renamed: {"action": "script.leaving"} can run
                # script.leave_home. So the script of that key is walked as well as any of
                # that entity id, and a name that's neither can't be checked.
                keyed = _keyed_script(name, snapshot)
                if keyed is not None and keyed not in by_id:
                    # The registry can be up to five minutes behind a rename, still putting
                    # the key at an entity id the states no longer have - so which script it
                    # is now can't be told.
                    found.append(UNSEEN)
                elif keyed not in (None, entity):
                    found.append(self._walk(keyed, by_id, snapshot, checked, configs))
                if entity not in by_id:
                    if keyed is None:
                        found.append(UNSEEN)
                    continue
                if entity not in configs:
                    configs[entity] = self._script_config(entity, snapshot)
                config = configs[entity]
                found.append(UNSEEN if config is None else self._walk(config, by_id, snapshot, checked, configs))
                continue
            state = by_id.get(entity)
            attributes = (state or {}).get("attributes") or {}
            if domain == "automation":
                # An automation's contents aren't walked, and one could unlock - nor is what
                # automation.trigger sets off.
                found.append(UNSEEN)
            elif _guarded(entity, attributes, snapshot) is not None:
                found.append(WORKS_GUARDED)
            elif state is None or entity in checked:
                # An action's name, e.g. "light.turn_off", or nothing there to work - or
                # something walked already.
                continue
            elif _target_refused(entity, state, snapshot):
                # A button the registry hasn't caught up with yet, which could be Proxmox's.
                found.append(UNSEEN)
            else:
                # A scene's members, or a group's, are its entity_id attribute. A scene with
                # none, as an integration's can be, can't be checked - nor any without the
                # registry, which says which of its members are Proxmox's or the PC's.
                members = attributes.get("entity_id")
                if domain == "scene" and (snapshot is None or not isinstance(members, list)):
                    found.append(UNSEEN)
                elif members:
                    checked.add(entity)
                    found.append(self._walk(members, by_id, snapshot, checked, configs))
        return _worst(found)

    def _script_config(self, entity_id: str, snapshot: Optional[Snapshot]) -> Optional[Dict]:
        """
        A script's configuration, as Home Assistant's script editor has it - e.g. {"alias":
        "Goodnight", "sequence": [{"action": "light.turn_off", ...}]} - asked for by the
        registry's unique id for the script, which is its key there.

        Args:
            entity_id: e.g. "script.goodnight".
            snapshot: The registry, or None if it can't be read.

        Returns:
            It, or None if it can't be read: without the registry, for a script it hasn't
            got, for one written in YAML outside scripts.yaml (which Home Assistant answers
            404 for), with a token which isn't an administrator's, or for an answer which
            isn't a script's - such as one made from a blueprint, whose steps are elsewhere.
        """
        if snapshot is None:
            return None
        entry = snapshot.entries.get(entity_id)
        config, why = None, "the registry has no key for it"
        if entry is not None and entry.unique_id:
            try:
                # Quoted whole, so that however the key was made up, it's one part of the path.
                config = self._request("GET", "/api/config/script/config/" + quote(entry.unique_id, safe="")).json()
                why = None if isinstance(config, dict) and "sequence" in config else "its answer isn't a script's"
            except (requests.RequestException, ValueError) as e:
                why = f"Home Assistant wouldn't say ({e})"
        # The settings page asks for its list every fifteen seconds, so each script that
        # can't be read is said once, until it can be.
        if why is None:
            self._unreadable.discard(entity_id)
            return config
        if entity_id not in self._unreadable:
            logger.warning(f"Can't see what {entity_id} does, as {why}, so it isn't run by voice or from the "
                           f"settings page")
        self._unreadable.add(entity_id)
        return None

    def calendar_names(self) -> List[str]:
        """The calendars to read - those config.yaml names, or every one there is."""
        if self.calendars:
            return self.calendars
        return [calendar["entity_id"] for calendar in self._request("GET", "/api/calendars").json()]

    def events(self, first: date, days: int = 1) -> List[Dict]:
        """
        Everything on the calendars over some days.

        Args:
            first: The first day.
            days: How many days, from 1.

        Returns:
            The events, earliest first, each a dict with "summary", "start" and
            "all_day" - start a local datetime, or a date for an all-day event.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
        """
        start = datetime.combine(first, day_time()).astimezone()
        end = start + timedelta(days=days)
        found = []
        for calendar in self.calendar_names():
            for event in self._request("GET", f"/api/calendars/{calendar}",
                                       params={"start": start.isoformat(), "end": end.isoformat()}).json():
                found.append(_read_event(event))
        return sorted(found, key=lambda event: _sort_key(event["start"]))

    def agenda(self, first: date, days: int = 1, today: Optional[date] = None) -> Dict:
        """
        What's on the calendar, as something to say.

        Args:
            first: The first day asked about.
            days: How many days, e.g. 2 for a weekend.
            today: Today's date - only given by the tests.

        Returns:
            Dict with 'success' and the agenda as the 'message'.
        """
        try:
            events = self.events(first, days)
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error(f"Couldn't read the calendar: {e}")
            return {"success": False, "message": self._failure_message(e)}
        events = _as_activities(events, {name: _called(words) for name, words in self.activities.items()}, first)
        return {"success": True, "message": describe_agenda(events, first, days, today or date.today())}

    def find(self, wanted: str, first: date, today: Optional[date] = None) -> Dict:
        """
        When one thing is on the calendar, for "when's my MOT?". Reading out whole
        days instead, Claude once read a fortnight of them looking for it.

        Args:
            wanted: A word or two from its name, e.g. "MOT".
            first: The first day to look from.
            today: Today's date - only given by the tests.

        Returns:
            Dict with 'success' and when it is as the 'message'.
        """
        words = [word.strip(".,!?'\"") for word in (wanted or "").split()]
        patterns = [_called([word]) for word in words if word]
        if not patterns:
            return {"success": False, "message": "I didn't catch what to look for."}
        try:
            events = self.events(first, SEARCH_DAYS)
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error(f"Couldn't read the calendar: {e}")
            return {"success": False, "message": self._failure_message(e)}
        found = [event for event in events if all(pattern.search(event["summary"]) for pattern in patterns)]
        return {"success": True, "message": describe_found(" ".join(words), found, today or date.today())}

    def activity(self, name: str, first: date, days: int = 1, today: Optional[date] = None,
                 count: Optional[int] = None, now: Optional[datetime] = None) -> Dict:
        """
        When the user has one of their activities - work, uni - from the events named as it.

        Args:
            name: The activity, e.g. "work".
            first: The first day asked about.
            days: How many days.
            today: Today's date - only given by the tests.
            count: Only say the next this many times, for "when's my next shift?".
            now: The time now - only given by the tests.

        Returns:
            Dict with 'success' and the times as the 'message'.
        """
        called = self.activities.get((name or "").strip().lower())
        if called is None:
            return {"success": False,
                    "message": f"I don't know what {name} is called on your calendar. It can be added to "
                               f"home_assistant.activities in my settings."}
        try:
            events = self.events(first, days)
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error(f"Couldn't read the calendar: {e}")
            return {"success": False, "message": self._failure_message(e)}
        pattern = _called(called)
        found = [event for event in events if pattern.search(event["summary"])]
        name = name.strip().lower()
        if count:
            return {"success": True, "message": describe_next(name, found, count, first,
                                                              now or datetime.now().astimezone())}
        return {"success": True, "message": describe_activity(name, found, first, days, today or date.today())}


def _sensitive(entity_id: str, name: str) -> bool:
    """Whether a script or scene's id or name says it locks or opens something, as SENSITIVE has it."""
    return _sensitive_words(name) or _sensitive_words(entity_id.replace("_", " "))


def _sensitive_words(text: str) -> bool:
    """Whether some words say they'd lock, unlock or open something, as SENSITIVE has it - bar PC_LOCK."""
    return bool(SENSITIVE.search(PC_LOCK.sub(" ", text.lower())))


def _words(text: str) -> Tuple[str, ...]:
    """Words alone, in lower case, e.g. ("front", "door") for "Front-door!"."""
    return tuple(re.findall(r"[a-z0-9]+", text.lower()))


def _guarded(entity_id: str, attributes: Dict, snapshot: Optional[Snapshot]) -> Optional[Sequence[str]]:
    """
    Whether something is never worked by voice, by what Home Assistant says it is -
    see GUARDED, PROXMOX and PC_SAFE.

    Args:
        entity_id: e.g. "cover.garage".
        attributes: Its attributes, for a cover's device_class.
        snapshot: The registry, for what the states don't say - which integration a
            thing is from, and which of the PC's buttons does what - or None if it
            can't be read, when only the states are gone by.

    Returns:
        The words Assist takes as all of its kind, e.g. ("window", "windows"), or ()
        for something guarded with no such words - or None for anything which may be
        worked by voice.
    """
    domain = entity_id.partition(".")[0]
    kind = GUARDED_COVERS.get(attributes.get("device_class")) if domain == "cover" else GUARDED.get(domain)
    if kind is not None or snapshot is None:
        return kind
    if snapshot.platform(entity_id) == PROXMOX:
        return ()
    # Only a button can be one of the PC's, so the rest aren't looked for among them.
    if domain == "button":
        _, pc_kind = snapshot.hass_agent_buttons().get(entity_id, (None, None))
        if pc_kind is not None and pc_kind not in PC_SAFE:
            return ()
    return None


def _names_guarded(request: str, states: List[Dict], snapshot: Optional[Snapshot]) -> bool:
    """
    Whether a request for Assist names anything which is never worked by voice - by
    its name, the words of its entity id or an other name from the registry, or by a
    word for all of its kind - as whole words. That's everything Home Assistant has,
    not only what's exposed to Assist.

    A harmless thing's name which holds a guarded one's doesn't let it by, as Assist
    reads a room before a name: with a lock called "front door", "turn off the bedroom
    front door" is refused, whatever else is called "bedroom front door". So is "turn on
    the front door light" - which, as a light sentence, Nova's own lights then do.

    Nor does a name said as other words, as speech-to-text writes what it hears: "pve 2
    shutdown" is "PVE2 Shutdown", and "shut down" is "Shutdown". Only whole words are
    run together, though, so "casanova" is never "nova".

    Args:
        request: What would go to Assist, e.g. "turn off the front door".
        states: Every entity's state, as /api/states has them.
        snapshot: The registry, or None if it can't be read - see _guarded.

    Returns:
        Whether it's to be refused.
    """
    said, excused = _said(request)
    names, kinds = set(), set()
    for state in states:
        entity = str(state.get("entity_id") or "")
        attributes = state.get("attributes") or {}
        kind = _guarded(entity, attributes, snapshot)
        if kind is None:
            continue
        names |= _names(entity, attributes, snapshot)
        kinds |= set(kind)
    return any(name in said for name in names) or any(kind in excused for kind in kinds)


def _runnables_named(request: str, states: List[Dict], snapshot: Optional[Snapshot]) -> List[str]:
    """
    The scripts and scenes a request names, found as _names_guarded finds a guarded thing:
    by name, the words of its entity id or an other name, as whole words.

    Args:
        request: e.g. "turn on the porch light".
        states: Every entity's state, as /api/states has them.
        snapshot: The registry, or None if it can't be read.

    Returns:
        Their entity ids, e.g. ["script.porch_light"].
    """
    said, _ = _said(request)
    named = []
    for state in states:
        entity = str(state.get("entity_id") or "")
        if RUNNABLE.fullmatch(entity) and _names(entity, state.get("attributes") or {}, snapshot) & said:
            named.append(entity)
    return named


def _said(request: str) -> Tuple[Set[str], Set[str]]:
    """
    What a request says, for finding names in it as whole words - see _runs.

    Returns:
        (every run of its words, with and without PC_LOCK's words; the runs of those
        left once PC_LOCK's are taken out, which alone may say a guarded kind's word).
    """
    # PC_LOCK only excuses words, so a guarded thing's name is looked for with and
    # without them: neither "turn on the PC lock", for a shutdown button called that,
    # nor a name with "lock my PC" said in the middle of it, gets by.
    excused = _runs(_words(PC_LOCK.sub(" ", request.lower())))
    return _runs(_words(request)) | excused, excused


def _names(entity_id: str, attributes: Dict, snapshot: Optional[Snapshot]) -> Set[str]:
    """
    Every name something goes by, its words run together as _runs has them: its name, the
    words of its entity id and its other names from the registry - e.g. {"frontdoor"}.
    """
    names = {_words(str(attributes.get("friendly_name") or "")), _words(entity_id.partition(".")[2].replace("_", " "))}
    names |= {_words(alias) for alias in (snapshot.aliases(entity_id) if snapshot else ())}
    return {"".join(name) for name in names if name}


def _runs(words: Tuple[str, ...]) -> Set[str]:
    """
    Every run of whole words, run together - for ("pve", "2", "shutdown"), "pve", "pve2",
    "pve2shutdown", "2", "2shutdown" and "shutdown". So a name is found as whole words
    whether it's said as one word or more, and never inside a word.
    """
    return {"".join(words[start:end]) for start in range(len(words)) for end in range(start + 1, len(words) + 1)}


def _target_refused(entity_id: str, state: Optional[Dict], snapshot: Optional[Snapshot]) -> bool:
    """
    Whether Assist mustn't be asked to act on something its sentence debugging says it would.

    Args:
        entity_id: e.g. "lock.main".
        state: Its state, as /api/states has it, or None if that hasn't got it.
        snapshot: The registry, or None if it can't be read - see _guarded.

    Returns:
        True for anything guarded, anything that wasn't there a moment ago to be checked,
        and a button the registry can't vouch for.
    """
    if state is None:
        return True
    if _guarded(entity_id, state.get("attributes") or {}, snapshot) is not None:
        return True
    # A new Proxmox or HASS.Agent button is in the states at once, but in the registry
    # only up to five minutes later - and both only ever act through buttons.
    return entity_id.partition(".")[0] == "button" and (snapshot is None or snapshot.platform(entity_id) is None)


def _worst(reasons: Iterable[Optional[str]]) -> Optional[str]:
    """The worst of some reasons a script or scene isn't run - WORKS_GUARDED, then UNSEEN - or None for none."""
    reasons = set(reasons)
    return WORKS_GUARDED if WORKS_GUARDED in reasons else UNSEEN if UNSEEN in reasons else None


def _step_refused(part: Dict, by_id: Dict[str, Dict], snapshot: Optional[Snapshot]) -> Optional[str]:
    """
    Why a dict in a script's configuration isn't to be run, by its keys together rather
    than by any one string in it: what a step's action is given, the action itself, or the
    devices it names.

    Args:
        part: e.g. a step, {"action": "homeassistant.turn_off", "target": {"area_id":
            "hall"}}, or its target.
        by_id: Every entity's state, by entity id.
        snapshot: The registry, or None if it can't be read.

    Returns:
        WORKS_GUARDED, UNSEEN or None, as for HomeAssistant._walk.
    """
    found = []
    action = next((part[key] for key in ACTION_KEYS if key in part), None)
    if action is not None:
        # What an action's given is in its target, or the old way, its data or the step itself.
        given = [where for where in (part, part.get("target"), part.get("data"), part.get("data_template"))
                 if isinstance(where, dict)]
        entities = [piece for where in given for piece in _pieces(where.get("entity_id"))]
        # Home Assistant also takes an entity by the registry's own id for it, e.g.
        # "0123...cdef", which only it can say whose it is. A device action's entity is
        # given that way too, but that calls no action, and its device is checked instead.
        if any(piece not in ("", "all", "none") and not ENTITY.fullmatch(piece) for piece in entities):
            found.append(UNSEEN)
        if isinstance(action, str) and "{{" not in action:
            action = action.strip().lower()
            domain = action.partition(".")[0]
            if domain in AREA_WIDE and ("all" in entities
                                        or any(where.get(key) for where in given for key in AREA_TARGETS)):
                found.append(WORKS_GUARDED)
            if domain in PASSED_ON or action in PASSED_ON:
                found.append(UNSEEN)
    devices = part.get("device_id")
    if devices is not None:
        # A device action names its device and the domain it acts in, but its entity only
        # by the registry's own id for it: {"type": "unlock", "device_id": "...",
        # "entity_id": "4b1e...", "domain": "lock"}.
        if str(part.get("domain") or "").lower() in GUARDED:
            found.append(WORKS_GUARDED)
        for device in devices if isinstance(devices, list) else [devices]:
            if not isinstance(device, str) or "{{" in device:
                continue
            # One the registry hasn't got - or any, without it - could have anything on it.
            if snapshot is None or device not in snapshot.devices:
                found.append(UNSEEN)
            elif any(_guarded(entity, (by_id.get(entity) or {}).get("attributes") or {}, snapshot) is not None
                     for entity in snapshot.on_device(device)):
                found.append(WORKS_GUARDED)
    return _worst(found)


def _pieces(value) -> List[str]:
    """
    The entities some configuration gives, as Home Assistant reads them - in any case, and
    several to a string: "light.lamp, Lock.Front_Door" is ["light.lamp", "lock.front_door"].
    From a string or a list of them, leaving templates out - see HomeAssistant._walk.
    """
    strings = value if isinstance(value, list) else [value]
    return [piece.strip().lower() for string in strings if isinstance(string, str) and "{{" not in string
            for piece in string.split(",")]


def _keyed_script(key: str, snapshot: Optional[Snapshot]) -> Optional[str]:
    """The entity id of the script whose key - the registry's unique id for it - this is, or None."""
    if snapshot is None:
        return None
    return next((entry.entity_id for entry in snapshot.entries.values()
                 if entry.platform == "script" and entry.unique_id.lower() == key), None)


def _script_name(name: str) -> str:
    """
    A script's or scene's name as run_script matches it, by its letters and digits alone, as
    speech-to-text writes a name as it hears it: "my goodnight routine", "The Goodnight
    script" and "Good night!" are all "goodnight".
    """
    words = list(_words(name))
    if len(words) > 1 and words[0] in ("my", "the"):
        words = words[1:]
    if len(words) > 1 and words[-1] in ("script", "scene", "routine"):
        words = words[:-1]
    return "".join(words)


def _refused(error: Exception) -> bool:
    """Whether Home Assistant turned a template down, as it does for a token which isn't an administrator's."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return isinstance(error, requests.HTTPError) and status in (401, 403)


def _plain(text: str) -> str:
    """Words for comparing, e.g. "The Main Light!" is "the main light"."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _out_of_reach(lights: List[Dict]) -> Dict:
    """What's said when none of the lights asked for can be reached, e.g. "Home Assistant can't reach that light."."""
    which = "that light" if len(lights) == 1 else "those lights"
    return {"success": False, "message": f"Home Assistant can't reach {which}."}


def _dims(light: Dict) -> bool:
    """Whether a light can be set to a brightness, rather than only switched."""
    return any(mode != "onoff" for mode in light["colour_modes"])


def _stepped(light: Dict, direction: str) -> Optional[int]:
    """
    The brightness a light is to be set to, or None to leave it alone.

    A light that is off, unavailable or only switches is left as it is - except that
    brightening an off one switches it on. One already at the end of the way, or past
    the floor, is left rather than moved the wrong way: a light at 3% stays there.
    """
    if not light["reachable"] or not _dims(light):
        return None
    if not light["on"]:
        return DIM_STEP if direction == "up" else None
    now = light["brightness"]
    if now is None:
        return None
    if direction == "down":
        new = max(DIM_FLOOR, now - DIM_STEP)
        return new if new < now else None
    new = min(100, now + DIM_STEP)
    return new if new > now else None


def _light(state: Dict, aliases: Iterable[str] = ()) -> Dict:
    """
    A light as lights() has it, from its state as LIGHTS_TEMPLATE renders it - or as
    /api/states has it, which has no area.

    Its colour is only what it's set to now - the hue and saturation in colour mode,
    the kelvin in colour temperature mode. For a light which can do both, Home
    Assistant works out the other from it, so a warm white would otherwise be read as
    "hue 30", and a blue as "6500K".

    Args:
        state: The light's state.
        aliases: The other names the user gave it in Home Assistant.
    """
    attributes = state.get("attributes") or {}
    mode = attributes.get("color_mode")
    brightness = attributes.get("brightness")
    colour = attributes.get("hs_color") if mode == "hs" else None
    hue, saturation = [round(part) for part in colour] if colour else (None, None)
    return {"id": state["entity_id"], "name": attributes.get("friendly_name") or state["entity_id"],
            "area": state.get("area") or None, "on": state.get("state") == "on",
            "reachable": state.get("state") not in ("unavailable", "unknown"),
            "brightness": round(brightness / 255 * 100) if isinstance(brightness, (int, float)) else None,
            "colour_modes": list(attributes.get("supported_color_modes") or []),
            "hue": hue, "saturation": saturation,
            "kelvin": attributes.get("color_temp_kelvin") if mode == "color_temp" else None,
            "min_kelvin": attributes.get("min_color_temp_kelvin"),
            "max_kelvin": attributes.get("max_color_temp_kelvin"), "aliases": list(aliases)}


def _light_said(light: Dict) -> str:
    """One light as Claude is told of it: "Lamp, also reading (light.lamp, Bedroom) on at 40%, 2700K"."""
    where = f"{light['id']}, {light['area']}" if light["area"] else light["id"]
    if not light["reachable"]:
        how = "unavailable"
    elif not light["on"]:
        how = "off"
    else:
        how = "on" if light["brightness"] is None else f"on at {light['brightness']}%"
        if light["kelvin"] is not None:
            how += f", {light['kelvin']}K"
        elif light["hue"] is not None:
            how += f", hue {light['hue']} saturation {light['saturation']}%"
    # "Bedroom Light, also main light" - or "also main or big" for two, which reads aloud.
    also = f", also {' or '.join(light['aliases'])}" if light["aliases"] else ""
    return f"{light['name']}{also} ({where}) {how}"


def _called(words: List[str]) -> re.Pattern:
    """
    Finds an event's name with a word in it starting with one of these - so "CSC"
    finds "CSC203 Lecture" and "shift" finds "Night shift", but neither turns up
    inside some other word.
    """
    return re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + ")", re.IGNORECASE)


def _as_activities(events: List[Dict], activities: Dict[str, re.Pattern], first: date) -> List[Dict]:
    """
    The user's activities - work, uni - as one stretch of time each, named after the
    activity. A day of uni was read out session by session, by each one's own name -
    "CSC2035/P01/01 <04-08> at 11:30 AM" - which took 25 seconds and meant little.
    Now it's "uni from 9 AM until 1 PM". Everything else is left as it is.

    Args:
        events: From HomeAssistant.events, earliest first.
        activities: Each activity's name, and what finds its events - see _called.
        first: The first day asked about.

    Returns:
        The events, earliest first, with each activity once a day. Its stretches of
        time that day are in "spans", to be said with their ends as well as their
        starts.
    """
    kept, theirs = [], {name: [] for name in activities}
    for event in events:
        name = next((name for name, called in activities.items() if called.search(event["summary"])), None)
        (theirs[name] if name else kept).append(event)
    for name, its_events in theirs.items():
        for day, on_day in _by_day(its_events, first).items():
            if any(event["all_day"] for event in on_day):
                kept.append({"summary": name, "start": day, "end": None, "all_day": True, "activity": True})
            spans = _stretches(on_day)
            if spans:
                kept.append({"summary": name, "start": spans[0][0], "end": None, "all_day": False, "spans": spans,
                             "activity": True})
    return sorted(kept, key=lambda event: _sort_key(event["start"]))


def _by_day(events: List[Dict], first: date) -> Dict[date, List[Dict]]:
    """Events grouped by the day they start on - or the first day, for one already going."""
    by_day: Dict[date, List[Dict]] = {}
    for event in events:
        day = event["start"] if event["all_day"] else event["start"].date()
        by_day.setdefault(max(day, first), []).append(event)
    return by_day


def _stretches(events: List[Dict]) -> List[List]:
    """
    A day's timed events as stretches of time. Back-to-back sessions - uni's 2:30
    until 3:30, then 3:30 until 5:30 - are one stretch, 2:30 until 5:30, rather than
    read out one by one.
    """
    stretches = []
    for event in sorted((event for event in events if not event["all_day"]), key=lambda e: e["start"]):
        end = event.get("end") or event["start"]
        if stretches and event["start"] <= stretches[-1][1]:
            stretches[-1][1] = max(stretches[-1][1], end)
        else:
            stretches.append([event["start"], end])
    return stretches


def _span(start: datetime, end: datetime) -> str:
    """'from 2:30 PM until 5:30 PM', or 'at 2:30 PM' for something with no length."""
    said = spoken_clock_time(start.hour, start.minute)
    return f"from {said} until {spoken_clock_time(end.hour, end.minute)}" if end > start else f"at {said}"


def describe_activity(name: str, events: List[Dict], first: date, days: int, today: date) -> str:
    """
    Says when the user has an activity, a day at a time.

    Args:
        name: The activity, e.g. "work".
        events: Its events, from HomeAssistant.events, earliest first.
        first: The first day asked about.
        days: How many days.
        today: Today's date.

    Returns:
        e.g. "Tomorrow, you've got work from 9:00 AM until 5:30 PM." or "You haven't
        got uni tomorrow."
    """
    by_day = _by_day(events, first)
    sentences = []
    for day in sorted(by_day):
        times = ["all day"] if any(event["all_day"] for event in by_day[day]) else []
        times += [_span(start, end) for start, end in _stretches(by_day[day])]
        listed = times[0] if len(times) == 1 else ", ".join(times[:-1]) + ", and " + times[-1]
        on = _on(day, today)
        sentences.append(f"{on[0].upper()}{on[1:]}, you've got {name} {listed}.")

    if sentences:
        return " ".join(sentences)
    last = first + timedelta(days=days - 1)
    if days == 1:
        return f"You haven't got {name} {spoken_day(first, today)}."
    return f"You haven't got {name} between {spoken_day(first, today)} and {spoken_day(last, today)}."


def describe_next(name: str, events: List[Dict], count: int, first: date, now: datetime) -> str:
    """
    Says when the user next has an activity - once, or the next few times.

    Asked "when do I next have work?", every shift for the next fortnight used to be
    read out, when only the next one was wanted.

    Args:
        name: The activity, e.g. "work".
        events: Its events from the first day on, earliest first.
        count: How many times to say.
        first: The first day looked at - usually today.
        now: The time now, so one already over today isn't counted.

    Returns:
        e.g. "You're next at work tomorrow, from 3:30 PM until 11:30 PM." or "Your
        next 2 times at work: tomorrow, from ...; and on Saturday, from ...".
    """
    today = now.date()
    upcoming = []
    by_day = _by_day(events, first)
    for day in sorted(by_day):
        if any(event["all_day"] for event in by_day[day]) and day >= today:
            upcoming.append((day, "all day", None, None))
        upcoming += [(day, _span(start, end), start, end) for start, end in _stretches(by_day[day]) if end > now]
    upcoming = upcoming[:count]

    if not upcoming:
        return f"You haven't got {name} in the next two weeks."
    if len(upcoming) == 1:
        day, span, start, end = upcoming[0]
        if start is not None and start <= now:
            return f"You're at {name} now, until {spoken_clock_time(end.hour, end.minute)}."
        return f"You're next at {name} {_on(day, today)}, {span}."
    said = [f"{_on(day, today)}, {span}" for day, span, _, _ in upcoming]
    return f"Your next {len(upcoming)} times at {name}: {'; '.join(said[:-1])}; and {said[-1]}."


def _read_event(event: Dict) -> Dict:
    """One event from Home Assistant, with its start and end read into dates or local times."""
    start, end = event.get("start") or {}, event.get("end") or {}
    if "dateTime" in start:
        when, all_day = datetime.fromisoformat(start["dateTime"]).astimezone(), False
        until = datetime.fromisoformat(end["dateTime"]).astimezone() if "dateTime" in end else None
    else:
        when, all_day, until = date.fromisoformat(start["date"]), True, None
    return {"summary": (event.get("summary") or "something").strip(), "start": when, "end": until,
            "all_day": all_day}


def _sort_key(when) -> datetime:
    """Sorts all-day events before anything at a time on the same day."""
    return when if isinstance(when, datetime) else datetime.combine(when, day_time()).astimezone()


def spoken_day(day: date, today: date) -> str:
    """
    A day as it's said: "today", "tomorrow", "Thursday" within the week, or
    "Thursday the 30th of October" further off.
    """
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if today < day < today + timedelta(days=7):
        return day.strftime("%A")
    suffix = "th" if 11 <= day.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day.day % 10, "th")
    return f"{day.strftime('%A')} the {day.day}{suffix} of {day.strftime('%B')}"


def _on(day: date, today: date) -> str:
    """'today' and 'tomorrow' stand alone; any other day is 'on Thursday'."""
    said = spoken_day(day, today)
    return said if said in ("today", "tomorrow") else f"on {said}"


def describe_agenda(events: List[Dict], first: date, days: int, today: date) -> str:
    """
    Says what's on - a day at a time for a day or two, or as an overview for more.

    Args:
        events: From HomeAssistant.events, earliest first - with the activities
            joined up, see _as_activities.
        first: The first day asked about.
        days: How many days.
        today: Today's date.

    Returns:
        e.g. "On Thursday, you've got the dentist at 9:30 AM, and dinner with Sam at
        7:00 PM.", "Over the next week, you've got uni today and Thursday. There's
        also the dentist on Friday." or "You've got nothing on tomorrow."
    """
    if not events:
        return _nothing_on(first, days, today)
    if days >= OVERVIEW_FROM_DAYS:
        return _overview(events, first, days, today)
    return _day_by_day(events, first, days, today)


def _nothing_on(first: date, days: int, today: date) -> str:
    """What's said when there's nothing on - "You've got nothing on tomorrow", say."""
    last = first + timedelta(days=days - 1)
    if days == 1:
        return f"You've got nothing on {spoken_day(first, today)}."
    if days == 2:
        return f"You've got nothing on {spoken_day(first, today)} or {spoken_day(last, today)}."
    return f"You've got nothing on from {spoken_day(first, today)} to {spoken_day(last, today)}."


def _day_by_day(events: List[Dict], first: date, days: int, today: date) -> str:
    """What's on, each day in full - see describe_agenda."""
    by_day = {first + timedelta(days=offset): [] for offset in range(days)}
    for event in events:
        when = event["start"]
        day = when if event["all_day"] else when.date()
        # An event which started before the days asked about, and runs into them.
        by_day.setdefault(max(day, first), []).append(event)

    sentences = []
    for day, on_day in by_day.items():
        if not on_day:
            continue
        shown = on_day[:MOST_EVENTS_SAID]
        said = [_event_said(event) for event in shown]
        if len(on_day) > len(shown):
            said.append(f"{len(on_day) - len(shown)} more")
        listed = said[0] if len(said) == 1 else ", ".join(said[:-1]) + ", and " + said[-1]
        on = _on(day, today)
        sentences.append(f"{on[0].upper()}{on[1:]}, you've got {listed}.")

    return " ".join(sentences) if sentences else _nothing_on(first, days, today)


def _overview(events: List[Dict], first: date, days: int, today: date) -> str:
    """
    What's on over several days, without the times - see describe_agenda. Each
    activity is said with the days it's on - by name within a week, or as a count
    over longer - and then the other things, each with its day.
    """
    last = first + timedelta(days=days - 1)
    if first == today:
        over = {7: "Over the next week", 14: "Over the next two weeks"}.get(days, f"Over the next {days} days")
    else:
        over = f"From {spoken_day(first, today)} to {spoken_day(last, today)}"

    activity_days: Dict[str, List[date]] = {}
    others = []
    for event in events:
        day = max(event["start"] if event["all_day"] else event["start"].date(), first)
        if event.get("activity"):
            if day not in activity_days.setdefault(event["summary"], []):
                activity_days[event["summary"]].append(day)
        else:
            others.append((event, day))

    activities = []
    for name, on in activity_days.items():
        if len(on) == days:
            activities.append(f"{name} every day")
        elif days > 7:
            activities.append(f"{name} on {len(on)} day{'s' if len(on) != 1 else ''}")
        else:
            # Within a week a day's name is enough, even one further off than
            # spoken_day would name alone - the opening has already said which week.
            names = [spoken_day(day, today) if day <= today + timedelta(days=1) else day.strftime("%A")
                     for day in sorted(on)]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            activities.append(f"{name} {'' if names[0] in ('today', 'tomorrow') else 'on '}{listed}")

    named = [f"{event['summary']} {_on(day, today)}" for event, day in others[:MOST_OTHERS_IN_OVERVIEW]]
    if len(others) > len(named):
        named.append(f"{len(others) - len(named)} more")

    def joined(items: List[str]) -> str:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + ", and " + items[-1]

    if not activities:
        return f"{over}, you've got {joined(named)}."
    said = f"{over}, you've got {joined(activities)}."
    return f"{said} There's also {joined(named)}." if named else said


def _event_said(event: Dict) -> str:
    """One event, as it's said: "the dentist at 9:30 AM", "uni from 9 AM until 1 PM", "Sam's birthday, all day"."""
    if event["all_day"]:
        return f"{event['summary']}, all day"
    if event.get("spans"):
        return f"{event['summary']} " + " and ".join(_span(start, end) for start, end in event["spans"])
    return f"{event['summary']} at {spoken_clock_time(event['start'].hour, event['start'].minute)}"


def describe_found(wanted: str, events: List[Dict], today: date) -> str:
    """
    Says when something looked for on the calendar is on.

    Args:
        wanted: What was looked for, e.g. "MOT".
        events: The events found, earliest first.
        today: Today's date.

    Returns:
        e.g. "You've got Car MOT on Thursday the 30th of October, at 9:30 AM." or
        "I can't see MOT on your calendar in the next three months."
    """
    if not events:
        return f"I can't see {wanted} on your calendar in the next three months."
    said = []
    for event in events[:MOST_FOUND]:
        day = event["start"] if event["all_day"] else event["start"].date()
        when = "all day" if event["all_day"] else f"at {spoken_clock_time(event['start'].hour, event['start'].minute)}"
        said.append(f"{event['summary']} {_on(day, today)}, {when}")
    listed = said[0] if len(said) == 1 else "; ".join(said[:-1]) + "; and " + said[-1]
    more = f" And {len(events) - MOST_FOUND} more after that." if len(events) > MOST_FOUND else ""
    return f"You've got {listed}.{more}"
