"""
PC Module
Turning the user's PC on and off by voice, through Home Assistant - decision 13 of
docs/decisions/0008.

The PC is a HASS.Agent device, found by its maker in the registry, never by the
windows_pc_ in its entity ids, which the user can rename. Its buttons are found by what
each does (see ha_registry.button_kind), and only those in PC_KINDS are ever pressed - never
its launchurl, which opens any web address. It's woken by the Wake-on-LAN integration's
button: the only one, with one PC and one button, else the one chosen for it on the page.

Whether it's on is whether any of its entities is available: while it's off, every one of
HASS.Agent's reads "unavailable" (live check 5).

Shutting down and restarting ask first - "Shut down the PC?" - and only a yes said next, in
the same conversation, does it (see Conversation.handle). The yes runs press_pc_button,
which Claude isn't offered, so it can't skip the question. Sleep, screen off and lock don't
ask, as each is undone by touching the PC.

Every press goes through HomeAssistant.press, whose own check of the snapshot is the last
word on which buttons may be pressed.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from voice_assistant.actions import Action
from voice_assistant.batteries import spoken_name
from voice_assistant.choices import ChoiceStore
from voice_assistant.ha_registry import PC_KINDS, Snapshot
from voice_assistant.home_assistant import CANT_REACH, HomeAssistant, NotAllowed
from voice_assistant.lists import NO_ANSWER

# Logger setup
logger = logging.getLogger(__name__)

# The button each action presses, by HASS.Agent's kind.
KINDS = {"off": "shutdown", "restart": "restart", "sleep": "sleep", "screen_off": "monitorsleep", "lock": "lock"}
# What's asked before pressing, and said once agreed.
ASKED = {"off": "Shut down the PC?", "restart": "Restart the PC?"}
AGREED = {"off": "Shutting down the PC.", "restart": "Restarting the PC."}
# What's said as the buttons which don't ask are pressed.
PRESSED = {"sleep": "Putting your PC to sleep.", "screen_off": "Turning your PC's screen off.",
           "lock": "Locking your PC."}
# Each kind as it's said, for a button the PC hasn't got.
KIND_SAID = {"shutdown": "shutdown", "restart": "restart", "sleep": "sleep", "monitorsleep": "monitor sleep",
             "lock": "lock"}

# The words which say nothing of which PC was meant - "my PC" is any of them.
NAME_FILLER = {"my", "the", "pc", "computer"}


@dataclass
class PC:
    """
    One HASS.Agent PC, as Home Assistant has it now.

    Attributes:
        device: Its Home Assistant device id.
        name: Its name, as it's said, e.g. "Desk PC".
        on: Whether any of its entities is available.
        buttons: Its buttons in PC_KINDS, by kind, e.g. {"shutdown": "button.desk_pc_shutdown"}.
        wake: The Wake-on-LAN button which wakes it, or None if that isn't known.
    """

    device: str
    name: str
    on: bool
    buttons: Dict[str, str] = field(default_factory=dict)
    wake: Optional[str] = None


def find_pcs(states: List[Dict], snapshot: Snapshot, wake: Dict[str, str]) -> List[PC]:
    """
    Every HASS.Agent PC.

    Args:
        states: Every entity's state, as /api/states has them.
        snapshot: The registry.
        wake: The Wake-on-LAN button chosen for each PC, by device id.

    Returns:
        One for each HASS.Agent device, in the order the registry has them.
    """
    state_of = {state.get("entity_id"): str(state.get("state")) for state in states}
    buttons = snapshot.hass_agent_buttons()
    devices = list(dict.fromkeys(device for device, _ in snapshot.hass_agent_commands.values()))
    wake_buttons = snapshot.wake_buttons()
    pcs = []
    for device in devices:
        entities = snapshot.on_device(device)
        chosen = wake.get(device)
        if len(devices) == 1 and len(wake_buttons) == 1:
            woken_by = wake_buttons[0]
        else:
            woken_by = chosen if chosen in wake_buttons else None
        pcs.append(PC(
            device=device,
            name=spoken_name(snapshot.device_name(device) or "PC"),
            # An entity missing from the states says nothing either way.
            on=any(entity in state_of and state_of[entity] != "unavailable" for entity in entities),
            buttons={kind: entity for entity, (owner, kind) in buttons.items() if owner == device and kind in PC_KINDS},
            wake=woken_by))
    return pcs


def _words(name: str) -> set:
    """A name's words, lower-cased without punctuation, e.g. "Gaming-PC's" as {"gaming", "pcs"}."""
    return set(re.sub(r"[^a-z0-9 ]", "", name.lower().replace("-", " ")).split())


def _named(pcs: List[PC], name: Optional[str]) -> List[PC]:
    """The PCs a name fits - all of them for none, else those whose name has every word said."""
    words = _words(name or "") - NAME_FILLER
    if not words:
        return pcs
    return [pc for pc in pcs if words <= _words(pc.name)]


class PCPower:
    """Turns the user's PC on and off, asking first before shutting it down or restarting it."""

    def __init__(self, home: HomeAssistant, choices: ChoiceStore):
        """
        Args:
            home: The Home Assistant to ask.
            choices: The Wake-on-LAN button chosen for each PC, under "wake", by device id -
                needed only with more than one PC or more than one button.
        """
        self.home = home
        self.choices = choices

    def _find(self, name: Optional[str]):
        """
        The PC meant, read afresh.

        Returns:
            (the PC, the snapshot, None) - or (None, None, the reply to give instead).
        """
        snapshot = self.home._snapshot()
        if snapshot is None:
            return None, None, {"success": False, "message": "I can't check your PC's buttons just now."}
        try:
            states = self.home._states()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Couldn't read Home Assistant's states for the PC: {e}")
            return None, None, {"success": False, "message": CANT_REACH}
        pcs = find_pcs(states, snapshot, self.choices.get("wake"))
        if not pcs:
            return None, None, {"success": False, "message": "I can't find your PC in Home Assistant."}
        if len(pcs) == 1:
            return pcs[0], snapshot, None
        fits = _named(pcs, name)
        if len(fits) != 1:
            names = [pc.name for pc in pcs]
            return None, None, {"success": False, "message": f"Which PC, {', '.join(names[:-1])} or {names[-1]}?"}
        return fits[0], snapshot, None

    def act(self, action: str, name: Optional[str] = None) -> Dict:
        """
        Does what was asked of the PC - bar shutting down or restarting, which ask first.

        Args:
            action: "on", "off", "restart", "sleep", "screen_off", "lock" or "status".
            name: Which PC, as the user said it, or None - needed only with several.

        Returns:
            {"success", "message"}, with "ask_first", the Action to run on a yes, for
            "off" and "restart" with the PC on.
        """
        if action not in ("on", "status", *KINDS):
            return {"success": False, "message": "I didn't catch what to do with your PC."}
        pc, snapshot, reply = self._find(name)
        if reply:
            return reply
        if action == "status":
            return {"success": True, "message": "Your PC's on." if pc.on else "Your PC's off."}
        if action == "on":
            if pc.on:
                return {"success": True, "message": "Your PC's already on."}
            if pc.wake is None:
                if not snapshot.wake_buttons():
                    return {"success": False, "message": "There's no Wake-on-LAN button for your PC in Home Assistant."}
                return {"success": False, "message": "I don't know which Wake-on-LAN button wakes your PC yet."}
            return self._press(pc.wake, "Wake-on-LAN", "Turning your PC on.")
        if action in ASKED:
            if not pc.on:
                return {"success": False, "message": "Your PC's already off."}
            # Not asked about a button that isn't there, which a yes couldn't press.
            missing = self._missing(pc, action)
            if missing:
                return missing
            return {"success": True, "message": ASKED[action],
                    "ask_first": Action("press_pc_button", {"action": action, "name": name})}
        if not pc.on:
            return {"success": False, "message": "Your PC's off."}
        return self._missing(pc, action) or self._press(pc.buttons[KINDS[action]], KINDS[action], PRESSED[action])

    def press(self, action: str, name: Optional[str] = None) -> Dict:
        """
        Shuts the PC down or restarts it, once the user has said yes to act's question.

        Args:
            action: "off" or "restart".
            name: Which PC, as the user said it, or None.

        Returns:
            {"success", "message"}.
        """
        if action not in ASKED:
            return {"success": False, "message": "I didn't catch what to do with your PC."}
        pc, _, reply = self._find(name)
        if reply:
            return reply
        if not pc.on:
            return {"success": False, "message": "Your PC's already off."}
        return self._missing(pc, action) or self._press(pc.buttons[KINDS[action]], KINDS[action], AGREED[action])

    @staticmethod
    def _missing(pc: PC, action: str) -> Optional[Dict]:
        """What to say if the PC hasn't got the button an action presses, else None."""
        if KINDS[action] in pc.buttons:
            return None
        return {"success": False,
                "message": f"There's no {KIND_SAID[KINDS[action]]} button for your PC in Home Assistant."}

    def _press(self, entity_id: str, kind: str, said: str) -> Dict:
        """
        Presses a button, saying so - or why not.

        Args:
            entity_id: The button.
            kind: What it is, for the log, e.g. "shutdown" - never its entity id, as the
                Wake-on-LAN button's holds the PC's hardware address.
            said: What to say once it's pressed.
        """
        try:
            self.home.press(entity_id)
        except requests.Timeout:
            # Sent, but unanswered - so it may well have been pressed.
            logger.warning(f"Home Assistant didn't answer in time pressing the PC's {kind} button")
            return {"success": False, "message": f"{NO_ANSWER}, so I can't tell whether your PC heard."}
        except (requests.RequestException, NotAllowed) as e:
            logger.warning(f"Couldn't press the PC's {kind} button: {e}")
            return {"success": False, "message": HomeAssistant._failure_message(e)}
        logger.info(f"Pressed the PC's {kind} button")
        return {"success": True, "message": said}
