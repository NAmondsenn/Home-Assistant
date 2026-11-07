"""
Home Assistant Test
Checks the lights and the rest of what's done through Home Assistant, using a fake in
place of its API, so nothing is sent anywhere. The calendar is Google's - see
test_google_calendar.py.
"""

import inspect
import json
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import pytest
import requests

from voice_assistant import ha_guard, home_assistant, network
from voice_assistant.actions import Action, ActionExecutor
from voice_assistant.ha_registry import Entry, HomeSocketError, Snapshot
from voice_assistant.home_assistant import REQUEST_TIMEOUT, HomeAssistant, NotAllowed
from voice_assistant.intents import LocalIntents
from voice_assistant.llm import LLMHandler
from voice_assistant.settings_page import SettingsPage


@pytest.fixture(autouse=True)
def london(monkeypatch):
    """
    Runs every test on UK time, whatever the machine's own time zone, as times are
    read and said in the brain's own - see test_google_calendar.py's, where the
    calendar's events are.
    """
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        # The template endpoint answers in plain text, not JSON.
        self.text = body if isinstance(body, str) else ""

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class FakeSession:
    """Stands in for Home Assistant's API, answering by path and noting what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.headers = {}
        self.requests = []
        self.timeouts = []

    def request(self, method, url, timeout=None, **kwargs):
        path = url.split("8123", 1)[1]
        self.requests.append((method, path, kwargs))
        self.timeouts.append(timeout)
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)


class FakeSocket:
    """
    Stands in for Home Assistant's WebSocket, which Assist's sentence debugging is asked
    over. It raises HomeSocketError, as with Home Assistant down, unless a test gives it
    the debugging's answer - so no test reaches a network.
    """

    def __init__(self, session, registry):
        self.session = session
        self.registry = registry
        self.answer = HomeSocketError("not answering")
        # The registry as Home Assistant has it now, by entity id, for a script's key looked
        # up afresh - the snapshot's, unless a test says it's moved on since - or an error
        # to raise instead.
        self.live: Optional[Dict[str, Entry]] = None
        self.lookup_fails: Optional[Exception] = None
        # What the lookup answers instead, for an answer of the wrong shape.
        self.lookup_answer = None
        # Each call's messages, with how many requests the REST API had been sent by then.
        self.calls = []

    def call(self, messages):
        self.calls.append((messages, len(self.session.requests)))
        if messages[0].get("type") == "config/entity_registry/get_entries":
            if self.lookup_fails is not None:
                raise self.lookup_fails
            if self.lookup_answer is not None:
                return [self.lookup_answer]
            live = self.live if self.live is not None else (self.registry.read.entries if self.registry.read else {})
            # Home Assistant answers None for an entity id it hasn't got.
            entries = {entity: live.get(entity) for entity in messages[0]["entity_ids"]}
            return [{entity: entry and {"entity_id": entry.entity_id, "platform": entry.platform,
                                        "unique_id": entry.unique_id}
                     for entity, entry in entries.items()}]
        if isinstance(self.answer, Exception):
            raise self.answer
        return [self.answer]

    def lookups(self):
        """The entity ids whose registry entries were looked up afresh, in order."""
        return [entity for messages, _ in self.calls
                if messages[0].get("type") == "config/entity_registry/get_entries"
                for entity in messages[0]["entity_ids"]]

    def lookup_calls(self):
        """How many times the socket was called to look scripts' keys up."""
        return sum(messages[0].get("type") == "config/entity_registry/get_entries" for messages, _ in self.calls)


class FakeRegistry:
    """Stands in for the HomeRegistry, with whichever snapshot a test gives it - none to begin with."""

    def __init__(self):
        self.read = None

    def snapshot(self):
        return self.read


def _assist(kind, speech):
    return {"response": {"response_type": kind, "speech": {"plain": {"speech": speech}}}}


@pytest.fixture
def make_home():
    def factory(answers):
        home = HomeAssistant("http://homeassistant.local:8123/", "token", registry=FakeRegistry())
        # A house with nothing in it, unless a test says otherwise - which is what's
        # checked before anything goes to Assist.
        home._session = FakeSession({"/api/states": [], **answers})
        home.socket = FakeSocket(home._session, home.registry)
        return home
    return factory


def _asked(home):
    """What was sent to Assist."""
    return next(sent["json"] for _, path, sent in home._session.requests if path == "/api/conversation/process")


def _paths(home):
    """The REST API's paths asked for, in order."""
    return [path for _, path, _ in home._session.requests]


def _thing(entity, name, device_class=None):
    """A state as /api/states has it."""
    attributes = {"friendly_name": name, **({"device_class": device_class} if device_class else {})}
    return {"entity_id": entity, "state": "unknown", "attributes": attributes}


# What command() answers when the guard refuses - and when all it refuses for is a machine's.
REFUSED = {"success": False, "message": "I don't lock, unlock or open anything by voice.", "refused": True}
REFUSED_MACHINE = {"success": False, "message": "I don't shut down or restart machines by voice.", "refused": True}


def _entry(entity, platform="demo", device=None, original_name=None, aliases=(), exposed=True, registry_id="",
           area=None):
    """One entity as the registry has it, its unique id its object id - in the area with this id, if one's given."""
    return Entry(entity, platform, device, area, entity.partition(".")[2], original_name, tuple(aliases), exposed,
                 registry_id)


def _registry(*entries, devices=None):
    """A registry snapshot of these entities, and their devices by id, e.g. {"pc": {"name": ..., "maker": ...}}."""
    return Snapshot(entries={entry.entity_id: entry for entry in entries}, devices=devices or {}, areas={},
                    read_at=0.0)


# The lights

def test_a_light_command_goes_to_assist_and_is_chimed(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Turned off the lights")})
    result = home.command("turn off the kitchen lights")

    assert result == {"success": True, "message": "Turned off the lights", "chime": True}
    assert _asked(home) == {"text": "turn off the kitchen lights", "language": "en",
                            "agent_id": "conversation.home_assistant"}


def test_a_question_is_answered_in_assists_words(make_home):
    home = make_home({"/api/conversation/process": _assist("query_answer", "The hall light is on")})
    assert home.command("is the hall light on") == {"success": True, "message": "The hall light is on"}


def test_something_assist_couldnt_do_says_why(make_home):
    home = make_home({"/api/conversation/process": _assist("error", "Sorry, I am not aware of any area called attic")})
    assert home.command("turn on the attic lights") == {
        "success": False, "message": "Sorry, I am not aware of any area called attic"}


def test_assist_is_told_which_device_is_speaking(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Turned on the lights")})
    home.device_id = "da94"
    home.command("lights on")
    assert _asked(home)["device_id"] == "da94"


def test_without_a_device_assist_is_asked_as_before(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Done")})
    home.command("lights on")
    assert "device_id" not in _asked(home)


@pytest.mark.parametrize("code", ["no_intent_match", "no_valid_targets"])
def test_assist_not_understanding_is_told_apart(make_home, code):
    reply = _assist("error", "Sorry, I couldn't understand that")
    reply["response"]["data"] = {"code": code}
    result = make_home({"/api/conversation/process": reply}).command("dim the lights")
    assert result == {"success": False, "message": "Sorry, I couldn't understand that", "not_understood": True}


def test_an_error_which_isnt_not_understanding_is_not_told_apart(make_home):
    # A request Assist understood but couldn't carry out has its answer already, so
    # Claude shouldn't be asked to have another go at it.
    reply = _assist("error", "Sorry, something went wrong")
    reply["response"]["data"] = {"code": "failed_to_handle"}
    result = make_home({"/api/conversation/process": reply}).command("turn on the lights")
    assert result == {"success": False, "message": "Sorry, something went wrong"}


def test_the_device_is_found_by_its_identifier_not_an_entity_name(make_home):
    home = make_home({"/api/template": FakeResponse("da94|Bedroom")})
    assert home.find_device("nova_home_assistant") is True
    assert (home.device_id, home.area) == ("da94", "Bedroom")
    assert "nova_home_assistant" in home._session.requests[0][2]["json"]["template"]

    home._session.answers["/api/template"] = FakeResponse("|")
    assert home.find_device("nova_home_assistant") is False
    assert (home.device_id, home.area) == (None, None)


def test_a_device_not_yet_placed_in_an_area_is_still_found(make_home):
    home = make_home({"/api/template": FakeResponse("da94|")})
    assert home.find_device("nova_home_assistant") is True
    assert (home.device_id, home.area) == ("da94", None)


def test_when_home_assistant_cant_be_asked_the_device_is_remembered(make_home):
    home = make_home({"/api/template": FakeResponse("da94|Bedroom")})
    home.find_device("nova_home_assistant")
    home._session.answers["/api/template"] = requests.ConnectionError("down")
    assert home.find_device("nova_home_assistant") is False
    assert (home.device_id, home.area) == ("da94", "Bedroom")


def test_looking_for_the_device_logs_each_problem_once(make_home, caplog):
    # It's looked for every 30 seconds until it's found, which was a line in the log
    # every time while Home Assistant was down.
    home = make_home({"/api/template": requests.ConnectionError("down")})
    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        home.find_device("nova_home_assistant")
        home.find_device("nova_home_assistant")
        assert len(caplog.records) == 1 and caplog.records[0].levelname == "ERROR"

        # Back, but without Nova's device - not taken in yet, or set up wrongly.
        home._session.answers["/api/template"] = FakeResponse("|")
        home.find_device("nova_home_assistant")
        home.find_device("nova_home_assistant")
        assert len(caplog.records) == 3
        assert caplog.records[1].message == "Home Assistant's answering again"
        assert caplog.records[2].levelname == "WARNING" and "nova_home_assistant" in caplog.records[2].message

        home._session.answers["/api/template"] = FakeResponse("da94|Bedroom")
        home.find_device("nova_home_assistant")
        assert caplog.records[3].message == "Nova is in the Bedroom, as Home Assistant has it"
        assert len(caplog.records) == 4


def test_the_room_is_only_logged_when_it_changes(make_home, caplog):
    home = make_home({"/api/template": FakeResponse("da94|Bedroom")})
    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        home.find_device("nova_home_assistant")
        home.find_device("nova_home_assistant")
        assert caplog.messages.count("Nova is in the Bedroom, as Home Assistant has it") == 1

        home._session.answers["/api/template"] = FakeResponse("da94|Kitchen")
        home.find_device("nova_home_assistant")
        assert "Nova is in the Kitchen, as Home Assistant has it" in caplog.messages

        home._session.answers["/api/template"] = FakeResponse("da94|")
        home.find_device("nova_home_assistant")
    assert "Nova isn't in a room, as Home Assistant has it" in caplog.messages


@pytest.mark.parametrize("request_text", ["unlock the front door", "open the garage", "open the back door",
                                          "disarm the alarm", "lock the door"])
def test_nothing_which_lets_anyone_in_is_done_by_voice(make_home, request_text):
    home = make_home({"/api/conversation/process": _assist("action_done", "Done")})
    assert home.command(request_text)["success"] is False
    assert home._session.requests == []


def test_a_light_named_after_a_door_is_fine(make_home):
    home = make_home({"/api/conversation/process": _assist("action_done", "Turned on the light")})
    assert home.command("turn on the front door light")["success"]


# Assist's own "turn off" unlocks a lock, and its "turn on" opens a cover or a valve,
# so what's refused is decided by what Home Assistant says a thing is - its domain, or
# a cover's device class - not only by the words.
@pytest.mark.parametrize("thing, request_text", [
    (_thing("lock.front_door", "Front door"), "turn off the front door"),
    (_thing("lock.back", "Back"), "turn off the doors"),
    (_thing("valve.water_main", "Water main"), "turn on the water main"),
    (_thing("alarm_control_panel.house", "House"), "turn off the house"),
    (_thing("cover.patio", "Patio", "door"), "turn on the patio"),
    (_thing("cover.driveway", "Driveway", "gate"), "turn on the driveway"),
    (_thing("cover.side", "Side", "garage"), "turn on the side"),
    (_thing("cover.skylight", "Skylight", "window"), "turn on the skylight"),
    (_thing("cover.skylight", "Skylight", "window"), "turn on the windows"),
])
def test_what_would_let_anyone_in_is_refused_whatever_its_called(make_home, thing, request_text):
    home = make_home({"/api/states": [thing], "/api/conversation/process": _assist("action_done", "Done")})
    assert home.command(request_text) == REFUSED
    assert [path for _, path, _ in home._session.requests] == ["/api/states"]


@pytest.mark.parametrize("device_class", ["blind", "curtain", "shade", "shutter", "awning", None])
def test_blinds_curtains_and_the_like_may_open_and_close(make_home, device_class):
    # The user's call: doors, garages, gates and windows never, anything else may.
    home = make_home({"/api/states": [_thing("cover.bedroom", "Bedroom cover", device_class)],
                      "/api/conversation/process": _assist("action_done", "Opened")})
    assert home.command("turn on the bedroom cover")["success"]
    assert _asked(home)["text"] == "turn on the bedroom cover"


@pytest.mark.parametrize("device_class", [["garage"], {"kind": "garage"}, 3])
def test_a_cover_whose_device_class_isnt_a_word_is_guarded(make_home, device_class):
    # Home Assistant only ever gives a word, so anything else is a cover whose kind can't
    # be told - which could be a garage door - rather than one to fail at.
    cover = _thing("cover.side", "Side", device_class)
    assert ha_guard.guarded("cover.side", cover["attributes"], None) == ()
    home = make_home({"/api/states": [cover], "/api/conversation/process": _assist("action_done", "Opened")})
    assert home.command("turn on the side") == REFUSED
    assert "/api/conversation/process" not in _paths(home)


def test_a_light_whose_name_holds_a_locks_is_that_light(make_home):
    # Assist isn't asked, as it could take the words for the lock - but Nova's own
    # lights, which only ever work lights, switch it on.
    light = _rendered_light("light.front_door_light", "Front door light", "Hall", state="off")
    home = _house(make_home, light)
    home._session.answers["/api/states"] = [_thing("lock.front_door", "Front door"),
                                            _thing("light.front_door_light", "Front door light")]
    home._session.answers["/api/conversation/process"] = _assist("action_done", "Turned on the light")
    assert home.command("turn on the front door light") == REFUSED
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "turn on the front door light", "on": True, "words": "the front door light"}))
    assert result.ok and _dimmed(home) == [("turn_on", {"entity_id": ["light.front_door_light"]})]
    # Though nothing is done when the lock's named as well.
    assert home.command("turn on the front door light and the front door") == REFUSED
    assert "/api/conversation/process" not in [path for _, path, _ in home._session.requests]


# A doorbell camera or a contact sensor is often called what its lock or window is, and
# Assist could still take the words as the lock. Nor may a harmless name hide a kind's
# word, which Assist takes with a room - "the bedroom window" opens the bedroom's
# windows - or take half of a guarded name with it.
@pytest.mark.parametrize("things, request_text", [
    ([_thing("lock.front_door", "Front door"), _thing("camera.front_door", "Front door")],
     "turn off the front door"),
    ([_thing("lock.front_door", "Front door"), _thing("binary_sensor.front_door", "Front door", "door")],
     "turn off the front door"),
    ([_thing("lock.front_door", "Front door"), _thing("light.front_door", "Porch light")],
     "turn off the front door"),
    ([_thing("cover.bedroom_window", "Bedroom window", "window"),
      _thing("binary_sensor.bedroom_window", "Bedroom window", "window")], "turn on the bedroom window"),
    ([_thing("cover.skylight", "Skylight", "window"),
      _thing("binary_sensor.bedroom_window", "Bedroom window", "window")], "turn on the bedroom window"),
    ([_thing("lock.front", "Front"), _thing("cover.skylight", "Skylight", "window"),
      _thing("binary_sensor.front_window", "Front window", "window")], "turn on the front window"),
    ([_thing("lock.front_door", "Front door"), _thing("light.front_door_light", "Front door light"),
      _thing("cover.light_well", "Light well", "window")], "turn on the front door light well"),
])
def test_a_harmless_name_never_hides_a_guarded_one(make_home, things, request_text):
    home = make_home({"/api/states": things, "/api/conversation/process": _assist("action_done", "Done")})
    assert not home.command(request_text)["success"]
    assert [path for _, path, _ in home._session.requests] == ["/api/states"]


def test_locking_the_pc_is_the_one_exception(make_home):
    # HASS.Agent's button.windows_pc_lock is a button, not a lock, and locking the PC
    # only makes things safer - the user's call. Unlocking anything still isn't done.
    home = make_home({"/api/states": [_thing("button.windows_pc_lock", "Windows PC lock"),
                                      _thing("lock.front_door", "Front door")],
                      "/api/conversation/process": _assist("action_done", "Pressed")})
    assert home.command("lock my PC")["success"] and _asked(home)["text"] == "lock my PC"
    for refused in ("unlock my PC", "lock my PC and the front door", "lock my PC and lock the door"):
        assert not home.command(refused)["success"], refused


# What the registry knows that /api/states doesn't: the other names, the integration
# each thing is from, and which of a PC's buttons does what.

def test_a_harmless_thing_holding_a_locks_name_doesnt_hide_it(make_home):
    # Assist reads a room before a name, so this could be the lock "front door" in the bedroom.
    home = make_home({"/api/states": [_thing("lock.front_door", "Front door"),
                                      _thing("switch.bedroom_front_door", "Bedroom front door")],
                      "/api/conversation/process": _assist("action_done", "Done")})
    assert home.command("turn off the bedroom front door") == REFUSED
    assert [path for _, path, _ in home._session.requests] == ["/api/states"]


def test_a_lock_called_door_is_refused_beside_a_sensor_called_back_door(make_home):
    home = make_home({"/api/states": [_thing("lock.door", "Door"),
                                      _thing("binary_sensor.back_door", "Back door", "door")],
                      "/api/conversation/process": _assist("action_done", "Done")})
    assert home.command("turn off the back door") == REFUSED
    assert [path for _, path, _ in home._session.requests] == ["/api/states"]


def test_a_guarded_thing_is_refused_by_its_other_name(make_home):
    home = make_home({"/api/states": [_thing("valve.water_main", "Water main")],
                      "/api/conversation/process": _assist("action_done", "Done")})
    home.snapshot = lambda: _registry(_entry("valve.water_main", aliases=["garden tap"]))
    assert home.command("turn on the garden tap") == REFUSED
    assert [path for _, path, _ in home._session.requests] == ["/api/states"]
    # It's the other name that did it.
    home.snapshot = lambda: None
    assert home.command("turn on the garden tap")["success"]


def test_proxmox_is_refused_whatever_its_called(make_home):
    home = make_home({"/api/states": [_thing("button.bedroom_pve1_shut_down", "PVE2 Shut down"),
                                      _thing("switch.ct_navidrome", "Navidrome")],
                      "/api/conversation/process": _assist("action_done", "Done")})
    home.snapshot = lambda: _registry(_entry("button.bedroom_pve1_shut_down", "proxmoxve"),
                                      _entry("switch.ct_navidrome", "proxmoxve"))
    for request in ("turn on pve2 shut down", "turn off navidrome"):
        assert home.command(request) == REFUSED_MACHINE, request
    assert [path for _, path, _ in home._session.requests] == ["/api/states"] * 2
    # It's the integration that did it.
    home.snapshot = lambda: None
    assert home.command("turn off navidrome")["success"]


def test_a_machine_is_refused_in_words_of_its_own(make_home):
    # "I don't lock, unlock or open anything" was said for shutting down PVE2 as well - the
    # user's call: a machine has its own sentence, and anything with a lock in it the lock's.
    states, registry = _desk_pc("Restart")
    states += [_thing("button.pve2_shutdown", "PVE2 Shutdown"), _thing("lock.shed", "Shed"),
               _thing("switch.ct_navidrome", "Navidrome")]
    registry = _registry(*registry.entries.values(), _entry("button.pve2_shutdown", "proxmoxve"),
                         _entry("lock.shed"), _entry("switch.ct_navidrome", "proxmoxve"), devices=DESK)
    home = make_home({"/api/states": states, "/api/conversation/process": _assist("action_done", "Done")})
    home.registry.read = registry
    assert home.command("turn on pve2 shutdown") == REFUSED_MACHINE
    assert home.command("turn on desk pc restart") == REFUSED_MACHINE
    assert home.command("turn on pve2 shutdown and the shed") == REFUSED
    assert home.command("unlock the pve2 shutdown") == REFUSED
    # So is what Assist would act on, by a name the registry hasn't caught up with.
    home.socket.answer = _debugged(_match("HassTurnOff", "switch.ct_navidrome"))
    assert home.command("turn off the music box", wider=True) == REFUSED_MACHINE
    home.socket.answer = _debugged(_match("HassTurnOff", "switch.ct_navidrome", "lock.shed"))
    assert home.command("turn off the music box", wider=True) == REFUSED
    assert "/api/conversation/process" not in _paths(home)


def _desk_pc(*kinds):
    """A HASS.Agent device "Desk PC" with a button for each kind, e.g. "Shutdown": its states and registry."""
    states = [_thing(f"button.desk_pc_{kind.lower()}", f"Desk PC {kind}") for kind in kinds]
    registry = _registry(*[_entry(f"button.desk_pc_{kind.lower()}", "mqtt", "desk", kind) for kind in kinds],
                         devices={"desk": {"name": "Desk PC", "maker": "HASS.Agent Team"}})
    return states, registry


def test_the_pcs_shutdown_restart_and_web_address_buttons_are_refused(make_home):
    states, registry = _desk_pc("Shutdown", "Restart", "LaunchUrl", "Sleep")
    home = make_home({"/api/states": states, "/api/conversation/process": _assist("action_done", "Pressed")})
    home.snapshot = lambda: registry
    for request in ("turn on desk pc shutdown", "turn on desk pc restart", "turn on desk pc launchurl"):
        assert home.command(request) == REFUSED_MACHINE, request
    assert [path for _, path, _ in home._session.requests] == ["/api/states"] * 3
    assert home.command("turn on desk pc sleep")["success"] and _asked(home)["text"] == "turn on desk pc sleep"


def test_the_pcs_commands_are_refused_whatever_kind_of_entity_each_is(make_home):
    # HASS.Agent lets each command be a switch, a light or a siren as well as a button -
    # so a shutdown as a switch is "turn on desk pc shutdown" with no yes. Only the
    # buttons for sleep, monitor sleep and lock are safe; a switch is never vouched for
    # by its name, as only a button's name is HASS.Agent's own.
    things = [("switch", "Shutdown"), ("light", "Restart"), ("siren", "Lock"), ("switch", "Sleep")]
    states = [_thing(f"{domain}.desk_pc_{kind.lower()}", f"Desk PC {kind}") for domain, kind in things]
    states += [_thing("sensor.desk_pc_cpu", "Desk PC CPU"), _thing("button.desk_pc_monitorsleep", "Desk PC Monitor")]
    registry = _registry(*[_entry(f"{domain}.desk_pc_{kind.lower()}", "mqtt", "desk", kind) for domain, kind in things],
                         _entry("sensor.desk_pc_cpu", "mqtt", "desk", "CPU"),
                         _entry("button.desk_pc_monitorsleep", "mqtt", "desk", "MonitorSleep"), devices=DESK)
    for entity in ("switch.desk_pc_shutdown", "light.desk_pc_restart", "siren.desk_pc_lock", "switch.desk_pc_sleep"):
        assert ha_guard.guarded(entity, {}, registry) == (), entity
    assert ha_guard.guarded("sensor.desk_pc_cpu", {}, registry) is None
    assert ha_guard.guarded("button.desk_pc_monitorsleep", {}, registry) is None
    home = make_home({"/api/states": states, "/api/conversation/process": _assist("action_done", "Done")})
    home.snapshot = lambda: registry
    for request in ("turn on desk pc shutdown", "turn on desk pc restart", "turn on desk pc sleep"):
        assert home.command(request) == REFUSED_MACHINE, request
    assert "/api/conversation/process" not in _paths(home)


def test_locking_the_pc_is_no_way_past_a_guarded_name(make_home):
    # The PC's lock button is safe by its kind, so "lock my PC" still goes to Assist. But
    # a shutdown button the user has called "PC lock" is refused by that name, though
    # the words for locking the PC are let be.
    states, registry = _desk_pc("Lock", "Shutdown")
    states[1]["attributes"]["friendly_name"] = "PC lock"
    home = make_home({"/api/states": states, "/api/conversation/process": _assist("action_done", "Pressed")})
    home.snapshot = lambda: registry
    assert home.command("turn on the pc lock") == REFUSED_MACHINE
    states[1]["attributes"]["friendly_name"] = "Desk PC Shutdown"
    assert home.command("lock my PC")["success"] and _asked(home)["text"] == "lock my PC"


def test_nothing_goes_to_assist_when_whats_there_cant_be_read(make_home):
    home = make_home({"/api/states": requests.ConnectionError("refused"),
                      "/api/conversation/process": _assist("action_done", "Done")})
    assert home.command("turn on the lamp") == {"success": False, "message": "I can't reach Home Assistant right now."}
    assert [path for _, path, _ in home._session.requests] == ["/api/states"]


@pytest.mark.parametrize("attributes", [None, "door", ["device_class", "door"]])
def test_nothing_goes_to_assist_when_a_things_attributes_cant_be_read(make_home, attributes):
    # Taken for no attributes, a door cover would have no device class - and pass for a blind.
    patio = {"entity_id": "cover.patio", "state": "closed", "attributes": attributes}
    home = make_home({"/api/states": [patio], "/api/conversation/process": _assist("action_done", "Opened")})
    assert home.command("turn on the patio") == {"success": False, "message": "Home Assistant had a problem with that."}
    assert _paths(home) == ["/api/states"]


def test_a_guarded_name_said_in_other_words_is_still_refused(make_home):
    # Speech-to-text writes what it hears, so "PVE2 Shutdown" can come out as "pve 2
    # shutdown", and "Shutdown" as "shut down" - which Assist could still take for it.
    states = [_thing("button.desk_pc_shutdown", "nova Shutdown"), _thing("button.pve2_shutdown", "pve2 Shutdown"),
              _thing("cover.skylight", "Skylight", "window")]
    home = make_home({"/api/states": states, "/api/conversation/process": _assist("action_done", "Pressed")})
    home.registry.read = _registry(_entry("button.desk_pc_shutdown", "mqtt", "desk", "Shutdown"),
                                   _entry("button.pve2_shutdown", "proxmoxve"),
                                   devices={"desk": {"name": "Desk PC", "maker": "HASS.Agent Team"}})
    for request, refused in (("turn on pve 2 shutdown", REFUSED_MACHINE), ("turn on nova shut down", REFUSED_MACHINE),
                             ("turn on the bedroom win dows", REFUSED),
                             # What's left once the words for locking the PC are taken out, as before.
                             ("turn on nova lock my pc shut down", REFUSED_MACHINE)):
        assert home.command(request) == refused, request
    assert _paths(home) == ["/api/states"] * 4


def test_a_guarded_name_is_never_found_inside_a_word(make_home):
    home = make_home({"/api/states": [_thing("switch.ct_nova", "Nova")],
                      "/api/conversation/process": _assist("action_done", "Turned on the lamp")})
    home.registry.read = _registry(_entry("switch.ct_nova", "proxmoxve"))
    for request in ("turn on the casanova lamp", "turn on the novas lamp"):
        assert home.command(request)["success"], request
    assert _paths(home).count("/api/conversation/process") == 2
    assert home.command("turn on the casa nova lamp") == REFUSED_MACHINE


# Asking Assist what a sentence would act on, before it acts - and failing shut without that

# What command() answers when it won't ask Assist to act, as it can't tell what Assist would act on.
DIDNT_UNDERSTAND = {"success": False, "message": "Home Assistant didn't understand that.", "not_understood": True}

TV = _thing("switch.tv", "TV")
LAMP = _thing("light.lamp", "Lamp")
MAIN_LOCK = _thing("lock.main", "Main lock")


def _match(intent, *targets, unmatched=()):
    """
    One sentence's match, as Assist's sentence debugging gives it, e.g. _match("HassTurnOff",
    "switch.tv"). Those in unmatched are marked "matched": false, as a question marks those
    not in the state it asks about.
    """
    return {"intent": {"name": intent}, "slots": {}, "details": {},
            "targets": {target: {"matched": target not in unmatched} for target in targets}, "match": True,
            "sentence_template": "[<turn>] {name} [on|off]", "unmatched_slots": {}, "source": "builtin"}


def _debugged(found):
    """The debugging's whole answer for one sentence: its match, or None where it matched nothing."""
    return {"results": [found]}


def _assisted(make_home, states, entries, found):
    """A house with these states, a registry of these entries, and the debugging answering with found."""
    home = make_home({"/api/states": states, "/api/conversation/process": _assist("action_done", "Done")})
    home.registry.read = _registry(*entries)
    home.socket.answer = _debugged(found)
    return home


def test_assist_is_asked_what_a_sentence_would_act_on_before_acting(make_home):
    home = _assisted(make_home, [TV], [_entry("switch.tv")], _match("HassTurnOff", "switch.tv"))
    home.device_id = "da94"
    assert home.command("turn off the TV", wider=True) == {"success": True, "message": "Done", "chime": True}
    (messages, before), = home.socket.calls
    assert messages == [{"type": "conversation/agent/homeassistant/debug", "sentences": ["turn off the TV"],
                         "language": "en", "device_id": "da94"}]
    assert _paths(home)[:before] == ["/api/states"] and _paths(home)[before:] == ["/api/conversation/process"]
    assert _asked(home)["device_id"] == "da94"

    # Without a device, neither is told one.
    home = _assisted(make_home, [TV], [_entry("switch.tv")], _match("HassTurnOff", "switch.tv"))
    home.command("turn off the TV", wider=True)
    assert "device_id" not in home.socket.calls[0][0][0] and "device_id" not in _asked(home)


def test_the_debugging_and_assist_are_told_the_same_device(make_home):
    # Nova's device can be found, or lost, while a request is under way - here as the
    # registry's looked at.
    home = _assisted(make_home, [TV], [_entry("switch.tv")], _match("HassTurnOff", "switch.tv"))
    home.device_id = "da94"
    snapshot = home.registry.read

    def found_elsewhere():
        home.device_id = "ab12"
        return snapshot

    home.snapshot = found_elsewhere
    assert home.command("turn off the TV", wider=True)["success"]
    assert home.socket.calls[0][0][0]["device_id"] == _asked(home)["device_id"] == "da94"


def test_assists_answer_is_read_as_what_it_would_act_on(make_home):
    home = _assisted(make_home, [TV], [_entry("switch.tv")], _match("HassTurnOff", "switch.tv"))
    assert home._assist_targets("turn off the TV") == (
        home_assistant.AssistMatch(True, "HassTurnOff", ["switch.tv"]), None)
    home.socket.answer = _debugged(None)
    assert home._assist_targets("turn off the toaster") == (home_assistant.AssistMatch(False, None, []), None)
    home.socket.answer = HomeSocketError("timed out")
    match, why = home._assist_targets("turn off the TV")
    assert match is None and "timed out" in why


def test_a_lock_exposed_as_front_door_is_never_reached_either_way(make_home):
    # 0008 decision 14: "There's a test for each."
    entries = [_entry("lock.main", aliases=["front door"])]
    home = _assisted(make_home, [MAIN_LOCK], entries, _match("HassTurnOff", "lock.main"))
    assert home.command("Turn off the front door", wider=True) == REFUSED
    home.registry.read = None
    assert home.command("Turn off the front door", wider=True) == DIDNT_UNDERSTAND
    home.registry.read = _registry(*entries)
    home.socket.answer = HomeSocketError("not answering")
    assert home.command("Turn off the front door", wider=True) == REFUSED
    # Nor as a light sentence, with neither answering.
    home.registry.read = None
    assert home.command("Turn off the front door") == REFUSED
    assert "/api/conversation/process" not in _paths(home)


def test_a_guarded_target_assist_would_act_on_is_refused(make_home):
    # The registry was read before the lock was given the other name "the shed", which
    # Assist knows already.
    home = _assisted(make_home, [MAIN_LOCK], [_entry("lock.main")], _match("HassTurnOff", "lock.main"))
    assert home.command("turn off the shed", wider=True) == REFUSED
    assert home.command("turn off the shed") == REFUSED
    assert "/api/conversation/process" not in _paths(home)


def test_one_guarded_target_among_many_refuses_the_lot(make_home):
    bedroom = _thing("light.bedroom_light", "Bedroom Light")
    home = _assisted(make_home, [LAMP, bedroom, MAIN_LOCK],
                     [_entry("light.lamp"), _entry("light.bedroom_light"), _entry("lock.main")],
                     _match("HassTurnOff", "light.lamp", "light.bedroom_light", "lock.main"))
    assert home.command("turn off the bedroom", wider=True) == REFUSED
    assert "/api/conversation/process" not in _paths(home)


def test_every_target_counts_whether_its_marked_matched_or_not(make_home):
    # "matched": false is only how a question marks a state - it's still a target.
    home = _assisted(make_home, [LAMP, MAIN_LOCK], [_entry("light.lamp"), _entry("lock.main")],
                     _match("HassTurnOff", "light.lamp", "lock.main", unmatched=("lock.main",)))
    assert home.command("turn off the bedroom", wider=True) == REFUSED
    assert "/api/conversation/process" not in _paths(home)


def test_a_target_missing_from_the_states_is_refused(make_home):
    # Added since the states were read a moment ago, so what it is can't be checked.
    home = _assisted(make_home, [LAMP], [_entry("light.lamp")], _match("HassTurnOn", "light.lamp", "lock.shed"))
    assert home.command("turn on the shed", wider=True) == REFUSED
    assert "/api/conversation/process" not in _paths(home)


def test_a_button_the_registry_cant_vouch_for_is_refused(make_home):
    # A new Proxmox or HASS.Agent button is in the states at once, but in the registry
    # only up to five minutes later - and both only ever act through buttons.
    chime, new = _thing("button.hall_chime", "Hall chime"), _thing("button.pve3_shutdown", "pve3 Shutdown")
    home = _assisted(make_home, [chime, new], [_entry("button.hall_chime")],
                     _match("HassTurnOn", "button.pve3_shutdown"))
    assert home.command("turn on pve3 shutdown", wider=True) == REFUSED
    assert "/api/conversation/process" not in _paths(home)
    # One the registry knows, of a harmless integration, is pressed.
    home.socket.answer = _debugged(_match("HassTurnOn", "button.hall_chime"))
    assert home.command("turn on the hall chime", wider=True)["success"]
    # With no registry to go by, no button is vouched for, even for a light sentence.
    home.registry.read = None
    assert home.command("turn on the hall chime") == REFUSED


@pytest.mark.parametrize("entity", ["switch.pve3_vm_101", "update.pve3", "light.desk_pc_restart", "cover.new",
                                    "siren.desk_pc_shutdown", "number.new", "select.new"])
def test_anything_acting_the_registry_cant_vouch_for_is_refused(make_home, entity):
    # Ruling 104, widened: whatever Proxmox or HASS.Agent adds is in the states at once,
    # but in the registry only up to five minutes later - and either can act through a
    # switch or an update as well as a button.
    home = _assisted(make_home, [_thing(entity, "New"), _thing("sensor.new", "New")], [],
                     _match("HassTurnOn", entity))
    assert home.command("turn on new", wider=True) == REFUSED
    assert "/api/conversation/process" not in _paths(home)
    # What only says how something is can still be asked about.
    home.socket.answer = _debugged(_match("HassGetState", "sensor.new"))
    assert home.command("is new on", wider=True)["success"]


def test_the_refusal_says_why_the_registry_cant_vouch(make_home, caplog):
    # A YAML template switch with no unique id is never in the registry, so it's refused
    # for good - and the log says what to do about it.
    home = _assisted(make_home, [_thing("switch.heater", "Heater"), LAMP], [_entry("light.lamp")],
                     _match("HassTurnOn", "switch.heater"))
    with caplog.at_level("WARNING", logger="voice_assistant.home_assistant"):
        assert home.command("turn on the heater", wider=True) == REFUSED
    assert caplog.records[-1].message == (
        "Refused a Home Assistant request Assist would act on ['switch.heater'] (switch.heater is not in Home "
        "Assistant's registry - give it a unique id to work it by voice) with: 'turn on the heater'")
    # Without a read of the registry at all, it's that which is said instead.
    home.registry.read = None
    home.socket.answer = _debugged(_match("HassTurnOn", "light.lamp"))
    caplog.clear()
    with caplog.at_level("WARNING", logger="voice_assistant.home_assistant"):
        assert home.command("turn on the lamp") == REFUSED
    message = caplog.records[-1].message
    assert "light.lamp is not vouched for while Home Assistant's registry can't be read" in message
    assert "unique id" not in message


def test_a_group_assist_would_act_on_is_refused_for_a_guarded_member(make_home):
    # The final review's repro: a cover group holding the garage door and a blind is one
    # target, and Home Assistant opens every member.
    garage, blind = _thing("cover.garage", "Garage", "garage"), _thing("cover.blind", "Blind", "blind")
    group = _scene("cover.front", "Front of house", ["cover.garage", "cover.blind"])
    home = _assisted(make_home, [group, garage, blind],
                     [_entry("cover.front", "group"), _entry("cover.garage"), _entry("cover.blind")],
                     _match("HassTurnOn", "cover.front"))
    assert home.command("open front of house", wider=True) == REFUSED
    # So is a light group with a lock among its members, as an old-style group can be.
    porch = _scene("light.porch_group", "Porch", ["light.lamp", "lock.main"])
    home = _assisted(make_home, [porch, LAMP, MAIN_LOCK],
                     [_entry("light.porch_group", "group"), _entry("light.lamp"), _entry("lock.main")],
                     _match("HassTurnOn", "light.porch_group"))
    assert home.command("turn on the porch") == REFUSED
    # And one named in a light sentence while the debugging isn't answering.
    home.socket.answer = HomeSocketError("not answering")
    assert home.command("turn on the porch") == REFUSED
    assert "/api/conversation/process" not in _paths(home)
    # With only harmless members, it's sent.
    porch["attributes"]["entity_id"] = ["light.lamp"]
    assert home.command("turn on the porch")["success"]
    home.socket.answer = _debugged(_match("HassTurnOn", "light.porch_group"))
    assert home.command("turn on the porch")["success"]


@pytest.mark.parametrize("wider", [True, False])
def test_a_sentence_assist_matches_nothing_in_isnt_sent(make_home, wider):
    home = _assisted(make_home, [LAMP], [_entry("light.lamp")], None)
    assert home.command("turn on the attic lights", wider=wider) == DIDNT_UNDERSTAND
    assert "/api/conversation/process" not in _paths(home)


@pytest.mark.parametrize("targets", [(), ("switch.tv",)])
@pytest.mark.parametrize("match", [{"match": False}, {}])
@pytest.mark.parametrize("wider", [True, False])
def test_a_near_miss_isnt_a_match(make_home, targets, match, wider):
    # Found live: "pause the tv", with no media player called TV, fits Assist's "pause
    # {name}" - so it's answered in full, with the name it couldn't find left unmatched
    # and "match": false. A near miss can carry targets too. One saying nothing of a
    # match isn't taken for one either.
    near = {**_match("HassMediaPause", *targets), "unmatched_slots": {"name": "tv"}}
    near.pop("match")
    home = _assisted(make_home, [TV], [_entry("switch.tv")], {**near, **match})
    assert home._assist_targets("pause the tv") == (home_assistant.AssistMatch(False, None, []), None)
    assert home.command("pause the tv", wider=wider) == DIDNT_UNDERSTAND
    assert "/api/conversation/process" not in _paths(home)


def test_only_the_intents_asked_for_are_sent(make_home):
    shopping = _thing("todo.shopping", "Shopping")
    home = _assisted(make_home, [shopping], [_entry("todo.shopping")], _match("HassListRemoveItem", "todo.shopping"))
    assert home.command("milk off the shopping list", wider=True, only=("HassListAddItem",)) == DIDNT_UNDERSTAND
    assert "/api/conversation/process" not in _paths(home)
    home.socket.answer = _debugged(_match("HassListAddItem", "todo.shopping"))
    assert home.command("add milk to the shopping list", wider=True, only=("HassListAddItem",))["success"]


def test_a_match_with_no_targets_isnt_sent_on_the_wider_rule(make_home):
    # As Assist matches a script which isn't exposed: the guard can't see what it would
    # act on.
    home = _assisted(make_home, [_thing("script.goodnight", "Goodnight")], [_entry("script.goodnight")],
                     _match("HassTurnOn"))
    assert home.command("run goodnight", wider=True) == DIDNT_UNDERSTAND
    home.socket.answer = _debugged(_match("HassListAddItem"))
    assert home.command("add milk to notes", wider=True, only=("HassListAddItem",)) == DIDNT_UNDERSTAND
    # Nor is a light sentence - here for a script "Goodnight lights" which isn't exposed:
    # every light it means is a target, by its room or its name.
    home.socket.answer = _debugged(_match("HassTurnOn"))
    assert home.command("turn on goodnight lights") == DIDNT_UNDERSTAND
    assert "/api/conversation/process" not in _paths(home)


# How Home Assistant's sentence debugging answers an automation's own sentence trigger.
TRIGGER = {"match": True, "source": "trigger", "sentence_template": "turn off the lights"}


def _wider_rule_said(caplog):
    """What's been logged about the wider rule."""
    return [record.message for record in caplog.records if "wider rule" in record.message]


@pytest.mark.parametrize("wider", [True, False])
def test_an_automations_own_sentence_is_never_sent(make_home, caplog, wider):
    # It's answered without an intent or targets, as what the automation does can't be
    # told without running it - which could be anything, such as closing the garage as
    # it turns off the lights. The debugging did answer, though, so the wider rule's on.
    home = _assisted(make_home, [LAMP], [_entry("light.lamp")], TRIGGER)
    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        assert home.command("turn off the lights", wider=wider) == DIDNT_UNDERSTAND
    assert "/api/conversation/process" not in _paths(home)
    assert _wider_rule_said(caplog) == []
    assert home._assist_targets("turn off the lights") == (home_assistant.AssistMatch(True, None, []), None)


@pytest.mark.parametrize("found", [
    {"match": True},
    {"match": True, "intent": "HassTurnOn", "targets": {}},
    {"match": True, "intent": {"name": None}, "targets": {"light.lamp": {"matched": True}}},
    {"match": True, "intent": {"name": ""}, "targets": {"light.lamp": {"matched": True}}},
    {"match": True, "intent": {"name": "HassTurnOn"}, "targets": ["light.lamp"]},
])
@pytest.mark.parametrize("wider", [True, False])
def test_a_match_that_cant_be_read_is_never_sent(make_home, caplog, found, wider):
    # A match it can't see the intent or targets of is a match all the same, so the
    # debugging answered and the wider rule stays on - but nothing is sent.
    home = _assisted(make_home, [LAMP], [_entry("light.lamp")], found)
    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        assert home.command("turn on the lamp", wider=wider) == DIDNT_UNDERSTAND
    assert "/api/conversation/process" not in _paths(home)
    assert _wider_rule_said(caplog) == []
    assert [record for record in caplog.records if record.levelname == "WARNING" and "can't be read" in record.message]
    assert home._assist_targets("turn on the lamp") == (home_assistant.AssistMatch(True, None, []), None)


def test_the_wider_rule_is_off_without_the_registry(make_home):
    home = _assisted(make_home, [TV, LAMP], [], _match("HassTurnOn", "light.lamp"))
    home.registry.read = None
    assert home.command("turn off the TV", wider=True) == DIDNT_UNDERSTAND
    assert _paths(home) == ["/api/states"] and home.socket.calls == []
    # Nor is a light sentence given Assist to act on anything, as without the registry
    # nothing which acts is vouched for - Nova's own lights do it instead (see
    # test_a_refused_light_sentence_is_done_by_novas_own_lights).
    assert home.command("turn on the lamp") == REFUSED
    # With the debugging not answering either, there's nothing it would act on to vouch
    # for, and a light sentence goes to Assist with the checks of what it names.
    home.socket.answer = HomeSocketError("not answering")
    assert home.command("turn on the lamp")["success"] and _asked(home)["text"] == "turn on the lamp"


@pytest.mark.parametrize("answer", [HomeSocketError("timed out"), {"results": []}, {"results": [True]},
                                    {"results": ["match"]}, {"results": "no"}, {}, [], "odd"])
def test_the_wider_rule_is_off_when_assist_cant_be_asked_what_it_would_do(make_home, answer):
    home = _assisted(make_home, [TV, LAMP], [_entry("switch.tv"), _entry("light.lamp")], None)
    home.socket.answer = answer
    match, why = home._assist_targets("turn off the TV")
    assert match is None and why
    assert home.command("turn off the TV", wider=True) == DIDNT_UNDERSTAND
    assert "/api/conversation/process" not in _paths(home)
    assert home.command("turn on the lamp")["success"] and _asked(home)["text"] == "turn on the lamp"


def test_the_debugging_only_speaks_for_home_assistants_own_agent(make_home):
    home = _assisted(make_home, [TV, LAMP], [_entry("switch.tv"), _entry("light.lamp")],
                     _match("HassTurnOff", "switch.tv"))
    home.agent = "conversation.some_language_model"
    assert home.command("turn off the TV", wider=True) == DIDNT_UNDERSTAND
    assert home.command("turn on the lamp")["success"]
    assert home.socket.calls == [] and _asked(home)["agent_id"] == "conversation.some_language_model"


def test_the_wider_rule_comes_back_once_both_answer(make_home, caplog):
    home = _assisted(make_home, [LAMP], [_entry("light.lamp")], _match("HassTurnOn", "light.lamp"))

    def said():
        return [(record.levelname, record.message) for record in caplog.records if "wider rule" in record.message]

    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        # On to begin with, which isn't said.
        home.command("turn on the lamp")
        assert said() == []

        home.registry.read = None
        home.command("turn on the lamp")
        home.command("turn off the TV", wider=True)
        (level, message), = said()
        assert level == "WARNING" and "registry" in message

        # Still off, with the debugging not answering now.
        home.registry.read = _registry(_entry("light.lamp"))
        home.socket.answer = HomeSocketError("timed out")
        home.command("turn on the lamp")
        assert len(said()) == 1

        home.socket.answer = _debugged(_match("HassTurnOn", "light.lamp"))
        home.command("turn on the lamp")
        home.command("turn on the lamp")
        assert [level for level, _ in said()] == ["WARNING", "INFO"]

        home.socket.answer = HomeSocketError("timed out")
        home.command("turn on the lamp")
        level, message = said()[-1]
        assert len(said()) == 3 and level == "WARNING" and "debugging" in message and "timed out" in message


@pytest.mark.parametrize("domain, service, data", [
    ("lock", "unlock", {"entity_id": "lock.front_door"}),
    ("cover", "open_cover", {"entity_id": "cover.garage"}),
    ("homeassistant", "turn_on", {"entity_id": "lock.front_door"}),
    ("light", "turn_on", {"entity_id": "lock.front_door"}),
    ("light", "turn_on", {"entity_id": ["light.lamp", "lock.front_door"]}),
    ("light", "turn_on", {"device_id": "da94"}),
    ("light", "turn_on", {}),
    ("script", "turn_on", {"area_id": "hall"}),
    ("button", "press", {"entity_id": "button.bedroom_pve1_shut_down"}),
])
def test_only_the_services_nova_calls_are_ever_called(make_home, domain, service, data):
    home = make_home({f"/api/services/{domain}/{service}": []})
    with pytest.raises(NotAllowed):
        home._call_service(domain, service, data)
    assert home._session.requests == []


def test_every_service_call_goes_through_the_one_allowlist():
    # So nothing calls a service which hasn't been checked.
    assert inspect.getsource(home_assistant).count("/api/services/") == 1


# The weather, as live check 3 found Met's through Home Assistant.
FORECAST_HOME = {"entity_id": "weather.forecast_home", "state": "partlycloudy",
                 "attributes": {"temperature": 12.3, "humidity": 80, "friendly_name": "Forecast Home"},
                 "last_changed": "2026-10-24T09:00:00+00:00", "last_reported": "2026-10-24T12:30:00+00:00",
                 "last_updated": "2026-10-24T12:00:00+00:00"}
HOURLY = [{"condition": "rainy", "datetime": "2026-10-24T15:00:00+00:00", "humidity": 90, "precipitation": 0.4,
           "temperature": 11.0, "uv_index": 0.3, "wind_bearing": 220, "wind_speed": 25.2, "cloud_coverage": 100}]
GET_FORECASTS = "/api/services/weather/get_forecasts?return_response"


def test_the_forecast_is_asked_for_its_answer_through_the_allowlist(make_home):
    home = make_home({GET_FORECASTS: {"changed_states": [],
                                      "service_response": {"weather.forecast_home": {"forecast": HOURLY}}}})
    assert home.forecasts("weather.forecast_home", "hourly") == HOURLY
    assert home._session.requests == [
        ("POST", GET_FORECASTS, {"json": {"entity_id": "weather.forecast_home", "type": "hourly"}})]



@pytest.mark.parametrize("domain, service, data, returns", [
    # Only what's meant to answer may be asked for an answer...
    ("light", "turn_on", {"entity_id": "light.lamp"}, True),
    # ...and only with what it's given to act on...
    ("weather", "get_forecasts", {"entity_id": "light.lamp", "type": "daily"}, True),
    ("weather", "get_forecasts", {"type": "daily"}, True),
    ("todo", "get_items", {"entity_id": "light.lamp"}, True),
    # ...nor is what answers called without asking for its answer.
    ("weather", "get_forecasts", {"entity_id": "weather.forecast_home", "type": "daily"}, False),
    ("todo", "get_items", {"entity_id": "todo.shopping_list"}, False),
])
def test_an_answer_is_only_asked_of_what_both_lists_allow(make_home, domain, service, data, returns):
    home = make_home({f"/api/services/{domain}/{service}" + ("?return_response" if returns else ""): {}})
    with pytest.raises(NotAllowed):
        home._call_service(domain, service, data, returns=returns)
    assert home._session.requests == []


def test_returns_never_lets_through_what_services_doesnt(make_home, monkeypatch):
    monkeypatch.setattr(home_assistant, "RETURNS", home_assistant.RETURNS | {("lock", "get_codes")})
    home = make_home({"/api/services/lock/get_codes?return_response": {}})
    with pytest.raises(NotAllowed):
        home._call_service("lock", "get_codes", {"entity_id": "lock.front_door"}, returns=True)
    assert home._session.requests == []


def test_the_weather_entities_are_read_with_when_each_was_last_reported(make_home):
    no_report = {**FORECAST_HOME, "entity_id": "weather.other_place", "attributes": {}}
    del no_report["last_reported"]
    home = make_home({"/api/states": [_thing("light.lamp", "Lamp"), FORECAST_HOME, no_report]})
    assert home.weather_entities() == [
        {"id": "weather.forecast_home", "name": "Forecast Home", "state": "partlycloudy",
         "attributes": FORECAST_HOME["attributes"],
         "reported": datetime.fromisoformat("2026-10-24T12:30:00+00:00").timestamp()},
        # Without last_reported, last_updated says when.
        {"id": "weather.other_place", "name": "weather.other_place", "state": "partlycloudy", "attributes": {},
         "reported": datetime.fromisoformat("2026-10-24T12:00:00+00:00").timestamp()}]


# The to-do lists, as the live checks found them: each list's state is how many things are
# still to do on it.
SHOPPING_LIST = {"entity_id": "todo.shopping_list", "state": "3",
                 "attributes": {"friendly_name": "Shopping list", "supported_features": 15}}
GET_ITEMS = "/api/services/todo/get_items?return_response"


def test_the_todo_lists_are_read_with_their_other_names(make_home):
    notepad = {"entity_id": "todo.notepad", "state": "unavailable", "attributes": {"friendly_name": "notepad"}}
    home = make_home({"/api/states": [_thing("light.lamp", "Lamp"), SHOPPING_LIST, notepad]})
    home.registry.read = _registry(_entry("todo.shopping_list", aliases=("groceries",)))
    assert home.todo_lists() == [
        {"id": "todo.shopping_list", "name": "Shopping list", "count": 3, "aliases": ["groceries"]},
        {"id": "todo.notepad", "name": "notepad", "count": None, "aliases": []}]


def test_a_lists_count_which_isnt_a_plain_number_is_none(make_home):
    # "²" is a digit to str.isdigit, but not a number int() reads.
    odd = {"entity_id": "todo.odd", "state": "²", "attributes": {"friendly_name": "Odd"}}
    home = make_home({"/api/states": [odd]})
    assert home.todo_lists() == [{"id": "todo.odd", "name": "Odd", "count": None, "aliases": []}]


def test_a_lists_things_still_to_do_are_asked_for_through_the_allowlist(make_home):
    items = [{"status": "needs_action", "summary": "milk", "uid": "a1"},
             {"status": "needs_action", "summary": "oat milk", "uid": "b2"}]
    home = make_home({GET_ITEMS: {"changed_states": [],
                                  "service_response": {"todo.shopping_list": {"items": items}}}})
    assert home.list_items("todo.shopping_list") == [{"summary": "milk", "uid": "a1"},
                                                     {"summary": "oat milk", "uid": "b2"}]
    assert home._session.requests == [
        ("POST", GET_ITEMS, {"json": {"entity_id": "todo.shopping_list", "status": ["needs_action"]}})]

    home = make_home({GET_ITEMS: {"changed_states": [], "service_response": {}}})
    with pytest.raises(ValueError):
        home.list_items("todo.shopping_list")


def test_a_thing_is_added_to_a_list_and_nothing_else(make_home):
    home = make_home({"/api/services/todo/add_item": []})
    home.add_item("todo.shopping_list", "milk")
    assert home._session.requests == [
        ("POST", "/api/services/todo/add_item", {"json": {"entity_id": "todo.shopping_list", "item": "milk"}})]
    with pytest.raises(NotAllowed):
        home.add_item("light.lamp", "milk")


def test_a_thing_is_ticked_off_not_deleted(make_home):
    home = make_home({"/api/services/todo/update_item": [], "/api/services/todo/remove_item": [],
                      "/api/services/todo/remove_completed_items": []})
    home.tick_off("todo.shopping_list", "a1")
    assert home._session.requests == [
        ("POST", "/api/services/todo/update_item",
         {"json": {"entity_id": "todo.shopping_list", "item": "a1", "status": "completed"}})]
    for service in ("remove_item", "remove_completed_items"):
        with pytest.raises(NotAllowed):
            home._call_service("todo", service, {"entity_id": "todo.shopping_list", "item": "a1"})
    assert len(home._session.requests) == 1


@pytest.mark.parametrize("data", [
    {"rename": "beer"}, {"status": "needs_action"}, {}, {"status": "completed", "rename": "beer"},
    {"status": "completed", "description": "and some bread"}, {"status": "completed", "due_date": "2026-10-25"},
])
def test_updating_a_thing_only_ever_ticks_it_off(make_home, data):
    # Renaming it or putting it back to do is never Nova's - only completing it.
    home = make_home({"/api/services/todo/update_item": []})
    with pytest.raises(NotAllowed):
        home._call_service("todo", "update_item", {"entity_id": "todo.shopping_list", "item": "a1", **data})
    assert home._session.requests == []


@pytest.mark.parametrize("failure, said", [
    (requests.ConnectionError("refused"), "I can't reach Home Assistant right now."),
    (FakeResponse({}, status=401), "HA_TOKEN in the .env file needs checking"),
])
def test_a_failed_request_says_what_went_wrong(make_home, failure, said):
    home = make_home({"/api/conversation/process": failure})
    result = home.command("turn on the lights")
    assert result["success"] is False and said in result["message"]


@pytest.mark.parametrize("failure", [requests.ConnectionError("refused"), requests.Timeout("slow")])
def test_any_sentence_of_assists_goes_to_claude_when_home_assistant_cant_be_reached(make_home, failure):
    # "Is the post office open?" is caught by Assist's shapes too - Claude still gets it,
    # as it does with the wider rule off. A light sentence still says why.
    home = make_home({"/api/states": failure})
    assert home.command("is the post office open", wider=True) == DIDNT_UNDERSTAND
    assert home.command("turn on the lamp") == {"success": False, "message": "I can't reach Home Assistant right now."}


@pytest.mark.parametrize("failure, to_claude", [
    (requests.ReadTimeout("slow"), False), (requests.ConnectionError("aborted"), False),
    (requests.ConnectTimeout("slow to connect"), True)])
def test_a_sentence_assist_may_have_acted_on_isnt_given_to_claude(make_home, failure, to_claude):
    # Once it's been sent, Assist may have run "run goodnight" with no answer back in
    # time - and Claude's run_home_script would run it again. Only a connection never
    # made can't have reached Assist.
    home = _assisted(make_home, [TV], [_entry("switch.tv")], _match("HassTurnOff", "switch.tv"))
    home._session.answers["/api/conversation/process"] = failure
    result = home.command("turn off the tv", wider=True)
    assert result == (DIDNT_UNDERSTAND if to_claude else {"success": False, "message": "I can't reach Home Assistant "
                                                                                       "right now."})


def test_a_sentence_never_sent_for_want_of_a_network_is_given_to_claude(make_home, monkeypatch):
    home = _assisted(make_home, [TV], [_entry("switch.tv")], _match("HassTurnOff", "switch.tv"))
    # The network goes once the states are read, so the sentence is never sent.
    monkeypatch.setattr(network, "online", lambda: not home._session.requests)
    assert home.command("turn off the tv", wider=True) == DIDNT_UNDERSTAND
    assert _paths(home) == ["/api/states"]


def test_a_token_home_assistant_refuses_is_said_for_any_sentence(make_home):
    # Home Assistant answered, so the user is told what needs putting right.
    home = make_home({"/api/states": FakeResponse({}, status=401)})
    result = home.command("pause the tv", wider=True)
    assert result["success"] is False and "HA_TOKEN in the .env file needs checking" in result["message"]
    assert "not_understood" not in result


def test_with_no_network_it_isnt_asked_at_all(make_home, monkeypatch):
    # With the Pi's cable out, "turn on the lamp" waited 20 seconds for Home
    # Assistant's name to fail to be looked up.
    monkeypatch.setattr(network, "online", lambda: False)
    home = make_home({})
    assert home.command("turn on the lamp")["message"] == "I can't reach Home Assistant right now."
    assert home._session.requests == []


def test_without_an_address_and_a_token_its_off(make_config):
    assert HomeAssistant.from_config(make_config(), "token") is None
    assert HomeAssistant.from_config(make_config(home_assistant={"url": "http://ha:8123"}), None) is None
    home = HomeAssistant.from_config(make_config(home_assistant={"url": "http://ha:8123"}), "token")
    assert home.url == "http://ha:8123" and home._session.headers["Authorization"] == "Bearer token"


# The settings page

STATES = [
    {"entity_id": "script.movie_night", "state": "off", "attributes": {"friendly_name": "Movie night"}},
    {"entity_id": "scene.relax", "state": "2026-10-20T21:00:00",
     "attributes": {"friendly_name": "Relax", "entity_id": ["light.kitchen"]}},
    {"entity_id": "script.open_garage", "state": "off", "attributes": {"friendly_name": "Garage"}},
    {"entity_id": "script.leaving", "state": "off", "attributes": {"friendly_name": "Unlock the front door"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"}},
    {"entity_id": "switch.adguard_protection", "state": "on", "attributes": {"friendly_name": "AdGuard protection"}},
]

# What Home Assistant's script editor has for Movie night, under the registry's key for it.
MOVIE_NIGHT = {"/api/config/script/config/movie_night": {
    "alias": "Movie night", "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.kitchen"}}]}}


def _page_house(make_home, answers):
    """A house with STATES, every one of them in the registry, and what Movie night does - as run() checks it."""
    home = make_home({"/api/states": STATES, **MOVIE_NIGHT, **answers})
    home.registry.read = _registry(*[_entry(state["entity_id"]) for state in STATES])
    return home


def test_the_page_is_shown_the_lights_and_only_whats_safe_to_run(make_home):
    # The lights come from the template, with their areas; the scripts and scenes from the states.
    lights = _rendered(
        _rendered_light("light.kitchen", "Kitchen", "Kitchen", brightness=128, supported_color_modes=["color_temp"],
                        color_mode="color_temp", color_temp_kelvin=3000),
        _rendered_light("light.porch", "Porch", "Hall", state="off", supported_color_modes=["onoff"]),
        _rendered_light("light.garage", "Garage light", None, state="unavailable", supported_color_modes=None))
    overview = _page_house(make_home, {"/api/template": lights}).overview()

    assert [light["name"] for light in overview["lights"]] == ["Porch", "Kitchen", "Garage light"]
    assert overview["lights"][1] == {"id": "light.kitchen", "name": "Kitchen", "area": "Kitchen", "on": True,
                                     "reachable": True, "dimmable": True, "brightness": 50,
                                     "colour_modes": ["color_temp"], "hue": None, "saturation": None, "kelvin": 3000,
                                     "min_kelvin": 2500, "max_kelvin": 6500, "aliases": []}
    assert not overview["lights"][0]["dimmable"] and not overview["lights"][2]["reachable"]
    # A light can't let anyone in, whatever it's called; a script or scene might, and
    # nothing else is offered at all.
    assert overview["routines"] == [
        {"id": "script.movie_night", "name": "Movie night", "kind": "script", "by_voice": True},
        {"id": "scene.relax", "name": "Relax", "kind": "scene", "by_voice": True}]


def test_the_page_lists_novas_own_room_first_then_the_rest_by_room_and_name(make_home):
    home = make_home({"/api/states": [], "/api/template": _rendered(
        _rendered_light("light.zed", "Zed", "Bedroom"), _rendered_light("light.hall", "Hall", "Hall"),
        _rendered_light("light.loft", "Loft", None), _rendered_light("light.bed", "Bed", "Bedroom"),
        _rendered_light("light.attic", "Attic lamp", "Attic"))})

    home.area = "Bedroom"
    assert [light["name"] for light in home.overview()["lights"]] == ["Bed", "Zed", "Attic lamp", "Hall", "Loft"]
    # Without a room of its own, none comes first - and the lights in no room still come last.
    home.area = None
    assert [light["name"] for light in home.overview()["lights"]] == ["Attic lamp", "Bed", "Zed", "Hall", "Loft"]


@pytest.mark.parametrize("down", [{"/api/states": requests.ConnectionError("down")},
                                  {"/api/states": [], "/api/template": requests.ConnectionError("down")}])
def test_the_page_is_told_when_home_assistant_cant_be_reached(make_home, down):
    assert make_home(down).overview() == {"success": False, "message": "I can't reach Home Assistant right now."}


@pytest.mark.parametrize("status", [401, 403])
def test_without_an_administrators_token_the_page_still_has_the_lights(make_home, status):
    # Home Assistant only renders templates for an administrator, which Nova's user
    # needn't be - so the lights come from the states instead, as they did before
    # Nova had rooms, just without theirs.
    kitchen = {"entity_id": "light.kitchen", "state": "on",
               "attributes": {"friendly_name": "Kitchen", "brightness": 128, "supported_color_modes": ["color_temp"],
                              "color_mode": "color_temp", "color_temp_kelvin": 3000, "min_color_temp_kelvin": 2500,
                              "max_color_temp_kelvin": 6500, "icon": "mdi:ceiling-light"}}
    home = _page_house(make_home, {"/api/states": STATES + [kitchen], "/api/template": FakeResponse({}, status=status)})
    home.registry.read = _registry(*home.registry.read.entries.values(), _entry("light.kitchen"))
    overview = home.overview()

    assert overview["lights"] == [{"id": "light.kitchen", "name": "Kitchen", "area": None, "on": True,
                                   "reachable": True, "dimmable": True, "brightness": 50,
                                   "colour_modes": ["color_temp"], "hue": None, "saturation": None, "kelvin": 3000,
                                   "min_kelvin": 2500, "max_kelvin": 6500, "aliases": []}]
    assert [routine["id"] for routine in overview["routines"]] == ["script.movie_night", "scene.relax"]


def test_the_page_is_told_when_home_assistant_fails_at_the_lights_otherwise(make_home):
    home = make_home({"/api/states": [], "/api/template": FakeResponse({}, status=500)})
    assert home.overview() == {"success": False, "message": "Home Assistant had a problem with that."}


@pytest.mark.parametrize("status", [401, 403])
def test_a_token_which_isnt_an_administrators_is_said_once(make_home, caplog, status):
    # Nova's device is looked for every half a minute, and the lights read for Claude
    # with every request, so a line each time would bury the one which says what to do.
    home = make_home({"/api/states": [], "/api/template": FakeResponse({}, status=status)})
    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        for _ in range(3):
            assert home.find_device("nova_home_assistant") is False
            assert home.describe_lights() == ""
            home.overview()
        assert len(caplog.records) == 1 and caplog.records[0].levelname == "WARNING"
        assert "administrator" in caplog.records[0].message

        # Made an administrator, the same token works - and if that's undone, it's said again.
        home._session.answers["/api/template"] = FakeResponse("da94|Bedroom")
        assert home.find_device("nova_home_assistant") is True
        home._session.answers["/api/template"] = FakeResponse({}, status=status)
        home.describe_lights()
        home.describe_lights()
    assert sum("administrator" in message for message in caplog.messages) == 2


def test_a_light_is_switched_and_dimmed_by_its_own_id(make_home):
    home = make_home({"/api/services/light/turn_on": [], "/api/services/light/turn_off": []})
    assert home.set_light("light.kitchen", on=False)["success"]
    assert home.set_light("light.kitchen", brightness=30)["success"]
    assert [(path, sent["json"]) for _, path, sent in home._session.requests] == [
        ("/api/services/light/turn_off", {"entity_id": "light.kitchen"}),
        ("/api/services/light/turn_on", {"entity_id": "light.kitchen", "brightness_pct": 30})]


def test_a_light_is_set_to_a_colour(make_home):
    home = make_home({"/api/services/light/turn_on": []})
    assert home.set_light("light.lamp", hue=240, saturation=100)["success"]
    # Out of range is kept to it, and brightness goes along with the colour.
    assert home.set_light("light.lamp", brightness=50, hue=400, saturation=-5)["success"]
    assert [(path, sent["json"]) for _, path, sent in home._session.requests] == [
        ("/api/services/light/turn_on", {"entity_id": "light.lamp", "hs_color": [240, 100]}),
        ("/api/services/light/turn_on", {"entity_id": "light.lamp", "brightness_pct": 50, "hs_color": [360, 0]})]


def test_a_hue_needs_its_saturation(make_home):
    home = make_home({})
    assert not home.set_light("light.lamp", hue=240)["success"]
    assert not home.set_light("light.lamp", saturation=50)["success"]
    assert home._session.requests == []


@pytest.mark.parametrize("asked, sent", [(9000, 6500), (1000, 2500), (4000, 4000)])
def test_a_whites_warmth_is_kept_to_the_lights_own_range(make_home, asked, sent):
    lamp = {"entity_id": "light.lamp", "attributes": {"min_color_temp_kelvin": 2500, "max_color_temp_kelvin": 6500}}
    home = make_home({"/api/states/light.lamp": lamp, "/api/services/light/turn_on": []})
    assert home.set_light("light.lamp", kelvin=asked)["success"]
    assert [(method, path, sent_.get("json")) for method, path, sent_ in home._session.requests] == [
        ("GET", "/api/states/light.lamp", None),
        ("POST", "/api/services/light/turn_on", {"entity_id": "light.lamp", "color_temp_kelvin": sent})]


def test_a_colour_and_a_warmth_together_are_just_the_colour(make_home):
    # Home Assistant answers 400 to both, and nothing needs asking about the range.
    home = make_home({"/api/services/light/turn_on": []})
    assert home.set_light("light.lamp", hue=10, saturation=20, kelvin=3000)["success"]
    assert [(path, sent["json"]) for _, path, sent in home._session.requests] == [
        ("/api/services/light/turn_on", {"entity_id": "light.lamp", "hs_color": [10, 20]})]


def test_a_light_which_cant_be_asked_about_its_range_says_so(make_home):
    home = make_home({"/api/states/light.lamp": requests.ConnectionError("down")})
    assert home.set_light("light.lamp", kelvin=3000) == {
        "success": False, "message": "I can't reach Home Assistant right now."}


@pytest.mark.parametrize("entity", ["lock.front_door", "switch.adguard_protection", "light.kitchen/../lock", ""])
def test_only_a_light_is_switched(make_home, entity):
    home = make_home({})
    assert not home.set_light(entity, on=True)["success"]
    assert home._session.requests == []


def test_a_script_is_run_and_a_scene_set(make_home):
    home = _page_house(make_home, {"/api/services/script/turn_on": [], "/api/services/scene/turn_on": []})
    assert home.run("script.movie_night") == {"success": True, "message": "Started Movie night."}
    assert home.run("scene.relax") == {"success": True, "message": "Set Relax."}
    assert [(path, sent.get("json")) for _, path, sent in home._session.requests if "services" in path] == [
        ("/api/services/script/turn_on", {"entity_id": "script.movie_night"}),
        ("/api/services/scene/turn_on", {"entity_id": "scene.relax"})]


@pytest.mark.parametrize("entity", ["script.open_garage", "script.leaving"])
def test_a_script_which_could_let_anyone_in_isnt_run_from_the_page_either(make_home, entity):
    # Looked up by name again first - one could have been renamed since the page was drawn.
    home = _page_house(make_home, {"/api/services/script/turn_on": []})
    assert not home.run(entity)["success"]
    assert _paths(home) == ["/api/states"]


# Scripts and scenes: what each does is checked before it's run - by Assist, by Claude or
# from the settings page

FRONT_DOOR = _thing("lock.front_door", "Front door")
PORCH = _thing("light.porch", "Porch light")
# The PC's sleep button, which is safe by voice, and the HASS.Agent device it's on.
PC_SLEEP = _thing("button.desk_pc_sleep", "Desk PC Sleep")
PC_SLEEP_ENTRY = _entry("button.desk_pc_sleep", "mqtt", "desk", "Sleep")
DESK = {"desk": {"name": "Desk PC", "maker": "HASS.Agent Team"}}

# What run_script says when a script or scene can't be checked.
UNSEEN = "I can't see what {} does, so I don't run it by voice."


class NotJSON(FakeResponse):
    """An answer which isn't JSON, as from a proxy's error page."""

    def json(self):
        raise json.JSONDecodeError("Expecting value", self.body, 0)


def _step(action, **target):
    """One step of a script, as Home Assistant's script editor writes it - e.g. _step("light.turn_off", area_id="hall")."""
    return {"action": action, "metadata": {}, "data": {}, "target": target}


def _config(alias, *steps):
    """A script's configuration, as /api/config/script/config/<key> answers for it."""
    return {"alias": alias, "sequence": list(steps), "mode": "single", "description": ""}


# Goodnight as live check 9 found it: the lights off, the music paused if it's playing and
# the PC put to sleep - by entity id alone, with one if and then.
GOODNIGHT = _config("Goodnight",
                    _step("light.turn_off", entity_id=["light.lamp", "light.porch"]),
                    {"if": [{"condition": "state", "entity_id": "media_player.bedroom", "state": "playing"}],
                     "then": [_step("media_player.media_pause", entity_id="media_player.bedroom")]},
                    _step("button.press", entity_id="button.desk_pc_sleep"))
UNLOCKS = _config("Leaving", _step("lock.unlock", entity_id="lock.front_door"))


def _scripted(make_home, scripts, *others, entries=(), devices=None, exposed=True):
    """
    A house with these scripts - {entity id: (name, what Home Assistant answers for its
    configuration)} - and these other things' states. The registry has them all, each with
    its object id as its key and exposed to Assist as exposed says, bar those given in entries.
    """
    states = [_thing(entity, name) for entity, (name, _) in scripts.items()] + list(others)
    home = make_home({"/api/states": states, "/api/services/script/turn_on": [], "/api/services/scene/turn_on": [],
                      "/api/conversation/process": _assist("action_done", "Done"), "/api/template": _rendered(),
                      **{f"/api/config/script/config/{entity.partition('.')[2]}": answer
                         for entity, (_, answer) in scripts.items()}})
    given = {entry.entity_id for entry in entries}
    home.registry.read = _registry(*entries, *[_entry(state["entity_id"], exposed=exposed) for state in states
                                               if state["entity_id"] not in given], devices=devices)
    return home


def _now(home):
    """The house's states and registry, as works_guarded is given them."""
    return home._session.answers["/api/states"], home.registry.read


def _ran(home):
    """The scripts run and scenes set, as each service call's path and entity."""
    return [(path, sent["json"]) for _, path, sent in home._session.requests if "/api/services/" in path]


def _scene(entity, name, members):
    """A scene's state, its members as its entity_id attribute - or none at all, for None."""
    attributes = {"friendly_name": name, **({"entity_id": members} if members is not None else {})}
    return {"entity_id": entity, "state": "2026-10-24T21:00:00", "attributes": attributes}


def test_a_scene_with_a_lock_among_its_members_is_refused(make_home):
    scene = _scene("scene.movie_night", "Movie night", ["light.lamp", "lock.front_door"])
    home = _scripted(make_home, {}, scene, LAMP, FRONT_DOOR)
    assert home.works_guarded("scene.movie_night", *_now(home))
    assert home.run_script("movie night") == {"success": False, "message": REFUSED["message"]}
    # As is one with a garage among them, by its device class.
    scene["attributes"]["entity_id"] = ["light.lamp", "cover.side"]
    home._session.answers["/api/states"].append(_thing("cover.side", "Side", "garage"))
    assert home.works_guarded("scene.movie_night", *_now(home))
    # Without them, it's set.
    scene["attributes"]["entity_id"] = ["light.lamp"]
    assert not home.works_guarded("scene.movie_night", *_now(home))
    assert home.run_script("movie night") == {"success": True, "message": "Set Movie night."}
    assert _ran(home) == [("/api/services/scene/turn_on", {"entity_id": "scene.movie_night"})]


def test_a_script_that_unlocks_is_refused(make_home):
    home = _scripted(make_home, {"script.leaving": ("Leaving", UNLOCKS)}, FRONT_DOOR)
    assert home.works_guarded("script.leaving", *_now(home))
    assert home.run_script("leaving") == {"success": False, "message": REFUSED["message"]}
    # The action alone does it, whatever it's given.
    home._session.answers["/api/config/script/config/leaving"] = _config(
        "Leaving", _step("lock.unlock", entity_id="{{ door }}"))
    assert home.works_guarded("script.leaving", *_now(home))
    # And the lock, given to any action - here the old way, two entities to a string, under "service" and "data".
    home._session.answers["/api/config/script/config/leaving"] = _config(
        "Leaving", {"service": "homeassistant.turn_off", "data": {"entity_id": "light.lamp, Lock.Front_Door"}})
    assert home.works_guarded("script.leaving", *_now(home))
    # Nor, as on the settings page, is one whose name says it unlocks, whatever it does.
    home = _scripted(make_home, {"script.shed": ("Unlock the shed", _config("Shed", _step("light.turn_on",
                                                                                          entity_id="light.lamp")))})
    assert home.run_script("unlock the shed") == {"success": False, "message": REFUSED["message"]}
    assert _ran(home) == []


def test_a_script_working_a_group_with_a_garage_in_it_is_refused(make_home):
    # A group's members, like a scene's, are its entity_id attribute.
    group = _scene("group.outside", "Outside", ["light.porch", "cover.side"])
    home = _scripted(make_home, {"script.away": ("Away", _config("Away", _step("cover.open_cover",
                                                                               entity_id="group.outside")))},
                     group, PORCH, _thing("cover.side", "Side", "garage"))
    assert home.works_guarded("script.away", *_now(home))
    group["attributes"]["entity_id"] = ["light.porch"]
    assert not home.works_guarded("script.away", *_now(home))


def test_a_script_pressing_a_button_the_registry_cant_vouch_for_cant_be_checked(make_home):
    # A new Proxmox or HASS.Agent button is in the states at once, but in the registry only
    # up to five minutes later.
    press = _config("Chime", _step("button.press", entity_id="button.pve3_shutdown"))
    home = _scripted(make_home, {"script.chime": ("Chime", press)}, _thing("button.pve3_shutdown", "pve3 Shutdown"))
    home.registry.read = _registry(_entry("script.chime"))
    assert home.run_script("chime") == {"success": False, "message": UNSEEN.format("Chime")}
    # Once the registry has it, of a harmless integration, it's pressed.
    home.registry.read = _registry(_entry("script.chime"), _entry("button.pve3_shutdown"))
    assert home.run_script("chime") == {"success": True, "message": "Started Chime."}


def test_a_script_that_runs_one_that_unlocks_is_refused(make_home):
    for evening in (_config("Evening", {"action": "script.leaving"}),
                    _config("Evening", _step("script.turn_on", entity_id="script.leaving")),
                    _config("Evening", {"if": [], "then": [_step("light.turn_on", entity_id="light.lamp")],
                                        "else": [{"action": "script.leaving"}]})):
        home = _scripted(make_home, {"script.evening": ("Evening", evening), "script.leaving": ("Leaving", UNLOCKS)},
                         LAMP, FRONT_DOOR)
        assert home.works_guarded("script.evening", *_now(home)), evening
        assert home.run_script("evening") == {"success": False, "message": REFUSED["message"]}

    # A script which runs itself, or two which run each other, are each read once, not for ever.
    again = _config("Again", _step("light.turn_on", entity_id="light.lamp"), {"action": "script.again"},
                    _step("script.turn_on", entity_id=["script.other", "script.again"]))
    home = _scripted(make_home, {"script.again": ("Again", again),
                                 "script.other": ("Other", _config("Other", {"action": "script.again"}))}, LAMP)
    assert not home.works_guarded("script.again", *_now(home))
    assert _paths(home) == ["/api/config/script/config/again", "/api/config/script/config/other"]


def test_a_script_with_a_locks_device_action_is_refused(make_home):
    # A device action names the lock's device and the registry's own id for it, never its entity id.
    unlock = {"type": "unlock", "device_id": "door", "entity_id": "4b1e0c7d", "domain": "lock"}
    entries = [_entry("lock.front_door", device="door"), _entry("light.lamp", device="lamp"),
               _entry("sensor.hub_signal", device="hub")]
    devices = {"door": {"name": "Front door"}, "lamp": {"name": "Lamp"}, "hub": {"name": "Hub"}}
    home = _scripted(make_home, {"script.leaving": ("Leaving", _config("Leaving", unlock))}, FRONT_DOOR, LAMP,
                     entries=entries, devices=devices)
    assert home.works_guarded("script.leaving", *_now(home))
    # By its domain alone, too - and any action on a device with a lock on it.
    for step in ({**unlock, "device_id": "hub"}, _step("homeassistant.turn_off", device_id="door"),
                 _step("light.turn_off", device_id=["lamp", "door"])):
        home._session.answers["/api/config/script/config/leaving"] = _config("Leaving", step)
        assert home.run_script("leaving") == {"success": False, "message": REFUSED["message"]}, step
    # A device the registry hasn't got can't be checked.
    home._session.answers["/api/config/script/config/leaving"] = _config(
        "Leaving", {"type": "turn_on", "device_id": "stranger", "entity_id": "9f2a", "domain": "light"})
    assert home.run_script("leaving") == {"success": False, "message": UNSEEN.format("Leaving")}
    # While a light's device action is fine.
    home.registry.read = _registry(_entry("lock.front_door", device="door"),
                                   _entry("light.lamp", device="lamp", registry_id="9f2a"),
                                   _entry("sensor.hub_signal", device="hub"), _entry("script.leaving"), devices=devices)
    home._session.answers["/api/config/script/config/leaving"] = _config(
        "Leaving", {"type": "turn_on", "device_id": "lamp", "entity_id": "9f2a", "domain": "light"})
    assert not home.works_guarded("script.leaving", *_now(home))


def test_a_device_actions_entity_is_checked_as_well_as_its_device(make_home):
    # Home Assistant acts on the entity a device action gives by its registry id, without
    # checking it's on the device given - so the lamp's device with the PC's shutdown
    # button, as only hand-written YAML would pair them, presses the shutdown.
    states = [LAMP, _thing("button.desk_pc_shutdown", "Desk PC Shutdown")]
    entries = [_entry("light.lamp", device="lamp", registry_id="4b1e"),
               _entry("button.desk_pc_shutdown", "mqtt", "desk", "Shutdown", registry_id="9f2a")]
    devices = {"lamp": {"name": "Lamp"}, **DESK}
    press = {"type": "press", "device_id": "lamp", "entity_id": "9F2A", "domain": "button"}
    home = _scripted(make_home, {"script.away": ("Away", _config("Away", press))}, *states, entries=entries,
                     devices=devices)
    assert home.run_script("away") == {"success": False, "message": REFUSED_MACHINE["message"]}
    # One the registry has no such id for, or whose entity isn't in the states, can't be checked.
    for given in ("0123", ["4b1e", "0123"]):
        home._session.answers["/api/config/script/config/away"] = _config("Away", {**press, "entity_id": given})
        assert home.run_script("away") == {"success": False, "message": UNSEEN.format("Away")}, given
    home._session.answers["/api/states"].remove(LAMP)
    home._session.answers["/api/config/script/config/away"] = _config("Away", {**press, "entity_id": "4b1e"})
    assert home.works_guarded("script.away", *_now(home))
    # While the lamp's own, given by its id, is fine.
    home._session.answers["/api/states"].append(LAMP)
    assert not home.works_guarded("script.away", *_now(home))
    assert _ran(home) == []


def test_an_area_target_is_refused_only_for_services_that_could_reach_a_lock(make_home):
    # Ruling 3: any action of these domains, given a whole area, floor or label - or every
    # entity, as "all" - could reach a lock, a door, the PC's shutdown or Proxmox's buttons
    # and switches in it.
    refused = [("homeassistant.turn_off", {"area_id": "hall"}), ("homeassistant.turn_on", {"label_id": "outside"}),
               ("cover.open_cover", {"floor_id": "ground"}), ("button.press", {"area_id": "study"}),
               ("switch.turn_off", {"area_id": "rack"}), ("homeassistant.turn_off", {"entity_id": "all"}),
               ("lock.lock", {"area_id": "hall"}), ("script.turn_on", {"area_id": "hall"})]
    fine = [("light.turn_off", {"area_id": "hall"}), ("light.turn_off", {"entity_id": "all"}),
            ("media_player.media_pause", {"floor_id": "ground"})]
    for (action, target), want in [*[(step, True) for step in refused], *[(step, False) for step in fine]]:
        home = _scripted(make_home, {"script.away": ("Away", _config("Away", _step(action, **target)))})
        assert home.works_guarded("script.away", *_now(home)) is want, (action, target)
    # The old way, with the area in the step's data.
    home = _scripted(make_home, {"script.away": ("Away", _config("Away", {"service": "homeassistant.turn_off",
                                                                          "data": {"area_id": "hall"}}))})
    assert home.works_guarded("script.away", *_now(home))


def test_a_templated_target_is_left_to_the_user(make_home):
    # 0008's "My calls": a script which builds its targets from a template is the user's
    # own, and they exposed it.
    porch = _config("Porch", _step("homeassistant.turn_on", entity_id="{{ 'lock.front_door' if late else 'light.porch' }}"))
    home = _scripted(make_home, {"script.porch": ("Porch", porch)}, FRONT_DOOR, PORCH)
    assert not home.works_guarded("script.porch", *_now(home))
    assert home.run_script("porch") == {"success": True, "message": "Started Porch."}


@pytest.mark.parametrize("answer", [
    FakeResponse({"message": "Resource not found"}, status=404),  # written in YAML outside scripts.yaml
    FakeResponse({"message": "Unauthorized"}, status=403),  # a token which isn't an administrator's
    NotJSON("<html>Bad gateway</html>"),
    {"alias": "Goodnight", "use_blueprint": {"path": "goodnight.yaml", "input": {}}},  # no sequence to read
    ["light.turn_off"],
    requests.ConnectionError("down"),
])
def test_a_script_that_cant_be_checked_is_refused(make_home, answer):
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", answer)}, LAMP)
    assert home.works_guarded("script.goodnight", *_now(home))
    # Said apart from working something guarded, on the page as by voice (ruling 2).
    assert home.run_script("goodnight") == {"success": False, "message": UNSEEN.format("Goodnight")}
    assert home.run("script.goodnight") == {"success": False,
                                            "message": "Nova can't see what Goodnight does, so it doesn't run it."}
    assert _ran(home) == []


def test_a_scripts_configuration_is_read_by_its_unique_id(make_home):
    # Ruling 8: the key is the registry's unique id, which a script made in the editor
    # needn't share with its object id.
    home = make_home({"/api/states": [_thing("script.goodnight", "Goodnight"), LAMP],
                      "/api/config/script/config/1730000000000": GOODNIGHT,
                      "/api/config/script/config/a%2F..%2Fb": GOODNIGHT})
    home.registry.read = _registry(Entry("script.goodnight", "script", None, None, "1730000000000", None, (), True),
                                   _entry("light.lamp"))
    assert not home.works_guarded("script.goodnight", *_now(home))
    assert _paths(home) == ["/api/config/script/config/1730000000000"]
    # A key is sent as one part of the path, never as more of it.
    home.registry.read = _registry(Entry("script.goodnight", "script", None, None, "a/../b", None, (), True),
                                   _entry("light.lamp"))
    assert not home.works_guarded("script.goodnight", *_now(home))
    assert _paths(home)[-1] == "/api/config/script/config/a%2F..%2Fb"
    # One the registry hasn't got has no key, so it can't be checked.
    home.registry.read = _registry(_entry("light.lamp"))
    assert home.works_guarded("script.goodnight", *_now(home))
    assert len(_paths(home)) == 2


def test_a_script_naming_a_lock_as_a_key_is_refused(make_home):
    # Ruling 4: scene.apply's own scene names each thing as a key.
    arrive = _config("Arrive", {"action": "scene.apply",
                                "data": {"entities": {"light.lamp": "on", "lock.front_door": "unlocked"}}})
    home = _scripted(make_home, {"script.arrive": ("Arrive", arrive)}, LAMP, FRONT_DOOR)
    assert home.works_guarded("script.arrive", *_now(home))
    assert home.run_script("arrive") == {"success": False, "message": REFUSED["message"]}


def test_a_script_that_works_an_automation_cant_be_checked(make_home):
    # Ruling 5: an automation's contents aren't walked, and one could unlock.
    for step in (_step("automation.trigger", entity_id="automation.arriving"),
                 _step("homeassistant.turn_on", entity_id="automation.arriving"),
                 {"action": "automation.turn_on", "target": {"label_id": "doors"}}):
        home = _scripted(make_home, {"script.arrive": ("Arrive", _config("Arrive", step))},
                         _thing("automation.arriving", "Arriving"))
        assert home.run_script("arrive") == {"success": False, "message": UNSEEN.format("Arrive")}, step


def test_a_scene_an_integration_made_cant_be_checked(make_home):
    # Ruling 6: with no entity_id attribute, its members can't be told.
    home = _scripted(make_home, {}, _scene("scene.hue_relax", "Relax", None))
    assert home.works_guarded("scene.hue_relax", *_now(home))
    assert home.run_script("relax") == {"success": False, "message": UNSEEN.format("Relax")}
    assert _ran(home) == []


def test_a_script_that_only_works_the_lights_runs_by_voice(make_home):
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", GOODNIGHT)}, LAMP, PORCH, PC_SLEEP,
                     _thing("media_player.bedroom", "Bedroom"), entries=[PC_SLEEP_ENTRY], devices=DESK)
    assert home.run_script("goodnight") == {"success": True, "message": "Started Goodnight."}
    assert _ran(home) == [("/api/services/script/turn_on", {"entity_id": "script.goodnight"})]
    # Asked for as people say it.
    for said in ("my goodnight routine", "The Goodnight script", "Good night!"):
        assert home.run_script(said) == {"success": True, "message": "Started Goodnight."}, said


def test_a_script_not_exposed_to_assist_isnt_run_by_voice(make_home):
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", GOODNIGHT)}, LAMP, PORCH, exposed=False)
    assert home.run_script("goodnight") == {"success": False,
                                            "message": "Goodnight isn't exposed to Assist, so I don't run it by voice."}
    assert _paths(home) == ["/api/states"]


def test_without_the_registry_no_script_is_run_by_voice(make_home):
    # Ruling 7: a script's configuration is found by the registry's key for it.
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", GOODNIGHT)}, LAMP, PORCH)
    home.registry.read = None
    assert home.run_script("goodnight") == {"success": False,
                                            "message": "I can't tell which scripts can be run by voice just now."}
    assert _paths(home) == []


def test_two_scripts_that_fit_are_asked_about(make_home):
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", GOODNIGHT)}, _scene("scene.bedtime", "Bedtime", []),
                     entries=[_entry("script.goodnight", aliases=["bedtime"])])
    assert home.run_script("bedtime") == {"success": False, "message": "Which one, Bedtime or Goodnight?"}
    assert home.run_script("the hall lights") == {"success": False,
                                                  "message": "I haven't got a script or scene called that."}
    assert _ran(home) == []


def test_assist_never_runs_a_script_that_unlocks(make_home):
    home = _scripted(make_home, {"script.evening": ("Evening", UNLOCKS)}, FRONT_DOOR)
    home.socket.answer = _debugged(_match("HassTurnOn", "script.evening"))
    assert home.command("run evening", wider=True) == REFUSED
    # By a name Assist knows it by that the registry hasn't caught up with, too.
    assert home.command("run nightfall", wider=True) == REFUSED
    assert "/api/conversation/process" not in _paths(home)
    # Once it only works the lights, Assist runs it.
    home._session.answers["/api/config/script/config/evening"] = _config(
        "Evening", _step("light.turn_on", entity_id="light.porch"))
    assert home.command("run evening", wider=True) == {"success": True, "message": "Done", "chime": True}


def test_a_light_sentence_naming_a_script_that_unlocks_is_refused_without_the_debugging(make_home):
    # The debugging isn't answering - FakeSocket raises - so only light sentences go to
    # Assist, and only the checks of what they name stand between Assist and the script.
    for scripts, entries in (({"script.porch_light": ("Porch light", UNLOCKS)}, ()),
                             ({"script.welcome": ("Welcome", UNLOCKS)}, [_entry("script.welcome", aliases=["Porch light"])])):
        home = _scripted(make_home, scripts, FRONT_DOOR, PORCH, entries=entries)
        assert home.command("turn on the porch light") == REFUSED
        assert "/api/conversation/process" not in _paths(home)
        # Its name alone doesn't refuse it: once it only works the lights, the sentence goes to Assist.
        for path in [path for path in home._session.answers if path.startswith("/api/config/script/config/")]:
            home._session.answers[path] = _config("Porch light", _step("light.turn_on", entity_id="light.porch"))
        assert home.command("turn on the porch light")["success"]


def test_the_settings_page_wont_run_a_script_that_unlocks_either(make_home):
    home = _scripted(make_home, {"script.evening": ("Evening", UNLOCKS)}, FRONT_DOOR)
    assert home.run("script.evening") == {"success": False,
                                          "message": "Nova doesn't run anything which locks, unlocks or opens."}
    # Nor, without the registry, one which only works the lights (ruling 7).
    home._session.answers["/api/config/script/config/evening"] = _config(
        "Evening", _step("light.turn_on", entity_id="light.porch"))
    home.registry.read = None
    assert home.run("script.evening") == {"success": False,
                                          "message": "Nova can't see what Evening does, so it doesn't run it."}
    # Nor one that isn't there to check.
    home.registry.read = _registry(_entry("script.evening"))
    assert not home.run("script.gone")["success"]
    assert _ran(home) == []


def test_the_page_only_lists_the_scripts_and_scenes_it_can_run(make_home):
    # Ruling 1: never a button that can't work.
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", GOODNIGHT), "script.evening": ("Evening", UNLOCKS),
                                 "script.laptop_sleep": ("Laptop sleep", FakeResponse({}, status=404))},
                     _scene("scene.relax", "Relax", ["light.lamp"]), _scene("scene.hue_relax", "Hue relax", None),
                     LAMP, PORCH, FRONT_DOOR, PC_SLEEP, entries=[PC_SLEEP_ENTRY], devices=DESK)
    assert [routine["id"] for routine in home.overview()["routines"]] == ["script.goodnight", "scene.relax"]
    # Without the registry, run() would refuse them all, so the page has none.
    home.registry.read = None
    overview = home.overview()
    assert overview["success"] and overview["routines"] == []


def test_each_script_says_whether_nova_runs_it_by_voice(make_home):
    bedtime = _config("Bedtime", _step("light.turn_off", entity_id="light.lamp"))
    reading = _config("Reading", _step("light.turn_on", entity_id="light.lamp"))
    home = _scripted(make_home, {"script.bedtime": ("Bedtime", bedtime), "script.reading": ("Reading", reading)},
                     _scene("scene.relax", "Relax", ["light.lamp"]), LAMP,
                     entries=[_entry("script.reading", exposed=False)])
    assert [(routine["id"], routine["by_voice"]) for routine in home.overview()["routines"]] == [
        ("script.bedtime", True), ("script.reading", False), ("scene.relax", True)]
    # Without the registry nothing's offered at all - but were the guard ever to let one
    # through, nothing could say it's exposed, so none is said to run by voice.
    home.registry.read = None
    home.works_guarded = lambda *_, **__: False
    routines = home.overview()["routines"]
    assert [routine["id"] for routine in routines] == ["script.bedtime", "script.reading", "scene.relax"]
    assert not any(routine["by_voice"] for routine in routines)


def test_a_guarded_script_is_still_never_offered(make_home):
    # Exposed to Assist, and asked for with the page's yes - but it unlocks, so the page
    # never shows it, and a run of it from an old tab is refused before Home Assistant's asked.
    home = _scripted(make_home, {"script.evening": ("Evening", UNLOCKS)}, FRONT_DOOR)
    assert home.overview()["routines"] == []
    assert home.run("script.evening") == {"success": False,
                                          "message": "Nova doesn't run anything which locks, unlocks or opens."}
    assert _ran(home) == []


def test_the_settings_page_yes_doesnt_get_a_guarded_script_past_the_guard(make_home):
    # The page's yes lets a script reach Home Assistant, but the 0008 guard still decides.
    home = _scripted(make_home, {"script.evening": ("Evening", UNLOCKS)}, FRONT_DOOR)
    session = SimpleNamespace(home=home)
    brain = SimpleNamespace(session_for=lambda satellite: session, default_satellite=lambda: "bedroom")
    page = SettingsPage(["you@example.com"], port=0, brain=brain)
    assert page.run("script.evening", "you@example.com", confirmed=True) == {
        "success": False, "message": "Nova doesn't run anything which locks, unlocks or opens."}
    assert _ran(home) == []


def test_a_script_that_cant_be_read_is_said_once(make_home, caplog):
    # The settings page asks for its list every fifteen seconds.
    home = _scripted(make_home, {"script.laptop_sleep": ("Laptop sleep", FakeResponse({}, status=404))})
    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        for _ in range(3):
            home.overview()
        home._session.answers["/api/config/script/config/laptop_sleep"] = _config("Laptop sleep")
        home.overview()
        home._session.answers["/api/config/script/config/laptop_sleep"] = FakeResponse({}, status=404)
        home.overview()
    said = [record for record in caplog.records if "script.laptop_sleep" in record.getMessage()]
    assert len(said) == 2 and all(record.levelname == "WARNING" for record in said)


def _script_entry(entity, key):
    """A script as the registry has it, under its own key - which renaming its entity id leaves as it was."""
    return Entry(entity, "script", None, None, key, None, (), True)


def test_a_script_called_by_its_key_is_walked_after_its_entity_id_is_renamed(make_home):
    # Home Assistant names each script's own action by its key, so {"action": "script.leaving"}
    # runs the script whose key is "leaving", whatever its entity id is now.
    states = [_thing("script.evening", "Evening"), _thing("script.leave_home", "Leave home"), FRONT_DOOR]
    home = make_home({"/api/states": states, "/api/services/script/turn_on": [],
                      "/api/config/script/config/evening": _config("Evening", {"action": "script.leaving"}),
                      "/api/config/script/config/leaving": UNLOCKS})
    home.registry.read = _registry(_script_entry("script.evening", "evening"),
                                   _script_entry("script.leave_home", "leaving"), _entry("lock.front_door"))
    assert home.run_script("evening") == {"success": False, "message": REFUSED["message"]}
    # A script's action that's no key the script editor has can't be checked.
    home._session.answers["/api/config/script/config/evening"] = _config("Evening", {"action": "script.nowhere"})
    home._session.answers["/api/config/script/config/nowhere"] = FakeResponse({}, status=404)
    assert home.run_script("evening") == {"success": False, "message": UNSEEN.format("Evening")}
    # While the script domain's own actions are only its own.
    home._session.answers["/api/config/script/config/evening"] = _config(
        "Evening", {"action": "script.reload"}, _step("script.turn_off", entity_id="script.evening"))
    assert home.run_script("evening") == {"success": True, "message": "Started Evening."}
    # With another script given the entity id script.leaving since, the one of that key is
    # still walked as well, whichever was meant.
    home._session.answers["/api/config/script/config/evening"] = _config("Evening", {"action": "script.leaving"})
    states.append(_thing("script.leaving", "Bedtime"))
    home._session.answers["/api/config/script/config/bedtime"] = _config("Bedtime")
    home.registry.read = _registry(*home.registry.read.entries.values(), _script_entry("script.leaving", "bedtime"))
    assert home.works_guarded("script.evening", *_now(home))


def test_a_script_called_by_its_key_is_read_by_that_key_whatever_the_registry_has_at_it(make_home):
    # The registry can be up to five minutes behind a rename: here it still has the script
    # whose key is "leaving" at script.leaving, which the states now call
    # script.front_door_routine. The key is read straight, so that's no matter.
    states = [_thing("script.evening", "Evening"), _thing("script.front_door_routine", "Front door routine"),
              FRONT_DOOR]
    home = make_home({"/api/states": states, "/api/services/script/turn_on": [],
                      "/api/config/script/config/evening": _config("Evening", {"action": "script.leaving"}),
                      "/api/config/script/config/leaving": UNLOCKS})
    home.registry.read = _registry(_script_entry("script.evening", "evening"), _script_entry("script.leaving", "leaving"),
                                   _entry("lock.front_door"))
    assert home.run_script("evening") == {"success": False, "message": REFUSED["message"]}
    # Nor is it taken for another script whose key is the entity id's object id the
    # registry still has for it.
    states.append(_thing("script.bedtime", "Bedtime"))
    home._session.answers["/api/config/script/config/old_name"] = _config("Bedtime")
    home.registry.read = _registry(_script_entry("script.evening", "evening"),
                                   _script_entry("script.old_name", "leaving"),
                                   _script_entry("script.bedtime", "old_name"), _entry("lock.front_door"))
    assert home.run_script("evening") == {"success": False, "message": REFUSED["message"]}
    assert _ran(home) == []


def test_a_script_named_by_entity_id_is_read_by_its_key_as_home_assistant_has_it_now(make_home):
    # The final review's repro: within the registry's five minutes, script.leaving was
    # renamed script.leave_home - keeping its key "leaving" - and another script, of key
    # "b", which unlocks, was given script.leaving. Goodnight runs script.leaving, which
    # is now the one that unlocks, so its key is looked up afresh, never the snapshot's.
    states = [_thing("script.goodnight", "Goodnight"), _thing("script.leave_home", "Leave home"),
              _thing("script.leaving", "Leaving"), FRONT_DOOR]
    home = make_home({"/api/states": states, "/api/services/script/turn_on": [],
                      "/api/config/script/config/goodnight": _config(
                          "Goodnight", _step("script.turn_on", entity_id="script.leaving")),
                      "/api/config/script/config/leaving": _config("Leave home"),
                      "/api/config/script/config/b": UNLOCKS})
    home.registry.read = _registry(_script_entry("script.goodnight", "goodnight"),
                                   _script_entry("script.leaving", "leaving"), _entry("lock.front_door"))
    home.socket.live = {entry.entity_id: entry for entry in (
        _script_entry("script.goodnight", "goodnight"), _script_entry("script.leave_home", "leaving"),
        _script_entry("script.leaving", "b"), _entry("lock.front_door"))}
    assert home.run_script("goodnight") == {"success": False, "message": REFUSED["message"]}
    assert home.run("script.leaving")["success"] is False
    # Each over the WebSocket, as Home Assistant has it at the time.
    assert home.socket.lookups()[:2] == ["script.goodnight", "script.leaving"]
    assert _ran(home) == []


def test_two_scripts_swapping_keys_are_each_read_by_their_own_now(make_home):
    # A swap leaves the same entity ids in the states and the snapshot, so it can't be
    # told from them: script.a was "ka", which only works the lights, and is now "kb",
    # which unlocks.
    states = [_thing("script.goodnight", "Goodnight"), _thing("script.a", "A"), _thing("script.b", "B"),
              FRONT_DOOR, LAMP]
    home = make_home({"/api/states": states, "/api/services/script/turn_on": [],
                      "/api/config/script/config/goodnight": _config(
                          "Goodnight", _step("script.turn_on", entity_id="script.a")),
                      "/api/config/script/config/ka": _config("A", _step("light.turn_on", entity_id="light.lamp")),
                      "/api/config/script/config/kb": UNLOCKS})
    home.registry.read = _registry(_script_entry("script.goodnight", "goodnight"), _script_entry("script.a", "ka"),
                                   _script_entry("script.b", "kb"), _entry("lock.front_door"), _entry("light.lamp"))
    home.socket.live = {**home.registry.read.entries, "script.a": _script_entry("script.a", "kb"),
                        "script.b": _script_entry("script.b", "ka")}
    assert home.run_script("goodnight") == {"success": False, "message": REFUSED["message"]}
    assert home.run_script("a") == {"success": False, "message": REFUSED["message"]}
    assert home.run_script("b") == {"success": True, "message": "Started B."}
    assert _ran(home) == [("/api/services/script/turn_on", {"entity_id": "script.b"})]


def test_a_script_whose_key_cant_be_looked_up_cant_be_checked(make_home):
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", GOODNIGHT)}, LAMP, PORCH,
                     _thing("media_player.bedroom", "Bedroom"), PC_SLEEP, entries=[PC_SLEEP_ENTRY], devices=DESK)
    assert home.run_script("goodnight") == {"success": True, "message": "Started Goodnight."}
    for failure in (HomeSocketError("timed out"), HomeSocketError("config/entity_registry/get_entries failed")):
        home.socket.lookup_fails = failure
        assert home.run_script("goodnight") == {"success": False, "message": UNSEEN.format("Goodnight")}
        assert home.run("script.goodnight")["success"] is False
        assert home.overview()["routines"] == []
    assert _ran(home) == [("/api/services/script/turn_on", {"entity_id": "script.goodnight"})]


def test_a_draw_looks_up_every_scripts_key_in_one_socket_call(make_home):
    # The settings page draws every fifteen seconds, and a call is one connection with its
    # own deadline - so however many scripts there are, a stalled socket costs one deadline.
    lights_only = _config("Lights", _step("light.turn_off", entity_id="light.lamp"))
    scripts = {f"script.lights_{number}": (f"Lights {number}", lights_only) for number in range(5)}
    home = _scripted(make_home, scripts, LAMP)
    assert [routine["id"] for routine in home.overview()["routines"]] == sorted(scripts)
    assert home.socket.lookup_calls() == 1 and sorted(home.socket.lookups()) == sorted(scripts)
    home.socket.lookup_fails = HomeSocketError("timed out")
    assert home.overview()["routines"] == []
    assert home.socket.lookup_calls() == 2
    # With no scripts, nothing is looked up.
    home = _scripted(make_home, {}, LAMP, _scene("scene.relax", "Relax", ["light.lamp"]))
    assert [routine["id"] for routine in home.overview()["routines"]] == ["scene.relax"]
    assert home.socket.lookup_calls() == 0


def test_scripts_assist_would_run_together_are_looked_up_together(make_home):
    lights_only = _config("Lights", _step("light.turn_off", entity_id="light.lamp"))
    home = _scripted(make_home, {"script.lamp_off": ("Lamp off", lights_only), "script.dim": ("Dim", lights_only)}, LAMP)
    home.socket.answer = _debugged(_match("HassTurnOn", "script.lamp_off", "script.dim"))
    assert home.command("run lamp off and dim", wider=True)["success"]
    assert home.socket.lookup_calls() == 1 and home.socket.lookups() == ["script.lamp_off", "script.dim"]


def test_a_draw_only_looks_up_the_scripts_it_would_walk(make_home):
    # One whose name says it unlocks is never offered, so its key isn't asked for.
    lights_only = _config("Lights", _step("light.turn_off", entity_id="light.lamp"))
    home = _scripted(make_home, {"script.dim": ("Dim", lights_only), "script.unlock_door": ("Unlock door", UNLOCKS)},
                     LAMP, FRONT_DOOR)
    assert [routine["id"] for routine in home.overview()["routines"]] == ["script.dim"]
    assert home.socket.lookups() == ["script.dim"]


def test_a_bad_entry_in_a_batch_fails_only_its_own_script(make_home):
    lights_only = _config("Lights", _step("light.turn_off", entity_id="light.lamp"))
    home = _scripted(make_home, {f"script.lights_{number}": (f"Lights {number}", lights_only) for number in range(3)},
                     LAMP)
    live = {**home.registry.read.entries}
    del live["script.lights_1"]
    home.socket.live = live
    assert [routine["id"] for routine in home.overview()["routines"]] == ["script.lights_0", "script.lights_2"]
    assert home.socket.lookup_calls() == 1


@pytest.mark.parametrize("answer", [[], "odd", {"script.goodnight": "goodnight"}, {"script.goodnight": {}},
                                    {"script.goodnight": {"unique_id": 7}}, {}])
def test_a_script_whose_key_comes_back_odd_cant_be_checked(make_home, answer):
    home = _scripted(make_home, {"script.goodnight": ("Goodnight", GOODNIGHT)}, LAMP, PORCH,
                     _thing("media_player.bedroom", "Bedroom"), PC_SLEEP, entries=[PC_SLEEP_ENTRY], devices=DESK)
    home.socket.lookup_answer = answer
    assert home.overview()["routines"] == []
    assert home.run_script("goodnight") == {"success": False, "message": UNSEEN.format("Goodnight")}
    # Nor one the registry hasn't got now.
    home.socket.lookup_answer = None
    home.socket.live = {}
    assert home.overview()["routines"] == []
    assert _ran(home) == []


# A registry id, as Home Assistant takes in place of an entity id - only it can say whose it is.
REGISTRY_ID = "0123456789abcdef0123456789abcdef"


def test_a_script_giving_an_entity_by_its_registry_id_cant_be_checked(make_home):
    for step in (_step("button.press", entity_id=REGISTRY_ID),
                 {"service": "cover.open_cover", "data": {"entity_id": ["light.lamp", REGISTRY_ID.upper()]}},
                 {"action": "switch.turn_off", "entity_id": f"light.lamp, {REGISTRY_ID}"}):
        home = _scripted(make_home, {"script.away": ("Away", _config("Away", step))}, LAMP)
        assert home.run_script("away") == {"success": False, "message": UNSEEN.format("Away")}, step
    # "all" and "none" are Home Assistant's own words, not ids.
    home = _scripted(make_home, {"script.away": ("Away", _config("Away", _step("light.turn_off", entity_id="all"),
                                                                 _step("light.turn_on", entity_id="none")))})
    assert not home.works_guarded("script.away", *_now(home))


def test_a_script_with_a_service_template_given_an_area_cant_be_run(make_home):
    # Home Assistant still takes service_template as a step's action, plain or templated.
    home = _scripted(make_home, {"script.away": ("Away", _config("Away", {"service_template": "button.press",
                                                                          "target": {"area_id": "study"}}))})
    assert home.works_guarded("script.away", *_now(home))
    assert home.run_script("away") == {"success": False, "message": REFUSED["message"]}


def test_a_script_handing_a_sentence_to_assist_or_publishing_over_mqtt_cant_be_checked(make_home):
    # Inside Home Assistant, Nova's guard doesn't stand between conversation.process and
    # Assist - and HASS.Agent takes its commands over MQTT.
    for step in ({"action": "conversation.process", "data": {"text": "unlock the front door",
                                                              "agent_id": "conversation.home_assistant"}},
                 {"action": "mqtt.publish", "data": {"topic": "hass.agent/commands/desk_pc/shutdown", "payload": ""}}):
        home = _scripted(make_home, {"script.arrive": ("Arrive", _config("Arrive", step))}, FRONT_DOOR)
        assert home.run_script("arrive") == {"success": False, "message": UNSEEN.format("Arrive")}, step
    # Though a helper's change, and an event, are left to the user, as any change can set off an automation.
    home = _scripted(make_home, {"script.arrive": ("Arrive", _config(
        "Arrive", _step("input_boolean.turn_on", entity_id="input_boolean.home"), {"event": "arrived", "event_data": {}}))},
        _thing("input_boolean.home", "Home"))
    assert home.run_script("arrive") == {"success": True, "message": "Started Arrive."}


@pytest.mark.parametrize("action", ["hassio.host_shutdown", "hassio.host_reboot", "homeassistant.stop",
                                    "homeassistant.restart", "HomeAssistant.Restart"])
def test_a_script_stopping_home_assistant_or_its_host_is_refused(make_home, action):
    # Ruling 147, revised: they take down the hub Nova works the house through, as
    # Proxmox's buttons take down the machines - and they need no entity to do it. So
    # what's said is the machine's sentence, the user's call on 7 Nov.
    home = _scripted(make_home, {"script.reset": ("Reset", _config("Reset", {"action": action}))})
    assert home.run_script("reset") == {"success": False, "message": REFUSED_MACHINE["message"]}
    home.socket.answer = _debugged(_match("HassTurnOn", "script.reset"))
    assert home.command("run reset", wider=True) == REFUSED_MACHINE
    home._session.answers["/api/config/script/config/reset"] = _config("Reset", {"service": action, "data": {}})
    assert home.works_guarded("script.reset", *_now(home))
    assert _ran(home) == [] and "/api/conversation/process" not in _paths(home)
    # One which unlocks the front door as well is the lock's.
    home = _scripted(make_home, {"script.reset": ("Reset", _config(
        "Reset", {"action": action}, _step("lock.unlock", entity_id="lock.front_door")))}, FRONT_DOOR)
    assert home.run_script("reset") == {"success": False, "message": REFUSED["message"]}
    assert _ran(home) == []


PVE2_SHUTDOWN = _thing("button.pve2_shutdown", "PVE2 Shutdown")
PVE2_SHUTDOWN_ENTRY = _entry("button.pve2_shutdown", "proxmoxve")


def test_a_script_refused_for_working_a_machine_says_so(make_home):
    # The user's call: the lock's sentence is no answer for a script that shuts PVE2 down.
    shuts_down = _config("Lab off", _step("button.press", entity_id="button.pve2_shutdown"))
    home = _scripted(make_home, {"script.lab_off": ("Lab off", shuts_down)}, PVE2_SHUTDOWN,
                     entries=[PVE2_SHUTDOWN_ENTRY])
    assert home.run_script("lab off") == {"success": False, "message": REFUSED_MACHINE["message"]}
    # By a light sentence naming it, with the debugging not answering - and by what Assist would act on.
    assert home.command("turn on lab off") == REFUSED_MACHINE
    home.socket.answer = _debugged(_match("HassTurnOn", "script.lab_off"))
    assert home.command("run lab off", wider=True) == REFUSED_MACHINE
    # And one run by another.
    home = _scripted(make_home, {"script.evening": ("Evening", _config("Evening", {"action": "script.lab_off"})),
                                 "script.lab_off": ("Lab off", shuts_down)}, PVE2_SHUTDOWN,
                     entries=[PVE2_SHUTDOWN_ENTRY])
    assert home.run_script("evening") == {"success": False, "message": REFUSED_MACHINE["message"]}
    assert _ran(home) == [] and "/api/conversation/process" not in _paths(home)


def test_a_script_working_a_machine_and_a_lock_is_the_locks(make_home):
    # Walked on past the machine, so the lock it also unlocks is what's said.
    both = _config("Leaving", _step("button.press", entity_id="button.pve2_shutdown"),
                   _step("lock.unlock", entity_id="lock.front_door"))
    home = _scripted(make_home, {"script.leaving": ("Leaving", both)}, PVE2_SHUTDOWN, FRONT_DOOR,
                     entries=[PVE2_SHUTDOWN_ENTRY])
    assert home.run_script("leaving") == {"success": False, "message": REFUSED["message"]}
    home.socket.answer = _debugged(_match("HassTurnOn", "script.leaving"))
    assert home.command("run leaving", wider=True) == REFUSED
    # So is one with something that can't be checked beside the machine - it could unlock.
    home._session.answers["/api/config/script/config/leaving"] = _config(
        "Leaving", _step("button.press", entity_id="button.pve2_shutdown"), {"action": "automation.trigger"})
    assert home.command("run leaving", wider=True) == REFUSED
    assert _ran(home) == [] and "/api/conversation/process" not in _paths(home)


class TooDeep(FakeResponse):
    """An answer nested deeper than Python's json will decode."""

    def json(self):
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array from a unicode string")


def test_a_script_too_deep_to_walk_cant_be_checked_and_nothing_raises(make_home):
    deep = _step("light.turn_on", entity_id="light.lamp")
    for _ in range(3000):
        deep = {"sequence": [deep]}
    home = _scripted(make_home, {"script.deep": ("Deep", _config("Deep", deep))}, LAMP)
    assert home.works_guarded("script.deep", *_now(home))
    assert home.run_script("deep") == {"success": False, "message": UNSEEN.format("Deep")}
    assert home.run("script.deep") == {"success": False, "message": "Nova can't see what Deep does, so it doesn't run it."}
    assert home.overview()["routines"] == []
    home.socket.answer = _debugged(_match("HassTurnOn", "script.deep"))
    assert home.command("run deep", wider=True) == REFUSED
    # Nor an answer too deep to decode, nor a long enough chain of scripts, each running the next.
    home._session.answers["/api/config/script/config/deep"] = TooDeep("[[[[")
    assert home.run_script("deep") == {"success": False, "message": UNSEEN.format("Deep")}
    links = {f"script.link_{n}": (f"Link {n}", _config(f"Link {n}", {"action": f"script.link_{n + 1}"}))
             for n in range(600)}
    home = _scripted(make_home, {**links, "script.link_600": ("Link 600", _config("Link 600"))})
    assert home.works_guarded("script.link_0", *_now(home))
    assert _ran(home) == []


# What's logged for a script too deep to walk.
TOO_DEEP = ("Can't see what script.deep does, as it goes too deep to follow, so it isn't run by voice or from "
            "the settings page")


def _said_too_deep(caplog):
    """Each time a script was logged as too deep to walk, as (logger, line)."""
    return [(record.name, record.getMessage()) for record in caplog.records if "too deep" in record.getMessage()]


def test_a_script_too_deep_to_walk_is_said_once_by_each_home_assistant(make_home, caplog):
    # The settings page asks every fifteen seconds, so it's said once, not each time - by
    # each HomeAssistant, which keeps its own, and by the guard's logger.
    with caplog.at_level("WARNING", logger="voice_assistant.ha_guard"):
        for _ in range(2):
            home = _scripted(make_home, {"script.deep": ("Deep", TooDeep("[[[["))}, LAMP)
            for _ in range(3):
                assert home.works_guarded("script.deep", *_now(home))
    assert _said_too_deep(caplog) == [("voice_assistant.ha_guard", TOO_DEEP)] * 2


def test_a_script_too_deep_to_walk_is_said_again_once_its_been_put_right(make_home, caplog):
    # Said once while it stays too deep - and again should it go too deep after it's been
    # put right, rather than never again until Nova restarts.
    home = _scripted(make_home, {"script.deep": ("Deep", TooDeep("[[[["))}, LAMP)
    with caplog.at_level("WARNING", logger="voice_assistant.ha_guard"):
        assert home.works_guarded("script.deep", *_now(home)) and home.works_guarded("script.deep", *_now(home))
        home._session.answers["/api/config/script/config/deep"] = _config(
            "Deep", _step("light.turn_on", entity_id="light.lamp"))
        assert not home.works_guarded("script.deep", *_now(home))
        home._session.answers["/api/config/script/config/deep"] = TooDeep("[[[[")
        assert home.works_guarded("script.deep", *_now(home)) and home.works_guarded("script.deep", *_now(home))
    assert _said_too_deep(caplog) == [("voice_assistant.ha_guard", TOO_DEEP)] * 2


def test_the_scripts_and_those_too_deep_are_only_given_by_name():
    # Both are kept by the caller and passed on, so they can never be given the wrong way round.
    scripts = ha_guard.Scripts(lambda key, name: None, lambda entity_ids: {})
    with pytest.raises(TypeError):
        ha_guard.why_not_run([], [], None, scripts, set())
    assert ha_guard.why_not_run([], [], None, scripts=scripts, too_deep=set()) is None


def test_a_script_run_by_two_others_is_read_once_a_refresh(make_home):
    lights = _config("Lights", _step("light.turn_on", entity_id="light.lamp"))
    home = _scripted(make_home, {"script.morning": ("Morning", _config("Morning", {"action": "script.lights"})),
                                 "script.evening": ("Evening", _config("Evening", {"action": "script.lights"})),
                                 "script.lights": ("Lights", lights)}, LAMP)
    assert [routine["id"] for routine in home.overview()["routines"]] == ["script.evening", "script.lights",
                                                                         "script.morning"]
    assert _paths(home).count("/api/config/script/config/lights") == 1
    home.overview()
    assert _paths(home).count("/api/config/script/config/lights") == 2


def test_a_lock_buried_in_a_scripts_choose_parallel_repeat_or_variables_is_found(make_home):
    unlock = _step("lock.unlock", entity_id="lock.front_door")
    # A helper whose door comes from its variables, which is the user's own template - so
    # only the variables it's given name the lock.
    helper = _config("Helper", _step("homeassistant.turn_off", entity_id="{{ door }}"))
    for buried in ({"choose": [{"conditions": [{"condition": "state", "entity_id": "light.lamp", "state": "on"}],
                                "sequence": [_step("light.turn_off", entity_id="light.lamp")]}], "default": [unlock]},
                   {"parallel": [_step("light.turn_on", entity_id="light.lamp"), {"sequence": [unlock]}]},
                   {"repeat": {"count": 2, "sequence": [unlock]}},
                   {"action": "script.turn_on", "target": {"entity_id": "script.helper"},
                    "data": {"variables": {"door": "lock.front_door"}}}):
        home = _scripted(make_home, {"script.away": ("Away", _config("Away", buried)), "script.helper": ("Helper", helper)},
                         LAMP, FRONT_DOOR)
        assert home.run_script("away") == {"success": False, "message": REFUSED["message"]}, buried
    assert not home.works_guarded("script.helper", *_now(home))


# Reading and working the lights

# The attributes the lights template asks Home Assistant for, as the settings page and Claude need them.
LIGHT_ATTRIBUTES = ("friendly_name", "brightness", "supported_color_modes", "color_mode", "hs_color",
                    "color_temp_kelvin", "min_color_temp_kelvin", "max_color_temp_kelvin")


def _rendered_light(entity, name, area, state="on", **attributes):
    """
    One light as the template renders it: its state and area's name, and every
    attribute asked for, null where Home Assistant has none - a light which is off
    has no brightness or colour. The capabilities are those of the user's lights.
    """
    shown = {"friendly_name": name, "supported_color_modes": ["color_temp", "hs"], "min_color_temp_kelvin": 2500,
             "max_color_temp_kelvin": 6500, **attributes}
    return {"entity_id": entity, "area": area, "state": state,
            "attributes": {key: shown.get(key) for key in LIGHT_ATTRIBUTES}}


def _rendered(*lights):
    """What Home Assistant answers the lights template with - plain text, like any template."""
    return FakeResponse(json.dumps(list(lights)))


# Home Assistant can give a colour temperature in colour mode, and a colour in colour temperature
# mode, so neither is to be taken for what the light is set to.
LAMP_BLUE = _rendered_light("light.lamp", "Lamp", "Bedroom", brightness=102, color_mode="hs",
                            hs_color=[240.0, 100.0], color_temp_kelvin=6500)
LAMP_OFF = _rendered_light("light.lamp", "Lamp", "Bedroom", state="off")
BEDROOM_LIGHT_WARM = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=102,
                                     color_mode="color_temp", color_temp_kelvin=2700, hs_color=[30.0, 56.0])


def test_lights_come_with_their_area_and_colour(make_home):
    home = make_home({"/api/template": _rendered(LAMP_BLUE)})
    assert home.lights() == [{"id": "light.lamp", "name": "Lamp", "area": "Bedroom", "on": True, "reachable": True,
                              "brightness": 40, "colour_modes": ["color_temp", "hs"], "hue": 240, "saturation": 100,
                              "kelvin": None, "min_kelvin": 2500, "max_kelvin": 6500, "aliases": []}]


def test_every_light_is_read_in_one_request(make_home):
    home = make_home({"/api/template": _rendered(LAMP_OFF, BEDROOM_LIGHT_WARM)})
    off, warm = home.lights()

    assert (off["on"], off["brightness"], off["hue"], off["saturation"], off["kelvin"]) == (False, None, None, None, None)
    assert off["colour_modes"] == ["color_temp", "hs"] and (off["min_kelvin"], off["max_kelvin"]) == (2500, 6500)
    assert (warm["brightness"], warm["kelvin"], warm["hue"], warm["saturation"]) == (40, 2700, None, None)
    (method, path, sent), = home._session.requests
    assert (method, path) == ("POST", "/api/template")
    assert "states.light" in sent["json"]["template"] and "area_name" in sent["json"]["template"]


def test_a_light_in_no_room_or_out_of_reach_is_still_a_light(make_home):
    hall = _rendered_light("light.hall", None, None, state="unavailable", supported_color_modes=None,
                           min_color_temp_kelvin=None, max_color_temp_kelvin=None)
    light, = make_home({"/api/template": _rendered(hall)}).lights()
    assert (light["name"], light["area"], light["reachable"], light["on"]) == ("light.hall", None, False, False)
    assert light["colour_modes"] == [] and (light["min_kelvin"], light["max_kelvin"]) == (None, None)


@pytest.mark.parametrize("answer, raises", [
    (requests.ConnectionError("down"), requests.ConnectionError),
    (requests.ReadTimeout("too slow"), requests.ReadTimeout),
    (FakeResponse("<html>not the lights</html>"), ValueError)])
def test_lights_which_cant_be_read_are_an_error_not_no_lights(make_home, answer, raises):
    with pytest.raises(raises):
        make_home({"/api/template": answer}).lights()


def test_claude_is_told_the_lights_and_the_room_nova_is_in(make_home):
    home = make_home({"/api/template": _rendered(LAMP_OFF, BEDROOM_LIGHT_WARM)})
    home.area = "Bedroom"
    assert home.describe_lights() == ("Nova is in the Bedroom. Lights: Bedroom Light (light.bedroom_light, Bedroom) "
                                      "on at 40%, 2700K; Lamp (light.lamp, Bedroom) off")


def test_a_light_set_to_a_colour_is_told_by_its_hue(make_home):
    home = make_home({"/api/template": _rendered(LAMP_BLUE)})
    assert home.describe_lights().endswith("Lamp (light.lamp, Bedroom) on at 40%, hue 240 saturation 100%")


def test_lights_are_told_by_room_with_novas_first(make_home):
    hall = _rendered_light("light.hall", "Hall", "Hall", state="unavailable")
    loft = _rendered_light("light.loft", "Loft", None, supported_color_modes=["onoff"])
    home = make_home({"/api/template": _rendered(hall, loft, LAMP_OFF)})
    home.area = "Bedroom"
    assert home.describe_lights() == ("Nova is in the Bedroom. Lights: Lamp (light.lamp, Bedroom) off; "
                                      "Hall (light.hall, Hall) unavailable; Loft (light.loft) on")


def test_claude_is_told_when_novas_room_isnt_known(make_home):
    home = make_home({"/api/template": _rendered(LAMP_OFF)})
    assert home.describe_lights() == "Nova's room isn't known. Lights: Lamp (light.lamp, Bedroom) off"


def test_claude_is_told_when_there_are_no_lights(make_home):
    home = make_home({"/api/template": _rendered()})
    home.area = "Bedroom"
    assert home.describe_lights() == "Nova is in the Bedroom. There are no lights."


@pytest.mark.parametrize("answer", [requests.ConnectionError("down"), requests.ReadTimeout("too slow"),
                                    FakeResponse("<html>not the lights</html>")])
def test_claude_is_told_nothing_when_the_lights_cant_be_read(make_home, answer):
    home = make_home({"/api/template": answer})
    home.area = "Bedroom"
    assert home.describe_lights() == ""


@pytest.mark.parametrize("odd", [{"state": "on", "attributes": {}},
                                 {"entity_id": "light.lamp", "attributes": {"color_mode": "hs", "hs_color": 240}},
                                 "light.lamp"])
def test_an_odd_light_leaves_claude_without_the_lights_not_without_a_reply(make_home, caplog, odd):
    # They're read with every request to Claude, so anything going wrong here would
    # lose the whole turn, rather than just the lights.
    home = make_home({"/api/template": _rendered(LAMP_OFF, odd)})
    assert home.describe_lights() == ""
    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_the_lights_can_be_asked_for_with_less_time_than_usual(make_home):
    # Claude is told the lights with every request, so a slow Home Assistant mustn't
    # hold each reply up for the usual five seconds.
    home = make_home({"/api/template": _rendered(LAMP_OFF)})
    home.lights(timeout=1.5)
    home.describe_lights(timeout=1.5)
    assert home._session.timeouts == [1.5, 1.5]


def test_the_lights_are_otherwise_given_the_usual_time(make_home):
    home = make_home({"/api/template": _rendered(LAMP_OFF)})
    home.lights()
    home.describe_lights()
    assert home._session.timeouts == [REQUEST_TIMEOUT, REQUEST_TIMEOUT]


def test_set_lights_defaults_to_novas_own_room(make_home):
    home = make_home({"/api/template": FakeResponse("bedroom"), "/api/services/light/turn_on": []})
    home.area = "Bedroom"

    assert home.set_lights(on=True, warmth=2700) == {"success": True, "message": "Done.", "chime": True}
    (_, _, asked), (_, service_path, sent) = home._session.requests
    assert 'area_id("Bedroom")' in asked["json"]["template"]
    assert (service_path, sent["json"]) == ("/api/services/light/turn_on",
                                            {"area_id": "bedroom", "color_temp_kelvin": 2700})


def test_set_lights_asks_which_room_when_novas_isnt_known(make_home):
    home = make_home({})
    assert home.set_lights(on=True) == {"success": False, "message": "Which room?"}
    assert home._session.requests == []


def test_set_lights_wont_touch_anything_but_a_light(make_home):
    home = make_home({})
    for entity in ("lock.front_door", "switch.adguard_protection", "light.lamp/../lock", "light.lamp, lock.front_door"):
        assert not home.set_lights(on=False, light=entity)["success"]
    assert home._session.requests == []


def test_a_named_light_is_switched_by_its_entity_id_whatever_room_nova_is_in(make_home):
    home = make_home({"/api/services/light/turn_off": []})
    home.area = "Kitchen"
    assert home.set_lights(on=False, light="light.lamp")["success"]
    assert [(path, sent["json"]) for _, path, sent in home._session.requests] == [
        ("/api/services/light/turn_off", {"entity_id": "light.lamp"})]


def test_a_named_room_is_looked_up_and_brightness_and_colour_are_passed_on(make_home):
    home = make_home({"/api/template": FakeResponse("living_room\n"), "/api/services/light/turn_on": []})
    assert home.set_lights(on=True, area="Living room", brightness=30, colour="Light Blue")["success"]
    assert [(path, sent["json"]) for _, path, sent in home._session.requests][1:] == [
        ("/api/services/light/turn_on", {"area_id": "living_room", "brightness_pct": 30, "color_name": "lightblue"})]


@pytest.mark.parametrize("asked, sent", [(150, 100), (0, 1), (-20, 1), ("40", 40)])
def test_brightness_stays_between_1_and_100(make_home, asked, sent):
    home = make_home({"/api/services/light/turn_on": []})
    home.set_lights(on=True, light="light.lamp", brightness=asked)
    assert home._session.requests[0][2]["json"]["brightness_pct"] == sent


def test_switching_off_asks_for_nothing_else(make_home):
    # There's no brightness or colour to give a turn_off.
    home = make_home({"/api/template": FakeResponse("bedroom"), "/api/services/light/turn_off": []})
    home.set_lights(on=False, area="Bedroom", brightness=30, colour="red", warmth=2700)
    assert home._session.requests[1][2]["json"] == {"area_id": "bedroom"}


def test_a_room_name_cant_break_out_of_the_template(make_home):
    # Claude makes up the room's name from what was said, and it goes into a template.
    name = 'x") }}{{ states | list }}{{ ("'
    home = make_home({"/api/template": FakeResponse("None")})
    home.set_lights(on=True, area=name)
    assert f"area_id({json.dumps(name)})" in home._session.requests[0][2]["json"]["template"]


@pytest.mark.parametrize("unknown", ["None", ""])
def test_a_room_home_assistant_hasnt_got_is_said_not_guessed(make_home, unknown):
    home = make_home({"/api/template": FakeResponse(unknown)})
    assert home.set_lights(on=True, area="Attic") == {"success": False, "message": "I don't know a room called Attic."}
    assert len(home._session.requests) == 1


@pytest.mark.parametrize("down", [{"/api/template": requests.ConnectionError("down")},
                                  {"/api/template": FakeResponse("bedroom"),
                                   "/api/services/light/turn_on": requests.ConnectionError("down")}])
def test_set_lights_says_when_home_assistant_cant_be_reached(make_home, down):
    home = make_home(down)
    home.area = "Bedroom"
    assert home.set_lights(on=True) == {"success": False, "message": "I can't reach Home Assistant right now."}


# The batteries and the people

def _rendered_battery(entity="sensor.s25_ultra_battery_level", state="46", unit="%", **shown):
    """One battery sensor as BATTERIES_TEMPLATE renders it - the S25's, unless a test says otherwise."""
    return {"entity_id": entity, "state": state, "unit": unit, "changed": 1761200000.0,
            "friendly_name": "S25 Ultra Battery level", "device": "dev25", "device_name": "SM-S938B",
            "device_name_by_user": "s25-ultra", "maker": "samsung", "model": "SM-S938B",
            "charging": "discharging", "app": True, **shown}


def test_batteries_are_read_with_their_device_and_charging_state(make_home):
    home = make_home({"/api/template": FakeResponse(json.dumps([_rendered_battery()]))})
    assert home.batteries() == [{"id": "sensor.s25_ultra_battery_level", "name": "s25-ultra", "level": 46.0,
                                 "changed": 1761200000.0, "device": "dev25", "maker": "samsung",
                                 "model": "SM-S938B", "charging": "discharging", "app": True}]
    (method, path, sent), = home._session.requests
    assert (method, path) == ("POST", "/api/template")
    assert "battery_charging" in sent["json"]["template"] and "battery_state" in sent["json"]["template"]
    # "changed" is when Home Assistant last heard the level, not when it last moved: a level that holds
    # steady (a phone full on its charger) would otherwise read as old.
    assert "battery.last_reported | as_timestamp" in sent["json"]["template"]
    # "app" says whether the sensor belongs to the Home Assistant app, which is how a phone, tablet or
    # any other mobile device is told from a sensor without a list of makers.
    assert "integration_entities('mobile_app')" in sent["json"]["template"]
    assert "last_changed" not in sent["json"]["template"]


def test_a_charging_sensor_that_isnt_on_or_off_leaves_the_battery_state_to_say(make_home):
    # An unavailable battery_charging binary sensor would otherwise be the charging word, and hide
    # the readable battery_state sensor beside it. Jinja can't be rendered here, so the guard is read.
    home = make_home({"/api/template": FakeResponse(json.dumps([_rendered_battery()]))})
    home.batteries()
    template = home._session.requests[0][2]["json"]["template"]
    assert "entity.startswith('binary_sensor.') and states(entity) in ['on', 'off']" in template


def test_an_unreadable_battery_is_left_out(make_home):
    rows = [_rendered_battery(f"sensor.b{n}", state) for n, state in enumerate(["unavailable", "unknown", "", "nan"])]
    rows.append(_rendered_battery("sensor.fine", "80"))
    assert [b["id"] for b in make_home({"/api/template": FakeResponse(json.dumps(rows))}).batteries()] == ["sensor.fine"]


def test_a_decimal_level_is_read_and_other_units_are_left_out(make_home):
    rows = [_rendered_battery("sensor.mac", "15.0"), _rendered_battery("sensor.cell", "3.1", unit="V"),
            _rendered_battery("sensor.bare", "50", unit=None)]
    found = make_home({"/api/template": FakeResponse(json.dumps(rows))}).batteries()
    assert [(b["id"], b["level"]) for b in found] == [("sensor.mac", 15.0)]


def test_a_level_outside_zero_to_a_hundred_is_left_out(make_home):
    # Some integrations report -1 for "don't know", and 255 for the same: neither is a level, and -1 would be
    # a warning for nothing.
    rows = [_rendered_battery(f"sensor.b{n}", state) for n, state in enumerate(["-1", "255", "100.5", "0", "100"])]
    found = make_home({"/api/template": FakeResponse(json.dumps(rows))}).batteries()
    assert [(b["id"], b["level"]) for b in found] == [("sensor.b3", 0.0), ("sensor.b4", 100.0)]


def test_a_battery_with_no_device_is_named_by_its_friendly_name(make_home):
    row = _rendered_battery("sensor.spare", "70", friendly_name="Spare", device=None, device_name=None,
                            device_name_by_user=None, maker=None, model=None, charging=None, app=False)
    battery, = make_home({"/api/template": FakeResponse(json.dumps([row]))}).batteries()
    assert (battery["name"], battery["device"], battery["maker"], battery["model"], battery["charging"], battery["app"]) == \
        ("Spare", None, None, None, None, False)
    unnamed = _rendered_battery(device_name_by_user=None)
    assert make_home({"/api/template": FakeResponse(json.dumps([unnamed]))}).batteries()[0]["name"] == "SM-S938B"


def test_people_come_with_their_trackers_devices(make_home):
    rows = [{"entity_id": "person.nathaniel", "name": "Nathaniel", "state": "not_home",
             "devices": ["dev25", "devwatch"]},
            {"entity_id": "person.guest", "name": None, "state": "home", "devices": []}]
    home = make_home({"/api/template": FakeResponse(json.dumps(rows))})
    # A person with no name of their own is called by their id, so the page always has something to show.
    assert home.people() == [
        {"id": "person.nathaniel", "name": "Nathaniel", "state": "not_home", "devices": ["dev25", "devwatch"]},
        {"id": "person.guest", "name": "person.guest", "state": "home", "devices": []}]
    template = home._session.requests[0][2]["json"]["template"]
    assert "states.person" in template and "person.name" in template


# What's playing

def test_whats_playing_is_read_with_its_room_device_and_source(make_home):
    rows = [{"entity_id": "media_player.spotify_nathan", "name": "Spotify Nathan", "area": None, "device": None,
             "source": "Voice-Assistant"},
            {"entity_id": "media_player.bedroom_tv", "name": "Bedroom TV", "area": "Bedroom", "device": "devtv",
             "source": None}]
    home = make_home({"/api/template": FakeResponse(json.dumps(rows))})
    assert home.playing_media() == rows
    (method, path, sent), = home._session.requests
    assert (method, path) == ("POST", "/api/template")
    assert home._session.timeouts == [2]


def test_only_players_playing_are_rendered_with_what_the_watcher_needs(make_home):
    home = make_home({"/api/template": FakeResponse("[]")})
    assert home.playing_media() == []
    template = home._session.requests[0][2]["json"]["template"]
    # Exactly "playing": paused, idle, off and unavailable players are left in Home Assistant.
    assert "states.media_player | selectattr('state', 'eq', 'playing')" in template
    assert "area_name(player.entity_id)" in template and "device_id(player.entity_id)" in template
    assert "attributes.get('source')" in template
    assert "attributes.get('friendly_name') or player.entity_id" in template
    assert "| tojson" in template


def test_no_value_of_novas_is_written_into_the_playing_template(make_home):
    # Each satellite's room and device are compared in Python, so the template's text is the same whatever they are.
    home = make_home({"/api/template": FakeResponse("[]")})
    home.area, home.device_id = 'Bed") }}{{ states | list }}', "devnova"
    home.playing_media()
    template = home._session.requests[0][2]["json"]["template"]
    assert template == home_assistant.PLAYING_MEDIA_TEMPLATE
    assert "Bed" not in template and "devnova" not in template


@pytest.mark.parametrize("answer, raises", [
    (requests.ConnectionError("down"), requests.ConnectionError),
    (FakeResponse("", status=403), requests.HTTPError),
    (FakeResponse("<html>not JSON</html>"), ValueError)])
def test_whats_playing_raises_when_home_assistant_cant_say(make_home, answer, raises):
    with pytest.raises(raises):
        make_home({"/api/template": answer}).playing_media()


# The Home Assistant app's devices, as live check 4 found them

def _rendered_app_device(device="dev25", own_name="s25-ultra", **shown):
    """One device as APP_DEVICES_TEMPLATE renders it - the S25's, unless a test says otherwise."""
    return {"device": device, "own_name": own_name, "name_by_user": None, "maker": "samsung", "model": "SM-S938B",
            "tracker": {"state": "not_home", "reported": 1761200000.0},
            "place": {"state": "1 Example Road, Sometown, EX1 1AA, UK",
                      "attributes": [["thoroughfare", "Example Road"], ["locality", "Sometown"]]}, **shown}


NOTIFY_SERVICES = [{"domain": "light", "services": {"turn_on": {}}},
                   {"domain": "notify", "services": {"mobile_app_s25_ultra": {}, "notify": {}, "send_message": {}}}]


def test_a_devices_notify_service_is_found_by_its_name(make_home):
    mac = _rendered_app_device("devmac", "MacBook Pro", name_by_user="Work Mac", maker="Apple", model="Mac16,8",
                               tracker=None, place={"state": "Example Road, Sometown",
                                                    "attributes": [["Thoroughfare", "Example Road"],
                                                                   ["Locality", "Sometown"]]})
    home = make_home({"/api/template": FakeResponse(json.dumps([_rendered_app_device(), mac])),
                      "/api/services": NOTIFY_SERVICES})
    assert home.app_devices() == [
        {"device": "dev25", "name": "s25-ultra", "maker": "samsung", "model": "SM-S938B",
         "notify": "mobile_app_s25_ultra", "tracker": {"state": "not_home", "reported": 1761200000.0},
         "place": {"state": "1 Example Road, Sometown, EX1 1AA, UK", "thoroughfare": "Example Road", "locality": "Sometown"}},
        # The user's name for it is said, but its notify service is named after its own. /api/services
        # hasn't got mobile_app_macbook_pro, so it has none - and the Mac's capitals are read all the same.
        {"device": "devmac", "name": "Work Mac", "maker": "Apple", "model": "Mac16,8", "notify": None,
         "tracker": None, "place": {"state": "Example Road, Sometown", "thoroughfare": "Example Road", "locality": "Sometown"}}]
    template = home._session.requests[0][2]["json"]["template"]
    assert "integration_entities('mobile_app')" in template and "geocoded_location" in template
    # Nothing but the two parts of the place leaves Home Assistant - never its coordinates.
    assert "thoroughfare" in template and "locality" in template and "| lower" in template


def test_a_device_with_no_tracker_or_place_has_none(make_home):
    row = _rendered_app_device(tracker=None, place=None)
    home = make_home({"/api/template": FakeResponse(json.dumps([row])), "/api/services": []})
    found, = home.app_devices()
    assert (found["notify"], found["tracker"], found["place"]) == (None, None, None)


def test_the_app_is_sent_a_notification_with_no_target(make_home):
    home = make_home({"/api/services/notify/mobile_app_s25_ultra": []})
    home.notify("mobile_app_s25_ultra", "TTS", data={"tts_text": "Hello."})
    assert home._session.requests == [
        ("POST", "/api/services/notify/mobile_app_s25_ultra", {"json": {"message": "TTS", "data": {"tts_text": "Hello."}}})]
    home.notify("mobile_app_s25_ultra", "Hello.", title="Nova")
    assert home._session.requests[-1][2] == {"json": {"message": "Hello.", "title": "Nova"}}


@pytest.mark.parametrize("service, data", [
    ("notify", {"message": "Hello."}),
    ("send_message", {"message": "Hello.", "entity_id": "notify.mobile_app_s25_ultra"}),
    ("persistent_notification", {"message": "Hello."}),
    ("mobile_app_", {"message": "Hello."}),
    ("mobile_app_S25", {"message": "Hello."}),
    ("mobile_app_s25_ultra", {"message": "Hello.", "entity_id": "notify.mobile_app_s25_ultra"}),
    ("mobile_app_s25_ultra", {"message": "Hello.", "device_id": "dev25"}),
    # Given no target at all - not notify's own, nor one inside its data.
    ("mobile_app_s25_ultra", {"message": "Hello.", "target": ["mobile_app_other"]}),
    ("mobile_app_s25_ultra", {"message": "Hello.", "area_id": "hall"}),
    ("mobile_app_s25_ultra", {"message": "Hello.", "floor_id": "upstairs"}),
    ("mobile_app_s25_ultra", {"message": "Hello.", "label_id": "phones"}),
    ("mobile_app_s25_ultra", {"message": "Hello.", "data": {"target": ["mobile_app_other"]}}),
])
def test_only_the_apps_notify_services_are_called(make_home, service, data):
    home = make_home({f"/api/services/notify/{service}": []})
    with pytest.raises(NotAllowed):
        home._call_service("notify", service, data)
    with pytest.raises(NotAllowed):
        home._call_service("notify", "mobile_app_s25_ultra", {"message": "Hello."}, returns=True)
    assert home._session.requests == []


def test_only_the_pcs_buttons_are_ever_pressed(make_home):
    # The Wake-on-LAN button's made up: a real one's id holds the PC's hardware address.
    home = make_home({"/api/services/button/press": []})
    home.registry.read = _registry(
        _entry("button.desk_off", "mqtt", "desk", "Shutdown"),
        _entry("button.desk_web", "mqtt", "desk", "LaunchUrl"),
        _entry("button.wake_desk_pc", "wake_on_lan"),
        _entry("button.pve1_shutdown", "proxmoxve", "pve1", "Shutdown"),
        _entry("button.nova_restart", "mqtt", "nova", "Restart"),
        devices={"desk": {"name": "Desk PC", "maker": "HASS.Agent Team"},
                 "pve1": {"name": "pve1", "maker": "Proxmox"}, "nova": {"name": "Nova", "maker": "Nova"}})
    home.press("button.desk_off")
    home.press("button.wake_desk_pc")
    assert [sent["json"] for _, _, sent in home._session.requests] == [{"entity_id": "button.desk_off"},
                                                                        {"entity_id": "button.wake_desk_pc"}]

    # HASS.Agent's launchurl, a Proxmox button, one on no HASS.Agent device, and one the
    # registry hasn't got - nor several at once, or anything but a button.
    for data in ({"entity_id": "button.desk_web"}, {"entity_id": "button.pve1_shutdown"},
                 {"entity_id": "button.nova_restart"}, {"entity_id": "button.new_one"},
                 {"entity_id": ["button.desk_off", "button.pve1_shutdown"]}, {"entity_id": "switch.desk_off"},
                 {"area_id": "office"}, {}):
        with pytest.raises(NotAllowed):
            home._call_service("button", "press", data)
    # Without a snapshot, not even the PC's own.
    home.registry.read = None
    for entity in ("button.desk_off", "button.wake_desk_pc"):
        with pytest.raises(NotAllowed):
            home.press(entity)
    assert len(home._session.requests) == 2


def _rings(make_home, answer):
    """What find_device says, ringing the S25 through the real notify and _call_service, which answer so."""
    home = make_home({"/api/template": FakeResponse(json.dumps([_rendered_app_device()])),
                      "/api/services": NOTIFY_SERVICES, "/api/services/notify/mobile_app_s25_ultra": answer})
    result = ActionExecutor(home=home).execute(Action("find_device", {"device": "phone", "ring": True}))
    assert home._session.requests[-1][1] == "/api/services/notify/mobile_app_s25_ultra"
    return result


def test_ringing_is_only_said_once_home_assistant_answered_it(make_home):
    assert _rings(make_home, []).say.endswith(" Ringing your phone.")
    result = _rings(make_home, FakeResponse("", status=500))
    assert (result.ok, result.say.split(". ")[-1]) == (False, "I couldn't ring your phone.")
    # Sent but unanswered, it may have rung - so that's what's said.
    result = _rings(make_home, requests.Timeout("slow"))
    assert not result.ok and result.say.endswith(
        " Home Assistant didn't answer in time, so I can't tell whether your phone rang.")
    assert "Ringing" not in result.say


MAC_SERVICES = [{"domain": "notify", "services": {"mobile_app_macbook_pro": {}}}]


def _sends(make_home, answer, action="send_to_phone", **params):
    """What an action says, going through the real notify and _call_service to the Mac, which answer so."""
    mac = _rendered_app_device("devmac", "MacBook Pro", maker="Apple", model="Mac16,8")
    home = make_home({"/api/template": FakeResponse(json.dumps([mac])), "/api/services": MAC_SERVICES,
                      "/api/services/notify/mobile_app_macbook_pro": answer})
    executor = ActionExecutor(home=home)
    executor.last_answer = ("asked", "said")
    result = executor.execute(Action(action, {"device": "mac", **params}))
    assert home._session.requests[-1][1] == "/api/services/notify/mobile_app_macbook_pro"
    return result


def test_sending_is_only_said_once_home_assistant_answered_it(make_home):
    result = _sends(make_home, [])
    assert (result.ok, result.say, result.confirm) == (True, "Sent.", "chime")
    result = _sends(make_home, FakeResponse("", status=500))
    assert (result.ok, result.say, result.confirm) == (False, "I couldn't send that to your MacBook Pro.", "speak")
    result = _sends(make_home, requests.Timeout("slow"))
    assert (result.ok, result.say, result.confirm) == (
        False, "Home Assistant didn't answer in time, so I can't tell whether that was sent to your MacBook Pro.",
        "speak")


def test_a_refused_token_is_said_for_sending_and_ringing(make_home):
    token = "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."
    assert _sends(make_home, FakeResponse("", status=401)).say == token
    assert _sends(make_home, FakeResponse("", status=401), "find_device", ring=True).say.endswith(token)


@pytest.mark.parametrize("ask", ["batteries", "people", "app_devices"])
@pytest.mark.parametrize("answer, raises", [
    (requests.ConnectionError("down"), requests.ConnectionError),
    (FakeResponse("", status=403), requests.HTTPError),
    (FakeResponse("<html>not JSON</html>"), ValueError)])
def test_a_refused_template_raises_as_for_the_lights(make_home, ask, answer, raises):
    with pytest.raises(raises):
        getattr(make_home({"/api/template": answer}), ask)()


# The lights by their other names, and dimming them

def _other_names(**aliases):
    """A registry snapshot of just these lights, with these other names, e.g. light_bedroom_light=["main light"]."""
    return _registry(*[_entry(key.replace("_", ".", 1), aliases=names) for key, names in aliases.items()])


KITCHEN_LIGHT = _rendered_light("light.kitchen_light", "Kitchen Light", "Kitchen", brightness=255)
TURN_ON = "/api/services/light/turn_on"
TURN_OFF = "/api/services/light/turn_off"


def _dimmed(home):
    """What the lights were told, as [(service, data)]."""
    return [(path.rsplit("/", 1)[1], sent["json"]) for _, path, sent in home._session.requests
            if path.startswith("/api/services/")]


def _house(make_home, *lights, area="Bedroom"):
    """A house with these lights, Nova in the area, and every service call answered."""
    home = make_home({"/api/template": _rendered(*lights), TURN_ON: FakeResponse(""), TURN_OFF: FakeResponse("")})
    home.area = area
    return home


def test_lights_come_with_their_other_names(make_home):
    home = make_home({"/api/template": _rendered(LAMP_OFF, BEDROOM_LIGHT_WARM)})
    home.snapshot = lambda: _other_names(light_bedroom_light=["main light"])
    lamp, bedroom = home.lights()
    assert (lamp["aliases"], bedroom["aliases"]) == ([], ["main light"])
    home.snapshot = lambda: None
    assert [light["aliases"] for light in home.lights()] == [[], []]


def test_claude_is_told_the_lights_other_names(make_home):
    home = make_home({"/api/template": _rendered(LAMP_OFF, BEDROOM_LIGHT_WARM)})
    home.area = "Bedroom"
    home.snapshot = lambda: _other_names(light_bedroom_light=["main light"], light_lamp=["reading", "desk"])
    assert home.describe_lights() == (
        "Nova is in the Bedroom. Lights: Bedroom Light, also main light (light.bedroom_light, Bedroom) on at 40%, "
        "2700K; Lamp, also reading or desk (light.lamp, Bedroom) off")


def test_lights_are_found_by_name_other_name_or_room(make_home):
    home = _house(make_home, LAMP_OFF, BEDROOM_LIGHT_WARM, KITCHEN_LIGHT)
    home.snapshot = lambda: _other_names(light_bedroom_light=["main light"])
    lights = home._in_order(home.lights())

    def called(words):
        found = home.lights_called(words, lights)
        return found if found is None else [light["id"] for light in found]

    assert called("the lights") == ["light.bedroom_light", "light.lamp"]
    assert called("all the lights") == called("my lights") == called("lights")
    assert called("main light") == ["light.bedroom_light"]
    assert called("the Bedroom Light!") == ["light.bedroom_light"]
    assert called("lamp") == ["light.lamp"]
    assert called("bedroom lights") == ["light.bedroom_light", "light.lamp"]
    assert called("kitchen lights") == ["light.kitchen_light"]
    assert called("attic lights") is None and called("the toaster") is None


def test_a_name_or_room_starting_with_the_or_my_is_found_as_said(make_home):
    # A leading "the", "my" or "all" comes off the names as it does off what was said,
    # so "my room lights" is the room called "My Room", not one called "room".
    big = _rendered_light("light.big_lamp", "The Big Lamp", "My Room")
    home = _house(make_home, big, KITCHEN_LIGHT)
    home.snapshot = lambda: _other_names(light_big_lamp=["the reading light"])
    lights = home._in_order(home.lights())
    for words in ("the big lamp", "Big Lamp", "reading light", "the reading light", "my room lights",
                  "all the My Room lights"):
        assert [light["id"] for light in home.lights_called(words, lights)] == ["light.big_lamp"], words


def test_a_light_called_all_lights_doesnt_take_the_place_of_the_rooms_lights(make_home):
    # Trimmed like what was said, "All Lights" was "lights" - so "dim the lights" dimmed
    # it alone, rather than the room's lights.
    every = _rendered_light("light.all_lights", "All Lights", "Bedroom")
    home = _house(make_home, LAMP_OFF, BEDROOM_LIGHT_WARM, every, KITCHEN_LIGHT)
    lights = home._in_order(home.lights())
    for words in ("the lights", "lights", "all the lights"):
        assert sorted(light["id"] for light in home.lights_called(words, lights)) == [
            "light.all_lights", "light.bedroom_light", "light.lamp"], words


def test_lamp_means_the_rooms_lights_when_no_light_is_called_that(make_home):
    # As "lights" does: "dim the lamps", with only a ceiling light in the bedroom.
    home = _house(make_home, BEDROOM_LIGHT_WARM, KITCHEN_LIGHT)
    lights = home._in_order(home.lights())
    for words in ("lamp", "the lamps", "bedroom lamps"):
        assert [light["id"] for light in home.lights_called(words, lights)] == ["light.bedroom_light"], words
    assert [light["id"] for light in home.lights_called("kitchen lamp", lights)] == ["light.kitchen_light"]


def test_a_name_shared_across_rooms_is_novas_rooms_light(make_home):
    # As Assist has it: "dim the lamp", with a Lamp in the bedroom and one in the kitchen,
    # is the bedroom's when Nova's there.
    kitchen_lamp = _rendered_light("light.kitchen_lamp", "Lamp", "Kitchen", brightness=204)
    bedroom_lamp = _rendered_light("light.lamp", "Lamp", "Bedroom", brightness=204)
    home = _house(make_home, bedroom_lamp, kitchen_lamp)
    assert home.dim("down", "the lamp")["success"]
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.lamp"], "brightness_pct": 50})]
    assert home.switch_lights(False, "the lamp")["success"]
    assert _dimmed(home)[-1] == ("turn_off", {"entity_id": ["light.lamp"]})
    # Elsewhere, or with no Lamp in Nova's room, it's both, as before.
    for area in ("Hall", None):
        home.area = area
        assert [light["id"] for light in home.lights_called("lamp", home.lights())] == ["light.lamp",
                                                                                      "light.kitchen_lamp"]


def test_dimming_steps_each_light_down_from_where_it_is(make_home):
    lamp = _rendered_light("light.lamp", "Lamp", "Bedroom", brightness=204)
    bedroom = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=51)
    home = _house(make_home, lamp, bedroom)
    result = home.dim("down", "the lights")
    assert result["success"] and result["chime"]
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.bedroom_light"], "brightness_pct": 5}),
                             ("turn_on", {"entity_id": ["light.lamp"], "brightness_pct": 50})]


def test_lights_at_the_same_brightness_are_dimmed_in_one_call(make_home):
    lamp = _rendered_light("light.lamp", "Lamp", "Bedroom", brightness=204)
    bedroom = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=204)
    home = _house(make_home, lamp, bedroom)
    home.dim("down", "lights")
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.bedroom_light", "light.lamp"], "brightness_pct": 50})]


def test_dimming_never_switches_a_light_off(make_home):
    floor = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=13)
    off = _rendered_light("light.lamp", "Lamp", "Bedroom", state="off")
    switch = _rendered_light("light.plug", "Plug", "Bedroom", supported_color_modes=["onoff"])
    gone = _rendered_light("light.hall", "Hall", "Bedroom", state="unavailable")
    home = _house(make_home, floor, off, switch, gone)
    assert home.dim("down", "bedroom lights") == {"success": True, "message": "They're as dim as they go."}
    assert _dimmed(home) == []

    # Below the floor already, a light is left alone rather than brightened.
    quiet = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=3)
    home = _house(make_home, quiet)
    assert home.dim("down", "lights")["message"] == "They're as dim as they go." and _dimmed(home) == []

    # The others still dim, and nothing is ever switched off.
    bright = _rendered_light("light.kitchen_light", "Kitchen Light", "Bedroom", brightness=255)
    home = _house(make_home, bright, off, switch, gone)
    home.dim("down", "lights")
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.kitchen_light"], "brightness_pct": 70})]
    assert not [call for call in _dimmed(home) if call[0] == "turn_off"]


def test_lights_which_only_switch_are_said_to_be_so(make_home):
    plug = _rendered_light("light.plug", "Plug", "Bedroom", supported_color_modes=["onoff"])
    home = _house(make_home, plug)
    assert home.dim("down", "lights")["message"] == "Those lights only switch on and off."
    assert home.dim("up", "lights")["message"] == "Those lights only switch on and off."
    assert _dimmed(home) == []


def test_brightening_switches_an_off_light_on_at_one_step(make_home):
    off = _rendered_light("light.lamp", "Lamp", "Bedroom", state="off")
    high = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=230)
    home = _house(make_home, off, high)
    assert home.dim("up", "lights")["success"]
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.bedroom_light"], "brightness_pct": 100}),
                             ("turn_on", {"entity_id": ["light.lamp"], "brightness_pct": 30})]


def test_brightening_the_brightest_changes_nothing(make_home):
    full = _rendered_light("light.lamp", "Lamp", "Bedroom", brightness=255)
    switch = _rendered_light("light.plug", "Plug", "Bedroom", supported_color_modes=["onoff"])
    gone = _rendered_light("light.hall", "Hall", "Bedroom", state="unavailable")
    home = _house(make_home, full, switch, gone)
    assert home.dim("up", "lights")["message"] == "They're as bright as they go."
    assert _dimmed(home) == []


def test_dimming_a_light_nova_doesnt_know_goes_to_claude(make_home):
    home = _house(make_home, LAMP_OFF)
    assert home.dim("down", "the attic lights") == {"success": False, "message": "I don't know a light called that.",
                                                    "not_understood": True}
    assert _dimmed(home) == []


def test_dimming_with_no_room_known_asks_which(make_home):
    home = _house(make_home, BEDROOM_LIGHT_WARM, area=None)
    assert home.dim("down", "the lights") == {"success": False, "message": "Which room?"}
    assert _dimmed(home) == []


def test_dimming_with_home_assistant_down_says_so(make_home):
    home = make_home({"/api/template": requests.ConnectionError("down")})
    assert home.dim("down", "lights") == {"success": False, "message": "I can't reach Home Assistant right now."}


@pytest.mark.parametrize("direction", ["down", "up"])
def test_a_light_that_cant_be_reached_is_said_so_not_called_as_dim_as_it_goes(make_home, direction):
    gone = _rendered_light("light.lamp", "Lamp", "Bedroom", state="unavailable")
    home = _house(make_home, gone)
    assert home.dim(direction, "the lamp") == {"success": False, "message": "Home Assistant can't reach that light."}
    assert _dimmed(home) == []


def test_lights_that_cant_be_reached_are_said_so(make_home):
    gone = _rendered_light("light.lamp", "Lamp", "Bedroom", state="unavailable")
    also = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", state="unavailable")
    home = _house(make_home, gone, also)
    assert home.dim("up", "lights") == {"success": False, "message": "Home Assistant can't reach those lights."}
    assert _dimmed(home) == []


class FailsAfter(FakeResponse):
    """Answers a service call the first few times, then fails as Home Assistant with a problem does."""

    def __init__(self, answered):
        super().__init__("")
        self.answered = answered

    def raise_for_status(self):
        self.answered -= 1
        if self.answered < 0:
            raise requests.HTTPError("500", response=FakeResponse("", status=500))


def test_a_failure_partway_through_dimming_says_how_many_lights_changed(make_home):
    # Each brightness is a call of its own, so the lights already dimmed stay dimmed.
    lamp = _rendered_light("light.lamp", "Lamp", "Bedroom", brightness=204)
    bedroom = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=51)
    kitchen = _rendered_light("light.kitchen_light", "Kitchen Light", "Bedroom", brightness=51)
    home = _house(make_home, lamp, bedroom, kitchen)
    home._session.answers[TURN_ON] = FailsAfter(1)
    assert home.dim("down", "the lights") == {
        "success": False, "message": "Home Assistant had a problem with that. Only 2 of the 3 lights changed."}
    # A failure at the first call is said as ever, with nothing changed.
    home = _house(make_home, lamp, bedroom)
    home._session.answers[TURN_ON] = FailsAfter(0)
    assert home.dim("down", "the lights") == {"success": False, "message": "Home Assistant had a problem with that."}


def test_only_the_lights_that_can_be_reached_are_dimmed(make_home):
    gone = _rendered_light("light.lamp", "Lamp", "Bedroom", state="unavailable")
    bright = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", brightness=255)
    home = _house(make_home, gone, bright)
    assert home.dim("down", "lights")["success"]
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.bedroom_light"], "brightness_pct": 70})]


# A light sentence the guard refuses

def test_a_refused_light_sentence_is_done_by_novas_own_lights(make_home):
    porch = _rendered_light("light.porch", "Porch light", "Outside", state="off")
    home = _house(make_home, porch)
    home._session.answers["/api/states"] = [_thing("lock.front_door", "Front door"),
                                            _thing("light.porch", "Porch light")]
    home._session.answers["/api/conversation/process"] = _assist("action_done", "Done")
    home.snapshot = lambda: _registry(_entry("lock.front_door"), _entry("light.porch", aliases=["front door light"]))
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "turn on the front door light", "on": True, "words": "the front door light"}))
    assert (result.ok, result.confirm) == (True, "chime")
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.porch"]})]
    assert "/api/conversation/process" not in [path for _, path, _ in home._session.requests]


def test_a_light_sentence_refused_by_its_words_is_done_by_novas_own_lights(make_home):
    # "Garage" is never said to Assist, whatever's called that - but a light is only ever
    # a light, so Nova's own lights switch it.
    garage = _rendered_light("light.garage_light", "Garage light", "Garage", state="off")
    home = _house(make_home, garage)
    home._session.answers["/api/states"] = [_thing("light.garage_light", "Garage light")]
    home._session.answers["/api/conversation/process"] = _assist("action_done", "Done")
    action, = LocalIntents(home=home).match("Turn on the garage light.")["actions"]
    result = ActionExecutor(home=home).execute(action)
    assert (result.ok, result.confirm) == (True, "chime")
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.garage_light"]})]
    assert "/api/conversation/process" not in _paths(home) and home.socket.calls == []


def test_the_log_says_a_refused_light_sentence_was_handed_to_novas_own_lights(make_home, caplog):
    # Refused only for Assist, so the log doesn't leave it at that - and says what was
    # named for what it is, which here is the PC's shutdown button, not a lock or a door.
    porch = _rendered_light("light.porch", "Porch light", "Outside", state="off")
    home = _house(make_home, porch)
    home._session.answers["/api/states"] = [_thing("button.desk_pc_shutdown", "Shutdown"),
                                            _thing("light.porch", "Porch light")]
    home.snapshot = lambda: _registry(_entry("button.desk_pc_shutdown", "mqtt", "desk", "Shutdown"),
                                      _entry("light.porch", aliases=["shutdown light"]), devices=DESK)
    with caplog.at_level("INFO", logger="voice_assistant.home_assistant"):
        result = ActionExecutor(home=home).execute(Action("control_home", {
            "command": "turn on the shutdown light", "on": True, "words": "the shutdown light"}))
    assert result.ok and _dimmed(home) == [("turn_on", {"entity_id": ["light.porch"]})]
    assert caplog.messages[:2] == [
        "Refused a Home Assistant request naming something never worked by voice: 'turn on the shutdown light'",
        "Handed to Nova's own lights instead, which only ever work lights: 'the shutdown light'"]


def test_a_light_sentence_that_fails_for_another_reason_isnt_done_by_novas_own_lights(make_home):
    # Only a refusal is handed over: with Home Assistant's states out of reach, the light
    # sentence says so rather than switching the lamp regardless.
    home = _house(make_home, LAMP_OFF)
    home._session.answers["/api/states"] = requests.ConnectionError("down")
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "turn on the lamp", "on": True, "words": "the lamp"}))
    assert (result.ok, result.say) == (False, "I can't reach Home Assistant right now.")
    assert _dimmed(home) == [] and "/api/template" not in _paths(home)


def test_a_guarded_thing_without_a_name_doesnt_refuse_a_light_sentence(make_home):
    # No name at all is never taken for one found in every request - and a lock whose
    # name is empty or missing is still known by its entity id's words.
    states = [_thing("lock.front_door", None), _thing("lock.back_door", ""), _thing("light.lamp", "Lamp")]
    home = make_home({"/api/states": states, "/api/conversation/process": _assist("action_done", "Done")})
    assert home.command("turn on the lamp")["success"]
    assert home.command("turn on the front door") == REFUSED and home.command("turn on the back door") == REFUSED


def test_a_refused_light_sentence_naming_no_light_goes_to_claude(make_home):
    home = _house(make_home, LAMP_OFF)
    home._session.answers["/api/states"] = [_thing("lock.front_door", "Front door")]
    result = ActionExecutor(home=home).execute(Action("control_home", {
        "command": "turn on the front door light", "on": True, "words": "the front door light"}))
    assert (result.ok, result.understood) == (False, False)
    assert _dimmed(home) == []


def test_lights_are_switched_by_what_theyre_called(make_home):
    home = _house(make_home, LAMP_OFF, BEDROOM_LIGHT_WARM, KITCHEN_LIGHT)
    assert home.switch_lights(True, "the bedroom lights", 30) == {"success": True, "message": "Done.", "chime": True}
    home.switch_lights(False, "the lamp", 30)
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.bedroom_light", "light.lamp"], "brightness_pct": 30}),
                             ("turn_off", {"entity_id": ["light.lamp"]})]
    assert home.switch_lights(True, "the attic lights") == {
        "success": False, "message": "I don't know a light called that.", "not_understood": True}
    home.area = None
    assert home.switch_lights(True, "the lights") == {"success": False, "message": "Which room?"}


def test_lights_that_cant_be_reached_arent_said_to_be_switched(make_home):
    gone = _rendered_light("light.lamp", "Lamp", "Bedroom", state="unavailable")
    also = _rendered_light("light.bedroom_light", "Bedroom Light", "Bedroom", state="unavailable")
    home = _house(make_home, gone, also)
    assert home.switch_lights(True, "the lamp") == {"success": False, "message": "Home Assistant can't reach that light."}
    assert home.switch_lights(False, "the lights") == {
        "success": False, "message": "Home Assistant can't reach those lights."}
    assert _dimmed(home) == []


def test_a_refused_light_set_to_0_percent_is_switched_off(make_home):
    # "Garage" is never said to Assist, so Nova's own lights do it - off, as Home
    # Assistant takes 0%, not on at 1%. The lamp's sentence goes to Assist as ever.
    garage = _rendered_light("light.garage_light", "Garage light", "Garage", brightness=255)
    home = _house(make_home, garage, LAMP_BLUE)
    home._session.answers["/api/conversation/process"] = _assist("action_done", "Turned off the lamp")
    for said in ("Set the garage light to 0 percent.", "Set the lamp to 0 percent."):
        action, = LocalIntents().match(said)["actions"]
        assert ActionExecutor(home=home).execute(action).ok, said
    assert _dimmed(home) == [("turn_off", {"entity_id": ["light.garage_light"]})]
    assert _asked(home)["text"] == "set the lamp to 0 percent"


def test_the_offline_fallback_sends_a_light_sentence_but_not_one_joining_more(make_home):
    # With Claude out of reach, its keyword fallback hands Assist the whole sentence. One
    # that joins more than one thing could name anything, so it's only sent on the wider
    # rule - which is off here, with no registry.
    home = make_home({"/api/conversation/process": _assist("action_done", "Turned off the lights")})
    fallback = LLMHandler(api_key=None)._parse_action
    result = ActionExecutor(home=home).execute(fallback("Turn off the lights and the TV."))
    assert (result.ok, result.understood) == (False, False) and _paths(home) == ["/api/states"]
    result = ActionExecutor(home=home).execute(fallback("Turn off the lights."))
    assert result.ok and _asked(home)["text"] == "Turn off the lights."


# The PC's commands made as lights. HASS.Agent can make a command a light rather than a
# button, and any of them could shut the PC down - even one whose name, as a button's,
# would be safe, like "Lock" - so Nova's own lights never work them, as Assist doesn't.

PC_SHUTDOWN_LIGHT = _rendered_light("light.desk_pc_shutdown", "Desk lamp", "Bedroom", state="off",
                                    supported_color_modes=["brightness"])
PC_LOCK_LIGHT = _rendered_light("light.desk_pc_lock", "PC lock", "Bedroom", state="off",
                                supported_color_modes=["brightness"])
LAMP_DIMMABLE = _rendered_light("light.lamp", "Lamp", "Bedroom", brightness=204, supported_color_modes=["brightness"])


def _pc_lights(make_home, template=None):
    """
    A house with the lamp, the kitchen light and the PC's shutdown and lock commands made
    as lights - those two and the lamp in the Bedroom, where Nova is - with the registry,
    which says which are the PC's. The template is answered with the lights, unless a
    test gives another answer.
    """
    home = _house(make_home, LAMP_DIMMABLE, KITCHEN_LIGHT, PC_SHUTDOWN_LIGHT, PC_LOCK_LIGHT)
    if template is not None:
        home._session.answers["/api/template"] = template
    home.registry.read = _registry(
        _entry("light.desk_pc_shutdown", "mqtt", "desk", "Shutdown", area="bedroom"),
        _entry("light.desk_pc_lock", "mqtt", "desk", "Lock", area="bedroom"),
        _entry("light.lamp", "hue", area="bedroom"), _entry("light.kitchen_light", "hue", area="kitchen"),
        devices=DESK)
    return home


def test_the_pcs_commands_made_as_lights_are_left_out_of_the_lights(make_home):
    home = _pc_lights(make_home)
    assert [light["id"] for light in home.lights()] == ["light.lamp", "light.kitchen_light"]
    assert "desk_pc" not in home.describe_lights()
    # Nor are they on the settings page, from the template or, without it, from the states.
    assert [light["id"] for light in home.overview()["lights"]] == ["light.lamp", "light.kitchen_light"]
    home._session.answers["/api/template"] = FakeResponse({}, status=403)
    home._session.answers["/api/states"] = [_thing("light.lamp", "Lamp"), _thing("light.desk_pc_shutdown", "Desk lamp"),
                                            _thing("light.desk_pc_lock", "PC lock")]
    assert [light["id"] for light in home.overview()["lights"]] == ["light.lamp"]


def test_dimming_and_switching_leave_out_the_pcs_commands_made_as_lights(make_home):
    home = _pc_lights(make_home)
    # Brightening would otherwise switch both on, as they're off.
    assert home.dim("up", "the lights")["success"] and home.dim("down", "the bedroom lights")["success"]
    assert home.switch_lights(True, "the lights")["success"] and home.switch_lights(False, "all the lights")["success"]
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.lamp"], "brightness_pct": 100}),
                             ("turn_on", {"entity_id": ["light.lamp"], "brightness_pct": 50}),
                             ("turn_on", {"entity_id": ["light.lamp"]}), ("turn_off", {"entity_id": ["light.lamp"]})]
    for words in ("the desk lamp", "pc lock"):
        assert home.dim("up", words)["not_understood"] and home.switch_lights(True, words)["not_understood"]
    assert len(_dimmed(home)) == 4


def test_a_refused_light_sentence_naming_the_pcs_command_isnt_done_by_novas_own_lights(make_home):
    # Assist refuses "turn on the desk lamp", as the desk lamp is the PC's shutdown - and
    # Nova's own lights, which do what Assist refuses of a light, mustn't do it instead.
    home = _pc_lights(make_home)
    home._session.answers["/api/states"] = [_thing("light.desk_pc_shutdown", "Desk lamp")]
    action, = LocalIntents(home=home).match("Turn on the desk lamp.")["actions"]
    result = ActionExecutor(home=home).execute(action)
    assert (result.ok, result.understood) == (False, False)
    assert _dimmed(home) == [] and "/api/conversation/process" not in _paths(home)


def test_claude_and_the_page_cant_work_the_pcs_commands_made_as_lights(make_home):
    home = _pc_lights(make_home)
    for entity in ("light.desk_pc_shutdown", "light.desk_pc_lock"):
        assert home.set_lights(on=True, light=entity) == {"success": False, "message": "I can't do that."}
        assert home.set_light(entity, on=True) == {"success": False, "message": "I can't do that."}
    assert _dimmed(home) == []
    # An ordinary light is worked as ever.
    assert home.set_lights(on=True, light="light.lamp")["success"] and home.set_light("light.lamp", on=False)["success"]
    assert _dimmed(home) == [("turn_on", {"entity_id": "light.lamp"}), ("turn_off", {"entity_id": "light.lamp"})]


def test_without_the_registry_every_light_is_worked_as_before(make_home):
    # Ruled to fail open: nothing says what's the PC's without the registry, and leaving
    # every light out while it can't be read would cut the lights by voice and on the page.
    home = _pc_lights(make_home)
    home.snapshot = lambda: None
    assert [light["id"] for light in home.lights()] == ["light.lamp", "light.kitchen_light", "light.desk_pc_shutdown",
                                                        "light.desk_pc_lock"]
    home._call_service("light", "turn_on", {"entity_id": "light.desk_pc_shutdown"})
    assert _dimmed(home) == [("turn_on", {"entity_id": "light.desk_pc_shutdown"})]


def test_claudes_room_of_lights_leaves_out_the_pcs_commands_made_as_lights(make_home):
    # Home Assistant would work every light in the room, so a room with one of the PC's
    # commands is given as its other lights instead. A room without is given as ever.
    home = _pc_lights(make_home, template=FakeResponse("bedroom"))
    assert home.set_lights(on=True, brightness=40)["success"]
    home._session.answers["/api/template"] = FakeResponse("kitchen")
    assert home.set_lights(on=False, area="Kitchen")["success"]
    assert _dimmed(home) == [("turn_on", {"entity_id": ["light.lamp"], "brightness_pct": 40}),
                             ("turn_off", {"area_id": "kitchen"})]
    # A room with nothing else isn't given at all.
    home.registry.read = _registry(_entry("light.desk_pc_shutdown", "mqtt", "desk", "Shutdown", area="study"),
                                   devices=DESK)
    home._session.answers["/api/template"] = FakeResponse("study")
    assert home.set_lights(on=True, area="Study") == {"success": False, "message": "I can't do that."}
    assert len(_dimmed(home)) == 2


# What Claude reads of the house - see HomeAssistant.status

# Noon on Tuesday 20 October 2026, in UTC - 1 PM in London, on BST.
STATUS_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()


def _status(entity, name, state, unit=None, reported="2026-10-20T11:30:00+00:00", **attributes):
    """A state as /api/states has it, with when it was last reported."""
    attributes = {"friendly_name": name, **({"unit_of_measurement": unit} if unit else {}), **attributes}
    return {"entity_id": entity, "state": state, "attributes": attributes,
            "last_reported": reported, "last_updated": "2026-10-19T08:00:00+00:00"}


def _status_home(make_home, things, platforms=None, devices=None, areas=None):
    """
    A Home Assistant with these states, and a registry which has each of them - from
    the "demo" platform on no device unless a test says, e.g. {"sensor.x": ("mobile_app", None)}.
    """
    home = make_home({"/api/states": things})
    platforms = platforms or {}
    entries = {}
    for thing in things:
        entity = thing["entity_id"]
        platform, device = platforms.get(entity, ("demo", None))
        entries[entity] = Entry(entity, platform, device, (areas or {}).get(entity), entity, None, (), True)
    home.registry.read = Snapshot(entries=entries, devices=devices or {},
                                  areas={area: area.title() for area in (areas or {}).values()}, read_at=0.0)
    return home


def test_status_gives_the_best_few_matches_with_their_state_and_age(make_home):
    things = [_status("sensor.navidrome_uptime", "Navidrome uptime", "12", "d"),
              _status("binary_sensor.navidrome_status", "Navidrome status", "on"),
              _status("update.navidrome_update", "Navidrome update", "off", reported="2026-10-20T09:00:00+00:00"),
              _status("sensor.sun_next_setting", "Sun next setting", "2026-10-20T16:52:00+00:00",
                      device_class="timestamp"),
              _status("sensor.bedroom_temperature", "Temperature", "19.5", "°C"),
              _status("light.desk_lamp", "Desk lamp", "off")]
    things += [_status(f"switch.plug_{number}", f"Plug {number}", "on") for number in range(10)]
    home = _status_home(make_home, things, areas={"sensor.bedroom_temperature": "bedroom"})

    assert home.status("navidrome", now=STATUS_NOW) == {"success": True, "message": "\n".join([
        "Navidrome status (binary_sensor.navidrome_status): on",
        "Navidrome update (update.navidrome_update): off, as of 3 hours ago",
        "Navidrome uptime (sensor.navidrome_uptime): 12 d"])}
    # Each asked word that begins a word of the name counts, so the uptime comes first.
    assert home.status("navidrome uptime", now=STATUS_NOW)["message"].splitlines()[0] == \
        "Navidrome uptime (sensor.navidrome_uptime): 12 d"
    # A timestamp is said in local time, and the area counts as a word of the name.
    assert home.status("next setting", now=STATUS_NOW)["message"] == \
        "Sun next setting (sensor.sun_next_setting): 2026-10-20 17:52"
    assert home.status("How warm is the bedroom?", now=STATUS_NOW)["message"] == \
        "Temperature (sensor.bedroom_temperature, Bedroom): 19.5 °C"
    # No more than the best few, and nothing is called.
    assert len(home.status("plug", now=STATUS_NOW)["message"].splitlines()) == home_assistant.MOST_STATUS == 8
    assert set(_paths(home)) == {"/api/states"}


def test_status_leaves_out_little_words_and_puts_whole_words_first(make_home):
    things = [_status("climate.thermostat", "Thermostat", "heat"),
              _status("binary_sensor.desk_pc_online", "Desk PC online", "on"),
              _status("binary_sensor.pcie_fan", "PCIe fan", "off"),
              _status("sensor.desk_pc_cpu", "Desk PC CPU", "31", "%")]
    home = _status_home(make_home, things)
    # "the", "is" and "on" match nothing, and "pc" itself comes before "pcie".
    assert home.status("is the pc on?", now=STATUS_NOW)["message"] == "\n".join([
        "Desk PC CPU (sensor.desk_pc_cpu): 31 %",
        "Desk PC online (binary_sensor.desk_pc_online): on",
        "PCIe fan (binary_sensor.pcie_fan): off"])
    assert home.status("is it on?")["message"] == "Nothing in Home Assistant matches that."


def test_status_leaves_out_batteries_and_where_things_are(make_home):
    # Where people and things are, free text, and what has tools of its own.
    things = [_status("sensor.s25_ultra_battery_level", "S25 Ultra battery level", "80", "%", device_class="battery"),
              _status("binary_sensor.s25_ultra_is_charging", "S25 Ultra is charging", "off",
                      device_class="battery_charging"),
              _status("sensor.s25_ultra_geocoded_location", "S25 Ultra geocoded location", "1 Example Road",
                      Locality="Exampleton"),
              _status("device_tracker.s25_ultra", "S25 Ultra", "home", latitude=51.5, longitude=-0.1),
              _status("person.example", "Example S25 Ultra owner", "home"),
              _status("zone.s25_ultra_home", "S25 Ultra home", "1"),
              _status("input_text.s25_ultra_event_location", "S25 Ultra event location", "Example Hall"),
              _status("notify.mobile_app_s25_ultra", "S25 Ultra notify", "unknown"),
              _status("tts.s25_ultra", "S25 Ultra speech", "unknown"),
              _status("conversation.s25_ultra", "S25 Ultra conversation", "unknown"),
              _status("image.s25_ultra", "S25 Ultra image", "unknown"),
              _status("calendar.s25_ultra", "S25 Ultra calendar", "on"),
              _status("sensor.s25_ultra_daily_steps", "S25 Ultra daily steps", "4200", "steps")]
    home = _status_home(make_home, things)
    message = home.status("s25 ultra", now=STATUS_NOW)["message"]
    assert message == "S25 Ultra daily steps (sensor.s25_ultra_daily_steps): 4200 steps"
    for left_out in ("Example Road", "Exampleton", "51.5", "Example Hall"):
        assert left_out not in message


def test_status_leaves_out_network_and_app_sensors_by_their_id(make_home):
    # Shaped like the live ones, on no personal device, so only their ids leave them out.
    ids = ["sensor.example_phone_last_used_app", "sensor.example_phone_last_app", "sensor.example_pc_activewindow",
           "sensor.example_pc_active_window", "sensor.example_laptop_ssid", "sensor.example_laptop_bssid",
           "sensor.example_router_ip_address", "sensor.example_phone_wi_fi_connection", "sensor.example_tablet_wifi",
           "sensor.example_phone_network_type", "sensor.example_laptop_frontmost_app",
           "sensor.example_phone_last_notification", "sensor.example_pc_loggedusers", "sensor.example_pc_logged_users",
           "sensor.example_laptop_active_camera", "sensor.example_laptop_active_audio_input",
           "sensor.example_laptop_displays", "sensor.example_pc_clipboard", "sensor.example_phone_charger_type",
           "sensor.example_phone_battery_health"]
    home = _status_home(make_home, [_status(entity, "Example thing", "Something private") for entity in ids]
                        + [_status("sensor.example_pc_cpu", "Example PC CPU", "31", "%")])
    assert home.status("example", now=STATUS_NOW)["message"] == "Example PC CPU (sensor.example_pc_cpu): 31 %"


@pytest.mark.parametrize("address", [
    "192.168.0.10", "fe80::1", "aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "10.0.0.2, 10.0.0.3", "10.0.0.2:8080",
    "http://10.0.0.2/", "[fe80::1]", "Loopback on ::1 only", "fe80::1%eth0", "(aa:bb:cc:dd:ee:ff)", "10.0.0.0/24",
    "Connected to 10.0.0.2.", "aabbccddeeff", "0011223344ab", "Link AABBCCDDEEFF up", "::ffff:10.0.0.2",
    "IP:fe80::1", "LAN:2001:db8::1", "addr:fe80::1", "host.10.0.0.2", "...10.0.0.2",
    # Dotted, as Cisco writes one, and without each byte's leading 0 - and an IPv6 address
    # with a tail of hex after it which, joined on, isn't one.
    "aabb.ccdd.eeff", "Port AABB.CCDD.EEFF up", "0:1b:63:84:45:e6", "0-1b-63-84-45-e6", "fe80::1:c0ffee",
    "fe80::1:c0ffee:beef1",
    # A Zigbee device's EUI-64, as Zigbee2MQTT names one - "0x" and 16 hex digits - and
    # without the "0x", or in bytes.
    "0x00158d0001a2b3c4", "Joined 0x00158D0001A2B3C4", "00158d0001a2b3c4", "00:15:8d:00:01:a2:b3:c4"])
def test_status_leaves_out_any_address_whatever_its_called(make_home, address):
    # Searched for anywhere in the state, as an address is still one inside other words.
    home = _status_home(make_home, [_status("sensor.example_router_wan", "Example router WAN", address),
                                    _status("sensor.example_router_uptime", "Example router uptime", "12.5", "d")])
    assert home.status("example router", now=STATUS_NOW)["message"] == \
        "Example router uptime (sensor.example_router_uptime): 12.5 d"


def test_status_leaves_out_anything_named_after_an_address(make_home):
    # A Ping entity is named after the host it pings, in its name and its entity id alike.
    things = [_status("binary_sensor.10_0_0_1", "10.0.0.1", "on"),
              _status("binary_sensor.example_host", "Example aa:bb:cc:dd:ee:ff", "on"),
              _status("binary_sensor.example_10_0_0_2_ping", "Example ping", "on"),
              _status("binary_sensor.example_cupboard", "Example cupboard", "on"),
              _status("binary_sensor.example_router", "Example router", "on")]
    home = _status_home(make_home, things, areas={"binary_sensor.example_cupboard": "fe80::1",
                                                  "binary_sensor.example_router": "hall"})
    assert home.status("example", now=STATUS_NOW)["message"] == "Example router (binary_sensor.example_router, " \
                                                                 "Hall): on"
    assert home.status("10", now=STATUS_NOW)["message"] == "Nothing in Home Assistant matches that."


def test_status_leaves_out_anything_with_a_hardware_address_in_its_id(make_home):
    # A BLE or Shelly sensor's id carries its device's hardware address, however plain its
    # name - run together, or with its colons made underscores.
    things = [_status("sensor.ble_temperature_a4c1381234ab", "Example temperature", "21.5", "°C"),
              _status("sensor.shelly1_c45bbe123456_temperature", "Example temperature", "20.5", "°C"),
              _status("sensor.x_a4_c1_38_12_34_ab", "Example temperature", "22.5", "°C"),
              _status("sensor.x_a4_c1_38_ab_12_cd", "Example temperature", "23.5", "°C"),
              # A GUID too, run together or in its groups, whatever its letters - the PC's
              # network sensors are named after their adapter's.
              _status("sensor.windows_pc_network_32113e4d1a6c4d418be75feec6c69c40", "Example temperature", "1",
                      "°C"),
              _status("sensor.example_32113e4d1a6c4d418be75feec6c69c40", "Example temperature", "2", "°C"),
              _status("sensor.example_1a2b3c4d_5e6f_47a8_b9c0_123456789012", "Example temperature", "3", "°C"),
              # A Zigbee device's EUI-64, as Zigbee2MQTT names its entities, in its id or its
              # name, with or without the "0x" - even with no letter after it.
              _status("sensor.0x00158d0001a2b3c4_temperature", "Example temperature", "4", "°C"),
              _status("sensor.example_0x0015880001234567", "Example temperature", "5", "°C"),
              _status("sensor.example_00158d0001a2b3c4", "Example temperature", "6", "°C"),
              _status("sensor.example_zigbee_temperature", "0x00158d0001a2b3c4 temperature", "7", "°C"),
              # Cisco's dotted form, whose dots the id makes underscores, and the last three
              # bytes alone, as a Xiaomi sensor with ATC firmware is named.
              _status("sensor.x_a4c1_3812_34ab", "Example temperature", "8", "°C"),
              _status("sensor.atc_34ab12_temperature", "Example temperature", "9", "°C"),
              _status("sensor.example_temperature", "Example temperature", "19.5", "°C")]
    home = _status_home(make_home, things)
    assert home.status("example temperature", now=STATUS_NOW)["message"] == \
        "Example temperature (sensor.example_temperature): 19.5 °C"
    # While a model's number in an id is no address, nor are twelve digits alone - more
    # likely a count.
    for entity in ("sensor.pve2_cpu_usage", "sensor.s25_ultra_storage", "sensor.example_500107862016",
                   "sensor.nextcloud_cpu_load_last_15_minutes", "sensor.backup_2026_10_25_1230",
                   # Nor is an address read from the middle of a word - "e:1:2:a:b:c" here.
                   "sensor.zone_1_2_a_b_c",
                   # Nor, without a letter, three groups of four digits or six digits alone - a
                   # date and a time, or a number - nor six hex letters with no digit, a word.
                   "sensor.backup_2026_1106_1230", "sensor.example_123456", "sensor.example_facade"):
        home = _status_home(make_home, [_status(entity, "Example thing", "31")])
        assert entity in home.status("example thing", now=STATUS_NOW)["message"], entity


@pytest.mark.parametrize("state", ["on", "21.5", "2026-10-25T10:00:00+00:00", "2026-10-25T10:00:00.123456+00:00",
                                   "1.2.3", "12:30", "12:30:45", "Version 2026.10.1", "heat",
                                   "500107862016", "1234567890123", "16:9", "2026-10-25T10:00:00-05:00",
                                   "1234567890123456"])
def test_status_doesnt_take_an_ordinary_state_for_an_address(make_home, state):
    home = _status_home(make_home, [_status("sensor.example_router_wan", "Example router WAN", state)])
    assert "sensor.example_router_wan" in home.status("example router", now=STATUS_NOW)["message"]


def test_status_gives_text_entities_only_as_numbers_and_times(make_home):
    # A text entity holds free text, whatever it's on.
    things = [_status("text.example_note", "Example note", "Meet at Example Hall"),
              _status("text.example_count", "Example count", "3")]
    home = _status_home(make_home, things)
    assert home.status("example", now=STATUS_NOW)["message"] == "Example count (text.example_count): 3"


def test_status_finds_a_plural_by_its_singular(make_home):
    home = _status_home(make_home, [_status("update.example_nas_firmware", "Example NAS firmware", "off"),
                                    _status("light.desk_lamp", "Desk lamp", "off")])
    assert home.status("any updates?", now=STATUS_NOW)["message"] == \
        "Example NAS firmware (update.example_nas_firmware): off"
    assert home.status("lamps", now=STATUS_NOW)["message"] == "Desk lamp (light.desk_lamp): off"
    # Not a short word, which is more likely a word of its own than a plural.
    home = _status_home(make_home, [_status("sensor.gas_meter", "Gas meter", "1203.5", "m³"),
                                    _status("sensor.garage_temperature", "Garage temperature", "12.5", "°C")])
    assert home.status("gas", now=STATUS_NOW)["message"] == "Gas meter (sensor.gas_meter): 1203.5 m³"


def test_status_gives_a_personal_devices_sensors_only_as_numbers_and_times(make_home):
    things = [_status("sensor.example_phone_message", "Example phone message", "See you at Example Hall"),
              _status("sensor.example_phone_steps", "Example phone steps", "4200"),
              _status("binary_sensor.example_phone_interactive", "Example phone interactive", "on"),
              _status("switch.example_phone_dnd", "Example phone dnd", "off"),
              _status("sensor.example_pc_session", "Example pc session", "Example window title"),
              _status("sensor.example_pc_last_boot", "Example pc last boot", "2026-10-20T08:00:00+00:00"),
              _status("sensor.example_nas_status", "Example nas status", "healthy"),
              _status("update.example_nas_firmware", "Example nas firmware", "off"),
              _status("light.example_lamp", "Example lamp", "on")]
    platforms = {"sensor.example_phone_message": ("mobile_app", None), "sensor.example_phone_steps": ("mobile_app", None),
                 "binary_sensor.example_phone_interactive": ("mobile_app", None),
                 "switch.example_phone_dnd": ("mobile_app", None), "sensor.example_pc_session": ("mqtt", "pc"),
                 "sensor.example_pc_last_boot": ("mqtt", "pc")}
    home = _status_home(make_home, things, platforms, devices={"pc": {"name": "Example PC", "maker": "HASS.Agent Team"}})
    # Only a sensor's state can be free text, so only a sensor is held to a number or a time.
    assert home.status("example", now=STATUS_NOW)["message"] == "\n".join([
        "Example lamp (light.example_lamp): on",
        "Example nas firmware (update.example_nas_firmware): off",
        "Example nas status (sensor.example_nas_status): healthy",
        "Example pc last boot (sensor.example_pc_last_boot): 2026-10-20 09:00",
        "Example phone dnd (switch.example_phone_dnd): off",
        "Example phone interactive (binary_sensor.example_phone_interactive): on",
        "Example phone steps (sensor.example_phone_steps): 4200"])
    # A sensor the registry hasn't got yet - added since it was read - may be a phone's.
    home._session.answers["/api/states"] = things + [
        _status("sensor.example_new_sensor", "Example new sensor", "Something private"),
        _status("sensor.example_new_count", "Example new count", "3")]
    said = home.status("example new", now=STATUS_NOW)["message"]
    assert said.splitlines()[0] == "Example new count (sensor.example_new_count): 3"
    assert "example_new_sensor" not in said and "Something private" not in said
    # Without the registry, nothing can be told to be personal, so every sensor is held to that.
    home._session.answers["/api/states"] = things
    home.registry.read = None
    assert home.status("example", now=STATUS_NOW)["message"] == "\n".join([
        "Example lamp (light.example_lamp): on",
        "Example nas firmware (update.example_nas_firmware): off",
        "Example pc last boot (sensor.example_pc_last_boot): 2026-10-20 09:00",
        "Example phone dnd (switch.example_phone_dnd): off",
        "Example phone interactive (binary_sensor.example_phone_interactive): on",
        "Example phone steps (sensor.example_phone_steps): 4200"])


def test_status_says_when_nothing_matches(make_home):
    home = _status_home(make_home, [_status("light.desk_lamp", "Desk lamp", "off")])
    assert home.status("kettle") == {"success": True, "message": "Nothing in Home Assistant matches that."}
    assert home.status("?!") == {"success": True, "message": "Nothing in Home Assistant matches that."}


def test_status_with_home_assistant_down_says_so(make_home):
    home = make_home({"/api/states": requests.ConnectionError("down")})
    assert home.status("navidrome") == {"success": False, "message": "I can't reach Home Assistant right now."}
    result = ActionExecutor(home=home).execute(Action("home_status", {"about": "navidrome"}, from_model=True))
    assert result.say == "I can't reach Home Assistant right now."
