import os
import re
import time
import logging
from datetime import datetime
from typing import Optional, Dict
from dotenv import load_dotenv
import anthropic
from anthropic import Anthropic
from voice_assistant.clock import spoken_clock_time
from voice_assistant.logging_setup import TRANSCRIPT
from voice_assistant import network

# Said when there's no way onto the internet - straight away, rather than after
# waiting out the timeouts (see network.py), and pointing at what still works.
OFFLINE_MESSAGE = "Sorry, I can't reach the internet right now. Timers, alarms and the time still work."

# loads environment variables from a .env file if present.
load_dotenv()
logger = logging.getLogger(__name__)


class LLMHandler:
    def __init__(self, config=None, api_key=None, model=None, max_tokens=None,
                 temperature=None, history_length=None, assistant_name=None):

        # Pulls llm, conversation, assistant and web_search sections from config.yaml
        llm_config = config.section("llm") if config else {}
        conversation_config = config.section("conversation") if config else {}
        assistant_config = config.section("assistant") if config else {}
        web_search_config = config.section("web_search") if config else {}

        # How long one request may take before it's given up on, in seconds, and how
        # many times it's retried. The SDK's own default waits up to 10 minutes and
        # retries twice, so a dropped connection could leave the assistant stuck - music
        # paused, not listening - for far longer than anyone would wait for an answer.
        # A web search does more work per request, so it's allowed longer.
        self.timeout = float(llm_config.get("timeout", 15))
        self.search_timeout = float(llm_config.get("search_timeout", 45))
        self.max_retries = int(llm_config.get("max_retries", 1))

        # If no API key is provided, ANTHROPIC_API_KEY is taken from .env, falling back to Claude_API_Key.
        # Missing API keys are logged as warnings, the program runs, but the LLM will not be available.
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("Claude_API_Key")
        if self.api_key:
            self.client = Anthropic(api_key=self.api_key,
                                    timeout=anthropic.Timeout(self.timeout, connect=5.0),
                                    max_retries=self.max_retries)
        else:
            self.client = None
            logger.warning("No API key found (ANTHROPIC_API_KEY), LLM queries will fail until one is set")

        self.model_name = model or llm_config.get("model", "claude-haiku-4-5-20251001")
        self.max_tokens = max_tokens if max_tokens is not None else llm_config.get("max_tokens", 150)
        self.temperature = temperature if temperature is not None else llm_config.get("temperature", 0.7)
        self.history_length = history_length if history_length is not None else conversation_config.get("history_length", 5)
        self.history = []
        # Seconds after which an exchange is forgotten once a new conversation starts - see forget_stale_history.
        self.history_max_age = conversation_config.get("history_max_age", 300)
        self.assistant_name = assistant_name or assistant_config.get("name", "Assistant")

        # Web search is opt-in per query rather than always on, so ordinary replies
        # aren't slowed down or billed by unnecessary searches.
        self.web_search_enabled = web_search_config.get("enabled", True)
        self.web_search_max_uses = web_search_config.get("max_uses", 3)

        logger.info(
            f"LLM initialised: {self.model_name} (max_tokens={self.max_tokens}, temperature={self.temperature}, "
            f"history_length={self.history_length}, web_search={self.web_search_enabled})")

    # Tools the model can call to control music and lights.
    # This selects actions more accurately than keyword matching and corrects mis-transcriptions.
    ACTION_TOOLS = [
        {
            "name": "play_music",
            "description": (
                "Play music on Spotify. Correct any obvious mis-transcriptions of artist "
                "or song names before calling, since the user's speech may have been "
                "misheard. Use this for requests to play a song, artist, album or genre, "
                "and to resume music that was paused."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
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
                    "search_type": {
                        "type": "string",
                        "enum": ["track", "artist", "album", "playlist"],
                        "description": (
                            "What the query names: a specific song, an artist, an album, or "
                            "a genre or mood to find a playlist for. Only use 'album' if the "
                            "user actually says the word album - otherwise a title is taken "
                            "as a song, even when you know it's also an album."
                        ),
                    },
                },
            },
        },
        {
            "name": "play_playlist",
            "description": (
                "Play one of the user's own saved Spotify playlists, for requests like "
                "'play my workout playlist'. The name is matched loosely, so pass it "
                "roughly as the user said it. Use play_music instead for songs, artists, "
                "or general genres the user doesn't have a playlist for."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The playlist name as the user said it."},
                },
                "required": ["name"],
            },
        },
        {
            "name": "control_playback",
            "description": (
                "Pause, skip, go back a track, start the current track again, or report "
                "what is currently playing. This tool never starts music: a bare 'play', "
                "'resume' or 'unpause' is play_music with no query."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
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
                },
                "required": ["action"],
            },
        },
        {
            "name": "repeat",
            "description": "Turn repeat off, repeat the current track, or repeat the whole playlist or album.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["off", "track", "all"]},
                },
                "required": ["mode"],
            },
        },
        {
            "name": "shuffle",
            "description": "Turn shuffle on or off.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean", "description": "True to shuffle, False to play in order."},
                },
                "required": ["enabled"],
            },
        },
        {
            "name": "set_timer",
            "description": (
                "Set a countdown timer or reminder. For a length of time, give "
                "duration_seconds ('ten minutes' is 600). For a clock time or date to count "
                "down to, give 'at' instead and leave the arithmetic to the clock - 'remind "
                "me at 4pm' is '16:00', 'set a reminder for the 4th at 9' is the date and "
                "time, e.g. '2026-11-04 09:00'. Include a label only when the user says what it's "
                "for - with a label it becomes a reminder and the label is spoken when it "
                "goes off, so word it as the reminder itself, e.g. 'Take the pizza out'. "
                "If the user explicitly said 'reminder' or 'remind me' but didn't say what "
                "it's for, still call this with is_reminder true and no label - the user "
                "will be asked to name it. When they answer, call name_reminder with that "
                "name rather than calling this again, so the original time is kept exact."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "duration_seconds": {
                        "type": "integer",
                        "description": "How long the timer runs for, in seconds. Leave out when giving 'at'.",
                    },
                    "at": {
                        "type": "string",
                        "description": (
                            "The local time to go off at, on the 24-hour clock: 'HH:MM' for "
                            "the next time it's that time, or 'YYYY-MM-DD HH:MM' for a "
                            "particular day. Leave out when giving duration_seconds."
                        ),
                    },
                    "label": {
                        "type": "string",
                        "description": (
                            "What to say when it goes off. Leave out entirely for a plain "
                            "timer with no message."
                        ),
                    },
                    "is_reminder": {
                        "type": "boolean",
                        "description": (
                            "True if the user explicitly said 'reminder' or 'remind me', "
                            "even if they gave no label. False for a plain 'timer'. This "
                            "only matters when there's no label: an unlabelled reminder "
                            "can't actually be created, so the user is asked to name it "
                            "instead."
                        ),
                    },
                },
                "required": [],
            },
        },
        {
            "name": "name_reminder",
            "description": (
                "Give a name to the reminder which was just asked about, right after the "
                "assistant said 'What would you like to name this reminder?'. Use this "
                "instead of set_timer for that reply - it keeps the reminder's original "
                "time exact rather than reworking it from how long ago it was asked."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "What to say when the reminder goes off, worded as the reminder itself.",
                    },
                },
                "required": ["label"],
            },
        },
        {
            "name": "list_timers",
            "description": "Report which timers are running and how long is left on each.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "dismiss_timer",
            "description": (
                "Acknowledge a timer or reminder which has just gone off, for 'okay', "
                "'stop', 'got it', 'thanks' or similar right after one sounds. This stops "
                "it announcing itself again. Not for cancelling a timer which hasn't gone "
                "off yet - use cancel_timer for that."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "check_missed",
            "description": (
                "Report timers and reminders which went off without being acknowledged, "
                "for 'did I miss anything?' or 'what did I miss?'."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "cancel_timer",
            "description": (
                "Cancel a running timer or reminder. Identify it by label, by its length for "
                "timers set without one, or by the clock time it's due to go off ('the 5:40 "
                "one', 'cancel the timer for 4pm'). Use cancel_all only when the user clearly "
                "means every timer. If they just say 'cancel the timer' and it's ambiguous, "
                "call this with neither argument - the user will be asked which one they "
                "meant. If several timers match equally (e.g. two both set for 10 minutes), "
                "the user is asked which one automatically - just pass through what they said."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "label": {
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
                    "cancel_all": {
                        "type": "boolean",
                        "description": "True only if the user asked to cancel every timer.",
                    },
                },
            },
        },
        {
            "name": "set_alarm",
            "description": (
                "Set an alarm for a time of day, e.g. 'set an alarm for 7am', 'wake me up at "
                "quarter past six', 'set an alarm for weekdays at 6:30'. Never give this a "
                "label - alarms don't take one, that's what a reminder is for. An alarm rings "
                "insistently until dismissed, so only use this when the user actually means a "
                "wake-up-style alarm, not a reminder or timer worded loosely as one."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "hour": {"type": "integer", "description": "0-23, 24-hour clock."},
                    "minute": {"type": "integer", "description": "0-59."},
                    "repeat": {
                        "type": "string",
                        "enum": ["daily", "weekdays", "weekends", "custom"],
                        "description": (
                            "Leave out entirely for a one-off alarm. Otherwise: 'daily' for "
                            "every day, 'weekdays' for Monday-Friday, 'weekends' for Saturday "
                            "and Sunday, or 'custom' for specific days, listed in days."
                        ),
                    },
                    "days": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["monday", "tuesday", "wednesday", "thursday",
                                                             "friday", "saturday", "sunday"]},
                        "description": "Only with repeat 'custom': the days it repeats on.",
                    },
                },
                "required": ["hour", "minute"],
            },
        },
        {
            "name": "list_alarms",
            "description": "Report which alarms are set, for 'what alarms do I have' or 'list my alarms'.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "next_alarm",
            "description": "Report how long until the next alarm, for 'how long until my alarm' or 'when's my next alarm'.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "cancel_alarm",
            "description": (
                "Cancel an alarm, identified by its time of day ('cancel my 7am alarm'). If "
                "the user says 'cancel it'/'turn off my alarm' with only one alarm set or it's "
                "otherwise unambiguous, leave label out. If the matched alarm repeats and the "
                "user didn't already say whether they mean just today or the whole schedule "
                "('just for today', 'stop it repeating', 'cancel it completely'), leave scope "
                "out too - the user gets asked which they meant rather than it being guessed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "The alarm's time of day ('7am', '6:30'), or left out when unambiguous.",
                    },
                    "cancel_all": {
                        "type": "boolean",
                        "description": "True only if the user asked to cancel every alarm.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["today", "all"],
                        "description": (
                            "'today' if the user only wants to skip the next occurrence of a "
                            "repeating alarm; 'all' if they want the whole repeat cancelled. "
                            "Leave out if they didn't say - only set this from what the user "
                            "explicitly said, never guessed."
                        ),
                    },
                },
            },
        },
        {
            "name": "set_volume",
            "description": (
                "Change how loud something is. For a bare 'turn it up', 'volume up', 'too loud' "
                "or 'set the volume to 40' that doesn't say what, leave category out - the "
                "assistant works out from what's playing whether that means the music or its own "
                "voice. Give either level (to set a specific value) or direction (to move it up "
                "or down), not both."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
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
                    "level": {
                        "type": "integer",
                        "description": (
                            "Set the volume to this exact percentage, 0 to 100. Only for "
                            "requests naming a number, like 'set the volume to 40'."
                        ),
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": (
                            "Use this for 'volume up', 'louder', 'turn it down' and so on. "
                            "The assistant decides how much by, so don't give an amount "
                            "unless the user asked for a specific one."
                        ),
                    },
                    "amount": {
                        "type": "integer",
                        "description": (
                            "Only when the user asks for a specific change, e.g. 'turn it "
                            "down by 30'. Leave out otherwise so the standard step is used."
                        ),
                    },
                },
            },
        },
        {
            "name": "get_weather",
            "description": (
                "Get the weather forecast for home - how it is now, and the rest of today, "
                "or tomorrow. Use it for any question about the weather, including ones "
                "like 'do I need a coat?' or 'will it rain later?'. The forecast it returns "
                "is what gets spoken, so just reply 'Okay.'"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "enum": ["today", "tomorrow"],
                        "description": "Which day the user asked about. 'today' includes right now.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "control_lights",
            "description": "Turn the smart lights on or off.",
            "input_schema": {
                "type": "object",
                "properties": {"state": {"type": "string", "enum": ["on", "off"]}},
                "required": ["state"],
            },
        },
    ]

    def _asked_for_album(self, text: str) -> bool:
        """
        Whether the user asked for an album, taking the conversation into account.

        The word won't be in the current utterance when the assistant had to ask
        which record was meant: "play the Chronic album" / "do you mean The Chronic
        by Dr. Dre?" / "yes" - by the time the answer arrives, "album" was two turns
        ago, and looking only at "yes" would quietly play the song instead.

        Args:
            text: What the user just said.

        Returns:
            True if an album was asked for, here or in the exchange being answered.
        """
        if "album" in text.lower():
            return True

        # Only the exchange immediately before counts. Anything older is a separate
        # request, and would otherwise turn later track requests into albums.
        if not self.history:
            return False

        last = self.history[-1]
        return ("album" in last.get("user", "").lower()
                or "album" in last.get("assistant", "").lower())

    def _action_from_tool_use(self, block) -> Optional[Dict]:
        """
        Converts a tool call from the model into the action dict the ActionExecutor expects.

        Args:
            block: A tool_use content block from the model's response.

        Returns:
            An action dict, or None if the tool isn't recognised.
        """
        params = block.input or {}

        if block.name == "play_music":
            return {"type": "spotify", "command": "play",
                    "query": params.get("query") or None,
                    "search_type": params.get("search_type")}

        if block.name == "play_playlist":
            return {"type": "spotify", "command": "play_playlist", "name": params.get("name")}

        if block.name == "control_playback":
            return {"type": "spotify", "command": params.get("action")}

        if block.name == "repeat":
            return {"type": "spotify", "command": "repeat", "mode": params.get("mode", "off")}

        if block.name == "shuffle":
            return {"type": "spotify", "command": "shuffle", "enabled": bool(params.get("enabled", True))}

        if block.name == "set_timer":
            return {"type": "clock", "command": "set_timer",
                    "duration_seconds": params.get("duration_seconds"),
                    "at": params.get("at"),
                    "label": params.get("label") or None,
                    "is_reminder": bool(params.get("is_reminder", False))}

        if block.name == "name_reminder":
            return {"type": "clock", "command": "name_reminder", "label": params.get("label")}

        if block.name == "list_timers":
            return {"type": "clock", "command": "list_timers"}

        if block.name == "dismiss_timer":
            return {"type": "clock", "command": "dismiss"}

        if block.name == "check_missed":
            return {"type": "clock", "command": "missed"}

        if block.name == "cancel_timer":
            return {"type": "clock", "command": "cancel_timer",
                    "label": params.get("label") or None,
                    "cancel_all": bool(params.get("cancel_all", False))}

        if block.name == "set_alarm":
            repeat = params.get("repeat") or None
            if repeat == "custom":
                # The days themselves. Left as "custom" if none were given, which the
                # clock can't read, so it asks which days rather than guessing.
                repeat = params.get("days") or "custom"
            return {"type": "clock", "command": "set_alarm",
                    "hour": params.get("hour"), "minute": params.get("minute"), "repeat": repeat}

        if block.name == "list_alarms":
            return {"type": "clock", "command": "list_alarms"}

        if block.name == "next_alarm":
            return {"type": "clock", "command": "next_alarm"}

        if block.name == "cancel_alarm":
            return {"type": "clock", "command": "cancel_alarm",
                    "label": params.get("label") or None,
                    "cancel_all": bool(params.get("cancel_all", False)),
                    "scope": params.get("scope") or None}

        if block.name == "set_volume":
            # Left as None when the user didn't say which - the executor works it out.
            return {"type": "volume",
                    "category": params.get("category") or None,
                    "level": params.get("level"),
                    "direction": params.get("direction"),
                    "amount": params.get("amount")}

        if block.name == "get_weather":
            return {"type": "weather", "day": params.get("day") or "today"}

        if block.name == "control_lights":
            command = "turn_on" if params.get("state") == "on" else "turn_off"
            return {"type": "home_assistant", "entity": "light.strip",
                    "command": command, "confirmation": "chime"}

        logger.warning(f"Model called an unknown tool: {block.name}")
        return None

    # Phrases which mean the user is explicitly asking for a web search.
    # Web search is only attached to the request when one of these appears.
    WEB_SEARCH_TRIGGERS = (
        "search", "look up", "lookup", "google", "check online", "check the internet",
        "on the web", "latest news", "what's the latest", "whats the latest",
        "current price", "right now online",
    )

    def _wants_web_search(self, text: str) -> bool:
        """
        Returns True if the user's query explicitly asks for a live web lookup.

        Args:
            text: The user's transcribed query.

        Returns:
            True if the query contains one of WEB_SEARCH_TRIGGERS.
        """
        if not self.web_search_enabled:
            return False

        # Matched on word boundaries, so a trigger can't fire from
        # inside a longer word, e.g. "research" containing "search".
        lowered = text.lower()
        return any(re.search(rf"\b{re.escape(trigger)}\b", lowered)
                   for trigger in self.WEB_SEARCH_TRIGGERS)

    def process_query(self, text, context=None):
        """
        Processes a user query using the LLM. It maintains a conversation history and constructs an initial prompt to guide the model's behaviour.
        This method checks the query against _parse_action to determine if there are any specific actions (e.g. Spotify commands) to be executed.
        Finally, it returns a dictionary containing the model's response, the list of actions it asked for, and whether the transaction was successful.
        """
        logger.info(f"Processing query: '{text}'", extra=TRANSCRIPT)
        use_web_search = self._wants_web_search(text)
        if self.client is None:
            logger.error("Cannot process query: no API key configured")
            return {"response": "Sorry, I'm not connected to my language model right now.", "actions": self._fallback_actions(text), "success": False}
        if not network.online():
            logger.warning("No network connection, so Claude isn't asked")
            return {"response": OFFLINE_MESSAGE, "actions": self._fallback_actions(text), "success": False}
        try:
            # The time is given already on the 12-hour clock ("7:36 PM"). Given "19:36",
            # the model would sometimes say it straight back despite being asked not to,
            # and Piper reads that out as "nineteen thirty-six" - more like a year than a time.
            now = datetime.now().astimezone()

            # System prompt explaining to the model how to behave, including instructions for formatting and response style.
            system_prompt = (
                f"You are {self.assistant_name}, a voice assistant running locally with smart home and utility features. "
                "You can control Spotify playback and (soon) the smart lights, so don't claim you're unable to play music. "
                "IMPORTANT: music and light commands only actually happen if you call the matching tool. Saying "
                "you have done something does not do it. So whenever the user asks for music or lights, call the "
                "tool - including for shuffle, repeat, skipping, restarting a track, and pausing. Your own text "
                "reply for those commands should just be a short acknowledgement like 'Okay.', since the tool's "
                "result is what gets spoken to the user. "
                "Act on what the user has just said, not on earlier turns: those requests have already been "
                "carried out, so don't act on them again. That does not mean skipping a new request which "
                "resembles an old one - if they ask to turn the volume up three times, turn it up three "
                "times. Only when the latest message is unclear, or sounds like the user talking to "
                "themselves rather than to you, ask what they meant instead of guessing at a tool. "
                "What you receive is speech that has been transcribed, so it often contains mishearings. "
                "When a word is clearly wrong but the intent is obvious, correct it silently and carry on - "
                "'set a timer for 10 cents' means 10 seconds, 'play goobah by six nine' means Gooba by "
                "6ix9ine. Only ask when you genuinely can't tell what was wanted, not when a word simply "
                "came out wrong. "
                "Your replies are converted to speech, so: "
                "never use markdown, bullet points, emojis, or special formatting - plain spoken sentences only. "
                "It is imperative you keep responses to 1-2 short sentences. Spoken answers are slow to listen to, "
                "so say the useful part and stop. Only go longer if the user explicitly asks for detail. "
                "Be direct and natural, like a helpful person talking, not a customer service bot. "
                "If you don't know something, say so plainly rather than guessing. "
                f"The current date and time is {now.strftime('%A %d %B %Y')}, "
                f"{spoken_clock_time(now.hour, now.minute)} ({now.strftime('%Z')}). "
                "Work out any times or dates the user asks about from that, and answer with only the "
                "part they asked for - the time alone for 'what time is it', the date alone for 'what "
                "day is it'. Say dates the way they're spoken, such as 'Tuesday the 4th of August 2026'. "
                "Use UK units and conventions only: degrees Celsius for temperature, "
                "miles for distance and road speeds, stones and pounds for body weight, kilograms and grams "
                "for other weights, litres and pints, and pounds sterling for money. Give dates as day then "
                "month, and use the 12-hour clock with AM / PM. Use British spelling. "
            )

            # Gives the model visibility of what's actually running, so a mangled
            # transcription of a custom label ("walk the frogs") can be matched
            # against the real thing ("walk the dogs") instead of failing to find it.
            if context:
                system_prompt += (
                    f"Timers and reminders currently running: {context} "
                    "Speech-to-text often mangles custom words like these labels, so match "
                    "what the user said against this list by meaning, not exact wording. If "
                    "one of these is obviously what was meant despite the mangled "
                    "transcription, treat it as that one: use its real label when calling a "
                    "tool and when confirming it out loud, not the mangled version the user "
                    "appeared to say. Only ask something like 'Are you trying to cancel your "
                    "walk the dogs reminder?' when it's a close enough call between two "
                    "plausible ones that guessing wrong is a real risk. "
                )

            # The same leniency applies everywhere, not just to timers: a mishearing you're
            # genuinely confident about should be corrected and acted on, not questioned.
            system_prompt += (
                "This same judgement - correct a likely mishearing and act, only ask when "
                "it's genuinely a close call - applies to everything you hear, not only "
                "timers and reminders. "
            )

            # The cutoff wording depends on whether a live lookup is available for this
            # query, so the model doesn't claim it can't check while holding search results.
            if use_web_search:
                system_prompt += (
                    "You can search the web for this query. Summarise what you find in one or two spoken "
                    "sentences and never read out URLs, since the user is listening rather than reading."
                )
            else:
                system_prompt += (
                    "You have no internet access for this query and your training data has a cutoff, so you "
                    "cannot know about recent events, prices, or news. If the user mentions something you don't "
                    "recognise, say it's likely after your cutoff or that you can't check - don't insist it "
                    "didn't happen. The user can ask you to search or look something up if they need current info."
                )

            # Maintains a conversation history to provide context for the model's responses.
            messages = []
            # Loops through the conversation history and adds each user / assistant message to the messages list.
            for turn in self.history:
                messages.append({"role": "user", "content": turn["user"]})
                messages.append({"role": "assistant", "content": turn["assistant"]})
            # Adds the current user query to the messages list.
            messages.append({"role": "user", "content": text})

            # The music and light tools are always offered, so the model decides what
            # the user meant rather than a keyword match guessing at it.
            # max_tokens is set to 300 to stop responses from reaching the limit and being cut off.
            request_kwargs = {"max_tokens": max(self.max_tokens, 300),
                              "tools": list(self.ACTION_TOOLS)}

            # Only attaches the web search tool when the user explicitly asked to look something up.
            if use_web_search:
                logger.info("Query asked for a live lookup, enabling web search")
                request_kwargs["tools"] = request_kwargs["tools"] + [{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": self.web_search_max_uses,
                }]
                # A search needs more room than a normal spoken reply, otherwise the
                # answer gets truncated mid-sentence by max_tokens.
                request_kwargs["max_tokens"] = max(self.max_tokens, 400)

            # A search is allowed longer than an ordinary reply - see search_timeout.
            client = self.client.with_options(timeout=self.search_timeout) if use_web_search else self.client

            # Sends the system prompt, conversation history, current query and model settings to the LLM
            response = client.messages.create(
                model=self.model_name, temperature=self.temperature,
                system=system_prompt, messages=messages, **request_kwargs)

            # A long-running search can come back as "pause_turn" rather than a finished
            # answer. The paused turn is sent back unchanged to let it continue, with a
            # small cap so a misbehaving search can't loop forever.
            for _ in range(3):
                if getattr(response, "stop_reason", None) != "pause_turn":
                    break
                logger.info("Search paused by the API, continuing the turn...")
                messages.append({"role": "assistant", "content": response.content})
                response = client.messages.create(
                    model=self.model_name, temperature=self.temperature,
                    system=system_prompt, messages=messages, **request_kwargs)

            # Retrieves text blocks from the model's response, joins them and strips excess whitespace.
            # This is because responses come as several text blocks which need to be formatted.
            text_blocks = [b.text for b in response.content if hasattr(b, "text")]
            response_text = " ".join(block.strip() for block in text_blocks if block.strip())

            # Strips any URLs, since a spoken web address is unusable and Piper
            # reads them out character by character.
            response_text = re.sub(r"https?://\S+", "", response_text)
            response_text = re.sub(r"\s+", " ", response_text).strip()

            # Picks up every tool the model called, in order - "pause the music and set a
            # ten minute timer" comes back as two, and only running the first did half of
            # what was asked. If it didn't call one, there is no action. The model's
            # judgement is trusted rather than keyword guessing.
            actions = [action for action in (self._action_from_tool_use(block) for block in response.content
                                             if getattr(block, "type", None) == "tool_use")
                       if action]

            # An album is only played whole when the user actually asked for one.
            # Titles are often both a song and an album, and the model tends to
            # assume the album, so the user's own words decide it.
            for action in actions:
                if action.get("search_type") == "album" and not self._asked_for_album(text):
                    logger.info("Album wasn't asked for by name, playing the track instead")
                    action["search_type"] = "track"

            # Fallback for when the action is called by the model, but no message is produced.
            if actions and not response_text:
                response_text = "Okay."

            self.history.append({"user": text, "assistant": response_text, "at": time.monotonic()})

            # Stops the history from growing indefinitely by only keeping a specified number of exchanges,
            # this is defined by history_length.
            if len(self.history) > self.history_length:
                self.history = self.history[-self.history_length:]

            logger.info(f"LLM response: '{response_text}'" + (f" (actions: {actions})" if actions else ""),
                        extra=TRANSCRIPT)

            # Returns a dictionary containing the model's response, the actions it asked for, and whether the transaction was successful
            return {"response": response_text, "actions": actions, "success": True}

        # Catches errors instead of crashing the voice assistant.
        # This logs the error and returns an apology message to the user.
        # The keyword parser is used as a fallback here, so music can still be
        # controlled when the API is unreachable.
        except Exception as e:
            logger.error(f"LLM processing failed ({type(e).__name__}): {e}")
            return {"response": self._failure_message(e),
                    "actions": self._fallback_actions(text), "success": False}

    def _fallback_actions(self, text: str) -> list:
        """
        The keyword parser's action, if it found one, for when the model can't be reached.

        Args:
            text: What the user said.

        Returns:
            A list holding that one action, or an empty list.
        """
        action = self._parse_action(text, "")
        return [action] if action else []

    @staticmethod
    def _failure_message(error: Exception) -> str:
        """
        What to tell the user when a request to the model failed, depending on why.

        Each cause needs something different from the user - waiting, checking the
        internet, or fixing the API key - so a single "trouble connecting" for all of
        them didn't say what was actually wrong.

        Args:
            error: The exception the request raised.

        Returns:
            A short spoken message.
        """
        # A timeout is a kind of connection error, so it's checked first.
        if isinstance(error, anthropic.APITimeoutError):
            return "Sorry, that took too long to answer. Try asking again in a moment."
        if isinstance(error, anthropic.APIConnectionError):
            return OFFLINE_MESSAGE
        if isinstance(error, anthropic.AuthenticationError):
            return "Sorry, my Claude API key isn't working. It needs checking in the .env file."
        if isinstance(error, anthropic.RateLimitError):
            return "Sorry, I've had too many requests just now. Try again in a minute."
        if isinstance(error, anthropic.APIStatusError):
            return "Sorry, Claude is having problems at the moment. Try again shortly."
        return "Sorry, I'm having trouble connecting right now."

    def _parse_action(self, user_text: str, response: str) -> Optional[Dict]:
        """
        Keyword-based action detection, used as a fallback when the model is
        unavailable (no API key, or the request failed). Normally the model picks
        the action itself via ACTION_TOOLS, which handles phrasing and
        mis-transcriptions far better than these keywords can.
        """
        user_lower = user_text.lower()
        # Splits the query into whole words, so keywords only match complete words rather than substrings.
        words = set(re.findall(r"[a-z]+", user_lower))

        # Spotify commands
        if words & {"play", "playing", "resume", "unpause", "music", "song", "songs", "track", "tracks", "spotify"}:
            if words & {"pause", "stop"}:
                return {"type": "spotify", "command": "pause"}
            elif words & {"skip", "next"}:
                return {"type": "spotify", "command": "skip"}
            elif words & {"previous", "back", "last"}:
                return {"type": "spotify", "command": "previous"}
            elif "what" in words and words & {"playing", "song", "track"}:
                return {"type": "spotify", "command": "current"}

            # Checks if the user said "play" and extracts the song or artist name if present, whilst removing filler words.
            # If no specific query is found, it will play the default playlist or resume playback.
            elif words & {"play", "resume", "unpause"}:
                query = None
                play_index = user_lower.find("play ")
                if play_index != -1 and len(user_text) > play_index + 5:

                    # Whisper strips punctuation that could end up in the search query.
                    query = user_text[play_index + 5:].strip().strip(".,!?").strip()
                    for filler in ("some ", "me ", "the ", "a "):
                        if query.lower().startswith(filler):
                            query = query[len(filler):].strip()

                    # "play Drake on Spotify" means play Drake, not search for "Drake on Spotify".
                    for suffix in (" on spotify", " in spotify", " from spotify", " with spotify"):
                        if query.lower().endswith(suffix):
                            query = query[:-len(suffix)].strip()

                    # "spotify" isn't a search term.
                    if query.lower() == "spotify":
                        query = None
                    query = query or None
                return {"type": "spotify", "command": "play", "query": query}

        # Home Assistant commands
        if words & {"light", "lights", "lamp", "lamps"}:
            if "on" in words:
                return {"type": "home_assistant", "entity": "light.strip", "command": "turn_on",
                        "confirmation": "chime"}
            elif "off" in words:
                return {"type": "home_assistant", "entity": "light.strip", "command": "turn_off",
                        "confirmation": "chime"}

        # If no specific action is detected, return None
        return None

    def forget_stale_history(self, now: Optional[float] = None):
        """
        Forgets exchanges older than history_max_age. Called as each new conversation
        starts, rather than every turn, so a back and forth is always remembered in
        full while it's going on.

        Without this, the last few exchanges were kept however old they were, so a
        conversation from the morning coloured a "yes" or "that one" in the evening.

        Args:
            now: The current time.monotonic() value - only given by the tests.
        """
        now = time.monotonic() if now is None else now
        kept = [turn for turn in self.history if now - turn.get("at", now) <= self.history_max_age]
        if len(kept) < len(self.history):
            logger.info(f"Forgot {len(self.history) - len(kept)} old exchange(s)")
        self.history = kept

    def remember(self, user_text: str, reply: str):
        """
        Adds an exchange the model wasn't asked about to its history - one handled
        locally (see intents.py) - so a follow-up like "actually, make it fifteen"
        still makes sense to it.

        Args:
            user_text: What the user said.
            reply: What the assistant replied.
        """
        self.history.append({"user": user_text, "assistant": reply, "at": time.monotonic()})
        if len(self.history) > self.history_length:
            self.history = self.history[-self.history_length:]

    def record_reply(self, user_text: str, spoken: str):
        """
        Replaces the assistant's side of the latest exchange with what was actually
        said back to the user.

        For a tool call the model's own text is usually just "Okay.", and the tool's
        result is what gets spoken instead - often a question, like "What would you
        like to name this reminder?" or "Which one?". History only ever had the
        "Okay.", so the model never saw those questions, and the user's answer arrived
        on the next turn with nothing to say what it was answering.

        Args:
            user_text: What the user said in that exchange, so only the right one is changed.
            spoken: What the assistant actually said.
        """
        if spoken and self.history and self.history[-1]["user"] == user_text:
            self.history[-1]["assistant"] = spoken

    # Clears the conversation history, used to start a new session or reset context
    def clear_history(self):
        self.history = []