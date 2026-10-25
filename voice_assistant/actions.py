"""
Actions Module
Everything the assistant can do for the user, each declared once.

Each tool is a method of ActionExecutor, with what the model is told about it - its
description and parameters - written in a @tool line just above the code which
carries it out. The model is offered these (see tool_schemas), bar any kept for the
local commands and the offline keyword fallback alone, and whatever it calls, or they
ask for, comes back here as an Action naming one of them.

Adding a tool used to mean editing three places in two files - the description in
llm.py, a mapping from the model's call to an action dict, and the dispatch here -
which could drift apart. Now there's one place, and a test checks every tool has
both a description and the code to run it.
"""

import inspect
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import requests

from voice_assistant import network
from voice_assistant.batteries import HOME_FAILURES, find, said
from voice_assistant.devices import AppDevice, ring_call, said_as, where
from voice_assistant.home_assistant import CANT_REACH, HomeAssistant, NotAllowed
from voice_assistant.lists import NO_ANSWER, names_list

# Logger setup
logger = logging.getLogger(__name__)

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# What's said when a tool is asked for but the part of the assistant it needs
# isn't set up, keyed by the ActionExecutor attribute it needs.
UNAVAILABLE = {
    "music": "The music isn't available right now.",
    "clock": "Timers aren't available right now.",
    "volume": "Volume control isn't available right now.",
    "weather": "The weather needs Home Assistant.",
    "home": "Home Assistant isn't set up yet.",
    "memory": "I can't remember things right now.",
    "ntfy": "Notifications aren't set up yet.",
    "batteries": "Battery levels need Home Assistant.",
    "lists": "Lists need Home Assistant.",
    "pc": "Your PC needs Home Assistant.",
    "calendar": "The calendar isn't set up yet.",
}

# The most days of calendar read at once - a fortnight is already a lot to listen to -
# and the most "next" times said at once.
MOST_CALENDAR_DAYS = 14
MOST_NEXT = 10


@dataclass
class Action:
    """
    One thing to do, e.g. Action("set_timer", {"duration_seconds": 600}).

    Attributes:
        tool: The name of an ActionExecutor tool.
        params: What it was given, by parameter name.
        from_model: Whether the model asked for it, which can only run what it's
            offered - as opposed to a local command or the offline keyword fallback.
    """
    tool: str
    params: Dict[str, Any] = field(default_factory=dict)
    from_model: bool = False


@dataclass
class ActionResult:
    """
    What came of an action, and how the user should find out.

    Attributes:
        ok: Whether it worked.
        say: What to tell the user - said out loud unless it confirms itself
            another way, when it's only logged.
        confirm: How a success is confirmed: "speak" says it; "chime" plays the
            success chime; "music" leaves it to the music changing, e.g. the next
            song starting; "sound" plays its own sound; "note" says it after
            whatever else is said, rather than instead of it - "I'll remember that",
            after the answer to the question it came up in.
        sound: The sound, for "sound" - e.g. "volume_beep.wav".
        sound_category: Which volume the sound plays at.
        understood: False when Home Assistant's Assist matched nothing in what was
            said - which the model may be able to do something with - rather than
            understanding it and failing.
        private: Whether what it says is the user's alone - where their phone is - and
            so kept from Claude when it was asked for locally (see Conversation.handle).
        ask_first: What to do if the user says yes to the question it asks - shutting
            the PC down, after "Shut down the PC?" - or None (see Conversation.handle).
    """
    ok: bool
    say: str = ""
    confirm: str = "speak"
    sound: Optional[str] = None
    sound_category: str = "general"
    understood: bool = True
    private: bool = False
    ask_first: Optional[Action] = None

    @classmethod
    def from_reply(cls, reply: Dict) -> "ActionResult":
        """
        Reads what the clock, music or weather returned - a dict with "success"
        and "message", and maybe a flag saying how it confirms itself - into one
        result. The flags are only read here, so it's decided in one place which
        wins if more than one is set.

        Args:
            reply: e.g. {"success": True, "message": "Paused", "chime": True}.

        Returns:
            The ActionResult.
        """
        ok = bool(reply.get("success"))
        say = reply.get("message") or ""
        kept = {"private": bool(reply.get("private")), "ask_first": reply.get("ask_first")}
        if ok and reply.get("silent"):
            return cls(ok, say, confirm="music", **kept)
        if ok and reply.get("note"):
            return cls(ok, say, confirm="note", **kept)
        if ok and reply.get("sound"):
            return cls(ok, say, confirm="sound", sound=reply["sound"],
                       sound_category=reply.get("sound_category", "general"), **kept)
        if ok and reply.get("chime"):
            return cls(ok, say, confirm="chime", **kept)
        # Only a failure can be not understood, and a failure always gets this far.
        return cls(ok, say, understood=not reply.get("not_understood"), **kept)


def confirm(results: List[ActionResult], chime_enabled: bool = True,
            fallback: Optional[str] = None) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Works out how the actions a request ran should be confirmed to the user.

    A self-confirming action's message is only ever logged, never spoken, even when
    there's something else to say as well: a reminder offer tacked onto a skipped
    song used to have the skip's own message read out in front of it.

    Args:
        results: What each action returned, in the order they ran.
        chime_enabled: Whether the success chime is switched on. When it isn't, an
            action which would have chimed says its message instead.
        fallback: Said for an action which needs words but didn't give any - the
            model's own reply.

    Returns:
        Tuple of (sounds, messages). sounds are (file name, volume category) pairs
        to play, each at most once. messages are what still needs saying, in order -
        empty when every action confirmed itself.
    """
    sounds, messages = [], []
    notes = [result.say for result in results if result.ok and result.confirm == "note" and result.say]
    others = [result for result in results if not (result.ok and result.confirm == "note")]
    # Only notes - something remembered while answering a question - so the answer
    # itself is said first, unless it's only the "Okay." which goes with a tool call.
    if not others and fallback and not bare_acknowledgement(fallback):
        messages.append(fallback)
    for result in others:
        sound = None
        if result.ok and result.confirm == "music":
            continue
        if result.ok and result.confirm == "sound" and result.sound:
            sound = (result.sound, result.sound_category)
        elif result.ok and result.confirm == "chime" and chime_enabled:
            sound = ("success_chime.wav", "general")

        if sound:
            if sound not in sounds:
                sounds.append(sound)
            continue

        message = result.say or fallback
        if message and message not in messages:
            messages.append(message)
    # Not said twice when the model has already said it - "Congratulations on the new
    # job! I'll remember that." was followed by another "I'll remember that."
    said = " ".join(messages).lower()
    return sounds, messages + [note for note in dict.fromkeys(notes) if note.lower() not in said]


# What the model says alongside a tool call when it has nothing else to say.
BARE_ACKNOWLEDGEMENTS = {"okay", "ok", "sure", "done", "got it", "will do", "noted", "alright", "all right"}


def _words(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def bare_acknowledgement(text: str) -> bool:
    """Whether the model's text is only an acknowledgement, e.g. "Okay." - nothing to say on its own."""
    return _words(text) in BARE_ACKNOWLEDGEMENTS


def tool(description: str, needs: Union[str, Tuple[str, ...], None] = None, required: Sequence[str] = (),
         for_model: bool = True, reads: bool = False, private: bool = False,
         **parameters: Dict[str, Any]) -> Callable:
    """
    Declares an ActionExecutor method as a tool the model can call.

    Args:
        description: What the model is told the tool is for, and when to use it.
        needs: The ActionExecutor attribute it can't work without, e.g. "music", or a
            tuple of them if any one will do, e.g. ("ntfy", "home"). The tool isn't
            offered while that isn't set up (or none of them is), and what's said then
            is the first's.
        required: The parameters it must always be given.
        for_model: Whether the model is offered it at all. False for one which only
            a local command or the offline keyword fallback may run - the model has a
            better tool, and execute refuses the model's own call to it.
        reads: Whether it only reads something for the model to answer from, so its
            answer goes back to the model rather than being said - see read_tools.
        private: Whether what it says is the user's alone, never to be in the model's
            history - see private_tools.
        parameters: Each parameter's JSON schema, by name - the method's own
            keyword arguments, which it's called with.

    Returns:
        A decorator which marks the method, leaving it otherwise unchanged.
    """
    def declare(method: Callable) -> Callable:
        method.tool = {"description": description, "needs": needs, "required": list(required),
                       "for_model": for_model, "reads": reads, "private": private, "parameters": parameters}
        return method
    return declare


def _things(items: Any) -> List[str]:
    """The things the model gave, if it gave a list of them, e.g. ["eggs", "milk"] - else none."""
    if not isinstance(items, list):
        return []
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


class ActionExecutor:
    """Runs an Action against the part of the assistant it's for."""

    def __init__(self, music=None, clock=None, volume=None, weather=None, home=None, memory=None, ntfy=None,
                 satellite: Optional[str] = None, batteries=None, lists=None, pc=None, calendar=None):
        """
        Args:
            music: The music player - SpotifyController - or None if there isn't one.
            clock: Clock instance, for timers, reminders and alarms.
            volume: VolumeControl instance, for the assistant's own volumes.
            weather: Weather instance, for the forecast.
            home: HomeAssistant instance, for the lights, or None if it isn't set up.
            memory: Memory instance, for what the assistant knows about the user.
            ntfy: Ntfy instance, for the user's notifications, or None if it isn't
                set up.
            satellite: The satellite the user is talking to, whose timers and alarms
                ring where they were set - or None for none in particular.
            batteries: BatteryWatcher, for asking how full the batteries are, or None
                if Home Assistant isn't set up.
            lists: Lists, for Home Assistant's to-do lists, or None if Home Assistant
                isn't set up.
            pc: PCPower, for turning the PC on and off, or None if Home Assistant isn't
                set up.
            calendar: GoogleCalendar, for the user's calendars, or None if Google isn't
                set up.
        """
        self.music = music
        self.clock = clock
        self.volume = volume
        self.weather = weather
        self.home = home
        self.memory = memory
        self.ntfy = ntfy
        self.satellite = satellite
        self.batteries = batteries
        self.lists = lists
        self.pc = pc
        self.calendar = calendar
        # What was last asked, and what the assistant said - or the last alert, and
        # all of it - for "send that to my phone". Kept up to date by the Conversation.
        self.last_answer: Optional[Tuple[str, str]] = None

    @classmethod
    def tools(cls) -> Dict[str, Callable]:
        """Every tool, by name, in the order they're declared."""
        return {name: member for name, member in vars(cls).items() if hasattr(member, "tool")}

    def tool_schemas(self) -> List[Dict]:
        """
        The tools to offer the model, in the API's format - only those which can
        actually work, so it isn't invited to call one that can only fail, and not
        those which are only for the local commands and the offline fallback.
        """
        return [{"name": name,
                 "description": method.tool["description"] + (self._activities_told() if name == "get_activity" else ""),
                 "input_schema": {"type": "object", "properties": method.tool["parameters"],
                                  "required": method.tool["required"]}}
                for name, method in self.tools().items()
                if method.tool["for_model"] and self._available(method)]

    def _activities_told(self) -> str:
        """
        The user's activities, named as Google has them, to end get_activity's description -
        " The user's activities: Uni and Work." - or nothing while there are none. In order,
        so a list Google gives in another order doesn't change the tools, and the API's cache.
        """
        names = sorted((calendar["name"].strip() for calendar in self.calendar.activity_calendars().values()),
                       key=str.lower)
        if not names:
            return ""
        return f" The user's activities: {names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]}."

    @classmethod
    def private_tools(cls) -> List[str]:
        """
        The tools whose answers are the user's alone - where their phone is - which Claude's
        history never has, and whose local rules are matched even when Nova is listening for
        an answer (see Conversation.handle).
        """
        return [name for name, method in cls.tools().items() if method.tool["private"]]

    def read_tools(self) -> List[str]:
        """
        The tools which only read something for the model to answer from - see
        LLMHandler.process_query, which sends their answers back to it.
        """
        return [name for name, method in self.tools().items() if method.tool["reads"]]

    def _available(self, method: Callable) -> bool:
        needs = method.tool["needs"]
        if needs is None:
            return True
        return any(getattr(self, name) is not None for name in ((needs,) if isinstance(needs, str) else needs))

    def execute(self, action: Optional[Action]) -> ActionResult:
        """
        Runs an action.

        Args:
            action: What to do, or None if nothing was asked for.

        Returns:
            What came of it.
        """
        if action is None:
            return ActionResult(False)

        method = self.tools().get(action.tool)
        if method is None:
            # The model can ask for any tool by name, offered or not, so one it made up
            # is refused here.
            logger.warning(f"Unknown tool: {action.tool}")
            return ActionResult(False, "Sorry, I don't know how to do that yet.")
        if action.from_model and not method.tool["for_model"]:
            # Nor can it run one it wasn't offered: control_home passes free text on to
            # Assist, and only the local commands and the offline fallback may use it.
            logger.warning(f"Refused {action.tool}, which the model isn't offered")
            return ActionResult(False, "Sorry, I can't do that.")
        if not self._available(method):
            needs = method.tool["needs"]
            return ActionResult(False, UNAVAILABLE[needs if isinstance(needs, str) else needs[0]])

        # Only the parameters the tool takes are passed on, so a stray one the model
        # made up can't stop it running.
        accepted = list(inspect.signature(method).parameters)[1:]
        params = {name: value for name, value in action.params.items() if name in accepted}
        reply = method(self, **params)
        return reply if isinstance(reply, ActionResult) else ActionResult.from_reply(reply)

    # Music

    def _music_offline(self) -> Optional[Dict]:
        """
        Says so straight away, rather than after its timeouts, when the music needs
        the internet and there's no network - see network.py.
        """
        if getattr(self.music, "needs_internet", True) and not network.online():
            name = getattr(self.music, "name", "The music")
            return {"success": False, "message": f"{name} needs the internet, and I can't reach it right now."}
        return None

    @tool(
        "Play music. Correct any obvious mis-transcriptions of artist or song names "
        "before calling, since the user's speech may have been misheard. Use this for "
        "requests to play a song, artist, album or genre, and to resume music that was "
        "paused.",
        needs="music",
        query={
            "type": "string",
            "description": (
                "What to play, spelled correctly, e.g. 'Gooba by 6ix9ine' or 'Drake'. "
                "Always include the artist as 'title by artist' when you know it, for "
                "albums as well as songs, e.g. 'The Chronic by Dr. Dre' - the search "
                "is far more accurate with it, and a title alone often finds the "
                "wrong record. Leave this out entirely for a bare 'play', 'resume' or "
                "'unpause', which means carry on with whatever was playing - don't "
                "ask the user what they want in that case."
            ),
        },
        search_type={
            "type": "string",
            "enum": ["track", "artist", "album", "playlist"],
            "description": (
                "What the query names: a specific song, an artist, an album, or "
                "a genre or mood to find a playlist for. Only use 'album' if the "
                "user actually says the word album - otherwise a title is taken "
                "as a song, even when you know it's also an album."
            ),
        },
    )
    def play_music(self, query: Optional[str] = None, search_type: Optional[str] = None):
        return self._music_offline() or self.music.play(query or None, search_type=search_type)

    @tool(
        "Play one of the user's own saved playlists, for requests like 'play my workout "
        "playlist'. The name is matched loosely, so pass it roughly as the user said it. "
        "Use play_music instead for songs, artists, or general genres the user doesn't "
        "have a playlist for.",
        needs="music",
        required=["name"],
        name={"type": "string", "description": "The playlist name as the user said it."},
    )
    def play_playlist(self, name: str = ""):
        return self._music_offline() or self.music.play_playlist(name or "")

    @tool(
        "Pause, skip, go back a track, start the current track again, or report what is "
        "currently playing. This tool never starts music: a bare 'play', 'resume' or "
        "'unpause' is play_music with no query.",
        needs="music",
        required=["action"],
        action={
            "type": "string",
            "enum": ["pause", "skip", "previous", "restart", "current"],
            "description": (
                "'current' only answers a question about what is playing, like "
                "'what's this song' or 'who sings this' - never use it for an "
                "instruction to play something. 'pause' is for 'pause', 'stop' "
                "or 'be quiet'. 'skip' moves on to the next song, for requests like 'skip', "
                "'skip song', 'skip this one', 'next' or 'next song' - always "
                "call this rather than replying, since the user is asking for "
                "something to happen, not for an answer. 'previous' goes back to "
                "the song before this one, for requests like 'go back', 'last "
                "song' or 'previous track'. 'restart' plays the current song "
                "again from the beginning, for requests like 'play that again', "
                "'start it over' or 'rewind'."
            ),
        },
    )
    def control_playback(self, action: Optional[str] = None):
        offline = self._music_offline()
        if offline:
            return offline
        if action == "pause":
            result = self.music.pause()
            # Asked for outright, so the music isn't brought back once the
            # assistant has finished talking - see MusicHold.
            if result.get("success"):
                self.music.cancel_resume()
            return result
        if action == "skip":
            return self.music.skip()
        if action == "previous":
            return self.music.previous()
        if action == "restart":
            return self.music.restart()
        if action == "current":
            result = self.music.current_track()
            if result.get("success"):
                return {"success": True, "message": f"This is {result['track']} by {result['artist']}"}
            return result
        logger.warning(f"Unknown playback action: {action}")
        return {"success": False, "message": "I'm not sure what to do with the music."}

    @tool(
        "Turn repeat off, repeat the current track, or repeat the whole playlist or album.",
        needs="music",
        required=["mode"],
        mode={"type": "string", "enum": ["off", "track", "all"]},
    )
    def repeat(self, mode: str = "off"):
        return self._music_offline() or self.music.repeat(mode or "off")

    @tool(
        "Turn shuffle on or off.",
        needs="music",
        required=["enabled"],
        enabled={"type": "boolean", "description": "True to shuffle, False to play in order."},
    )
    def shuffle(self, enabled: bool = True):
        return self._music_offline() or self.music.shuffle(bool(enabled))

    # Timers, reminders and alarms

    @tool(
        "Set a countdown timer or reminder. For a length of time, give duration_seconds "
        "('ten minutes' is 600). For a clock time or date to count down to, give 'at' "
        "instead and leave the arithmetic to the clock - 'remind me at 4pm' is '16:00', "
        "'set a reminder for the 4th at 9' is the date and time, e.g. '2026-11-04 09:00'. "
        "Include a label only when the user says what it's for - with a label it becomes "
        "a reminder and the label is spoken when it goes off, so word it as the reminder "
        "itself, e.g. 'Take the pizza out'. If the user explicitly said 'reminder' or "
        "'remind me' but didn't say what it's for, still call this with is_reminder true "
        "and no label - the user will be asked to name it. When they answer, call "
        "name_reminder with that name rather than calling this again, so the original "
        "time is kept exact.",
        needs="clock",
        duration_seconds={
            "type": "integer",
            "description": "How long the timer runs for, in seconds. Leave out when giving 'at'.",
        },
        at={
            "type": "string",
            "description": (
                "The local time to go off at, on the 24-hour clock: 'HH:MM' for "
                "the next time it's that time, or 'YYYY-MM-DD HH:MM' for a "
                "particular day. Leave out when giving duration_seconds."
            ),
        },
        label={
            "type": "string",
            "description": "What to say when it goes off. Leave out entirely for a plain timer with no message.",
        },
        is_reminder={
            "type": "boolean",
            "description": (
                "True if the user explicitly said 'reminder' or 'remind me', "
                "even if they gave no label. False for a plain 'timer'. This "
                "only matters when there's no label: an unlabelled reminder "
                "can't actually be created, so the user is asked to name it "
                "instead."
            ),
        },
    )
    def set_timer(self, duration_seconds: Optional[int] = None, at: Optional[str] = None,
                  label: Optional[str] = None, is_reminder: bool = False):
        return self.clock.set_timer(duration_seconds, label or None, bool(is_reminder), at=at, satellite=self.satellite)

    @tool(
        "Give a name to the reminder which was just asked about, right after the "
        "assistant said 'What would you like to name this reminder?'. Use this instead "
        "of set_timer for that reply - it keeps the reminder's original time exact "
        "rather than reworking it from how long ago it was asked.",
        needs="clock",
        required=["label"],
        label={
            "type": "string",
            "description": "What to say when the reminder goes off, worded as the reminder itself.",
        },
    )
    def name_reminder(self, label: Optional[str] = None):
        return self.clock.name_pending_reminder(label)

    @tool("Report which timers are running and how long is left on each.", needs="clock")
    def list_timers(self):
        return self.clock.list_timers()

    @tool(
        "Acknowledge a timer or reminder which has just gone off, for 'okay', 'stop', "
        "'got it', 'thanks' or similar right after one sounds. This stops it announcing "
        "itself again. Not for cancelling a timer which hasn't gone off yet - use "
        "cancel_timer for that.",
        needs="clock",
    )
    def dismiss_timer(self):
        # Only what rings where the user is talking, which is all they can hear.
        return self.clock.dismiss(satellite=self.satellite)

    @tool(
        "Report timers and reminders which went off without being acknowledged, for "
        "'did I miss anything?' or 'what did I miss?'.",
        needs="clock",
    )
    def check_missed(self):
        return self.clock.missed()

    @tool(
        "Cancel a running timer or reminder. Identify it by label, by its length for "
        "timers set without one, or by the clock time it's due to go off ('the 5:40 "
        "one', 'cancel the timer for 4pm'). Use cancel_all only when the user clearly "
        "means every timer. If they just say 'cancel the timer' and it's ambiguous, "
        "call this with neither argument - the user will be asked which one they "
        "meant. If several timers match equally (e.g. two both set for 10 minutes), "
        "the user is asked which one automatically - just pass through what they said.",
        needs="clock",
        label={
            "type": "string",
            "description": (
                "Which timer to cancel: its label, the length it was set for "
                "('10 minutes'), the clock time it's due ('5:40', '5:40 pm'), or "
                "how the user referred to it ('the next one', 'the upcoming "
                "reminder'). If a running reminder's real label is a close match "
                "for a mangled transcription ('walk the frogs' for a 'walk the "
                "dogs' reminder that's actually set), pass the real label, not the "
                "mangled one. Never invent a label from how long is left, and "
                "leave this out entirely when they just mean 'that one' or 'the "
                "timer'."
            ),
        },
        cancel_all={"type": "boolean", "description": "True only if the user asked to cancel every timer."},
    )
    def cancel_timer(self, label: Optional[str] = None, cancel_all: bool = False):
        return self.clock.cancel_timer(label or None, bool(cancel_all))

    @tool(
        "Set an alarm for a time of day, e.g. 'set an alarm for 7am', 'wake me up at "
        "quarter past six', 'set an alarm for weekdays at 6:30'. Never give this a "
        "label - alarms don't take one, that's what a reminder is for. An alarm rings "
        "insistently until dismissed, so only use this when the user actually means a "
        "wake-up-style alarm, not a reminder or timer worded loosely as one.",
        needs="clock",
        required=["hour", "minute"],
        hour={"type": "integer", "description": "0-23, 24-hour clock."},
        minute={"type": "integer", "description": "0-59."},
        repeat={
            "type": "string",
            "enum": ["daily", "weekdays", "weekends", "custom"],
            "description": (
                "Leave out entirely for a one-off alarm. Otherwise: 'daily' for "
                "every day, 'weekdays' for Monday-Friday, 'weekends' for Saturday "
                "and Sunday, or 'custom' for specific days, listed in days."
            ),
        },
        days={
            "type": "array",
            "items": {"type": "string", "enum": DAYS},
            "description": "Only with repeat 'custom': the days it repeats on.",
        },
    )
    def set_alarm(self, hour: Optional[int] = None, minute: Optional[int] = None,
                  repeat: Optional[str] = None, days: Optional[List[str]] = None):
        if repeat == "custom":
            # The days themselves. Left as "custom" if none were given, which the
            # clock can't read, so it asks which days rather than guessing.
            repeat = days or "custom"
        return self.clock.set_alarm(hour, minute, repeat or None, satellite=self.satellite)

    @tool("Report which alarms are set, for 'what alarms do I have' or 'list my alarms'.", needs="clock")
    def list_alarms(self):
        return self.clock.list_alarms()

    @tool(
        "Report how long until the next alarm, for 'how long until my alarm' or "
        "'when's my next alarm'.",
        needs="clock",
    )
    def next_alarm(self):
        return self.clock.next_alarm()

    @tool(
        "Cancel an alarm, identified by its time of day ('cancel my 7am alarm'). If "
        "the user says 'cancel it'/'turn off my alarm' with only one alarm set or it's "
        "otherwise unambiguous, leave label out. If the matched alarm repeats and the "
        "user didn't already say whether they mean just today or the whole schedule "
        "('just for today', 'stop it repeating', 'cancel it completely'), leave scope "
        "out too - the user gets asked which they meant rather than it being guessed.",
        needs="clock",
        label={
            "type": "string",
            "description": "The alarm's time of day ('7am', '6:30'), or left out when unambiguous.",
        },
        cancel_all={"type": "boolean", "description": "True only if the user asked to cancel every alarm."},
        scope={
            "type": "string",
            "enum": ["today", "all"],
            "description": (
                "'today' if the user only wants to skip the next occurrence of a "
                "repeating alarm; 'all' if they want the whole repeat cancelled. "
                "Leave out if they didn't say - only set this from what the user "
                "explicitly said, never guessed."
            ),
        },
    )
    def cancel_alarm(self, label: Optional[str] = None, cancel_all: bool = False, scope: Optional[str] = None):
        return self.clock.cancel_alarm(label or None, bool(cancel_all), scope or None)

    # Volume

    @tool(
        "Change how loud something is. For a bare 'turn it up', 'volume up', 'too loud' "
        "or 'set the volume to 40' that doesn't say what, leave category out - the "
        "assistant works out from what's playing whether that means the music or its own "
        "voice. Give either level (to set a specific value) or direction (to move it up "
        "or down), not both.",
        needs="volume",
        category={
            "type": "string",
            "enum": ["general", "music", "alarm"],
            "description": (
                "Only when the user says which. 'general' for the assistant's own "
                "voice ('talk quieter', 'your voice is too loud'). 'music' when they "
                "mean what's playing ('turn the music down'). 'alarm' for anything "
                "which goes off by itself - timers, reminders and alarms all share "
                "this one, so 'turn the timer volume down' and 'reminders are too "
                "loud' are both 'alarm'. Each is separate, so changing one leaves "
                "the others alone."
            ),
        },
        level={
            "type": "integer",
            "description": (
                "Set the volume to this exact percentage, 0 to 100. Only for "
                "requests naming a number, like 'set the volume to 40'."
            ),
        },
        direction={
            "type": "string",
            "enum": ["up", "down"],
            "description": (
                "Use this for 'volume up', 'louder', 'turn it down' and so on. "
                "The assistant decides how much by, so don't give an amount "
                "unless the user asked for a specific one."
            ),
        },
        amount={
            "type": "integer",
            "description": (
                "Only when the user asks for a specific change, e.g. 'turn it "
                "down by 30'. Leave out otherwise so the standard step is used."
            ),
        },
    )
    def set_volume(self, category: Optional[str] = None, level: Optional[int] = None,
                   direction: Optional[str] = None, amount: Optional[int] = None):
        category = category or self._unnamed_volume_category()

        if category == "music":
            return self._change_music_volume(level, direction, amount)

        if level is not None:
            return self._with_beep(category, self.volume.set(category, level))

        if direction in ("up", "down"):
            amount = self.volume.step if amount is None else abs(float(amount))
            return self._with_beep(category, self.volume.adjust(category, amount if direction == "up" else -amount))

        # Nothing to change, so the current level is reported instead.
        return {"success": True, "message": f"The {category} volume is at {self.volume.percent(category)} percent."}

    @staticmethod
    def _with_beep(category: str, result: Dict) -> Dict:
        """
        Confirms change of volume level with a beep at that volume.

        Args:
            category: Which volume changed, so the beep plays at that level.
            result: What the volume control returned.

        Returns:
            The result, with the beep attached if the change worked.
        """
        if result.get("success"):
            result["sound"] = "volume_beep.wav"
            result["sound_category"] = category
        return result

    def _unnamed_volume_category(self) -> str:
        """
        Which volume a request like "turn it down" means when it doesn't say.

        The music, if it was playing when the user spoke - that's what they'll have
        been listening to - and the assistant's own voice otherwise. Before this the
        model always chose the assistant's voice, so "turn it down" with music on
        changed nothing the user could actually hear.
        """
        if self.music and self.music.was_playing():
            return "music"
        return "general"

    def _change_music_volume(self, level: Optional[int], direction: Optional[str], amount: Optional[int]) -> Dict:
        """
        Changes the music's own volume.

        Kept apart from the assistant's volumes because the music player holds this
        level itself - Spotify's applies to every device playing - rather than it
        being something only this assistant's audio uses.

        Args:
            level: An exact percentage, or None.
            direction: "up" or "down" to step it, or None.
            amount: How far to step it, or None for the standard step.

        Returns:
            Dict with 'success' and a relevant message.
        """
        if not self.music:
            return {"success": False, "message": UNAVAILABLE["music"]}
        offline = self._music_offline()
        if offline:
            return offline

        current = self.music.get_volume()

        if level is not None:
            target = level
        else:
            if current is None:
                return {"success": False, "message": "I can't tell how loud the music is."}
            if direction not in ("up", "down"):
                return {"success": True, "message": f"The music is at {current} percent."}
            amount = self.volume.step if amount is None else abs(float(amount))
            target = current + (amount if direction == "up" else -amount)

        # Confirmed with the same beep as the other volumes. It plays at the
        # assistant's own level rather than the music's, since it comes out of the
        # assistant and not through the music player - the music itself demonstrates
        # the new level well enough.
        return self._with_beep("general", self.music.set_volume(target))

    # Weather

    @tool(
        "Get the weather forecast for home - how it is now, and the rest of today, or "
        "tomorrow. Use it for any question about the weather, including ones like 'do I "
        "need a coat?' or 'will it rain later?'. The forecast it returns is what gets "
        "spoken, so just reply 'Okay.'",
        needs="weather",
        day={
            "type": "string",
            "enum": ["today", "tomorrow"],
            "description": "Which day the user asked about. 'today' includes right now.",
        },
    )
    def get_weather(self, day: Optional[str] = None):
        return self.weather.forecast(day or "today")

    # Home Assistant

    # Not offered to the model, which works the lights itself with set_lights, but the
    # local commands ("lamp off") and the offline keyword fallback still pass their words
    # on to Home Assistant's Assist this way.
    @tool(
        "Control the lights through Home Assistant - on, off, brighter, dimmer, a "
        "percentage or a colour - or ask whether one is on. Pass the request on as a "
        "short, clear command in plain English, naming the room or light the way the "
        "user did, e.g. 'turn off the kitchen lights', 'set the living room lights to "
        "30%', 'turn on the lights'. Home Assistant's own answer, or a chime, is what the "
        "user hears, so just reply 'Okay.'",
        needs="home",
        required=["command"],
        for_model=False,
        command={"type": "string", "description": "The request, in plain English."},
        wider={"type": "boolean", "description": "Whether it's any of Assist's sentences, not a light sentence."},
        on={"type": "boolean", "description": "For a light sentence: on or off."},
        words={"type": "string", "description": "For a light sentence: the lights, as the user said them."},
        brightness={"type": "integer", "description": "For a light sentence: how bright, in percent."},
        question={"type": "boolean", "description": "Whether it only asks how something is."},
    )
    def control_home(self, command: Optional[str] = None, wider: bool = False, on: Optional[bool] = None,
                     words: Optional[str] = None, brightness: Optional[int] = None, question: bool = False):
        if not command:
            return {"success": False, "message": "I didn't catch what to do with the lights."}
        # Any sentence of Assist's, rather than a light sentence, is only sent on the
        # wider rule - see HomeAssistant.command.
        reply = self.home.command(command, wider=wider)
        # A question the guard won't give Assist - "is the garage door open?" - is
        # Claude's, which can read it with home_status and has nothing that opens.
        if reply.get("refused") and question:
            return {"success": False, "message": reply.get("message"), "not_understood": True}
        # A light sentence the guard won't give Assist - "turn on the front door light",
        # with a lock called "front door" - is done by Nova's own lights instead, which
        # only ever work lights. The offline fallback gives no on or off, so what it
        # passes on stays refused.
        if reply.get("refused") and on is not None:
            return self.home.switch_lights(on, words, brightness)
        return reply

    # Only the local "dim the lights" produces this: Nova steps each light's brightness
    # itself (see HomeAssistant.dim), as Assist has no sentence for it. Claude is only
    # asked when Nova doesn't know the lights by that name.
    @tool(
        "Make lights dimmer or brighter by a step.",
        needs="home",
        for_model=False,
        required=["direction", "words"],
        direction={"type": "string", "description": "down or up."},
        words={"type": "string", "description": "The lights, as the user said them."},
    )
    def dim_lights(self, direction: Optional[str] = None, words: Optional[str] = None):
        if direction not in ("down", "up") or not words:
            return {"success": False, "message": "I didn't catch what to dim."}
        return self.home.dim(direction, words)

    @tool(
        "Work the lights through Home Assistant: switch them on or off, set how bright "
        "they are, or change their colour or warmth - one light, or all the lights in a "
        "room. It works lights and nothing else. Name a light by its entity id, from the "
        "list of lights you were given, or a room by its name; with neither, as for 'turn "
        "on the lights' or 'dim the lights', it's the room Nova is in. If Nova's room "
        "isn't known, don't guess a room: ask which one. Work out 'brighter', 'a bit "
        "dimmer' and the like from how bright the lights are now, in that list. To "
        "change a colour or warmth, or brightness, pass on=true. The lights changing, "
        "or a chime, is what the user hears, so just reply 'Okay.'",
        needs="home",
        required=["on"],
        on={
            "type": "boolean",
            "description": "true to switch them on, or to change how they look; false to switch them off.",
        },
        light={"type": "string", "description": "One light's entity id, e.g. 'light.lamp'. Leave out for a room."},
        area={
            "type": "string",
            "description": ("A room's name, e.g. 'Bedroom', for all its lights. Leave out for the room "
                            "Nova is in, or when a light is given."),
        },
        brightness={"type": "integer", "description": "How bright, 1 to 100 percent. Leave out unless asked."},
        colour={
            "type": "string",
            "description": ("A colour, by its name in one or two words, e.g. 'blue' or 'light green'. "
                            "Leave out unless asked."),
        },
        warmth={
            "type": "integer",
            "description": ("A white's colour temperature in kelvin: 2700 for warm white, 4000 for neutral, "
                            "6500 for cool daylight. Lower is warmer. Leave out unless asked."),
        },
    )
    def set_lights(self, on: Optional[bool] = None, light: Optional[str] = None, area: Optional[str] = None,
                   brightness: Optional[int] = None, colour: Optional[str] = None, warmth: Optional[int] = None):
        if on is None:
            return {"success": False, "message": "I didn't catch whether to switch the lights on or off."}
        return self.home.set_lights(on, light=light, area=area, brightness=brightness, colour=colour, warmth=warmth)

    # What each script or scene does is checked before it's run - see HomeAssistant.run_script.
    @tool(
        "Run one of the user's Home Assistant scripts or scenes by name, for wordings like 'do my goodnight "
        "thing'. Only those exposed to Assist run. What happened is said for you, so just reply 'Okay.'",
        needs="home",
        required=["name"],
        name={"type": "string", "description": "Its name, as the user said it, e.g. 'goodnight'."},
    )
    def run_home_script(self, name: Optional[str] = None):
        if not isinstance(name, str) or not name.strip():
            return {"success": False, "message": "I didn't catch which script or scene to run."}
        return self.home.run_script(name)

    # Only ever read, by Claude, to answer from - see HomeAssistant.status.
    @tool(
        "Read the state of things in Home Assistant - the house, the devices and the homelab - for "
        "'when's sunset?', 'is Navidrome up?', 'any updates?', 'is the PC on?' or 'how warm is the "
        "bedroom?'. Give the words asked about. For batteries, use get_batteries. You get the answer "
        "to reply from.",
        needs="home",
        reads=True,
        required=["about"],
        about={
            "type": "string",
            "description": "The words asked about, e.g. 'sunset', 'navidrome', 'update' or 'bedroom temperature'.",
        },
    )
    def home_status(self, about: Optional[str] = None):
        if not isinstance(about, str) or not about.strip():
            return {"success": False, "message": "I didn't catch what to look up."}
        return self.home.status(about)

    @tool(
        "Read the user's calendar for a day or a few days, for 'what have I got on "
        "Thursday?', 'what's on tomorrow?', 'am I busy this weekend?' or 'what's planned "
        "for next week?'. Give the first day, worked out from today's date, and how many "
        "days to read - 1 for a single day, 2 for a weekend, 7 for a week. Or look for one "
        "thing on it, for 'when's my MOT?', 'when am I next seeing Sam?' or 'is the "
        "dentist soon?' - give what to search for, and today as the first day, rather "
        "than reading whole days. What's on is read out to the user, so just reply 'Okay.'",
        needs="calendar",
        required=["start"],
        start={"type": "string", "description": "The first day, as YYYY-MM-DD."},
        days={"type": "integer", "description": "How many days from it, 1 to 14. Leave out for one day."},
        search={
            "type": "string",
            "description": ("Only when looking for one thing: a word from its name as it'd be written "
                            "on the calendar, e.g. 'MOT', 'dentist' or 'Sam' - putting right anything "
                            "clearly misheard. The next three months are looked through."),
        },
    )
    def get_calendar(self, start: Optional[str] = None, days: Optional[int] = None, search: Optional[str] = None):
        if search and search.strip():
            return self._calendar_days(start, None, lambda first, days: self.calendar.find(search, first))
        return self._calendar_days(start, days, self.calendar.agenda)

    @tool(
        "When the user has one of their activities - each of their calendars but their own "
        "is one, by its name - for 'what time do I have work tomorrow?', 'am I working on "
        "Saturday?', 'when do I finish uni today?', 'what shifts have I got this week?' - "
        "or when they next have it, for 'when's my next shift?' or 'what are my next three "
        "lectures?'. Give the activity, the first day, worked out from today's date (today, "
        "for 'next'), and how many days, or for 'next', how many times. The times are read "
        "out to the user, so just reply 'Okay.'",
        needs="calendar",
        required=["activity", "start"],
        activity={
            "type": "string",
            "description": ("Which of the user's activities - named at the end of this tool's description - "
                            "as they said it."),
        },
        start={"type": "string", "description": "The first day, as YYYY-MM-DD."},
        days={"type": "integer", "description": "How many days from it, 1 to 14. Leave out for one day, or for 'next'."},
        count={
            "type": "integer",
            "description": ("Only for 'next': how many times to say - 1 for 'when do I next have work?' or "
                            "'when's my next shift?', 3 for 'my next three shifts'. Leave out otherwise."),
        },
    )
    def get_activity(self, activity: Optional[str] = None, start: Optional[str] = None, days: Optional[int] = None,
                     count: Optional[int] = None):
        if count:
            # Looked for across the next fortnight, and only the first few said.
            return self._calendar_days(start, MOST_CALENDAR_DAYS, lambda first, days: self.calendar.activity(
                activity or "", first, days, count=max(1, min(int(count), MOST_NEXT))))
        return self._calendar_days(start, days,
                                   lambda first, days: self.calendar.activity(activity or "", first, days))

    @staticmethod
    def _calendar_days(start: Optional[str], days: Optional[int], read) -> Dict:
        """Reads the calendar for the days the model gave, if it gave a day which can be read."""
        try:
            first = date.fromisoformat(start)
        except (TypeError, ValueError):
            return {"success": False, "message": "I didn't catch which day you meant."}
        return read(first, max(1, min(int(days or 1), MOST_CALENDAR_DAYS)))

    # Lists - see lists.py

    def _assist_first(self, command: Optional[str], intent: str) -> Optional[Dict]:
        """
        Asks Assist to do a list sentence, but only with the one intent live check 1 found
        for it - so "take milk off my shopping list", which Assist reads as removing it, is
        never sent.

        Args:
            command: The request as the local rule had it, or None from the offline fallback.
            intent: The intent Assist may act on it with, e.g. "HassListAddItem".

        Returns:
            What Assist said, or None for Nova to do it itself - when Assist wouldn't act
            on it with that intent, on a list, or Home Assistant couldn't be reached before
            anything was sent. Or when the guard refused it, as for "add a door lock to my
            shopping list": Nova's own lists only ever call todo services.
        """
        if not command:
            return None
        reply = self.home.command(command, wider=True, only=(intent,))
        if reply.get("not_understood") or reply.get("refused"):
            return None
        # Said, not chimed: a list changing isn't seen or heard, as a light's is.
        return {key: value for key, value in reply.items() if key != "chime"}

    @staticmethod
    def _unreached(reply: Dict, said: Optional[str]) -> Dict:
        """
        A list's failure to reach Home Assistant, as Claude's when the words don't say it's
        a list - "I've got a headache" is ticking something off to the local rule, and "put
        Drake on Spotify" adding to a list, but a sentence about the world mustn't end at "I
        can't reach Home Assistant".

        Args:
            reply: What Lists said.
            said: The list as it was said, or None for none.

        Returns:
            The reply, with 'not_understood' when Home Assistant couldn't be reached and
            the words have no "list" in them.
        """
        if not reply.get("success") and reply.get("message") == CANT_REACH and not names_list(said):
            return {**reply, "not_understood": True}
        return reply

    @tool(
        "Add a thing to a list.",
        needs="lists",
        required=["item"],
        for_model=False,
        item={"type": "string", "description": "The thing, as the user said it."},
        list={"type": "string", "description": "The list, as the user said it, or left out for the main one."},
        command={"type": "string", "description": "The request, for Assist to be asked first."},
    )
    def add_to_list(self, item: Optional[str] = None, list: Optional[str] = None, command: Optional[str] = None):
        if not item:
            return {"success": False, "message": "I didn't catch what to add."}
        assisted = self._assist_first(command, "HassListAddItem")
        if assisted:
            return assisted
        reply = self._unreached(self.lists.add([item], list), list)
        # Only the offline fallback gives no command. Its "add a reminder to call mum"
        # isn't a list, so the model's own reply - that Claude can't be reached - is said
        # rather than "I haven't got a list called that.".
        if command is None and reply.get("not_understood"):
            return {**reply, "message": ""}
        return reply

    @tool(
        "Read a list.",
        needs="lists",
        for_model=False,
        list={"type": "string", "description": "The list, as the user said it, or left out for the main one."},
    )
    def read_list(self, list: Optional[str] = None):
        # Always Nova's own, which says what's on it in a way that can be listened to.
        return self._unreached(self.lists.read(list), list)

    @tool(
        "Tick a thing off a list.",
        needs="lists",
        required=["item"],
        for_model=False,
        item={"type": "string", "description": "The thing, as the user said it."},
        list={"type": "string", "description": "The list, as the user said it, or left out to look through all."},
        command={"type": "string", "description": "The request, for Assist to be asked first."},
        exact={"type": "boolean", "description": "Whether only a thing with the same words will do."},
    )
    def tick_off_list(self, item: Optional[str] = None, list: Optional[str] = None, command: Optional[str] = None,
                      exact: bool = False):
        if not item:
            return {"success": False, "message": "I didn't catch what to tick off."}
        # Anything else Assist would read it as - HassTurnOn, for "tick off milk on my
        # shopping list" - isn't sent, so Nova ticks it off. What Assist itself said is
        # never Claude's, as once sent it may have ticked it off.
        assisted = self._assist_first(command, "HassListCompleteItem")
        if assisted:
            return assisted
        return self._unreached(self.lists.tick_off(item, list, exact=bool(exact)), list)

    @tool(
        "Add things to, read, or tick things off one of the user's to-do lists in Home Assistant. Split several "
        "things into separate items, but keep one thing whole ('salt and vinegar crisps'). Leave list out for their "
        "main list. What happened is said for you, so just reply 'Okay.'",
        needs="lists",
        required=["action"],
        action={"type": "string", "enum": ["add", "read", "tick_off"]},
        items={
            "type": "array",
            "items": {"type": "string"},
            "description": "The things to add or tick off, each as the user said it, e.g. ['eggs', 'milk', 'bread'].",
        },
        list={"type": "string", "description": "The list, as the user said it, e.g. 'shopping' or 'notes'."},
    )
    def manage_list(self, action: Optional[str] = None, items: Optional[List[str]] = None,
                    list: Optional[str] = None):
        if action == "read":
            return self.lists.read(list or None)
        things = _things(items)
        if action not in ("add", "tick_off") or not things:
            return {"success": False, "message": "I didn't catch what to do with the list."}
        if action == "add":
            return self.lists.add(things, list or None)
        # One at a time, as each is found on the list by its own words.
        replies = [self.lists.tick_off(thing, list or None) for thing in things]
        return {"success": all(reply["success"] for reply in replies),
                "message": " ".join(reply["message"] for reply in replies)}

    # Memory

    @tool(
        "Remember something about the user for the future: the people in their life, "
        "their work, home, routines, plans, likes and dislikes - anything a good personal "
        "assistant would want to know later. Call it whenever they mention something like "
        "that, even in passing and without being asked ('I've started a new job at Tesco', "
        "'my sister's called Amy'), as well as when they say 'remember that...' - "
        "alongside answering them, not instead. Not for what you already know, one-off "
        "requests, or passwords, codes or bank details, ever. 'I'll remember that' is "
        "said for you, after your reply.",
        needs="memory",
        required=["fact"],
        fact={
            "type": "string",
            "description": "One short fact, said to the user, e.g. 'You work at Tesco.' or 'Your sister is called Amy.'",
        },
    )
    def remember(self, fact: Optional[str] = None):
        return self.memory.remember(fact)

    @tool(
        "Forget something remembered about the user, when they ask you to or say it's "
        "wrong - 'forget that I work at Tesco', 'I don't live there any more'. Pass it "
        "roughly as they said it. To correct something, forget the old fact and "
        "remember the new one.",
        needs="memory",
        required=["fact"],
        fact={"type": "string", "description": "What to forget, roughly as the user put it."},
    )
    def forget(self, fact: Optional[str] = None):
        return self.memory.forget(fact)

    @tool(
        "Forget everything remembered about the user - only when they clearly ask for "
        "that, e.g. 'forget everything you know about me'. Never for forgetting one thing.",
        needs="memory",
    )
    def forget_everything(self):
        return self.memory.forget_everything()

    @tool(
        "Say what you remember about the user, for 'what do you know about me?' or "
        "'what have I told you?'. It's read out for you, so just reply 'Okay.'",
        needs="memory",
    )
    def list_memories(self):
        return self.memory.describe()

    # Batteries

    @tool(
        "Say how full the batteries of the user's devices are - their phone, tablet, Mac and "
        "the like - for 'how's my Mac's battery?', 'what's my phone on?' or 'battery levels'. "
        "Give the device as the user said it, or leave it out for all of them. It's read out "
        "for you, so just reply 'Okay.'",
        needs="batteries",
        device={
            "type": "string",
            "description": ("Which device, as the user said it, e.g. 'mac', 'phone', 'tablet' or "
                            "'s25 ultra'. Leave out for every battery."),
        },
    )
    def get_batteries(self, device: Optional[str] = None):
        try:
            batteries = self.batteries.read()
        except HOME_FAILURES as e:
            logger.warning(f"Couldn't read the batteries: {e}")
            return {"success": False, "message": CANT_REACH}
        found = find(batteries, device or "")
        if not found:
            return {"success": False, "message": "I can't find a battery called that."}
        now = time.time()
        return {"success": True, "message": " ".join(said(battery, now, self.batteries.stale_after)
                                                     for battery in found)}

    # Where the phone is, and ringing it - see devices.py

    # Not HomeAssistant.find_device, which finds Nova's own device for its room.
    @tool(
        "Say where one of the user's devices is - their phone, tablet or Mac - and ring it if asked ('find my "
        "phone', 'make my tablet ring'). What's found is said for you, so just reply 'Okay.'",
        needs="home",
        required=["device"],
        private=True,
        device={
            "type": "string",
            "description": "Which device, as the user said it, e.g. 'phone', 'tablet', 'mac' or 's25 ultra'.",
        },
        ring={"type": "boolean", "description": "True to ring it as well, wherever it is."},
    )
    def find_device(self, device: Optional[str] = None, ring: bool = False):
        # Every answer is private: even "I haven't got a device called that" is about the user's devices.
        try:
            devices = [AppDevice.from_home(found) for found in self.home.app_devices()]
        except HOME_FAILURES as e:
            logger.warning(f"Couldn't read the Home Assistant app's devices: {e}")
            return {"success": False, "message": CANT_REACH, "private": True}
        found = find(devices, device or "phone")
        if not found:
            return {"success": False, "message": "I haven't got a device called that.", "private": True}
        now = time.time()
        parts, ok = [], True
        for app_device in found:
            parts.append(where(app_device, now))
            # Never the place itself, which is the user's alone.
            logger.info(f"Said where the {app_device.name} is")
            if ring:
                rang, said_of_ringing = self._ring(app_device)
                ok = ok and rang
                parts.append(said_of_ringing)
        return {"success": ok, "message": " ".join(parts), "private": True}

    def _ring(self, device: AppDevice) -> Tuple[bool, str]:
        """
        Rings a device through its notify service.

        Args:
            device: The device.

        Returns:
            Whether Home Assistant took it, and what to say: "Ringing your phone.", or why not.
        """
        word = said_as(device)
        call = ring_call(device)
        outcome, error = self._notify(device, "ring", call["message"], data=call["data"])
        if outcome == "taken":
            return True, f"Ringing your {word}."
        if outcome == "no service":
            return False, f"Your {word} can't be rung - it hasn't got the Home Assistant app's notifications."
        if outcome == "unanswered":
            # Sent, but unanswered - so it may well be ringing.
            return False, f"{NO_ANSWER}, so I can't tell whether your {word} rang."
        if outcome == "token":
            return False, HomeAssistant._failure_message(error)
        return False, f"I couldn't ring your {word}."

    def _notify(self, device: AppDevice, doing: str, message: str, title: Optional[str] = None,
                data: Optional[Dict] = None) -> Tuple[str, Optional[Exception]]:
        """
        Sends a device a notification through its notify service - one place for ringing
        and sending, so what each says of a failure can't drift apart.

        Args:
            device: The device.
            doing: What for, for the log only, e.g. "ring".
            message: The message.
            title: Its title, or None.
            data: What else the app is given, or None.

        Returns:
            "taken" once Home Assistant took it, "no service" if the device hasn't got a notify
            service, "unanswered" if Home Assistant didn't answer in time, "token" if it
            refused Nova's token, else "failed" - with the error, if there was one.
        """
        if device.notify is None:
            return "no service", None
        try:
            self.home.notify(device.notify, message, title=title, data=data)
        except requests.Timeout as e:
            logger.warning(f"Home Assistant didn't answer in time to {doing} the {device.name}: {e}")
            return "unanswered", e
        except (requests.RequestException, NotAllowed) as e:
            logger.warning(f"Couldn't {doing} the {device.name}: {e}")
            if getattr(getattr(e, "response", None), "status_code", None) == 401:
                return "token", e
            return "failed", e
        # Never the words, which may be the user's alone.
        logger.info(f"Notified the {device.name} to {doing}")
        return "taken", None

    # The PC - see pc.py

    @tool(
        "Turn the user's PC on, shut it down, restart it, put it or its screen to sleep, lock it, or say whether "
        "it's on. Shutting down and restarting ask the user first. What happens is said for you, so just reply "
        "'Okay.'",
        needs="pc",
        required=["action"],
        action={"type": "string", "enum": ["on", "off", "restart", "sleep", "screen_off", "lock", "status"]},
        name={"type": "string", "description": "Which PC, as the user said it - only when they've been asked which."},
    )
    def pc_power(self, action: Optional[str] = None, name: Optional[str] = None):
        return self.pc.act(action or "", name or None)

    # Only ever run by the conversation, on the user's yes to pc_power's question - so Claude,
    # which isn't offered it, can't shut the PC down without one.
    @tool(
        "Shut down or restart the PC, once the user has said yes.",
        needs="pc",
        required=["action"],
        for_model=False,
        action={"type": "string", "enum": ["off", "restart"]},
        name={"type": "string", "description": "Which PC, as the user said it, or left out."},
    )
    def press_pc_button(self, action: Optional[str] = None, name: Optional[str] = None):
        return self.pc.press(action or "", name or None)

    # Notifications

    @tool(
        "Send something to the user's phone, as a notification - for 'send that to my phone', "
        "'text me the recipe', 'put the shopping list on my phone'. Write out in full what "
        "they'd want to read there: it isn't said out loud, so it can be longer and more "
        "detailed than a spoken reply. Leave text out to send exactly what you last said. "
        "Give device only when the user names one other than their phone, e.g. 'mac' or 'tablet'.",
        needs=("ntfy", "home"),
        text={"type": "string", "description": "What to send, written to be read."},
        title={"type": "string", "description": "A few words saying what it is, e.g. 'Pancake recipe'."},
        device={"type": "string", "description": "Which device, if not their phone, e.g. 'mac' or 'tablet'."},
    )
    def send_to_phone(self, text: Optional[str] = None, title: Optional[str] = None,
                      device: Optional[str] = None):
        # Claude's "my mac" or "the phone" is the device itself, so it's "your mac", and the
        # phone is still the phone.
        asked = re.sub(r"^(?:my|the) ", "", (device or "").strip().lower())
        # The phone is ntfy whatever Home Assistant is doing, so it's never looked up.
        phone_only = asked in ("", "phone", "mobile")
        found = []
        if not phone_only:
            if self.home is None:
                return {"success": False, "message": f"Sending to your {asked} needs Home Assistant."}
            try:
                found = find([AppDevice.from_home(app) for app in self.home.app_devices()], asked)
            except HOME_FAILURES as e:
                logger.warning(f"Couldn't read the Home Assistant app's devices: {e}")
                return {"success": False, "message": CANT_REACH}
            if not found:
                return {"success": False, "message": "I haven't got a device called that."}
        # Any phones among them go by ntfy, as the phone always has; the rest by notify.
        by_ntfy = phone_only or any(app_device.phone for app_device in found)
        others = [app_device for app_device in found if not app_device.phone]
        if by_ntfy and self.ntfy is None:
            return {"success": False, "message": UNAVAILABLE["ntfy"]}
        if by_ntfy and not self.ntfy.phone:
            return {"success": False, "message": "There's nowhere set up to send things to your phone."}
        if not text:
            if self.last_answer is None:
                return {"success": False, "message": "There's nothing to send yet."}
            asked_for, text = self.last_answer
            title = title or asked_for
        title = (title or "From Nova")[:100]

        sent, failed = [], []
        if by_ntfy:
            if self.ntfy.send(self.ntfy.phone, text, title=title):
                sent.append("phone")
            else:
                failed.append("I couldn't reach your notifications.")
        for app_device in others:
            failure = self._send_to(app_device, text, title)
            if failure:
                failed.append(failure)
            else:
                sent.append(app_device.name)
        if not failed:
            return {"success": True, "message": "Sent.", "chime": True}
        # Which got it matters when only some did, so it isn't sent again to those.
        got = f"Sent to your {' and your '.join(sent)}. " if sent else ""
        return {"success": False, "message": got + " ".join(failed)}

    def _send_to(self, device: AppDevice, text: str, title: str) -> Optional[str]:
        """
        Sends a notification to a device through its notify service.

        Args:
            device: The device.
            text: What to send.
            title: A few words saying what it is.

        Returns:
            None once Home Assistant took it, else what to say of why not.
        """
        outcome, error = self._notify(device, "send to", text, title=title)
        if outcome == "taken":
            return None
        if outcome == "no service":
            return f"Your {device.name} hasn't got the Home Assistant app's notifications."
        if outcome == "unanswered":
            return f"{NO_ANSWER}, so I can't tell whether that was sent to your {device.name}."
        if outcome == "token":
            return HomeAssistant._failure_message(error)
        return f"I couldn't send that to your {device.name}."

    @tool(
        "Read out the notifications which came in without being said - at night, or while "
        "they couldn't be fetched - for 'any notifications?' or 'what are my notifications?'. "
        "They're read out for you, so just reply 'Okay.'",
        needs="ntfy",
    )
    def read_notifications(self):
        return self.ntfy.unheard()

    @tool(
        "Read out the whole of the last notification, for 'tell me more' or 'what was that "
        "about?' just after one was said. It's read out for you, so just reply 'Okay.'",
        needs="ntfy",
    )
    def tell_me_more(self):
        return self.ntfy.tell_more()
