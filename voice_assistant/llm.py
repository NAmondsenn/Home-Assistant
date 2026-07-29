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

        # Pulls llm, conversation and assistant sections from config.yaml
        llm_config = config.section("llm") if config else {}
        conversation_config = config.section("conversation") if config else {}
        assistant_config = config.section("assistant") if config else {}

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

        logger.info(
            f"LLM initialised: {self.model_name} (max_tokens={self.max_tokens}, temperature={self.temperature}, history_length={self.history_length})")

    def process_query(self, text, context=None):
        """
        Processes a user query using the LLM. It maintains a conversation history and constructs an initial prompt to guide the model's behaviour.
        This method checks the query against _parse_action to determine if there are any specific actions (e.g. Spotify commands) to be executed.
        Finally, it returns a dictionary containing the model's response, any detected action, and whether the transaction was successful.
        """
        logger.info(f"Processing query: '{text}'")
        if self.client is None:
            logger.error("Cannot process query: no API key configured")
            return {"response": "Sorry, I'm not connected to my language model right now.", "action": self._parse_action(text, ""), "success": False}
        try:
            # System prompt explaining to the model how to behave, including instructions for formatting and response style.
            system_prompt = (
                f"You are {self.assistant_name}, a voice assistant running locally with smart home and utility features. "
                "Your replies are converted to speech, so: "
                "never use markdown, bullet points, emojis, or special formatting - plain spoken sentences only. "
                "Keep responses to 1-2 sentences unless the user asks for detail. "
                "Be direct and natural, like a helpful person talking, not a customer service bot. "
                "If you don't know something, say so plainly rather than guessing."
            )

            # Maintains a conversation history to provide context for the model's responses.
            messages = []
            # Loops through the conversation history and adds each user and assistant message to the messages list
            for turn in self.history:
                messages.append({"role": "user", "content": turn["user"]})
                messages.append({"role": "assistant", "content": turn["assistant"]})
            # Adds the current user query to the messages list
            messages.append({"role": "user", "content": text})

            # Sends the system prompt, conversation history, current query and model settings to the LLM
            response = self.client.messages.create(
                model=self.model_name, max_tokens=self.max_tokens,
                temperature=self.temperature, system=system_prompt, messages=messages)

            # Grabs text blocks from the model's response, joins them and strips excess whitespace.
            # This is the final response that will be spoken back to the user.
            response_text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
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