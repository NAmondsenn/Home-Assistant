"""
Batteries Module
The rules for what a battery is, from decisions 2 and 5 of docs/decisions/0007.

Home Assistant is the only source of batteries (see HomeAssistant.batteries), so
nothing here is a hand-kept list of devices. What it says about each device's
maker and model is enough to sort it into "mobile" (an Android phone or tablet,
an iPhone or iPad - warned about once, at 5%) or "other" (a Mac, a remote, a
sensor, or anything whose kind can't be worked out - warned about at 15% and 5%).
Whatever is on the Home Assistant app is mobile, bar a Mac, so a new Android
maker needs no list; and only a model that shows a phone is the phone, as a wrong
guess towards "tablet" costs a duplicate warning where the other loses one.

Charging is read from the words each app uses for its charging sensor, and a
reading old enough to be stale is said with its age, so that a tablet last heard
from three days ago isn't taken to be on 88% now.
"""

import copy
import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from voice_assistant import storage
from voice_assistant.clock import format_duration

logger = logging.getLogger(__name__)

# Spellings a plain capital letter would get wrong - "macbook" is not "Macbook".
_KNOWN_SPELLINGS = {"macbook": "MacBook", "iphone": "iPhone", "ipad": "iPad"}

_CHARGING_WORDS = {"charging", "full", "on"}
_NOT_CHARGING_WORDS = {"discharging", "not charging", "off"}

# Samsung's phone model prefixes. The others are tablets (SM-X, SM-T, SM-P) or watches (SM-R).
_SAMSUNG_PHONES = ("sm-s", "sm-g", "sm-a", "sm-f", "sm-n", "sm-m", "sm-e")

# Words a person uses for a kind of device, and the test of which batteries each one means.
_KIND_WORDS = {
    "phone": lambda b: b.phone,
    "tablet": lambda b: b.kind == "mobile" and not b.phone,
    "laptop": lambda b: b.computer,
    "computer": lambda b: b.computer,
    "mac": lambda b: b.computer,
}


@dataclass
class Battery:
    """
    One battery Home Assistant knows about, with what Nova works out from it.

    Attributes:
        id: The sensor's entity id.
        name: The spoken name, e.g. "MacBook Pro".
        level: The charge, as a percentage.
        changed: Epoch seconds when Home Assistant last heard the reading.
        device: The Home Assistant device id, or None if the sensor has none.
        kind: "mobile" or "other".
        phone: Whether the device is a phone, rather than a tablet or something else.
        computer: Whether it's a Mac running the Home Assistant app.
        charging: Whether it's charging, or None if that isn't known.
    """

    id: str
    name: str
    level: float
    changed: float
    device: Optional[str]
    kind: str
    phone: bool
    computer: bool
    charging: Optional[bool]

    @classmethod
    def from_home(cls, found: Dict) -> "Battery":
        """
        Builds a battery from one of HomeAssistant.batteries()'s dicts.

        Args:
            found: The dict, with id, name, level, changed, device, maker, model, charging and app.

        Returns:
            The battery. With no device there is nothing to read a kind or a
            charging sensor from, so it's something else and never charging.
        """
        device = found.get("device")
        maker = found.get("maker") if device else None
        model = found.get("model") if device else None
        app = bool(found.get("app")) and bool(device)
        kind = kind_of(maker, model, app)
        return cls(
            id=found["id"],
            name=spoken_name(found["name"]),
            level=found["level"],
            changed=found["changed"],
            device=device,
            kind=kind,
            phone=kind == "mobile" and is_phone(maker, model),
            computer=app and _is_mac(maker, model),
            charging=charging_from(found.get("charging")) if device else None,
        )


def _is_mac(maker: Optional[str], model: Optional[str]) -> bool:
    """Whether a device is an Apple Mac, e.g. maker "Apple", model "Mac16,8"."""
    return (maker or "").lower() == "apple" and (model or "").lower().startswith("mac")


def kind_of(maker: Optional[str], model: Optional[str], app: bool) -> str:
    """
    Sorts a device into mobile or other.

    Args:
        maker: The device's manufacturer, as Home Assistant says it, or None.
        model: The device's model, or None.
        app: Whether the battery sensor belongs to the Home Assistant app.

    Returns:
        "mobile" for what's on the app, apart from a Mac; "other" for a Mac,
        anything off the app, and anything unknown.
    """
    return "mobile" if app and not _is_mac(maker, model) else "other"


def is_phone(maker: Optional[str], model: Optional[str]) -> bool:
    """
    Whether a model shows a phone - the one device whose warnings aren't sent to
    the phone, as that's the phone running out.

    Args:
        maker: The device's manufacturer, or None.
        model: The device's model, or None.

    Returns:
        True for an iPhone or a Samsung model with a phone prefix (SM-S, G, A, F,
        N, M or E). Everything else, including a model Nova can't place, is not.
    """
    maker = (maker or "").lower()
    model = (model or "").lower()
    if maker == "apple":
        return model.startswith("iphone")
    return maker == "samsung" and model.startswith(_SAMSUNG_PHONES)


def charging_from(raw: Optional[str]) -> Optional[bool]:
    """
    Reads a charging sensor's state, in whichever words its app uses.

    Args:
        raw: The sensor's state, e.g. "Not Charging", "discharging" or "on", or None.

    Returns:
        True if charging, False if not, None if it can't be told.
    """
    if raw is None:
        return None
    word = raw.strip().lower().replace("_", " ")
    if word in _CHARGING_WORDS:
        return True
    if word in _NOT_CHARGING_WORDS:
        return False
    return None


def spoken_name(name: str) -> str:
    """
    Turns a Home Assistant name into words to say.

    Args:
        name: The name, e.g. "macbook-pro" or "s25-ultra".

    Returns:
        The words - "MacBook Pro", "S25 Ultra". A word with a digit is upper-cased
        as it is, a known spelling is used, and the rest are capitalised.
    """
    words = name.replace("-", " ").replace("_", " ").split()
    said_words = []
    for word in words:
        if word.lower() in _KNOWN_SPELLINGS:
            said_words.append(_KNOWN_SPELLINGS[word.lower()])
        elif any(c.isdigit() for c in word):
            said_words.append(word.upper())
        else:
            said_words.append(word.capitalize())
    return " ".join(said_words)


def find(batteries: List[Battery], asked: str) -> List[Battery]:
    """
    Finds the devices someone meant by what they said.

    Args:
        batteries: The batteries to choose from.
        asked: The words asked, e.g. "mac", "tab", "phone", or "" or "all" for every one.

    Returns:
        The batteries meant, in their given order. A word matches the start of
        any word in a name ("mac" finds "MacBook Pro"), or is a kind word:
        phone, tablet, laptop, computer or mac. Only the batteries matching the
        most asked words are kept, so "s25 ultra" doesn't also find the Tab S11
        Ultra. Empty if nothing matches.
    """
    words = [w for w in asked.lower().split() if w != "all"]
    if not words:
        return list(batteries)

    matches = []
    for battery in batteries:
        name_words = battery.name.lower().split()
        matches.append(sum(
            any(n.startswith(word) for n in name_words)
            or (word in _KIND_WORDS and _KIND_WORDS[word](battery))
            for word in words
        ))
    best = max(matches, default=0)
    return [b for b, count in zip(batteries, matches) if best and count == best]


def said(battery: Battery, now: float, stale_after: float) -> str:
    """
    Says a battery's level.

    Args:
        battery: The battery.
        now: The current epoch time.
        stale_after: How many seconds old a reading may be and still count as current.

    Returns:
        "Your MacBook Pro's on 72%, and not charging." for a fresh reading, or
        "Your Tab S11 Ultra was on 88%, 3 days ago." for an old one.
    """
    level = f"{round(battery.level)}%"
    age = now - battery.changed
    if age > stale_after:
        return f"Your {battery.name} was on {level}, {format_duration(age)} ago."
    if battery.charging is None:
        return f"Your {battery.name}'s on {level}."
    state = "charging" if battery.charging else "not charging"
    return f"Your {battery.name}'s on {level}, and {state}."


@dataclass
class BatteryWarning:
    """
    A warning that is due.

    Attributes:
        battery: The battery that has fallen.
        mark: The percentage mark it fell under, e.g. 15.
    """

    battery: Battery
    mark: int


class BatteryChoices:
    """
    What each battery's owner chose, and which marks have already warned.

    Both are kept in one file, so a restart neither forgets a choice nor warns a
    second time about a battery that is still low. A device Home Assistant stops
    listing keeps its entry, in case it comes back. The watcher thread calls due()
    while the settings page's thread changes choices, so one lock guards the state
    and every save: a save from one can't lose the other's change.
    """

    def __init__(self, path: str, mobile_marks: List[int], other_marks: List[int]):
        """
        Args:
            path: The file to keep choices and warned marks in, e.g. batteries.json.
            mobile_marks: The default marks for a mobile device, e.g. [5].
            other_marks: The default marks for anything else, e.g. [5, 15].
        """
        self._path = path
        self._defaults = {
            "mobile": sorted(mobile_marks, reverse=True),
            "other": sorted(other_marks, reverse=True),
        }
        self._lock = threading.RLock()
        self._state = {"devices": {}, "warned": {}, "also_send_when_said": True}
        try:
            saved = storage.load_json(path, {})
        except (OSError, ValueError) as e:
            # Better to warn again about a low battery than to refuse to start.
            logger.warning(f"Couldn't read {path}, starting without choices: {e}")
            saved = {}
        if isinstance(saved, dict):
            for key in self._state:
                if key in saved:
                    self._state[key] = saved[key]

    @property
    def also_send_when_said(self) -> bool:
        """Whether a battery level Nova says aloud is also sent to the phone."""
        with self._lock:
            return bool(self._state["also_send_when_said"])

    def default_marks(self, battery: Battery) -> List[int]:
        """Returns the marks for a battery's kind, highest first, e.g. [15, 5]."""
        return list(self._defaults[battery.kind])

    def marks_for(self, battery: Battery) -> List[int]:
        """Returns the device's own marks if it has any, else its kind's, highest first."""
        with self._lock:
            own = self._state["devices"].get(battery.id, {}).get("marks")
            return sorted(own, reverse=True) if own else self.default_marks(battery)

    def enabled(self, battery_id: str) -> bool:
        """Whether a device's warnings are on - they are, until switched off."""
        with self._lock:
            return self._state["devices"].get(battery_id, {}).get("enabled", True)

    def send_to_phone(self, battery: Battery) -> bool:
        """Whether a device's warnings go to the phone - not the phone's own, by default."""
        with self._lock:
            return self._state["devices"].get(battery.id, {}).get("send_to_phone", not battery.phone)

    def set(
        self,
        battery: Battery,
        enabled: Optional[bool] = None,
        marks: Optional[List[int]] = None,
        send_to_phone: Optional[bool] = None,
    ):
        """
        Changes a device's choices and saves them.

        Args:
            battery: The device. Its kind says how many marks it may have.
            enabled: Whether its warnings are on, or None to leave it as it is.
            marks: Its marks, or None to leave them as they are.
            send_to_phone: Whether its warnings go to the phone, or None to leave it.

        Raises:
            ValueError: With a sentence the page can show, if the marks aren't whole
                numbers from 1 to 99, distinct, one for a mobile device and two for
                anything else. Nothing is changed then.
            OSError: If it couldn't be saved. Nothing is changed then either.
        """
        if marks is not None:
            marks = self._checked(battery, marks)
        with self._lock:
            def change(state):
                device = state["devices"].setdefault(battery.id, {})
                if enabled is not None:
                    device["enabled"] = bool(enabled)
                if marks is not None:
                    device["marks"] = marks
                if send_to_phone is not None:
                    device["send_to_phone"] = bool(send_to_phone)
            self._change(change)

    def reset(self, battery_id: str):
        """
        Forgets a device's own choices, so it follows its kind's defaults again,
        and saves. Its warned marks are kept, so putting the defaults back while
        a battery is low doesn't make it warn a second time.

        Args:
            battery_id: The device's sensor id.
        """
        with self._lock:
            self._change(lambda state: state["devices"].pop(battery_id, None))

    def set_also_send_when_said(self, on: bool):
        """Sets whether a level Nova says aloud is also sent to the phone, and saves."""
        with self._lock:
            self._change(lambda state: state.update(also_send_when_said=bool(on)))

    def due(self, batteries: List[Battery]) -> List[BatteryWarning]:
        """
        Works out which warnings to give now, and remembers them.

        A mark warns once. The marks start over when the device charges, or when
        its level reaches its highest mark + 10 - the second is for a device with
        no charging state, or a Mac which says "not charging" even when it has
        been, and keeps a wobble from 14% to 16% from being taken for a charge.

        Args:
            batteries: Every battery Home Assistant shows just now.

        Returns:
            The warnings, at most one for each battery - for the lowest mark it is
            under that hadn't warned. Every mark it is under counts as warned, so
            a device first seen at 4% warns once, not for 15% then 5%.
        """
        with self._lock:
            warned = {}
            given = []
            for battery in batteries:
                if not self.enabled(battery.id):
                    continue
                marks = self.marks_for(battery)
                before = set(self._state["warned"].get(battery.id, []))
                if battery.charging or battery.level >= max(marks) + 10:
                    now = set()
                else:
                    under = {m for m in marks if battery.level < m}
                    fresh = under - before
                    if fresh:
                        given.append(BatteryWarning(battery, min(fresh)))
                    now = before | under
                if now != before:
                    warned[battery.id] = sorted(now)

            if warned:
                def remember(state):
                    for battery_id, marks in warned.items():
                        if marks:
                            state["warned"][battery_id] = marks
                        else:
                            state["warned"].pop(battery_id, None)
                try:
                    self._change(remember)
                except OSError as e:
                    # The warnings are still given: a lost save costs a repeat after a restart.
                    logger.warning(f"Couldn't save the warned battery marks: {e}")
            return given

    def _change(self, change):
        """Applies a change to a copy of the state, saves it, and only then keeps it."""
        new = copy.deepcopy(self._state)
        change(new)
        storage.save_json(self._path, new)
        self._state = new

    def _checked(self, battery: Battery, marks: List[int]) -> List[int]:
        """Returns marks highest first, or raises ValueError saying what is wrong."""
        wanted = 1 if battery.kind == "mobile" else 2
        if len(marks) != wanted:
            raise ValueError(
                f"{battery.name} needs {'one mark' if wanted == 1 else 'two marks'}, not {len(marks)}."
            )
        if any(type(m) is not int or not 1 <= m <= 99 for m in marks):
            raise ValueError("A mark must be a whole number from 1 to 99.")
        if len(set(marks)) != len(marks):
            raise ValueError("The two marks must be different.")
        return sorted(marks, reverse=True)
