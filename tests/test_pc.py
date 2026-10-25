"""
PC Test
Turning the PC on and off through Home Assistant, with a fake Home Assistant whose states
and registry snapshot each test gives, and which notes each button pressed - so nothing is
ever pressed on a real machine. The Wake-on-LAN buttons' ids are made up: a real one holds
the PC's hardware address.
"""

from voice_assistant.choices import ChoiceStore
from voice_assistant.ha_registry import Entry, Snapshot
from voice_assistant.pc import PC, PCPower, find_pcs

# HASS.Agent's buttons on the desk PC, by original name - the entity ids are the user's own.
DESK_BUTTONS = {"Shutdown": "button.desk_off", "Restart": "button.desk_again", "Sleep": "button.desk_nap",
                "MonitorSleep": "button.desk_dark", "Lock": "button.desk_locked", "LaunchUrl": "button.desk_web"}
AGENT = {"maker": "HASS.Agent Team", "model": "HASS.Agent"}


def _entry(entity, platform="mqtt", device=None, original_name=None, unique_id=None):
    return Entry(entity, platform, device, None, unique_id or entity.partition(".")[2], original_name, (), False)


def _pc(device="desk", name="Desk PC", buttons=None, sensor="sensor.desk_cpu"):
    """A HASS.Agent device's entries and its device, e.g. Desk PC's six buttons and a sensor."""
    entries = [_entry(entity, device=device, original_name=kind) for kind, entity in (buttons or DESK_BUTTONS).items()]
    entries.append(_entry(sensor, device=device, original_name="CPU Load"))
    return entries, {device: {"name": name, **AGENT}}


def _snapshot(*pcs, wake=("button.wake_desk_pc",), others=()):
    entries, devices = [], {}
    for pc_entries, pc_device in pcs:
        entries += pc_entries
        devices.update(pc_device)
    entries += [_entry(entity, platform="wake_on_lan") for entity in wake]
    entries += list(others)
    return Snapshot(entries={entry.entity_id: entry for entry in entries}, devices=devices, areas={}, read_at=0.0)


def _states(snapshot, on=True):
    """Every entity's state: each PC's sensor a number while it's on, and all of it unavailable while it's off."""
    states = []
    for entity, entry in snapshot.entries.items():
        if entry.platform == "wake_on_lan":
            state = "2026-10-25T09:00:00+00:00"
        elif not on:
            state = "unavailable"
        else:
            state = "12" if entity.startswith("sensor.") else "unknown"
        states.append({"entity_id": entity, "state": state, "attributes": {}})
    return states


class FakeHome:
    """Stands in for Home Assistant: its states and snapshot, and the buttons pressed."""

    def __init__(self, snapshot, on=True):
        self.snapshot = snapshot
        self.states = _states(snapshot, on) if snapshot else []
        self.pressed = []

    def _snapshot(self):
        return self.snapshot

    def _states(self):
        return self.states

    def press(self, entity_id):
        self.pressed.append(entity_id)


def _power(tmp_path, snapshot, on=True):
    home = FakeHome(snapshot, on)
    return PCPower(home, ChoiceStore(str(tmp_path / "pcs.json"), {"wake": {}})), home


def test_the_pc_is_found_by_its_device_not_its_name():
    # A shutdown called windows_pc_shutdown on no HASS.Agent device isn't the PC's.
    stray = _entry("button.windows_pc_shutdown", original_name="Shutdown")
    snapshot = _snapshot(_pc(), others=[stray])
    found = find_pcs(_states(snapshot), snapshot, {})
    assert found == [PC(device="desk", name="Desk PC", on=True,
                        buttons={"shutdown": "button.desk_off", "restart": "button.desk_again",
                                 "sleep": "button.desk_nap", "monitorsleep": "button.desk_dark",
                                 "lock": "button.desk_locked"},
                        wake="button.wake_desk_pc")]

    # Off, every one of its entities is unavailable - live check 5.
    assert [pc.on for pc in find_pcs(_states(snapshot, on=False), snapshot, {})] == [False]
    # On while any isn't: the sensor reading a number is enough.
    states = _states(snapshot, on=False)
    next(state for state in states if state["entity_id"] == "sensor.desk_cpu")["state"] = "7"
    assert [pc.on for pc in find_pcs(states, snapshot, {})] == [True]


def test_sleep_screen_off_and_lock_dont_ask(tmp_path):
    power, home = _power(tmp_path, _snapshot(_pc()))
    assert power.act("sleep") == {"success": True, "message": "Putting your PC to sleep."}
    assert power.act("screen_off") == {"success": True, "message": "Turning your PC's screen off."}
    assert power.act("lock") == {"success": True, "message": "Locking your PC."}
    assert home.pressed == ["button.desk_nap", "button.desk_dark", "button.desk_locked"]

    power, home = _power(tmp_path, _snapshot(_pc()), on=False)
    for action in ("sleep", "screen_off", "lock"):
        assert power.act(action) == {"success": False, "message": "Your PC's off."}
    assert home.pressed == []


def test_shutting_down_and_restarting_ask_first_and_press_once_agreed(tmp_path):
    power, home = _power(tmp_path, _snapshot(_pc()))
    asked = power.act("off")
    assert (asked["success"], asked["message"]) == (True, "Shut down the PC?")
    assert (asked["ask_first"].tool, asked["ask_first"].params) == ("press_pc_button", {"action": "off", "name": None})
    assert power.act("restart")["message"] == "Restart the PC?"
    assert home.pressed == []

    assert power.press("off") == {"success": True, "message": "Shutting down the PC."}
    assert power.press("restart") == {"success": True, "message": "Restarting the PC."}
    assert home.pressed == ["button.desk_off", "button.desk_again"]
    # Nothing else is pressed by the yes.
    assert power.press("sleep")["success"] is False and len(home.pressed) == 2

    power, home = _power(tmp_path, _snapshot(_pc()), on=False)
    assert power.act("off") == {"success": False, "message": "Your PC's already off."}
    assert power.act("restart") == {"success": False, "message": "Your PC's already off."}
    assert power.press("off") == {"success": False, "message": "Your PC's already off."}
    assert home.pressed == []


def test_turning_on_presses_wake_on_lan(tmp_path):
    power, home = _power(tmp_path, _snapshot(_pc()), on=False)
    assert power.act("on") == {"success": True, "message": "Turning your PC on."}
    assert home.pressed == ["button.wake_desk_pc"]

    power, home = _power(tmp_path, _snapshot(_pc()))
    assert power.act("on") == {"success": True, "message": "Your PC's already on."}
    assert home.pressed == []

    power, home = _power(tmp_path, _snapshot(_pc(), wake=()), on=False)
    assert power.act("on") == {"success": False,
                               "message": "There's no Wake-on-LAN button for your PC in Home Assistant."}
    assert home.pressed == []


def test_with_several_wake_buttons_the_chosen_one_is_pressed(tmp_path):
    snapshot = _snapshot(_pc(), wake=("button.wake_desk_pc", "button.wake_nas"))
    power, home = _power(tmp_path, snapshot, on=False)
    assert power.act("on") == {"success": False,
                               "message": "I don't know which Wake-on-LAN button wakes your PC yet."}
    assert home.pressed == []

    power.choices.change(lambda state: state["wake"].update(desk="button.wake_desk_pc"))
    assert power.act("on") == {"success": True, "message": "Turning your PC on."}
    assert home.pressed == ["button.wake_desk_pc"]

    # A chosen button that's gone from Home Assistant isn't pressed.
    gone = _snapshot(_pc(), wake=("button.wake_nas", "button.wake_tv"))
    assert find_pcs(_states(gone, on=False), gone, {"desk": "button.wake_desk_pc"})[0].wake is None


def test_whether_the_pc_is_on_is_said(tmp_path):
    assert _power(tmp_path, _snapshot(_pc()))[0].act("status") == {"success": True, "message": "Your PC's on."}
    assert _power(tmp_path, _snapshot(_pc()), on=False)[0].act("status") == {"success": True,
                                                                            "message": "Your PC's off."}


def test_without_a_snapshot_or_a_pc_nothing_is_pressed(tmp_path):
    power, home = _power(tmp_path, None)
    assert power.act("lock") == {"success": False, "message": "I can't check your PC's buttons just now."}
    assert power.press("off") == {"success": False, "message": "I can't check your PC's buttons just now."}

    power, home = _power(tmp_path, _snapshot())
    assert power.act("status") == {"success": False, "message": "I can't find your PC in Home Assistant."}
    assert home.pressed == []


def test_with_several_pcs_the_one_named_is_used(tmp_path):
    gaming = {kind: entity.replace("desk", "gaming") for kind, entity in DESK_BUTTONS.items()}
    snapshot = _snapshot(_pc(), _pc("gaming", "Gaming PC", gaming, "sensor.gaming_cpu"))
    power, home = _power(tmp_path, snapshot)
    assert power.act("lock") == {"success": False, "message": "Which PC, Desk PC or Gaming PC?"}
    assert power.act("lock", "the gaming PC") == {"success": True, "message": "Locking your PC."}
    assert power.act("off", "gaming")["ask_first"].params == {"action": "off", "name": "gaming"}
    assert power.press("off", "gaming")["success"]
    assert home.pressed == ["button.gaming_locked", "button.gaming_off"]
