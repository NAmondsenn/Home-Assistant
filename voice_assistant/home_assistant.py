"""
Home Assistant Module.

Works the lights, and reads the calendar, through Home Assistant.

Nova signs in as a Home Assistant user of its own, with a long-lived access token,
and asks for things in plain words through Home Assistant's own Assist - "turn off
the kitchen lights" - rather than naming devices itself. Assist only knows about
the devices exposed to it (Settings > Voice assistants > Expose), so that list is
what Assist can reach, enforced by Home Assistant rather than by anything Claude is
told. Nothing which unlocks or opens anything is passed on at all - see SENSITIVE.

What Assist doesn't understand - "dim the lights" - goes to Claude, which works the
lights through Home Assistant's own services instead, by light or by room, with a
brightness, colour or warmth. Those services are only ever given lights, by entity
id or by area - but any light, exposed to Assist or not, as exposing only limits
what Assist does.

The settings page names devices itself, as there's nothing to mishear: it lists the
lights, to switch and dim, and the scripts and scenes, to run - but nothing else,
and still no script or scene whose name says it locks or opens anything.

Knowing what's in which room - the room Nova's in, and the lights' - takes an
administrator's token, as Home Assistant only answers templates for one. With an
ordinary user's, Nova goes through Assist alone, and the settings page has the
lights without their rooms.

The calendar is whichever calendars Home Assistant has, such as a Google Calendar
added through its Google Calendar integration.
"""

import json
import logging
import re
from datetime import date, datetime, time as day_time, timedelta
from typing import Dict, List, Optional

import requests

from voice_assistant import network
from voice_assistant.clock import spoken_clock_time

# Logger setup
logger = logging.getLogger(__name__)

# Seconds to wait for Home Assistant before giving up. It's on the local network,
# so anything slower than this means it's down.
REQUEST_TIMEOUT = 5

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

# What the settings page may switch, and run, by entity id. A light can't let anyone
# in, whatever it's called; a script or scene could do anything, so its name is
# checked against SENSITIVE as well.
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


class HomeAssistant:
    """One Home Assistant, reached over its REST API."""

    def __init__(self, url: str, token: str, agent: str = DEFAULT_AGENT, calendars: Optional[List[str]] = None,
                 activities: Optional[Dict[str, List[str]]] = None):
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
        """
        self.url = url.rstrip("/")
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
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    @classmethod
    def from_config(cls, config, token: Optional[str]) -> Optional["HomeAssistant"]:
        """
        The Home Assistant config.yaml points at, or None if it isn't set up.

        Args:
            config: Configs loaded from config.yaml - home_assistant.url and friends.
            token: HA_TOKEN from .env.

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
        return cls(url, token, calendars=settings.get("calendars"), activities=settings.get("activities"))

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
                                   f"- HA_TOKEN needs to be an administrator's for Nova's room and Claude's lights, "
                                   f"so until then Nova goes through Assist alone")
                self._templates_refused = True
            raise
        if self._templates_refused:
            logger.info("Home Assistant tells Nova what's in which room now")
        self._templates_refused = False
        return text

    @staticmethod
    def _failure_message(error: Exception) -> str:
        """What to say when Home Assistant couldn't be asked, depending on why."""
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status == 401:
            return "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."
        if isinstance(error, (requests.ConnectionError, requests.Timeout)):
            return "I can't reach Home Assistant right now."
        return "Home Assistant had a problem with that."

    def command(self, request: str) -> Dict:
        """
        Asks Home Assistant's Assist to do something, in plain words.

        Args:
            request: e.g. "turn off the kitchen lights".

        Returns:
            Dict with 'success' and 'message' - Assist's own reply - and 'chime' for
            something done, since the lights changing speaks for itself. 'not_understood'
            is set too when Assist matched nothing, as opposed to failing at something
            it did understand.
        """
        if SENSITIVE.search(request.lower()):
            logger.warning(f"Refused a sensitive Home Assistant request: '{request}'")
            return {"success": False, "message": "I don't lock, unlock or open anything by voice."}

        asked = {"text": request, "language": "en", "agent_id": self.agent}
        if self.device_id:
            # Assist works out "the lights" from the area the device it's talking to is
            # in, so Nova's room is what's switched when no room is named.
            asked["device_id"] = self.device_id

        try:
            reply = self._request("POST", "/api/conversation/process", json=asked).json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}

        response = reply.get("response") or {}
        said = ((response.get("speech") or {}).get("plain") or {}).get("speech") or ""
        kind = response.get("response_type")
        logger.info(f"Home Assistant: {kind} - '{said}'")
        if kind == "action_done":
            return {"success": True, "message": said or "Done.", "chime": True}
        if kind == "query_answer":
            return {"success": True, "message": said}
        failed = {"success": False, "message": said or "Home Assistant didn't understand that."}
        if kind == "error" and (response.get("data") or {}).get("code") in NOT_UNDERSTOOD:
            failed["not_understood"] = True
        return failed

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
        # see main.py - so each problem is logged once, when it starts, rather than
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
        The lights, and the scripts and scenes, for the settings page - each by its
        entity id and name. The lights are in the order of _in_order, with Nova's own
        room first; the scripts and scenes are in name order.

        Returns:
            Dict with 'success', and 'lights' and 'routines', or a 'message'. Each light
            is as lights() has it - in no area if Home Assistant won't say, without an
            administrator's token - and 'dimmable' as well: whether it does more than
            switch on and off.
        """
        try:
            states = self._request("GET", "/api/states").json()
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
        routines = []
        for state in states:
            entity = state.get("entity_id") or ""
            name = (state.get("attributes") or {}).get("friendly_name") or entity
            if RUNNABLE.fullmatch(entity) and not _sensitive(entity, name):
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
            to a white. Whichever it isn't set to is None.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked, or turned
                the template down - see _template.
            ValueError: If it didn't answer with the lights.
        """
        found = self._template(LIGHTS_TEMPLATE, timeout=timeout)
        return [_light(state) for state in json.loads(found)]

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
            e.g. "Nova is in the Bedroom. Lights: Bedroom Light (light.bedroom_light,
            Bedroom) on at 40%, 2700K; Lamp (light.lamp, Bedroom) off" - or "Nova's
            room isn't known." to begin with, when it isn't. "" if the lights couldn't
            be read.
        """
        try:
            lights = self._in_order(self.lights(timeout=timeout))
        except (requests.RequestException, ValueError) as e:
            # Being turned down for want of an administrator's token is said once, by
            # _template, rather than with every request.
            if not _refused(e):
                logger.error(f"Couldn't read the lights: {e}")
            return ""
        room = f"Nova is in the {self.area}." if self.area else "Nova's room isn't known."
        if not lights:
            return f"{room} There are no lights."
        return f"{room} Lights: " + "; ".join(_light_said(light) for light in lights)

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
            service = "turn_on" if on or changes else "turn_off"
            self._request("POST", f"/api/services/light/{service}", json=data)
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
            self._request("POST", f"/api/services/light/{service}", json=data)
        except requests.RequestException as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": "Done.", "chime": True}

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

    def run(self, entity_id: str) -> Dict:
        """
        Runs a script, or sets a scene, from the settings page - never one whose name
        says it locks or opens anything, which is looked up again first, in case it's
        been renamed since the page was drawn.

        Args:
            entity_id: e.g. "script.movie_night" or "scene.relax".

        Returns:
            Dict with 'success' and a 'message'.
        """
        found = RUNNABLE.fullmatch(entity_id or "")
        if not found:
            return {"success": False, "message": "That isn't a script or a scene."}
        try:
            state = self._request("GET", f"/api/states/{entity_id}").json()
            name = (state.get("attributes") or {}).get("friendly_name") or entity_id
            if _sensitive(entity_id, name):
                logger.warning(f"Refused a sensitive Home Assistant {found[1]} from the settings page: '{name}'")
                return {"success": False, "message": "Nova doesn't run anything which locks, unlocks or opens."}
            self._request("POST", f"/api/services/{found[1]}/turn_on", json={"entity_id": entity_id})
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Home Assistant request failed: {e}")
            return {"success": False, "message": self._failure_message(e)}
        return {"success": True, "message": f"Started {name}." if found[1] == "script" else f"Set {name}."}

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
    return bool(SENSITIVE.search(name.lower()) or SENSITIVE.search(entity_id.replace("_", " ")))


def _refused(error: Exception) -> bool:
    """Whether Home Assistant turned a template down, as it does for a token which isn't an administrator's."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return isinstance(error, requests.HTTPError) and status in (401, 403)


def _light(state: Dict) -> Dict:
    """
    A light as lights() has it, from its state as LIGHTS_TEMPLATE renders it - or as
    /api/states has it, which has no area.

    Its colour is only what it's set to now - the hue and saturation in colour mode,
    the kelvin in colour temperature mode. For a light which can do both, Home
    Assistant works out the other from it, so a warm white would otherwise be read as
    "hue 30", and a blue as "6500K".
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
            "max_kelvin": attributes.get("max_color_temp_kelvin")}


def _light_said(light: Dict) -> str:
    """One light as Claude is told of it: "Lamp (light.lamp, Bedroom) on at 40%, 2700K"."""
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
    return f"{light['name']} ({where}) {how}"


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
