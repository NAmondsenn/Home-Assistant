import os
import logging
from typing import Optional, Dict, List
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
logger = logging.getLogger(__name__)

class LLMHandler:
    def __init__(self, api_key=None, model="claude-haiku-4-5-20251001", max_tokens=150, temperature=0.7, history_length=5):
        self.api_key = api_key or os.getenv("Claude_API_Key")
        if not self.api_key:
            raise ValueError("Claude_API_Key not found in environment")
        self.client = Anthropic(api_key=self.api_key)
        self.model_name = model
        self.max_tokens = max_tokens if max_tokens else 150
        self.temperature = temperature
        self.history_length = history_length
        self.history = []
        logger.info(f"LLM initialized: {model}")

    def process_query(self, text, context=None):
        logger.info(f"Processing query: '{text}'")
        try:
            system_prompt = "You are Jarvis, a helpful voice assistant. Keep responses concise — 1 to 2 sentences maximum, since your replies will be spoken aloud. Be direct, practical, and natural."
            messages = []
            for turn in self.history:
                messages.append({"role": "user", "content": turn["user"]})
                messages.append({"role": "assistant", "content": turn["assistant"]})
            messages.append({"role": "user", "content": text})
            response = self.client.messages.create(
                model=self.model_name, max_tokens=self.max_tokens,
                temperature=self.temperature, system=system_prompt, messages=messages)
            response_text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
            self.history.append({"user": text, "assistant": response_text})
            if len(self.history) > self.history_length:
                self.history = self.history[-self.history_length:]
            action = None
            user_lower = text.lower()
            if "light" in user_lower or "lamp" in user_lower:
                if "on" in user_lower:
                    action = {"type": "home_assistant", "entity": "light.strip", "command": "turn_on", "confirmation": "chime"}
                elif "off" in user_lower:
                    action = {"type": "home_assistant", "entity": "light.strip", "command": "turn_off", "confirmation": "chime"}
            logger.info(f"LLM response: '{response_text}'")
            return {"response": response_text, "action": action, "confidence": 1.0}
        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            return {"response": "Sorry, I'm having trouble connecting right now.", "action": None, "confidence": 0.0}

    def clear_history(self):
        self.history = []

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    llm = LLMHandler()
    for query in ["Hello, my name is Nathan.", "What's two plus two?", "Tell me a joke.", "Turn on the lights."]:
        print(f"\n> {query}")
        result = llm.process_query(query)
        print(f"< {result['response']}")
        if result["action"]:
            print(f"  Action: {result['action']}")
