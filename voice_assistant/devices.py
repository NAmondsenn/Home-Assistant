"""
Devices Module
Where the user's phone, tablet or Mac is, and ringing it, through the Home Assistant app.

Home Assistant is the only source of these, as of the batteries (see
HomeAssistant.app_devices): every device with the app on it, its tracker, the place its
geocoded location sensor names, and its notify service. What kind of device each is,
and which one "my phone" means, are batteries.py's own rules, so "my phone" is the same
device for both.

Where a device is never goes to Claude, whoever asked - see Conversation.handle, which
gives Claude's history only that it was said. The place is the user's, and Claude's
history goes to Anthropic with every later request.
"""

from dataclasses import dataclass
from typing import Dict, Optional

from voice_assistant.batteries import is_mac, is_phone, kind_of, spoken_name
from voice_assistant.clock import format_duration

# What a tracker says when it can't tell where its device is.
UNKNOWN_STATES = ("unavailable", "unknown", "")
# What a part of a place is when it isn't there - the Android app gives a town it hasn't
# got as the word "null" (live, 25 Oct), which would be "near Example Road, null".
MISSING_PARTS = ("null", "none", "unknown", "unavailable", "")


@dataclass
class AppDevice:
    """
    One device with the Home Assistant app on it, with what Nova works out from it.

    Attributes:
        id: The Home Assistant device id.
        name: The spoken name, e.g. "S25 Ultra".
        kind: "mobile" or "other", as batteries.kind_of sorts it.
        phone: Whether it's a phone, rather than a tablet.
        computer: Whether it's a Mac.
        apple: Whether Apple made it, whose app rings another way.
        notify: Its notify service, e.g. "mobile_app_s25_ultra", or None if it hasn't got one.
        tracker: Its tracker's state, e.g. "home", "not_home" or a zone's name, or None
            with no tracker.
        reported: When its tracker last reported, in epoch seconds, or None.
        place: Where its geocoded location sensor says it is, e.g. "Example Road, Sometown",
            or None.
    """

    id: str
    name: str
    kind: str
    phone: bool
    computer: bool
    apple: bool
    notify: Optional[str]
    tracker: Optional[str]
    reported: Optional[float]
    place: Optional[str]

    @classmethod
    def from_home(cls, found: Dict) -> "AppDevice":
        """
        Builds a device from one of HomeAssistant.app_devices()'s dicts.

        Args:
            found: The dict, with device, name, maker, model, notify, tracker and place.

        Returns:
            The device. Everything it has is on the app, so a Mac is the only one that isn't mobile.
        """
        maker, model = found.get("maker"), found.get("model")
        kind = kind_of(maker, model, True)
        tracker = found.get("tracker") or {}
        return cls(
            id=found["device"],
            name=spoken_name(found["name"]),
            kind=kind,
            phone=kind == "mobile" and is_phone(maker, model),
            computer=is_mac(maker, model),
            apple=(maker or "").lower() == "apple",
            notify=found.get("notify"),
            tracker=tracker.get("state"),
            reported=tracker.get("reported"),
            place=_place(found.get("place")),
        )


def _place(place: Optional[Dict]) -> Optional[str]:
    """
    The place a geocoded location sensor names, e.g. "Example Road, Sometown" - its street and
    town, or whichever of them it has, else the first two parts of its state, which is the
    whole address.
    """
    if not place:
        return None
    parts = [_part(place.get("thoroughfare")), _part(place.get("locality"))]
    if not any(parts):
        parts = [_part(part) for part in str(place.get("state") or "").split(",")[:2]]
    return ", ".join(part for part in parts if part) or None


def _part(value) -> Optional[str]:
    """One part of a place as text, e.g. "Sometown" - or None for one that isn't there, such as "null"."""
    text = "" if value is None else str(value).strip()
    return None if text.lower() in MISSING_PARTS else text


def said_as(device: AppDevice) -> str:
    """What a device is called aloud: "phone", "tablet" or "Mac", or its name if it's none of those."""
    if device.phone:
        return "phone"
    if device.computer:
        return "Mac"
    return "tablet" if device.kind == "mobile" else device.name


def where(device: AppDevice, now: float) -> str:
    """
    Says where a device is.

    Args:
        device: The device.
        now: The current epoch time.

    Returns:
        "Your phone's at home.", "Your phone's at Work." in another zone, "Your phone's
        near Example Road, Sometown, as of 10 minutes ago." away with a place, "Your phone's
        away from home, as of 10 minutes ago." without one, or "I can't tell where your
        phone is." with no tracker, or one that can't tell.
    """
    word = said_as(device)
    state = (device.tracker or "").strip()
    if state.lower() in UNKNOWN_STATES:
        return f"I can't tell where your {word} is."
    if state == "home":
        return f"Your {word}'s at home."
    if state != "not_home":
        return f"Your {word}'s at {state}."
    # Away, it may have moved since, so how old the place is goes with it.
    age = _age(device.reported, now)
    if device.place:
        return f"Your {word}'s near {device.place}{age}."
    return f"Your {word}'s away from home{age}."


def _age(reported: Optional[float], now: float) -> str:
    """", as of 10 minutes ago" - to the minute - or "" if when it reported isn't known."""
    if reported is None:
        return ""
    minutes = round(max(0.0, now - reported) / 60)
    return ", as of just now" if not minutes else f", as of {format_duration(minutes * 60)} ago"


def ring_call(device: AppDevice) -> Dict:
    """
    What's sent to a device's notify service to ring it, loud enough to find it by.

    Args:
        device: The device.

    Returns:
        The message and its data. Android's app says it aloud on the alarm stream, which
        is heard on silent; an iPhone or iPad gets a critical alert, which is too; a Mac
        a notification with its sound.
    """
    if not device.apple:
        return {"message": "TTS", "data": {"tts_text": f"Nova's looking for your {said_as(device)}.",
                                           "media_stream": "alarm_stream_max", "priority": "high", "ttl": 0}}
    if device.computer:
        return {"message": "Nova's looking for your Mac.", "data": {"push": {"sound": "default"}}}
    said = "iPhone" if device.phone else "iPad"
    return {"message": f"Nova's looking for your {said}.",
            "data": {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}}}


def told_claude(asked: str) -> str:
    """
    What Claude's history has in place of where a device is, when that was asked locally.

    Args:
        asked: The device, as the local rule had it, e.g. "phone" or "mac".

    Returns:
        e.g. "I said where your phone is."
    """
    return f"I said where your {'Mac' if asked.lower() == 'mac' else asked.lower()} is."
