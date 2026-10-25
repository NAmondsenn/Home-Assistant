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

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import requests

from voice_assistant.choices import ChoiceStore
from voice_assistant.home_assistant import HomeAssistant
from voice_assistant.clock import format_duration
from voice_assistant.config import marks_problem

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
            computer=app and is_mac(maker, model),
            charging=charging_from(found.get("charging")) if device else None,
        )


def is_mac(maker: Optional[str], model: Optional[str]) -> bool:
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
    return "mobile" if app and not is_mac(maker, model) else "other"


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
        as it is, a known spelling is used, and an all-lower-case word is capitalised.
        Any other keeps the capitals the user gave it ("TV remote" is "TV Remote").
    """
    words = name.replace("-", " ").replace("_", " ").split()
    said_words = []
    for word in words:
        if word.lower() in _KNOWN_SPELLINGS:
            said_words.append(_KNOWN_SPELLINGS[word.lower()])
        elif any(c.isdigit() for c in word):
            said_words.append(word.upper())
        else:
            said_words.append(word.capitalize() if word.islower() else word)
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
        self._store = ChoiceStore(
            path, {"devices": {}, "warned": {}, "held": {}, "also_send_when_said": True}
        )
        self._lock = self._store.lock

    @property
    def _state(self) -> Dict:
        return self._store.state

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

    def changed(self, battery: Battery) -> bool:
        """Whether any of a device's choices differ from the defaults - when the page offers to put them back."""
        return (self.marks_for(battery) != self.default_marks(battery) or not self.enabled(battery.id)
                or self.send_to_phone(battery) != (not battery.phone))

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
            def change(state: Dict):
                device = state["devices"].setdefault(battery.id, {})
                if enabled is not None:
                    device["enabled"] = bool(enabled)
                if marks is not None:
                    device["marks"] = marks
                if send_to_phone is not None:
                    device["send_to_phone"] = bool(send_to_phone)
            self._store.change(change)

    def reset(self, battery_id: str):
        """
        Forgets a device's own choices, so it follows its kind's defaults again,
        and saves. Its warned marks are kept, so putting the defaults back while
        a battery is low doesn't make it warn a second time.

        Args:
            battery_id: The device's sensor id.
        """
        with self._lock:
            self._store.change(lambda state: state["devices"].pop(battery_id, None))

    def set_also_send_when_said(self, on: bool):
        """Sets whether a level Nova says aloud is also sent to the phone, and saves."""
        with self._lock:
            self._store.change(lambda state: state.update(also_send_when_said=bool(on)))

    def due(self, batteries: List[Battery]) -> List[BatteryWarning]:
        """
        Works out which warnings to give now, and remembers them.

        A mark warns once. The marks start over when the device charges, or when
        its level reaches its highest mark + 10 (at most 100) - the second is for a device with
        no charging state, or a Mac which says "not charging" even when it has
        been, and keeps a wobble from 14% to 16% from being taken for a charge.

        Args:
            batteries: Every battery Home Assistant shows just now.

        Returns:
            The warnings, at most one for each battery - for the lowest mark it is at
            or under that hadn't warned, as a mark of 15 warns at 15%. Every mark it is
            at or under counts as warned, so a device first seen at 4% warns once, not
            for 15% then 5%.
        """
        with self._lock:
            warned = {}
            given = []
            for battery in batteries:
                marks = self.marks_for(battery)
                before = set(self._state["warned"].get(battery.id, []))
                # Capped at 100%, or a mark of 95 could never start over for a device with no charging state.
                start_over = min(max(marks) + 10, 100)
                if battery.charging or battery.level >= start_over:
                    now = set()
                elif self.enabled(battery.id):
                    under = {m for m in marks if battery.level <= m}
                    fresh = under - before
                    if fresh:
                        given.append(BatteryWarning(battery, min(fresh)))
                    now = before | under
                else:
                    # A switched-off device only starts over, so a charge while it was off isn't missed.
                    now = before
                if now != before:
                    warned[battery.id] = sorted(now)

            if warned:
                def remember(state: Dict):
                    for battery_id, marks in warned.items():
                        if marks:
                            state["warned"][battery_id] = marks
                        else:
                            state["warned"].pop(battery_id, None)
                try:
                    self._store.change(remember)
                except OSError as e:
                    # The warnings are still given and kept in memory, so they aren't repeated every
                    # minute while saves fail - only after a restart.
                    remember(self._state)
                    logger.warning(f"Couldn't save the warned battery marks: {e}")
            return given

    def hold(self, battery_id: str, mark: int):
        """
        Keeps a warning back until quiet hours end, and saves, so a restart in the
        night doesn't lose it. A device has at most one held warning - a newer one
        replaces it, as only the lowest mark is still worth saying.

        A failed save still holds it in memory, as due() does for the warned marks, so
        the warning isn't dropped - only forgotten by a restart.

        Args:
            battery_id: The device's sensor id.
            mark: The mark that warned, e.g. 15.
        """
        with self._lock:
            self._store.keep(lambda state: state["held"].__setitem__(battery_id, mark), "held warning")

    def held(self) -> Dict[str, int]:
        """Returns the warnings being held, as each device's sensor id and the mark that warned."""
        with self._lock:
            return dict(self._state["held"])

    def release(self, battery_id: str):
        """
        Lets a held warning go, said or no longer true, and saves.

        A failed save still lets it go in memory, so it isn't said again every pass.

        Args:
            battery_id: The device's sensor id. Nothing happens if it has none held.
        """
        with self._lock:
            if battery_id in self._state["held"]:
                self._store.keep(lambda state: state["held"].pop(battery_id), "release of a held warning")

    def _checked(self, battery: Battery, marks: List[int]) -> List[int]:
        """Returns marks highest first, or raises ValueError saying what is wrong."""
        problem = marks_problem(marks, 1 if battery.kind == "mobile" else 2, battery.name)
        if problem:
            raise ValueError(problem)
        return sorted(marks, reverse=True)


def deciding_person(people: List[Dict], battery: Battery) -> Dict:
    """
    Picks the person whose presence decides whether a battery's warning is said aloud.

    Args:
        people: Home Assistant's people, as HomeAssistant.people() has them. Not empty.
        battery: The battery which has fallen. Whoever's device it is decides; with
            only one user, a device which belongs to nobody still means them.

    Returns:
        The person whose trackers include the battery's device, else the first.
    """
    return next((p for p in people if battery.device and battery.device in p["devices"]), people[0])


# Every way a reading of Home Assistant can fail: it can't be reached, or it answers with
# something that isn't JSON, or with JSON of the wrong shape.
HOME_FAILURES = (requests.RequestException, ValueError, KeyError, TypeError)  # Only ever caught round a read.


class BatteryWatcher:
    """
    Reads the batteries every minute or so, and delivers the warnings that are due.

    Decision 4 of docs/decisions/0007: every warning goes to the phone, and is said
    aloud as well while the user is home by day. Where one can't reach the phone it
    is said instead, or held until the night is over.
    """

    def __init__(
        self,
        home: HomeAssistant,
        choices: BatteryChoices,
        say: Callable[[str], bool],
        send: Optional[Callable[[str, str], bool]],
        quiet: Callable[[], bool],
        check_every: float,
        stale_after: float,
    ):
        """
        Args:
            home: Home Assistant, for the batteries and where the user is.
            choices: The owner's choices, the warned marks and the held warnings.
            say: Says a message on the default satellite, returning whether it
                was said - it isn't when no satellite is connected.
            send: Sends a message and its title to the phone, returning whether ntfy
                took it, or None when there is no ntfy.
            quiet: Whether it is quiet hours now.
            check_every: Seconds between checks.
            stale_after: Seconds old a reading may be and still count as current.
        """
        self._home = home
        self._choices = choices
        self._say = say
        self._send = send
        self._quiet = quiet
        self._check_every = check_every
        self._stale_after = stale_after
        self._down = False
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def choices(self) -> BatteryChoices:
        """The owner's choices, which the settings page changes."""
        return self._choices

    @property
    def stale_after(self) -> float:
        """Seconds old a reading may be and still count as current - what a spoken answer needs, too."""
        return self._stale_after

    def read(self) -> List[Battery]:
        """
        Reads the batteries fresh from Home Assistant.

        Returns:
            Every battery it shows.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
            ValueError: If it didn't answer with the batteries.
        """
        return [Battery.from_home(found) for found in self._home.batteries()]

    def home_now(self, battery: Battery) -> Optional[bool]:
        """
        Whether the user is home, going by the person whose device it is.

        Args:
            battery: The battery which has fallen. Whoever's device it is decides; with
                only one user, a device which belongs to nobody still means them.

        Returns:
            True if the person is home, False if they are anywhere else, None if
            there is no person or Home Assistant couldn't say.
        """
        try:
            people = self._home.people()
        except HOME_FAILURES as e:
            logger.warning(f"Couldn't tell whether the user is home: {e}")
            return None
        if not people:
            return None
        return deciding_person(people, battery)["state"] == "home"

    def deciding_people(self, batteries: List[Battery]) -> Dict[str, str]:
        """
        Whose presence decides whether each battery's warning is said aloud, for the
        settings page to show - only worth showing with more than one person.

        Args:
            batteries: The batteries to name a person for.

        Returns:
            Each battery's sensor id and its person's name, e.g. {"sensor.mac_battery_level":
            "Amy"} - chosen as home_now does. Empty if Home Assistant has only one person,
            or none, or couldn't say.
        """
        try:
            people = self._home.people()
        except HOME_FAILURES as e:
            logger.warning(f"Couldn't tell whose battery is whose: {e}")
            return {}
        if len(people) < 2:
            return {}
        return {b.id: deciding_person(people, b)["name"] for b in batteries}

    def check(self, now: float):
        """
        Does one pass: reads the batteries, works out the warnings due, and delivers
        them. Once quiet hours are over it also says any held warning which is still true.

        If Home Assistant doesn't answer, that is logged once until it does, and
        nothing else happens. Any other error is a bug: it is logged with its
        traceback, and the pass goes on with the rest.

        Args:
            now: The current epoch time.
        """
        try:
            batteries = self.read()
        except HOME_FAILURES as e:
            # A minute-by-minute line for a night of Home Assistant being restarted would bury the log.
            if not self._down:
                logger.warning(f"Home Assistant didn't answer, so batteries aren't being watched: {e}")
            self._down = True
            return
        if self._down:
            logger.info("Home Assistant answers again, batteries are being watched.")
            self._down = False
        # Held ones go first, so a warning held in this pass isn't also looked at again in it.
        # Each step is wrapped, so a bug in one doesn't lose the warnings the others would give.
        try:
            self._say_held(batteries, now)
        except Exception:
            logger.exception("Saying the held battery warnings failed")
        for warning in self._choices.due(batteries):
            try:
                self._deliver(warning, now)
            except Exception:
                logger.exception(f"Delivering the battery warning for {warning.battery.name} failed")

    def start(self):
        """Starts the daemon thread which checks every check_every seconds."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="batteries")
        self._thread.start()

    def stop(self):
        """Stops the thread, waiting briefly for a pass already under way to end."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        while not self._stopping.is_set():
            self._pass()
            self._stopping.wait(self._check_every)

    def _pass(self):
        """One pass of the thread, in which no error may end the thread."""
        try:
            self.check(time.time())
        except Exception:
            logger.exception("Checking the batteries failed, will try again")

    def _deliver(self, warning: BatteryWarning, now: float):
        """
        Delivers one warning by the rules of decision 4.

        Args:
            warning: The warning which is due.
            now: The current epoch time.
        """
        battery = warning.battery
        message = self._message(battery, now)
        title = "Battery low"
        # With no ntfy there is nothing to send to, however the owner chose.
        sending = self._choices.send_to_phone(battery) and self._send is not None
        quiet = self._quiet()

        # 2. Out loud only when the user is there to hear it and isn't asleep.
        if not quiet and self.home_now(battery):
            if self._say(message):
                # The phone is where the user finds it later, if they didn't catch it.
                if sending and self._choices.also_send_when_said:
                    self._send(message, title)
                return

        # 3. Away, at night, or with nobody to say it to, the phone is where it always reaches them.
        if sending and self._send(message, title):
            return

        # 5. The phone warns the user about itself, so a warning it couldn't say aloud is dropped - unless its
        # "send to my phone" switch is on, which is the user saying it isn't their phone, or to send it anyway.
        if battery.phone and not self._choices.send_to_phone(battery):
            return

        # 4. It should have been sent but couldn't be, or there is no ntfy: say it if anyone can hear it,
        # else hold it, so the night's silence doesn't lose it.
        if quiet or not self._say(message):
            self._choices.hold(battery.id, warning.mark)

    def _message(self, battery: Battery, now: float) -> str:
        """
        The warning's words - with the reading's age if it's old, as said() does, so that
        4% last heard three days ago isn't taken to be the level now.

        Args:
            battery: The battery which has fallen.
            now: The current epoch time.

        Returns:
            e.g. "Your MacBook Pro's battery is down to 14%."
        """
        level = f"{round(battery.level)}%"
        age = now - battery.changed
        if age > self._stale_after:
            return f"Your {battery.name}'s battery was down to {level}, {format_duration(age)} ago."
        return f"Your {battery.name}'s battery is down to {level}."

    def _say_held(self, batteries: List[Battery], now: float):
        """
        Says the held warnings which are still true, once quiet hours are over.

        A warning is still true while its device is at or under the mark it warned at and
        isn't charging, and its warnings are on. Any other is let go, and one which
        couldn't be said stays held for the next pass.
        """
        held = self._choices.held()
        if not held or self._quiet():
            return
        by_id = {b.id: b for b in batteries}
        for battery_id, mark in held.items():
            battery = by_id.get(battery_id)
            if battery is None:
                # Home Assistant leaves out a sensor which is unavailable for a moment, as while it restarts - the
                # warning waits, rather than being lost to that. A device gone for good costs one small entry.
                continue
            gone = (
                "it's charging" if battery.charging
                else f"it's back above {mark}%" if battery.level > mark
                else "its warnings are switched off" if not self._choices.enabled(battery_id)
                else None
            )
            if gone is None and not self._say(self._message(battery, now)):
                continue
            if gone:
                logger.info(f"Letting go of the held battery warning for {battery.name}: {gone}")
            self._choices.release(battery_id)
