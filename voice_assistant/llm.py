"""
LLM Handler
Manages conversation with Gemini API (using new google-genai package).
"""

import os
import logging
from google import genai
from google.genai import types
from typing import Optional, Dict, List
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


class LLMHandler:
    """Handles conversation with Gemini API."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash",
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
        history_length: int = 5
    ):
        """
        Initialize LLM handler.
        
        Args:
            api_key: Gemini API key (None = load from env)
            model: Model name
            max_tokens: Max response length
            temperature: Response randomness (0-1)
            history_length: Number of conversation turns to remember
        """
        # Get API key
        self.api_key = api_key or os.getenv("Gemini_API_Key")
        if not self.api_key:
            raise ValueError("Gemini_API_Key not found in environment")
            
        # Initialize client
        self.client = genai.Client(api_key=self.api_key)
        
        self.model_name = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.history_length = history_length
        
        # Conversation history
        self.conversation_history: List[Dict[str, str]] = []
        
        logger.info(f"LLM initialized: {model}")
        
    def process_query(
        self,
        text: str,
        context: Optional[Dict] = None
    ) -> Dict[str, any]:
        """
        Process user query and return response.
        
        Args:
            text: User's transcribed speech
            context: Optional context (time, location, etc.)
            
        Returns:
            Dict with 'response', 'action', 'confidence'
        """
        logger.info(f"Processing query: '{text}'")
        
        try:
            # Build prompt with system instructions
            prompt = self._build_prompt(text, context)
            
            # Generate response using new API
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                max_output_tokens=self.max_tokens if self.max_tokens else 8192,
    		   temperature=self.temperature,
		)
            )
            
            response_text = response.text.strip()
            
            # Store in history
            self._add_to_history(text, response_text)
            
            # Parse for actions
            action = self._parse_action(text, response_text)
            
            result = {
                'response': response_text,
                'action': action,
                'confidence': 1.0
            }
            
            logger.info(f"LLM response: '{response_text}'")
            return result
            
        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            return self._get_fallback_response(text)
            
    def _build_prompt(self, text: str, context: Optional[Dict]) -> str:
        """Build prompt with system instructions and context."""
        
        system_prompt = """You are Jarvis, a helpful voice assistant.

Rules:
- Keep responses concise (1-2 sentences max for voice)
- Be direct and natural
- If controlling devices, confirm the action briefly

Current context:
- You are a local voice assistant running on a Raspberry Pi
- You can control smart home devices when integrated
"""
        
        # Add conversation history if exists
        history_text = ""
        if self.conversation_history:
            history_text = "\n\nRecent conversation:\n"
            for entry in self.conversation_history[-3:]:
                history_text += f"User: {entry['user']}\nAssistant: {entry['assistant']}\n"
        
        full_prompt = f"{system_prompt}{history_text}\n\nUser: {text}\nAssistant:"
        
        return full_prompt
        
    def _parse_action(self, user_text: str, response: str) -> Optional[Dict]:
        """Parse response for action commands."""
        user_lower = user_text.lower()
        
        # Light control detection
        if "light" in user_lower:
            if "turn on" in user_lower or "on" in user_lower:
                return {
                    'type': 'home_assistant',
                    'entity': 'light.strip',
                    'command': 'turn_on',
                    'confirmation': 'chime'
                }
            elif "turn off" in user_lower or "off" in user_lower:
                return {
                    'type': 'home_assistant',
                    'entity': 'light.strip',
                    'command': 'turn_off',
                    'confirmation': 'chime'
                }
                
        return None
        
    def _add_to_history(self, user_text: str, assistant_text: str):
        """Add exchange to conversation history."""
        self.conversation_history.append({
            'user': user_text,
            'assistant': assistant_text
        })
        
        if len(self.conversation_history) > self.history_length:
            self.conversation_history = self.conversation_history[-self.history_length:]
            
    def _get_fallback_response(self, text: str) -> Dict:
        """Return fallback response when API fails."""
        text_lower = text.lower()
        
        if "time" in text_lower:
            from datetime import datetime
            now = datetime.now()
            response = f"It's {now.strftime('%I:%M %p')}"
        elif "weather" in text_lower:
            response = "I can't check the weather right now"
        else:
            response = "Sorry, I'm having trouble connecting right now"
            
        return {
            'response': response,
            'action': None,
            'confidence': 0.5,
            'fallback': True
        }
        
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
        logger.info("Conversation history cleared")


# Test function
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("\n=== LLM Handler Test ===\n")
    
    llm = LLMHandler()
    
    test_queries = [
        "What's the weather like?",
        "Turn on the lights",
        "What's 2 plus 2?",
        "Tell me a joke"
    ]
    
    for query in test_queries:
        print(f"\nUser: {query}")
        result = llm.process_query(query)
        print(f"Jarvis: {result['response']}")
        if result.get('action'):
            print(f"Action: {result['action']}")
        print("-" * 40)
