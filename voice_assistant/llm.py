import os
import re
import logging
from typing import Optional, Dict
from dotenv import load_dotenv
from anthropic import Anthropic

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

        # If no API key is provided, ANTHROPIC_API_KEY is taken from .env, falling back to Claude_API_Key.
        # Missing API keys are logged as warnings, the program runs, but the LLM will not be available.
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("Claude_API_Key")
        if self.api_key:
            self.client = Anthropic(api_key=self.api_key)
        else:
            self.client = None
            logger.warning("No API key found (ANTHROPIC_API_KEY), LLM queries will fail until one is set")

        self.model_name = model or llm_config.get("model", "claude-haiku-4-5-20251001")
        self.max_tokens = max_tokens if max_tokens is not None else llm_config.get("max_tokens", 150)
        self.temperature = temperature if temperature is not None else llm_config.get("temperature", 0.7)
        self.history_length = history_length if history_length is not None else conversation_config.get("history_length", 5)
        self.history = []
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
                            "What to play, spelled correctly, e.g. 'Gooba by 6ix9ine' or 'Drake'."
                            "Leave out entirely to resume paused music."
                        ),
                    },
                    "search_type": {
                        "type": "string",
                        "enum": ["track", "artist", "playlist"],
                        "description": (
                            "Whether the query names a specific song, an artist, or a "
                            "genre or mood to build a playlist from."
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
                "what is currently playing."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "skip", "previous", "restart", "current"],
                        "description": (
                            "'previous' goes to the last song, 'restart' plays the current "
                            "song again from the start."
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
            "name": "control_lights",
            "description": "Turn the smart lights on or off.",
            "input_schema": {
                "type": "object",
                "properties": {"state": {"type": "string", "enum": ["on", "off"]}},
                "required": ["state"],
            },
        },
    ]

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
        Finally, it returns a dictionary containing the model's response, any detected action, and whether the transaction was successful.
        """
        logger.info(f"Processing query: '{text}'")
        use_web_search = self._wants_web_search(text)
        if self.client is None:
            logger.error("Cannot process query: no API key configured")
            return {"response": "Sorry, I'm not connected to my language model right now.", "action": self._parse_action(text, ""), "success": False}
        try:
            # System prompt explaining to the model how to behave, including instructions for formatting and response style.
            system_prompt = (
                f"You are {self.assistant_name}, a voice assistant running locally with smart home and utility features. "
                "You can control Spotify playback and (soon) the smart lights, so don't claim you're unable to play music. "
                "Music and light commands are carried out by a separate command system, not by you, and its result "
                "message replaces yours. So for those commands reply with a brief acknowledgement like 'Okay.' - "
                "never announce that an action has already happened, since you can't know whether it did. "
                "Your replies are converted to speech, so: "
                "never use markdown, bullet points, emojis, or special formatting - plain spoken sentences only. "
                "Keep responses to 1-2 short sentences. Spoken answers are slow to listen to, so say the useful part "
                "and stop. Only go longer if the user explicitly asks for detail. "
                "Be direct and natural, like a helpful person talking, not a customer service bot. "
                "If you don't know something, say so plainly rather than guessing. "
                "Use UK units and conventions only: degrees Celsius for temperature, "
                "miles for distance and road speeds, stones and pounds for body weight, kilograms and grams "
                "for other weights, litres and pints, and pounds sterling for money. Give dates as day then "
                "month, and use the 12-hour clock with AM / PM. Use British spelling. "
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
            request_kwargs = {"max_tokens": self.max_tokens, "tools": list(self.ACTION_TOOLS)}

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

            # Sends the system prompt, conversation history, current query and model settings to the LLM
            response = self.client.messages.create(
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
                response = self.client.messages.create(
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

            # Picks up any tool the model called. If it didn't call one, there is no action.
            # the model's judgement is trusted rather than keyword guessing.
            action = None
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    action = self._action_from_tool_use(block)
                    break

            # Fallback for when the action is called by the model, but no message is produced.
            if action and not response_text:
                response_text = "Okay."

            self.history.append({"user": text, "assistant": response_text})

            # Stops the history from growing indefinitely by only keeping a specified number of exchanges,
            # this is defined by history_length.
            if len(self.history) > self.history_length:
                self.history = self.history[-self.history_length:]

            logger.info(f"LLM response: '{response_text}'" + (f" (action: {action})" if action else ""))

            # Returns a dictionary containing the model's response, any detected action, and whether the transaction was successful
            return {"response": response_text, "action": action, "success": True}

        # Catches errors instead of crashing the voice assistant.
        # This logs the error and returns an apology message to the user.
        # The keyword parser is used as a fallback here, so music can still be
        # controlled when the API is unreachable.
        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            return {"response": "Sorry, I'm having trouble connecting right now.",
                    "action": self._parse_action(text, ""), "success": False}

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

    # Clears the conversation history, used to start a new session or reset context
    def clear_history(self):
        self.history = []