import os
import logging
from typing import Optional, Dict, List
from dotenv import load_dotenv
from anthropic import Anthropic

# loads environment variables from a .env file if present
load_dotenv()
logger = logging.getLogger(__name__)

class LLMHandler:
    def __init__(self, api_key=None, model="claude-haiku-4-5-20251001", max_tokens=150, temperature=0.7, history_length=5, assistant_name="Assistant"):
        # If no API key is provided, "Claude_API_Key" is taken from .env
        self.api_key = api_key or os.getenv("Claude_API_Key")
        # If no API key is found, an error is raised
        if not self.api_key:
            raise ValueError("Claude_API_Key not found in environment")
        
        self.client = Anthropic(api_key=self.api_key)
        self.model_name = model
        self.max_tokens = max_tokens if max_tokens else 150
        self.temperature = temperature
        self.history_length = history_length
        self.history = []
        self.assistant_name = assistant_name
        
        logger.info(f"LLM initialised: {model}")

    def process_query(self, text, context=None):
        """
        Processes a user query using the LLM. It maintains a conversation history and constructs a system prompt to guide the model's behavior.
        This method checks the query against _parse_action to determine if any specific actions (like Spotify commands) should be executed.
        Finally, it returns a dictionary containing the model's response, any detected action, and whether the transaction was successful.
        """
        logger.info(f"Processing query: '{text}'")
        try:
            # System prompt explaining to the model how to behave, including instructions for formatting and response style
            system_prompt = (
                f"You are {self.assistant_name}, a voice assistant running locally with smart home and utility features. "
                "Your replies are converted to speech, so: "
                "never use markdown, bullet points, emojis, or special formatting — plain spoken sentences only. "
                "Keep responses to 1-2 sentences unless the user asks for detail. "
                "Be direct and natural, like a helpful person talking, not a customer service bot. "
                "If you don't know something, say so plainly rather than guessing."
                )
            
            # Maintains a conversation history to provide context for the model's responses
            messages = []
            # Loops through the entire conversation history and adds each user and assistant message to the messages list
            for turn in self.history:
                messages.append({"role": "user", "content": turn["user"]})
                messages.append({"role": "assistant", "content": turn["assistant"]})
            # Adds the current user query to the messages list
            messages.append({"role": "user", "content": text})

            # Sends the system prompt, conversation history, current query and model settings to the LLM
            response = self.client.messages.create(
                model=self.model_name, max_tokens=self.max_tokens,
                temperature=self.temperature, system=system_prompt, messages=messages)

            # Grabs text blocks from the model's response, joins them and strips excess whitespace. This is the final response that will be spoken back to the user.
            response_text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
            self.history.append({"user": text, "assistant": response_text})

            # Stops the history from growing indefinitely by only keeping a specified number of exchanges, defined by history_length
            if len(self.history) > self.history_length:
                self.history = self.history[-self.history_length:]

            # Checks the users query and the model's response for any specific actions that should be executed, such as turning on the lights
            action = self._parse_action(text, response_text)
            logger.info(f"LLM response: '{response_text}'")

            # Returns a dictionary containing the model's response, any detected action, and whether the transaction was successful
            return {"response": response_text, "action": action, "success": True}
        
        # Catches errors instead of crashing the voice assistant. This logs the error and returns an apology message to the user.
        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            return {"response": "Sorry, I'm having trouble connecting right now.", "action": None, "success": False}

    def _parse_action(self, user_text: str, response: str) -> Optional[Dict]:
        user_lower = user_text.lower()
        
        # Spotify commands
        if any(word in user_lower for word in ["play", "music", "song", "spotify"]):
            if "pause" in user_lower or "stop" in user_lower:
                return {"type": "spotify", "command": "pause"}
            elif "skip" in user_lower or "next" in user_lower:
                return {"type": "spotify", "command": "skip"}
            elif "previous" in user_lower or "back" in user_lower or "last" in user_lower:
                return {"type": "spotify", "command": "previous"}
            elif "what" in user_lower and ("playing" in user_lower or "song" in user_lower):
                return {"type": "spotify", "command": "current"}

            # Checks if the user said "play" and extracts the song or artist name if present. 
            # If no specific query is found, it will play the default playlist or resume playback.
            elif "play" in user_lower:
                query = None
                play_index = user_lower.find("play ")
                if play_index != -1 and len(user_text) > play_index + 5:
                    query = user_text[play_index + 5:].strip()
                return {"type": "spotify", "command": "play", "query": query}
        
        # Home Assistant commands
        if "light" in user_lower or "lamp" in user_lower:
            if "on" in user_lower:
                return {"type": "home_assistant", "entity": "light.strip", "command": "turn_on", "confirmation": "chime"}
            elif "off" in user_lower:
                return {"type": "home_assistant", "entity": "light.strip", "command": "turn_off", "confirmation": "chime"}
        
        # If no specific action is detected, return None
        return None

    # Clears the conversation history, used to start a new session or reset context
    def clear_history(self):
        self.history = []

# Tests the LLMHandler class by sending a series of queries and printing the responses
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    llm = LLMHandler()
    for query in ["Hello, how are you?", "What's two plus two?", "Tell me a joke.", "Play some jazz music"]:
        print(f"\n> {query}")
        result = llm.process_query(query)
        print(f"< {result['response']}")
        if result["action"]:
            print(f"  Action: {result['action']}")
