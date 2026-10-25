"""
Home Assistant Module.

Works the lights, and reads the calendar, through Home Assistant.

Nova signs in as a Home Assistant user of its own, with a long-lived access token,
and asks for things in plain words through Home Assistant's own Assist - "turn off
the kitchen lights" - rather than naming devices itself. Assist only knows about
the devices exposed to it (Settings > Voice assistants > Expose), so that list is
what Assist can reach, enforced by Home Assistant rather than by anything Claude is
told.

What's never passed on to Assist, and the check of what a script or scene does before
it's run, are ha_guard.py's.

Dimming is Nova's own (see HomeAssistant.dim), as Assist has no sentence for it: it
steps each light's brightness itself. What Assist doesn't understand - "make the
lamp warmer" - goes to Claude, which works the lights through Home Assistant's own
services instead, by light or by room, with a brightness, colour or warmth. Those
services are only ever given lights, by entity id or by area - but any light,
exposed to Assist or not, as exposing only limits what Assist does.

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

Claude can read the state of anything else - "is Navidrome up?" - with status, which
only reads /api/states and calls nothing.

The calendar is whichever calendars Home Assistant has, such as a Google Calendar
added through its Google Calendar integration.
"""

import ipaddress
import json
import logging
import math
import re
import time
from datetime import date, datetime, time as day_time, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

import requests

from voice_assistant import network
from voice_assistant.clock import format_duration, spoken_clock_time
from voice_assistant.ha_guard import (REFUSED, RUNNABLE, WORKS_GUARDED, AssistMatch, Scripts, names_guarded,
                                      runnables_named, sensitive, sensitive_words, split_words, target_refused,
                                      why_not_run)
from voice_assistant.ha_registry import HASS_AGENT_MAKER, HomeRegistry, HomeSocket, HomeSocketError, Snapshot
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

# Every service Nova calls, by domain, and nothing else - no lock, cover, valve, alarm
# panel, hassio or homeassistant service. Each is only ever given entities in its own
# domain, and only the lights' an area as well, which light.turn_on and light.turn_off
# take as just the lights in it. A cover service added here would need each cover
# checked with ha_guard.guarded too, as a door's as much a cover as a blind is.
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

# What the settings page may switch, and run (ha_guard.RUNNABLE), by entity id. A light
# can't let anyone in, whatever it's called; a script or scene could do anything, so its
# name is checked against ha_guard.SENSITIVE as well, and what it does with works_guarded.
LIGHT = re.compile(r"light\.[a-z0-9_]+")

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

# The most things status gives Claude, the best matches first - "is the PC on?" matches
# every one of the PC's sensors, and each is more for Claude to read.
MOST_STATUS = 8

# What status leaves out, as what Claude reads goes to Anthropic. It fails shut, in four
# layers - see _private. First, whole domains: where people and things are, free text
# such as an event's address, and what has tools of its own, such as the calendar.
NOT_STATUS_DOMAINS = {"device_tracker", "person", "zone", "input_text", "notify", "tts", "conversation", "image",
                      "calendar"}

# Then anything with one of these anywhere in its id: the phones', the Mac's and the PC's
# network details, the apps and windows in use, notifications and who's logged in - and
# the batteries and charging, which get_batteries says (see batteries.py).
NOT_STATUS_IDS = ("ip_address", "bssid", "ssid", "wi_fi", "wifi", "network", "last_used_app", "last_app",
                  "frontmost_app", "activewindow", "active_window", "notification", "loggedusers", "logged_users",
                  "active_camera", "active_audio", "display", "clipboard", "geocoded", "battery", "charg")
NOT_STATUS_CLASSES = ("battery", "battery_charging")

# Addresses, looked for anywhere in a state: an IPv4 one, with or without a port or a
# prefix - "10.0.0.2:8080", "10.0.0.0/24" - and a hardware (MAC) one, as "aa:bb:cc:dd:ee:ff",
# "AA-BB-CC-DD-EE-FF", without each byte's leading 0 ("0:1b:63:84:45:e6"), dotted as Cisco
# writes one ("aabb.ccdd.eeff") or 12 hex digits run together with a letter among them -
# twelve digits alone are more likely a count, e.g. bytes used. Four numbers alone make an
# IPv4 address here, whether or not each is under 256 and whatever's before them, as in
# "host.10.0.0.2".
IPV4_ADDRESS = re.compile(r"(?<!\d)\d{1,3}(\.\d{1,3}){3}(?!\d)")
# An IPv4 address in an entity id, whose dots Home Assistant makes underscores - a Ping
# entity for 10.0.0.1 is binary_sensor.10_0_0_1.
IPV4_IN_ID = re.compile(r"(?<!\d)\d{1,3}(_\d{1,3}){3}(?!\d)")
HARDWARE_ADDRESS = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{1,2}([:-][0-9a-f]{1,2}){5}|[0-9a-f]{4}(\.[0-9a-f]{4}){2}"
    r"|(?=[0-9a-f]{0,11}[a-f])[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE)
# A run of hex digits and colons, each tried as an IPv6 address - "fe80::1" in "[fe80::1]".
# A time, "12:30:45", isn't one.
MAYBE_IPV6 = re.compile(r"[0-9a-f:]*:[0-9a-f:]*", re.IGNORECASE)
IPV6_MOST_PIECES = 10

# The integration of the Home Assistant app on a phone, tablet or Mac. Its sensors, and
# HASS.Agent's on a PC, are given only as numbers or times, as their free text is where
# app names, window titles and messages are - and a sensor the app adds later is
# covered without a new entry above.
MOBILE_APP = "mobile_app"

# Seconds after which a state is given with how old it is - a PC switched off still
# has the last CPU temperature it reported, which isn't how warm it is now.
STATUS_STALE_AFTER = 3600

# A state which is a moment, e.g. "2026-10-20T16:52:00+00:00" for the next sunset.
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}.*")

NOTHING_MATCHES = "Nothing in Home Assistant matches that."

# Words in what's asked about which say nothing of what's meant - "is the PC on?" is
# about "pc" alone, and "on" would otherwise find everything "online".
STATUS_STOP_WORDS = {"the", "is", "a", "an", "my", "on", "any", "how", "what", "whats", "are", "it"}


class NotAllowed(ValueError):
    """A service call SERVICES doesn't allow, which nothing in Nova should ever make."""


class NoNetwork(requests.ConnectionError):
    """There's no network at all, so Home Assistant isn't asked - nothing was sent."""


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
        # The scripts whose key or configuration last couldn't be read - see _script_config -
        # and those found too deep to walk, each said once.
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
            raise NoNetwork("No network connection")
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
            can't be told, or when a wider request can't reach Home Assistant before it's
            sent. 'refused' is set when Nova wouldn't ask Assist at all, by the words, by
            what they name or by what Assist would act on - which includes a script, scene
            or group, named or acted on, that works_guarded refuses.
        """
        refused = {"success": False, "message": REFUSED, "refused": True}
        not_understood = {"success": False, "message": DIDNT_UNDERSTAND, "not_understood": True}
        if sensitive_words(request):
            logger.warning(f"Refused a sensitive Home Assistant request: '{request}'", extra=TRANSCRIPT)
            return refused

        asked = {"text": request, "language": "en", "agent_id": self.agent}
        if self.device_id:
            # Assist works out "the lights" from the area the device it's talking to is
            # in, so Nova's room is what's switched when no room is named.
            asked["device_id"] = self.device_id

        # Whether the sentence has gone to Assist, which may then have acted on it whatever
        # comes back.
        posted = False
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
            if names_guarded(request, states, snapshot):
                logger.warning(f"Refused a Home Assistant request naming a lock, door or the like: '{request}'",
                               extra=TRANSCRIPT)
                return refused

            by_id = {state.get("entity_id"): state for state in states}
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
                barred = [target for target in match.targets if target_refused(target, by_id.get(target), snapshot)]
                if barred:
                    logger.warning(f"Refused a Home Assistant request Assist would act on {barred} with: '{request}'",
                                   extra=TRANSCRIPT)
                    return refused
            # A script or scene is only run once what it does has been checked - one Assist
            # would act on, and one the request names, as a light sentence can while the
            # debugging isn't answering: "turn on the porch light", with a script called
            # "Porch light" which unlocks the front door. So is a group worked, as Home
            # Assistant works every member of it: a cover group "Front of house" opens the
            # garage door in it.
            runs = [target for target in (match.targets if match else [])
                    if RUNNABLE.fullmatch(target) or _members(by_id.get(target)) is not None]
            runs += [entity for entity in runnables_named(request, states, snapshot) if entity not in runs]
            if why_not_run(runs, states, snapshot, self._scripts(), self._too_deep):
                logger.warning(f"Refused a Home Assistant request running {runs}, which work something guarded or "
                               f"can't be checked: '{request}'", extra=TRANSCRIPT)
                return refused
            posted = True
            reply = self._request("POST", "/api/conversation/process", json=asked).json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            # Any sentence of Assist's could as well be Claude's - "is the post office
            # open?" - so it's Claude's when Home Assistant can't be reached, as it is with
            # the wider rule off. A light sentence still says why it wasn't done, and so
            # does any sentence Home Assistant answered with an error, such as a bad token.
            # Once it's been sent, though, only a connection never made is Claude's: with
            # no answer in time, Assist may still have run "run goodnight", which Claude's
            # run_home_script would run again.
            unreached = (requests.ConnectTimeout, NoNetwork) if posted else (requests.ConnectionError,
                                                                             requests.Timeout)
            if wider and isinstance(e, unreached):
                return not_understood
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
        # Each script's key and configuration are read once a draw, however many others run it.
        scripts = self._scripts()
        routines = []
        for state in states:
            entity = state.get("entity_id") or ""
            name = (state.get("attributes") or {}).get("friendly_name") or entity
            # Only what run() would run, so the page never offers a button which can't work -
            # and none without the registry, which has the key to each script's contents.
            if (RUNNABLE.fullmatch(entity) and not sensitive(entity, name)
                    and not self.works_guarded(entity, states, snapshot, scripts=scripts)):
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
            The lights named, whether by name or by another name - only those in Nova's
            room, if some of them are; else, for "lights" or
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
            # A name shared across rooms is Nova's own room's, as Assist has it: "dim the
            # lamp", with a Lamp in the bedroom and one in the kitchen, is the bedroom's.
            here = [light for light in named if self.area and light["area"] == self.area]
            return here or named
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

    # What Claude reads

    def status(self, about: str, now: Optional[float] = None) -> Dict:
        """
        The state of whatever best matches the words asked about, for Claude to answer
        "when's sunset?" or "is Navidrome up?" from. It's only read: nothing is called,
        and of each entity's attributes only its unit is given, so no address or
        coordinates. What's private is left out - see _private.

        Each entity scores how many of the words asked, bar little ones such as "the"
        (STATUS_STOP_WORDS), begin a word of its name, its entity id or its area's name -
        with or without an "s" on the end - so "navidrome" finds "Navidrome status" and
        "update.navidrome_update" alike, and "updates" finds update.*. A tie goes to the
        one more of the words are whole words of, so "pc" puts the PC's own sensors
        before "PCIe fan".

        Args:
            about: The words asked about, e.g. "navidrome".
            now: The time to tell each state's age from, as time.time() has it - only
                given by the tests.

        Returns:
            Dict with 'success' and 'message' - up to MOST_STATUS lines, the best match
            first, e.g. "Navidrome status (binary_sensor.navidrome_status, Homelab): on".
        """
        try:
            states = self._states()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Couldn't read Home Assistant's states: {e}")
            return {"success": False, "message": self._failure_message(e)}
        now = time.time() if now is None else now
        # Without the registry, nothing has an area, which is no reason not to answer -
        # but nothing can be told not to be a phone's or the PC's, either (see _private).
        snapshot = self._snapshot()
        asked = set(split_words(about)) - STATUS_STOP_WORDS
        found = []
        for state in states:
            entity = str(state.get("entity_id") or "")
            attributes = state.get("attributes") or {}
            if _private(entity, attributes, str(state.get("state")), snapshot):
                continue
            name = str(attributes.get("friendly_name") or entity)
            area = snapshot.area_name(entity) if snapshot else None
            words = split_words(f"{name} {entity} {area or ''}")
            score = sum(1 for word in asked if any(its.startswith(said) for said in _forms(word) for its in words))
            whole = sum(1 for word in asked if any(said in words for said in _forms(word)))
            if score:
                found.append((-score, -whole, name.lower(), entity, name, area, state))
        if not found:
            return {"success": True, "message": NOTHING_MATCHES}
        best = sorted(found, key=lambda match: match[:4])[:MOST_STATUS]
        return {"success": True, "message": "\n".join(_status_said(name, area, state, now)
                                                       for *_, name, area, state in best)}

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
            why = WORKS_GUARDED if sensitive(entity_id, name) else why_not_run(
                [entity_id], states, self._snapshot(), self._scripts(), self._too_deep)
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
            why = WORKS_GUARDED if sensitive(entity, called) else why_not_run([entity], states, snapshot,
                                                                               self._scripts(), self._too_deep)
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
                      scripts: Optional[Scripts] = None) -> bool:
        """
        Whether a script or scene works anything never worked by voice (see
        ha_guard.guarded), or anything that can't be checked - so it's refused alike by
        Assist, by Claude and from the settings page.

        A scene is checked by its members. A script's configuration is read from Home
        Assistant's script editor, by its key, and walked whole: every entity it names,
        every device it acts on, every action given a whole area (see
        ha_guard.AREA_WIDE), and every script and scene it runs - by entity id, or by a
        script's own action, named by its key - checked the same way. A script's key is
        looked up afresh for its entity id each time, never taken from the registry's
        snapshot, which can be five minutes behind a rename. A template is left to the
        user. What can't be seen past is refused: an automation, an action that hands its
        words on (see ha_guard.PASSED_ON), an entity given by the registry's own id for it
        that the registry hasn't got, a scene with no members, a script whose key can't be
        looked up, a configuration that can't be read or is too deep to walk, and anything
        without the registry.

        Args:
            entity_id: e.g. "script.goodnight".
            states: Every entity's state, as /api/states has them.
            snapshot: The registry, or None if it can't be read.
            scripts: What's been read of the scripts so far, shared for a settings page
                draw - or None, to read them afresh.

        Returns:
            Whether it's to be refused. Why is ha_guard.why_not_run's, which this shares.
        """
        return why_not_run([entity_id], states, snapshot, scripts or self._scripts(), self._too_deep) is not None

    def _scripts(self) -> Scripts:
        """How a check reads the scripts, each once - see ha_guard.Scripts."""
        return Scripts(self._script_config, self._script_key)

    def _script_key(self, entity_id: str) -> Optional[str]:
        """
        The key a script's entity id has in Home Assistant now - the registry's unique id
        for it, and its key in the script editor - looked up afresh over the WebSocket, as
        the registry's snapshot can be five minutes behind a rename. One round of about 20
        ms, within the socket's own deadline.

        Args:
            entity_id: e.g. "script.goodnight".

        Returns:
            e.g. "goodnight", or None if Home Assistant doesn't answer in time, or the
            registry hasn't got the script or has no key for it.
        """
        try:
            entry, = self.socket.call([{"type": "config/entity_registry/get", "entity_id": entity_id}])
        except (HomeSocketError, ValueError) as e:
            why = f"Home Assistant's registry wouldn't say which script it is now ({e})"
        else:
            key = entry.get("unique_id") if isinstance(entry, dict) else None
            if isinstance(key, str) and key:
                return key
            why = "Home Assistant's registry has no key for it"
        self._cant_see(entity_id, why)
        return None

    def _script_config(self, key: str, name: str) -> Optional[Dict]:
        """
        A script's configuration, as Home Assistant's script editor has it - e.g. {"alias":
        "Goodnight", "sequence": [{"action": "light.turn_off", ...}]} - asked for by its key.

        Args:
            key: e.g. "goodnight".
            name: The script as it's named, e.g. "script.goodnight", for saying it can't be read.

        Returns:
            It, or None if it can't be read: for a key the script editor hasn't got, for a
            script written in YAML outside scripts.yaml (which Home Assistant answers 404
            for), with a token which isn't an administrator's, or for an answer which isn't
            a script's - such as one made from a blueprint, whose steps are elsewhere.
        """
        try:
            # Quoted whole, so that however the key was made up, it's one part of the path.
            config = self._request("GET", "/api/config/script/config/" + quote(key, safe="")).json()
        except (requests.RequestException, ValueError) as e:
            self._cant_see(name, f"Home Assistant wouldn't say ({e})")
            return None
        if not (isinstance(config, dict) and "sequence" in config):
            self._cant_see(name, "its answer isn't a script's")
            return None
        self._unreadable.discard(name)
        return config

    def _cant_see(self, name: str, why: str):
        """Says a script can't be checked, and why - once, until it can be."""
        # The settings page asks for its list every fifteen seconds, so each script that
        # can't be read is said once, until it can be.
        if name not in self._unreadable:
            logger.warning(f"Can't see what {name} does, as {why}, so it isn't run by voice or from the "
                           f"settings page")
        self._unreadable.add(name)

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


def _script_name(name: str) -> str:
    """
    A script's or scene's name as run_script matches it, by its letters and digits alone, as
    speech-to-text writes a name as it hears it: "my goodnight routine", "The Goodnight
    script" and "Good night!" are all "goodnight".
    """
    words = list(split_words(name))
    if len(words) > 1 and words[0] in ("my", "the"):
        words = words[1:]
    if len(words) > 1 and words[-1] in ("script", "scene", "routine"):
        words = words[:-1]
    return "".join(words)


def _members(state: Optional[Dict]) -> Optional[List]:
    """A group's members - its entity_id attribute, as a scene's are - or None for what isn't one."""
    members = ((state or {}).get("attributes") or {}).get("entity_id")
    return members if isinstance(members, list) else None


def _refused(error: Exception) -> bool:
    """Whether Home Assistant turned a template down, as it does for a token which isn't an administrator's."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return isinstance(error, requests.HTTPError) and status in (401, 403)


def _private(entity: str, attributes: Dict, state: str, snapshot: Optional[Snapshot]) -> bool:
    """
    Whether status leaves an entity out, as private or another tool's: by its domain
    (NOT_STATUS_DOMAINS), by its id (NOT_STATUS_IDS), its device class, a state, name,
    area or entity id which holds an address of any kind, or a state which isn't a number
    or a time for a text entity, or for a sensor on a personal device or without the
    registry to tell.

    Args:
        entity: e.g. "sensor.example_phone_steps".
        attributes: Its attributes, for its device class.
        state: Its state, e.g. "4200".
        snapshot: The registry, or None if it can't be read.

    Returns:
        Whether it's left out.
    """
    domain = entity.partition(".")[0]
    if domain in NOT_STATUS_DOMAINS or any(word in entity for word in NOT_STATUS_IDS):
        return True
    if attributes.get("device_class") in NOT_STATUS_CLASSES:
        return True
    # A Ping entity is named after the host it pings, so its name and entity id are
    # looked at as well as its state - and its area, which is a name too.
    area = snapshot.area_name(entity) if snapshot else None
    if (any(_holds_address(text) for text in (state, str(attributes.get("friendly_name") or ""), area or ""))
            or IPV4_IN_ID.search(entity)):
        return True
    # Only a sensor's or a text entity's state can be free text - a light's is on or off,
    # whatever the device. A text entity holds whatever was typed into it, on any device.
    if domain == "text" or (domain == "sensor" and (snapshot is None or _personal(entity, snapshot))):
        return not (_number(state) or _moment(state))
    return False


def _holds_address(state: str) -> bool:
    """
    Whether a state has an IP or hardware address anywhere in it, whatever it's called -
    "Connected to 10.0.0.2." or "[fe80::1]" as well as one alone.
    """
    if IPV4_ADDRESS.search(state) or HARDWARE_ADDRESS.search(state):
        return True
    for run in MAYBE_IPV6.findall(state):
        # Every run of its groups is tried, so a label in front - "LAN:2001:db8::1", where
        # "a" is a hex digit - or hex after it - "fe80::1:c0ffee" - can't hide one, and
        # "fe80::1:" at the end of a sentence still holds "fe80::1". No address split at its
        # colons is more than nine pieces - "::1:2:3:4:5:6:7" - or ten with a colon after
        # it, so no longer run is tried, which keeps a long state quick.
        groups = run.split(":")
        for start in range(len(groups)):
            for end in range(start + 1, min(start + IPV6_MOST_PIECES, len(groups)) + 1):
                rest = ":".join(groups[start:end])
                for maybe in {rest, rest.strip(":"), rest.rstrip(":")}:
                    try:
                        ipaddress.IPv6Address(maybe)
                        return True
                    except ValueError:
                        pass
    return False


def _forms(word: str) -> Tuple[str, ...]:
    """A word asked about, and without its "s" if it has one - "updates" is about update.*."""
    # Only for a longer word - "gas" is a word of its own, not more than one "ga".
    return (word, word[:-1]) if len(word) > 3 and word.endswith("s") else (word,)


def _personal(entity: str, snapshot: Snapshot) -> bool:
    """
    Whether an entity is the Home Assistant app's, on a phone, tablet or Mac, or on a
    HASS.Agent PC - or may be, as one added since the registry was read.
    """
    entry = snapshot.entries.get(entity)
    if entry is None:
        return True
    device = snapshot.devices.get(entry.device_id or "") or {}
    return entry.platform == MOBILE_APP or (device.get("maker") or "").startswith(HASS_AGENT_MAKER)


def _number(state: str) -> bool:
    """Whether a state is a number, e.g. "19.5"."""
    try:
        float(state)
    except ValueError:
        return False
    return True


def _moment(state: str) -> bool:
    """Whether a state is a moment, e.g. "2026-10-20T16:52:00+00:00"."""
    if not TIMESTAMP.fullmatch(state):
        return False
    try:
        datetime.fromisoformat(state)
    except ValueError:
        return False
    return True


def _status_said(name: str, area: Optional[str], state: Dict, now: float) -> str:
    """
    One entity's state, as status gives it.

    Args:
        name: Its name, e.g. "Navidrome update".
        area: Its area's name, or None.
        state: It, as /api/states has it.
        now: The time now, as time.time() has it.

    Returns:
        e.g. "Navidrome update (update.navidrome_update, Homelab): off, as of 3 hours ago".
    """
    entity = state.get("entity_id")
    value = str(state.get("state"))
    # Given in local time, as everything else Claude's told is - see LLMHandler._situation.
    # That's the brain's own zone, so it's right as long as the brain runs on the user's.
    if _moment(value):
        value = datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    unit = (state.get("attributes") or {}).get("unit_of_measurement")
    said = f"{name} ({entity}, {area}): {value}" if area else f"{name} ({entity}): {value}"
    if unit:
        said += f" {unit}"
    # When it was last heard from, though unchanged - "last_updated" alone would make a
    # temperature steady all day look hours old.
    try:
        age = now - datetime.fromisoformat(str(state.get("last_reported") or state.get("last_updated"))).timestamp()
    except ValueError:
        return said
    return said + (f", as of {format_duration(age)} ago" if age > STATUS_STALE_AFTER else "")


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
