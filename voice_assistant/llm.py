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

        # If no API key is provided, the standard ANTHROPIC_API_KEY is taken from .env,
        # falling back to the legacy Claude_API_Key name.
        # A missing key doesn't raise here: offline features like action parsing still
        # work, and process_query reports the problem gracefully instead.
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
        # aren't slowed down (or billed) by a search the user never asked for.
        self.web_search_enabled = web_search_config.get("enabled", True)
        self.web_search_max_uses = web_search_config.get("max_uses", 3)

        logger.info(
            f"LLM initialised: {self.model_name} (max_tokens={self.max_tokens}, temperature={self.temperature}, "
            f"history_length={self.history_length}, web_search={self.web_search_enabled})")

    # Phrases which mean the user is explicitly asking for a web search.
    # Web search is only attached to the request when one of these appears,
    # keeping normal conversation fast and avoiding needless search charges.
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

        # Matched on word boundaries so a trigger can't fire from inside a longer
        # word, e.g. "research" containing "search".
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
                "Your replies are converted to speech, so: "
                "never use markdown, bullet points, emojis, or special formatting - plain spoken sentences only. "
                "Keep responses to 1-2 short sentences. Spoken answers are slow to listen to, so say the useful part "
                "and stop. Only go longer if the user explicitly asks for detail. "
                "Be direct and natural, like a helpful person talking, not a customer service bot. "
                "If you don't know something, say so plainly rather than guessing. "
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
            # Loops through the conversation history and adds each user and assistant message to the messages list
            for turn in self.history:
                messages.append({"role": "user", "content": turn["user"]})
                messages.append({"role": "assistant", "content": turn["assistant"]})
            # Adds the current user query to the messages list
            messages.append({"role": "user", "content": text})

            # Only attaches the web search tool when the user explicitly asked to look
            # something up. Searches are billed per use and add several seconds of
            # latency, which is why this isn't left on for every query.
            request_kwargs = {"max_tokens": self.max_tokens}
            if use_web_search:
                logger.info("Query asked for a live lookup, enabling web search")
                request_kwargs["tools"] = [{
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

            # Grabs text blocks from the model's response, joins them and strips excess whitespace.
            # A search reply arrives as several text blocks (with tool blocks in between,
            # which have no .text), so they're joined with a space rather than run together.
            # This is the final response that will be spoken back to the user.
            text_blocks = [b.text for b in response.content if hasattr(b, "text")]
            response_text = " ".join(block.strip() for block in text_blocks if block.strip())

            # Strips any URLs, since a spoken web address is unusable and Piper
            # reads them out character by character.
            response_text = re.sub(r"https?://\S+", "", response_text)
            response_text = re.sub(r"\s+", " ", response_text).strip()
            self.history.append({"user": text, "assistant": response_text})

            # Stops the history from growing indefinitely by only keeping a specified number of exchanges,
            # this is defined by history_length.
            if len(self.history) > self.history_length:
                self.history = self.history[-self.history_length:]

            # Checks for any specific actions that should be executed, e.g. Spotify commands.
            action = self._parse_action(text, response_text)
            logger.info(f"LLM response: '{response_text}'")

            # Returns a dictionary containing the model's response, any detected action, and whether the transaction was successful
            return {"response": response_text, "action": action, "success": True}

        # Catches errors instead of crashing the voice assistant.
        # This logs the error and returns an apology message to the user.
        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            return {"response": "Sorry, I'm having trouble connecting right now.", "action": None, "success": False}

    def _parse_action(self, user_text: str, response: str) -> Optional[Dict]:
        user_lower = user_text.lower()
        # Splits the query into whole words, so keywords only match complete words
        # rather than substrings (e.g. "on" no longer matches inside "monitor").
        words = set(re.findall(r"[a-z]+", user_lower))

        # Spotify commands
        if words & {"play", "playing", "music", "song", "songs", "track", "tracks", "spotify"}:
            if words & {"pause", "stop"}:
                return {"type": "spotify", "command": "pause"}
            elif words & {"skip", "next"}:
                return {"type": "spotify", "command": "skip"}
            elif words & {"previous", "back", "last"}:
                return {"type": "spotify", "command": "previous"}
            elif "what" in words and words & {"playing", "song", "track"}:
                return {"type": "spotify", "command": "current"}

            # Checks if the user said "play" and extracts the song or artist name if present,
            # dropping leading filler words like "some".
            # If no specific query is found, it will play the default playlist or resume playback.
            elif "play" in words:
                query = None
                play_index = user_lower.find("play ")
                if play_index != -1 and len(user_text) > play_index + 5:
                    query = user_text[play_index + 5:].strip()
                    for filler in ("some ", "me ", "the ", "a "):
                        if query.lower().startswith(filler):
                            query = query[len(filler):].strip()
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