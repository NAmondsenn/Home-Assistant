"""
Actions Module
Everything the assistant can do for the user, each declared once.

Each tool is a method of ActionExecutor, with what the model is told about it - its
description and parameters - written in a @tool line just above the code which
carries it out. The model is offered exactly these (see tool_schemas), and whatever
it calls, or a local command or the offline keyword fallback asks for, comes back
here as an Action naming one of them.

Adding a tool used to mean editing three places in two files - the description in
llm.py, a mapping from the model's call to an action dict, and the dispatch here -
which could drift apart. Now there's one place, and a test checks every tool has
both a description and the code to run it.
"""

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from voice_assistant import network

# Logger setup
logger = logging.getLogger(__name__)

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# What's said when a tool is asked for but the part of the assistant it needs
# isn't set up, keyed by the ActionExecutor attribute it needs.
UNAVAILABLE = {
    "music": "The music isn't available right now.",
    "clock": "Timers aren't available right now.",
    "volume": "Volume control isn't available right now.",
    "weather": "The weather isn't available right now.",
}


@dataclass
class Action:
    """
    One thing to do, e.g. Action("set_timer", {"duration_seconds": 600}).

    Attributes:
        tool: The name of an ActionExecutor tool.
        params: What it was given, by parameter name.
    """
    tool: str
    params: Dict[str, Any] = field(default_factory=dict)


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
            song starting; "sound" plays its own sound.
        sound: The sound, for "sound" - e.g. "volume_beep.wav".
        sound_category: Which volume the sound plays at.
    """
    ok: bool
    say: str = ""
    confirm: str = "speak"
    sound: Optional[str] = None
    sound_category: str = "general"

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
        if ok and reply.get("silent"):
            return cls(ok, say, confirm="music")
        if ok and reply.get("sound"):
            return cls(ok, say, confirm="sound", sound=reply["sound"],
                       sound_category=reply.get("sound_category", "general"))
        if ok and reply.get("chime"):
            return cls(ok, say, confirm="chime")
        return cls(ok, say)


def confirm(results: List[ActionResult], chime_enabled: bool = True,
            fallback: Optional[str] = None) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Works out how the actions a request ran should be confirmed to the user.

    A self-confirming action's message is only ever logged, never spoken, even when
    there's something else to say as well: a reminder offer tacked onto a skipped
    song used to come out as "That song was shit anyway. Also, your reminder is
    still going."

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
    for result in results:
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
    return sounds, messages


def tool(description: str, needs: Optional[str] = None, required: Sequence[str] = (),
         **parameters: Dict[str, Any]) -> Callable:
    """
    Declares an ActionExecutor method as a tool the model can call.

    Args:
        description: What the model is told the tool is for, and when to use it.
        needs: The ActionExecutor attribute it can't work without, e.g. "music". The
            tool isn't offered while that isn't set up.
        required: The parameters it must always be given.
        parameters: Each parameter's JSON schema, by name - the method's own
            keyword arguments, which it's called with.

    Returns:
        A decorator which marks the method, leaving it otherwise unchanged.
    """
    def declare(method: Callable) -> Callable:
        method.tool = {"description": description, "needs": needs,
                       "required": list(required), "parameters": parameters}
        return method
    return declare


class ActionExecutor:
    """Runs an Action against the part of the assistant it's for."""

    def __init__(self, music=None, clock=None, volume=None, weather=None):
        """
        Args:
            music: The music player - SpotifyController - or None if there isn't one.
            clock: Clock instance, for timers, reminders and alarms.
            volume: VolumeControl instance, for the assistant's own volumes.
            weather: Weather instance, for the forecast.
        """
        self.music = music
        self.clock = clock
        self.volume = volume
        self.weather = weather

    @classmethod
    def tools(cls) -> Dict[str, Callable]:
        """Every tool, by name, in the order they're declared."""
        return {name: member for name, member in vars(cls).items() if hasattr(member, "tool")}

    def tool_schemas(self) -> List[Dict]:
        """
        The tools to offer the model, in the API's format - only those which can
        actually work, so it isn't invited to call one that can only fail.
        """
        return [{"name": name,
                 "description": method.tool["description"],
                 "input_schema": {"type": "object", "properties": method.tool["parameters"],
                                  "required": method.tool["required"]}}
                for name, method in self.tools().items() if self._available(method)]

    def _available(self, method: Callable) -> bool:
        needs = method.tool["needs"]
        return needs is None or getattr(self, needs) is not None

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
            # The model can only call what it's offered, so this would be a bug.
            logger.warning(f"Unknown tool: {action.tool}")
            return ActionResult(False, "Sorry, I don't know how to do that yet.")
        if not self._available(method):
            return ActionResult(False, UNAVAILABLE[method.tool["needs"]])

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
        return self.clock.set_timer(duration_seconds, label or None, bool(is_reminder), at=at)

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
        return self.clock.dismiss()

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
        return self.clock.set_alarm(hour, minute, repeat or None)

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
