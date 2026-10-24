"""
Batteries Module
The rules for what a battery is, from decisions 2 and 5 of docs/decisions/0007.

Home Assistant is the only source of batteries (see HomeAssistant.batteries), so
nothing here is a hand-kept list of devices. What it says about each device's
maker and model is enough to sort it into "mobile" (an Android phone or tablet,
an iPhone or iPad - warned about once, at 5%) or "other" (a Mac, a remote, a
sensor, or anything whose kind can't be worked out - warned about at 15% and 5%).

Charging is read from the words each app uses for its charging sensor, and a
reading old enough to be stale is said with its age, so that a tablet last heard
from three days ago isn't taken to be on 88% now.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from voice_assistant.clock import format_duration

# Makers of Android phones and tablets, apart from Samsung, whose models are
# told apart by their "SM-" prefix below (a Samsung TV is not a phone).
_ANDROID_MAKERS = {
    "google", "oneplus", "xiaomi", "motorola", "sony", "huawei", "oppo", "realme", "nothing",
}

# Spellings a plain capital letter would get wrong - "macbook" is not "Macbook".
_KNOWN_SPELLINGS = {"macbook": "MacBook", "iphone": "iPhone", "ipad": "iPad"}

_CHARGING_WORDS = {"charging", "full", "on"}
_NOT_CHARGING_WORDS = {"discharging", "not charging", "off"}

# Words a person uses for a kind of device, and which batteries each one means.
_KIND_WORDS = ("phone", "tablet", "laptop", "computer", "mac")


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
        charging: Whether it's charging, or None if that isn't known.
    """

    id: str
    name: str
    level: float
    changed: float
    device: Optional[str]
    kind: str
    phone: bool
    charging: Optional[bool]

    @classmethod
    def from_home(cls, found: Dict) -> "Battery":
        """
        Builds a battery from one of HomeAssistant.batteries()'s dicts.

        Args:
            found: The dict, with id, name, level, changed, device, maker, model and charging.

        Returns:
            The battery. With no device there is nothing to read a kind or a
            charging sensor from, so it's something else and never charging.
        """
        device = found.get("device")
        maker = found.get("maker") if device else None
        model = found.get("model") if device else None
        return cls(
            id=found["id"],
            name=spoken_name(found["name"]),
            level=found["level"],
            changed=found["changed"],
            device=device,
            kind=kind_of(maker, model),
            phone=is_phone(maker, model),
            charging=charging_from(found.get("charging")) if device else None,
        )


def _is_tablet(maker: Optional[str], model: Optional[str]) -> bool:
    """Whether a mobile device is a tablet, from its model."""
    model = (model or "").lower()
    if (maker or "").lower() == "samsung":
        # Galaxy Tabs are SM-X and SM-T; everything else mobile is a phone.
        return model.startswith(("sm-x", "sm-t"))
    return model.startswith("ipad") or "tab" in model or "pad" in model


def kind_of(maker: Optional[str], model: Optional[str]) -> str:
    """
    Sorts a device into mobile or other.

    Args:
        maker: The device's manufacturer, as Home Assistant says it, or None.
        model: The device's model, or None.

    Returns:
        "mobile" for an Android phone or tablet, an iPhone or an iPad;
        otherwise "other", including when the maker or model is unknown.
    """
    maker = (maker or "").lower()
    model = (model or "").lower()
    if maker == "apple":
        return "mobile" if model.startswith(("iphone", "ipad")) else "other"
    if maker == "samsung":
        return "mobile" if model.startswith("sm-") else "other"
    return "mobile" if maker in _ANDROID_MAKERS else "other"


def is_phone(maker: Optional[str], model: Optional[str]) -> bool:
    """
    Whether a device is a phone - the one device whose warnings aren't sent to
    the phone, as that's the phone running out.

    Args:
        maker: The device's manufacturer, or None.
        model: The device's model, or None.

    Returns:
        True for a mobile device that isn't a tablet.
    """
    return kind_of(maker, model) == "mobile" and not _is_tablet(maker, model)


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


def _kind_word_matches(word: str, battery: Battery) -> bool:
    """Whether a kind word like "tablet" describes a battery's device."""
    if word == "phone":
        return battery.phone
    if word == "tablet":
        return battery.kind == "mobile" and not battery.phone
    # A laptop, computer or Mac is something else, as nothing mobile is one.
    return battery.kind == "other"


def find(batteries: List[Battery], asked: str) -> List[Battery]:
    """
    Finds the devices someone meant by what they said.

    Args:
        batteries: The batteries to choose from.
        asked: The words asked, e.g. "mac", "tab", "phone", or "" or "all" for every one.

    Returns:
        The batteries meant, in their given order. A word matches the start of
        any word in a name ("mac" finds "MacBook Pro"), or is a kind word:
        phone, tablet, laptop, computer or mac. Empty if nothing matches.
    """
    words = [w for w in asked.lower().split() if w != "all"]
    if not words:
        return list(batteries)

    found = []
    for battery in batteries:
        name_words = battery.name.lower().split()
        for word in words:
            if any(n.startswith(word) for n in name_words) or (
                word in _KIND_WORDS and _kind_word_matches(word, battery)
            ):
                found.append(battery)
                break
    return found


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
