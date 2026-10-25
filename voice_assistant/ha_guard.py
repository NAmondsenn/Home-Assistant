"""
Home Assistant Guard Module.

What Nova never passes to Home Assistant's Assist, and what a script or scene does,
checked before it's run - for home_assistant.py, which asks Assist and runs them.

Some things are never passed on to Assist, by what Home Assistant says they are and
under any name it knows them by - their name, the words of their entity id, or an
other name from the registry (see ha_registry.py):
- every lock, valve and alarm panel, and every cover which is a door, garage door,
  gate or window (see GUARDED) - Assist's own "turn off" unlocks a lock;
- everything of the Proxmox integration, so "turn on PVE2 shut down" is refused even
  if it's been exposed;
- HASS.Agent's commands on the PC, whatever kind of entity each is, bar the buttons
  for sleep, monitor sleep and lock - through Assist, shutting the PC down or
  restarting it would be done with no yes.
Nor are the words for unlocking or opening anything (see SENSITIVE).

A request naming one of those is refused whole, even when a harmless thing's name
holds the guarded one's, as Assist reads a room before a name: "turn off the bedroom
front door" can be a lock called "front door" in the bedroom. So a light sentence can
be refused for its light's name - "turn on the front door light", with a lock called
"front door" - or for SENSITIVE's words, as "turn on the garage light" is. That's then
done by Nova's own lights instead (see home_assistant.HomeAssistant.switch_lights),
which only ever work lights, rather than refused for a lock nobody asked about.

Nor is anything passed on before Assist's own sentence debugging has said what it would
act on (see home_assistant.HomeAssistant._assist_targets). A request is refused if
any of that is guarded, or acts and isn't in the registry - which is up to five minutes
behind what Proxmox or HASS.Agent add - and isn't sent at all if Assist would act on
nothing the guard can see. Without the registry or the debugging, only Nova's own light
sentences go to Assist, with the checks of what they name - decision 2's wider rule is
off until both answer again - and without the registry, nothing the debugging says
they'd act on is vouched for, so Nova's own lights do them instead.

Nor is a script or scene run - by Assist, by Claude (see
home_assistant.HomeAssistant.run_script) or from the settings page - before what it does
has been checked: a script's configuration walked whole, and a scene's members, for
anything above (see home_assistant.HomeAssistant.works_guarded). A script is read by its
key: the key its own action names, or the one Home Assistant gives its entity id now,
looked up afresh rather than from the registry's snapshot, which can be five minutes
behind a rename. One whose contents can't be seen is refused too, and so is every one
without the registry, which says what's Proxmox's and the PC's. Claude only runs those
exposed to Assist, as Assist would.
"""

import logging
import re
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

from voice_assistant.ha_registry import PC_SAFE, Snapshot

# Logger setup
logger = logging.getLogger(__name__)

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
# The domains which only ever say how something is, and never act - the rest can all do
# something, as a HASS.Agent command can be a switch, a light or a siren.
NOT_ACTING = ("sensor", "binary_sensor")

# The Proxmox integration's platform in the registry. Everything of it is guarded, as
# its buttons and switches shut down, restart and stop the machines Nova itself runs on.
PROXMOX = "proxmoxve"

# Why a script or scene isn't run: it works something guarded, or what it does can't be
# seen. They're told apart in what's said, as "I don't lock, unlock or open anything" is
# no answer for a script whose contents Home Assistant won't show.
WORKS_GUARDED = "works something guarded"
UNSEEN = "can't be seen"

# Why Assist isn't asked to act on something, as the log says it. Something with no unique
# id - a YAML template switch, a scene with no id, an old-style group - is never in the
# registry, so it's refused for good, not only until the registry's next read: the log
# says what would let it be worked. An old-style group can't take one, and is made a
# group helper in the UI instead (see docs/troubleshooting.md). A new entity is told the
# same for its first five minutes, as the two can't be told apart.
NOT_IN_STATES = "not in Home Assistant's states"
GUARDED_TARGET = "never worked by voice"
UNREGISTERED = "not in Home Assistant's registry - give it a unique id to work it by voice"
NO_REGISTRY = "not vouched for while Home Assistant's registry can't be read"

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

# Actions which stop Home Assistant, or the machine it runs on - the hub Nova works the house
# through - so they work something guarded as Proxmox's buttons do, with no entity named.
STOPS_HOME = ("hassio.host_shutdown", "hassio.host_reboot", "homeassistant.stop", "homeassistant.restart")

# The script domain's own actions. Every other script.<name> is a script's own action, named
# by its key - see _walk.
SCRIPT_SERVICES = ("turn_on", "turn_off", "toggle", "reload")

# What could be an entity id in a script's configuration, e.g. "lock.front_door" - or an
# action's name, which reads as one: "lock.unlock" is the lock domain's, so guarded too.
ENTITY = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")

# What Assist's sentence debugging says it would do with a sentence, without doing it:
# whether it matched, the intent, e.g. "HassTurnOff", and every entity it would act on.
AssistMatch = NamedTuple("AssistMatch", [("matched", bool), ("intent", Optional[str]), ("targets", List[str])])

# A script or scene, by entity id - what Nova may run, once its name and what it does have
# been checked (see sensitive and why_not_run).
RUNNABLE = re.compile(r"(script|scene)\.[a-z0-9_]+")


def sensitive(entity_id: str, name: str) -> bool:
    """Whether a script or scene's id or name says it locks or opens something, as SENSITIVE has it."""
    return sensitive_words(name) or sensitive_words(entity_id.replace("_", " "))


def sensitive_words(text: str) -> bool:
    """Whether some words say they'd lock, unlock or open something, as SENSITIVE has it - bar PC_LOCK."""
    return bool(SENSITIVE.search(PC_LOCK.sub(" ", text.lower())))


def split_words(text: str) -> Tuple[str, ...]:
    """Words alone, in lower case, e.g. ("front", "door") for "Front-door!"."""
    return tuple(re.findall(r"[a-z0-9]+", text.lower()))


def guarded(entity_id: str, attributes: Dict, snapshot: Optional[Snapshot]) -> Optional[Sequence[str]]:
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
    if domain == "cover":
        device_class = attributes.get("device_class")
        # Home Assistant only ever gives a word, or none - a list, say, is a cover whose
        # kind can't be told, so it could be a garage door.
        if device_class is not None and not isinstance(device_class, str):
            return ()
        kind = GUARDED_COVERS.get(device_class)
    else:
        kind = GUARDED.get(domain)
    if kind is not None or snapshot is None:
        return kind
    if snapshot.platform(entity_id) == PROXMOX:
        return ()
    # A HASS.Agent command can be a switch, a light or a siren as well as a button, and
    # any of them could shut the PC down. Only a button is vouched for by its kind, as
    # only a button's name is HASS.Agent's own.
    if domain not in NOT_ACTING and entity_id in snapshot.hass_agent_commands:
        _, pc_kind = snapshot.hass_agent_commands[entity_id]
        if domain != "button" or pc_kind not in PC_SAFE:
            return ()
    return None


def names_guarded(request: str, states: List[Dict], snapshot: Optional[Snapshot]) -> bool:
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
        snapshot: The registry, or None if it can't be read - see guarded.

    Returns:
        Whether it's to be refused.
    """
    said, excused = _said(request)
    names, kinds = set(), set()
    for state in states:
        entity = str(state.get("entity_id") or "")
        attributes = state.get("attributes") or {}
        kind = guarded(entity, attributes, snapshot)
        if kind is None:
            continue
        names |= _names(entity, attributes, snapshot)
        kinds |= set(kind)
    return any(name in said for name in names) or any(kind in excused for kind in kinds)


def runnables_named(request: str, states: List[Dict], snapshot: Optional[Snapshot]) -> List[str]:
    """
    The scripts, scenes and groups a request names, found as names_guarded finds a guarded
    thing: by name, the words of its entity id or an other name, as whole words. A group
    is anything with its members as its entity_id attribute, of any domain.

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
        attributes = state.get("attributes") or {}
        if ((RUNNABLE.fullmatch(entity) or isinstance(attributes.get("entity_id"), list))
                and _names(entity, attributes, snapshot) & said):
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
    excused = _runs(split_words(PC_LOCK.sub(" ", request.lower())))
    return _runs(split_words(request)) | excused, excused


def _names(entity_id: str, attributes: Dict, snapshot: Optional[Snapshot]) -> Set[str]:
    """
    Every name something goes by, its words run together as _runs has them: its name, the
    words of its entity id and its other names from the registry - e.g. {"frontdoor"}.
    """
    names = {split_words(str(attributes.get("friendly_name") or "")),
             split_words(entity_id.partition(".")[2].replace("_", " "))}
    names |= {split_words(alias) for alias in (snapshot.aliases(entity_id) if snapshot else ())}
    return {"".join(name) for name in names if name}


def _runs(words: Tuple[str, ...]) -> Set[str]:
    """
    Every run of whole words, run together - for ("pve", "2", "shutdown"), "pve", "pve2",
    "pve2shutdown", "2", "2shutdown" and "shutdown". So a name is found as whole words
    whether it's said as one word or more, and never inside a word.
    """
    return {"".join(words[start:end]) for start in range(len(words)) for end in range(start + 1, len(words) + 1)}


def target_refused(entity_id: str, state: Optional[Dict], snapshot: Optional[Snapshot]) -> bool:
    """
    Whether Assist mustn't be asked to act on something its sentence debugging says it would.

    Args:
        entity_id: e.g. "lock.main".
        state: Its state, as /api/states has it, or None if that hasn't got it.
        snapshot: The registry, or None if it can't be read - see guarded.

    Returns:
        True for anything guarded, anything that wasn't there a moment ago to be checked,
        and anything which acts that the registry can't vouch for.
    """
    return why_target_refused(entity_id, state, snapshot) is not None


def why_target_refused(entity_id: str, state: Optional[Dict], snapshot: Optional[Snapshot]) -> Optional[str]:
    """
    Why Assist mustn't be asked to act on something, for the log - see target_refused.

    Args:
        entity_id, state, snapshot: As for target_refused.

    Returns:
        NOT_IN_STATES, GUARDED_TARGET, UNREGISTERED or NO_REGISTRY - or None if it may be
        acted on.
    """
    if state is None:
        return NOT_IN_STATES
    if guarded(entity_id, state.get("attributes") or {}, snapshot) is not None:
        return GUARDED_TARGET
    # Whatever Proxmox or HASS.Agent adds is in the states at once, but in the registry
    # only up to five minutes later - and either can act through a switch, an update or a
    # light as well as a button. Only a sensor's or a binary sensor's never acts.
    if entity_id.partition(".")[0] in NOT_ACTING:
        return None
    if snapshot is None:
        return NO_REGISTRY
    return UNREGISTERED if snapshot.platform(entity_id) is None else None


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
        WORKS_GUARDED, UNSEEN or None, as for _walk.
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
            if action in STOPS_HOME:
                found.append(WORKS_GUARDED)
    devices = part.get("device_id")
    if devices is not None:
        # A device action names its device and the domain it acts in, but its entity only
        # by the registry's own id for it: {"type": "unlock", "device_id": "...",
        # "entity_id": "4b1e...", "domain": "lock"}.
        if str(part.get("domain") or "").lower() in GUARDED:
            found.append(WORKS_GUARDED)
        # Home Assistant acts on that entity without checking it's on the device given, so
        # it's checked for itself: the lamp's device with the PC's shutdown button presses
        # the shutdown. An entity id is walked as any other string is.
        for piece in _pieces(part.get("entity_id")):
            if piece in ("", "all", "none") or ENTITY.fullmatch(piece):
                continue
            entity = snapshot.entity_for_registry_id(piece) if snapshot is not None else None
            state = by_id.get(entity) if entity is not None else None
            if entity is not None and guarded(entity, (state or {}).get("attributes") or {}, snapshot) is not None:
                found.append(WORKS_GUARDED)
            elif entity is None or target_refused(entity, state, snapshot):
                found.append(UNSEEN)
        for device in devices if isinstance(devices, list) else [devices]:
            if not isinstance(device, str) or "{{" in device:
                continue
            # One the registry hasn't got - or any, without it - could have anything on it.
            if snapshot is None or device not in snapshot.devices:
                found.append(UNSEEN)
            elif any(guarded(entity, (by_id.get(entity) or {}).get("attributes") or {}, snapshot) is not None
                     for entity in snapshot.on_device(device)):
                found.append(WORKS_GUARDED)
    return _worst(found)


def _pieces(value) -> List[str]:
    """
    The entities some configuration gives, as Home Assistant reads them - in any case, and
    several to a string: "light.lamp, Lock.Front_Door" is ["light.lamp", "lock.front_door"].
    From a string or a list of them, leaving templates out - see _walk.
    """
    strings = value if isinstance(value, list) else [value]
    return [piece.strip().lower() for string in strings if isinstance(string, str) and "{{" not in string
            for piece in string.split(",")]


class Scripts:
    """
    How a check reads scripts: each one's configuration by its key, and which key a
    script's entity id has now - each asked for once a check, however many scripts name
    it, and afresh for the next check.
    """

    def __init__(self, read_config: Callable[[str, str], Optional[Dict]],
                 keys_of: Callable[[List[str]], Dict[str, Optional[str]]]):
        """
        Args:
            read_config: Reads a script's configuration by its key, given the script as
                it's named for saying it can't be read, e.g. ("leaving", "script.leaving")
                - or gives None if it can't be read. See
                home_assistant.HomeAssistant._script_config.
            keys_of: Looks up the keys some scripts' entity ids have in Home Assistant
                now, all at once - each None if it can't be told. See
                home_assistant.HomeAssistant._script_keys.
        """
        self._read_config = read_config
        self._keys_of = keys_of
        self._configs: Dict[str, Optional[Dict]] = {}
        self._keys: Dict[str, Optional[str]] = {}

    def look_up(self, entity_ids: Iterable[str]):
        """
        Looks up the keys of these scripts' entity ids together, those not looked up
        already - so a settings page draw asks once for every script, rather than once each.
        """
        wanted = list(dict.fromkeys(entity for entity in entity_ids if entity not in self._keys))
        if wanted:
            found = self._keys_of(wanted)
            self._keys.update({entity: found.get(entity) for entity in wanted})

    def config(self, key: str, name: str) -> Optional[Dict]:
        """The configuration of the script of this key, or None if it can't be read."""
        if key not in self._configs:
            self._configs[key] = self._read_config(key, name)
        return self._configs[key]

    def key(self, entity_id: str) -> Optional[str]:
        """The key of the script with this entity id now, or None if it can't be told."""
        self.look_up([entity_id])
        return self._keys[entity_id]


def why_not_run(entity_ids: Sequence[str], states: List[Dict], snapshot: Optional[Snapshot], scripts: Scripts,
                too_deep: Set[str]) -> Optional[str]:
    """
    Why some scripts and scenes aren't to be run, if they aren't - see
    home_assistant.HomeAssistant.works_guarded.
    Each is walked on its own, so that a script another has walked already is still
    checked for itself - but each script's configuration is read once.

    Args:
        entity_ids: e.g. ["script.goodnight", "scene.relax"].
        states: Every entity's state, as /api/states has them.
        snapshot: The registry, or None if it can't be read.
        scripts: How to read the scripts, and what's been read of them so far - shared by
            the caller for a settings page draw, and otherwise new, to read them afresh.
        too_deep: The scripts and scenes said already to go too deep to follow, which
            this adds to - the HomeAssistant's own, so each is said once.

    Returns:
        WORKS_GUARDED if any of them works something guarded, else UNSEEN if any of
        them does what can't be checked, else None.
    """
    by_id = {state.get("entity_id"): state for state in states}
    reasons = []
    for entity_id in entity_ids:
        # One that wasn't there a moment ago can't be checked - it could be new since.
        if entity_id not in by_id:
            reasons.append(UNSEEN)
            continue
        try:
            reasons.append(_walk(entity_id, by_id, snapshot, set(), scripts))
        except RecursionError:
            # A configuration nested deeper than Python will walk or decode, or a long
            # enough chain of scripts each running the next, can't be checked - and
            # mustn't take the request, or the settings page's draw, with it.
            if entity_id not in too_deep:
                logger.warning(f"Can't see what {entity_id} does, as it goes too deep to follow, so it isn't "
                               f"run by voice or from the settings page")
            too_deep.add(entity_id)
            reasons.append(UNSEEN)
    return _worst(reasons)


def _walk(part, by_id: Dict[str, Dict], snapshot: Optional[Snapshot], checked: Set[str], scripts: Scripts,
          action: bool = False) -> Optional[str]:
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
        scripts: How to read the scripts - see why_not_run.
        action: Whether it's what a step calls its action, e.g. "script.leaving", rather
            than an entity id or anything else.

    Returns:
        WORKS_GUARDED if any of it works something guarded, else UNSEEN if any of it
        can't be checked, else None.

    Raises:
        RecursionError: For a configuration, or a chain of scripts, too deep to follow.
    """
    if isinstance(part, dict):
        found = [_step_refused(part, by_id, snapshot)]
        for key, value in part.items():
            found += [_walk(key, by_id, snapshot, checked, scripts),
                      _walk(value, by_id, snapshot, checked, scripts, action=key in ACTION_KEYS)]
        return _worst(found)
    if isinstance(part, list):
        return _worst([_walk(item, by_id, snapshot, checked, scripts) for item in part])
    found = []
    # A template is left out: it's the user's own, and they exposed what it's in (0008,
    # "My calls").
    for entity in _pieces(part):
        if not ENTITY.fullmatch(entity):
            continue
        domain, _, name = entity.partition(".")
        if domain == "script" and action:
            # Home Assistant gives each script an action of its own, named by its key,
            # which stays as it was when the script's entity id is renamed:
            # {"action": "script.leaving"} runs the script whose key is "leaving", whatever
            # its entity id is now - so it's read by that key. The script domain's own
            # actions only ever act on the entities they're given.
            if name not in SCRIPT_SERVICES:
                found.append(_walk_script(name, entity, by_id, snapshot, checked, scripts))
            continue
        if domain == "script" and (name not in SCRIPT_SERVICES or entity in by_id):
            if entity in checked:
                continue
            checked.add(entity)
            # An entity id is whichever script has it now, which the registry can be up to
            # five minutes behind - a rename, or two scripts swapping entity ids, would
            # read the wrong one's configuration. So its key is asked for afresh, and a
            # script that wasn't there a moment ago, or whose key can't be told, can't be
            # checked.
            key = scripts.key(entity) if entity in by_id and snapshot is not None else None
            found.append(UNSEEN if key is None else _walk_script(key, entity, by_id, snapshot, checked, scripts))
            continue
        state = by_id.get(entity)
        attributes = (state or {}).get("attributes") or {}
        if domain == "automation":
            # An automation's contents aren't walked, and one could unlock - nor is what
            # automation.trigger sets off.
            found.append(UNSEEN)
        elif guarded(entity, attributes, snapshot) is not None:
            found.append(WORKS_GUARDED)
        elif state is None or entity in checked:
            # An action's name, e.g. "light.turn_off", or nothing there to work - or
            # something walked already.
            continue
        elif target_refused(entity, state, snapshot):
            # Something the registry hasn't caught up with yet, which could be Proxmox's.
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
                found.append(_walk(members, by_id, snapshot, checked, scripts))
    return _worst(found)


def _walk_script(key: str, name: str, by_id: Dict[str, Dict], snapshot: Optional[Snapshot], checked: Set[str],
                 scripts: Scripts) -> Optional[str]:
    """
    Why the script of this key isn't to be run, if it isn't - its configuration walked, once
    a check. Without the registry, which says what's Proxmox's and the PC's, none can be.

    Args:
        key: Its key, e.g. "leaving".
        name: It as it's named, e.g. "script.leaving", for saying it can't be read.
        by_id, snapshot, checked, scripts: As for _walk.

    Returns:
        As for _walk.
    """
    # Keys share checked with entity ids, which never have a space in them.
    walked = "script key " + key
    if walked in checked:
        return None
    checked.add(walked)
    config = scripts.config(key, name) if snapshot is not None else None
    return UNSEEN if config is None else _walk(config, by_id, snapshot, checked, scripts)
