"""
Action Executor Module
Executes actions detected by the LLM (e.g. Spotify, Home Assistant).
It then dispatches to the correct handler based on the action's type.
"""

import logging
from typing import Optional, Dict

# Logger setup
logger = logging.getLogger(__name__)

class ActionExecutor:
    """
    Takes an action dict produced by the LLM Handler and runs it
    against the relevant controller.
    """

    def __init__(self, spotify=None, home_assistant=None, clock=None, calendar=None, weather=None):
        """
        Args:
            spotify: SpotifyController instance, or None if Spotify isn't available.
            home_assistant: Home Assistant client instance, or None if not yet configured.
            clock: Clock instance, for time-based actions.
            calendar: Calendar instance, for calendar-related actions.
            weather: Weather instance, for weather-related actions.
        """
        self.spotify = spotify
        self.home_assistant = home_assistant
        self.clock = clock
        self.calendar = calendar # TODO
        self.weather = weather # TODO

    def execute(self, action: Optional[Dict]) -> Dict:
        """
        Runs the given action based on its 'type' key.

        Args:
            action: Action dict from LLMHandler._parse_action / None if no
                    action was detected in the user's query.

        Returns:
            Dict with a 'success' key and a 'message' key if appropriate,
            in the same shape SpotifyController's methods already return.
        """
        # No action detected in the query, nothing to do.
        if not action:
            return {"success": False, "message": None}

        action_type = action.get("type")

        # Dispatches to the handler for the action's type.
        if action_type == "spotify":
            return self._execute_spotify(action)
        elif action_type == "home_assistant":
            return self._execute_home_assistant(action)
        elif action_type == "clock":
            return self._execute_clock(action)
        else:
            # Fallback incase the LLM Handler returns an invalid action type.
            logger.warning(f"Invalid action type: {action_type}")
            return {"success": False, "message": f"Sorry, I can't handle a '{action_type}' action yet."}

    def _execute_spotify(self, action: Dict) -> Dict:
        """
        Runs a Spotify command (play/pause/skip/previous/current).

        Args:
            action: Dict with a 'command', it includes 'query' for play.

        Returns:
            Dict with 'success' with a relevant message.
        """
        if not self.spotify:
            return {"success": False, "message": "Spotify is not available right now."}

        command = action.get("command")
        if command == "play":
            # search_type tells the controller whether the query names a track / artist / genre to play.
            return self.spotify.play(action.get("query"), search_type=action.get("search_type"))
        elif command == "pause":
            return self.spotify.pause()
        elif command == "skip":
            return self.spotify.skip()
        elif command == "previous":
            return self.spotify.previous()
        elif command == "restart":
            return self.spotify.restart()
        elif command == "repeat":
            return self.spotify.repeat(action.get("mode", "off"))
        elif command == "shuffle":
            return self.spotify.shuffle(action.get("enabled", True))
        elif command == "play_playlist":
            return self.spotify.play_playlist(action.get("name", ""))
        elif command == "current":
            result = self.spotify.current_track()
            # Returns the track and artist details if the current track is found.
            if result.get("success"):
                return {"success": True, "message": f"Playing {result['track']} by {result['artist']}"}
            return result
        else:
            logger.warning(f"Unknown Spotify command: {command}")
            return {"success": False, "message": f"I don't know how to '{command}' on Spotify."}

    def _execute_clock(self, action: Dict) -> Dict:
        """
        Runs a timer command (set / list / cancel).

        Args:
            action: Dict with a 'command', plus 'duration_seconds' and 'label'
                    for set_timer, or 'label' for cancel.

        Returns:
            Dict with 'success' and a relevant message.
        """
        if not self.clock:
            return {"success": False, "message": "Timers aren't available right now."}

        command = action.get("command")
        if command == "set_timer":
            return self.clock.set_timer(action.get("duration_seconds"), action.get("label"))
        elif command == "list_timers":
            return self.clock.list_timers()
        elif command == "cancel_timer":
            return self.clock.cancel_timer(action.get("label"), action.get("cancel_all", False))

        logger.warning(f"Unknown clock command: {command}")
        return {"success": False, "message": f"I don't know how to '{command}' a timer."}

    def _execute_home_assistant(self, action: Dict) -> Dict:
        """
        Runs a Home Assistant command (e.g. turning a light on/off).

        Args:
            action: Dict with 'entity' and 'command' keys.

        Returns:
            Dict with 'success' with a relevant message.
        """
        if not self.home_assistant:
            return {"success": False, "message": "Home Assistant is not available right now."}

        entity = action.get("entity")
        command = action.get("command")
        logger.info(f"Home Assistant action requested: {command} on {entity}")

        # TODO: replace with a real call once the Home Assistant client is built,
        # e.g. self.home_assistant.call_service(entity, command)
        return {"success": False, "message": "Home Assistant integration not yet implemented."}