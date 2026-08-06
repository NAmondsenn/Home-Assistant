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

    def __init__(self, spotify=None, home_assistant=None, clock=None, volume=None,
                 calendar=None, weather=None):
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
        self.volume = volume
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
        elif action_type == "volume":
            return self._change_volume(action)
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

    def _change_spotify_volume(self, action: Dict) -> Dict:
        """
        Changes Spotify's own volume.

        Kept apart from the assistant's volumes because Spotify holds this level
        itself, so it applies to every device playing rather than just to audio
        this assistant produces.

        Args:
            action: Dict with 'level' for an exact percentage, or 'direction'
                    ('up' / 'down') with an optional 'amount'.

        Returns:
            Dict with 'success' and a relevant message.
        """
        if not self.spotify:
            return {"success": False, "message": "Spotify isn't available right now."}

        current = self.spotify.get_volume()

        if action.get("level") is not None:
            target = action["level"]
        else:
            direction = action.get("direction")
            if direction not in ("up", "down"):
                if current is None:
                    return {"success": False, "message": "I can't tell how loud the music is."}
                return {"success": True, "message": f"The music is at {current} percent."}

            if current is None:
                return {"success": False, "message": "I can't tell how loud the music is."}

            amount = action.get("amount")
            amount = self.volume.step if amount is None else abs(float(amount))
            target = current + (amount if direction == "up" else -amount)

        return self.spotify.set_volume(target)

    def _change_volume(self, action: Dict) -> Dict:
        """
        Changes a volume level.

        Args:
            action: Dict with a 'category', and either 'level' to set an exact value
                    or 'direction' ('up' / 'down') to step it, optionally with an
                    'amount' to override the standard step.

        Returns:
            Dict with 'success' and a relevant message.
        """
        if not self.volume:
            return {"success": False, "message": "Volume control isn't available right now."}

        category = action.get("category", "general")

        if category == "music":
            return self._change_spotify_volume(action)

        if action.get("level") is not None:
            return self._with_beep(category, self.volume.set(category, action["level"]))

        direction = action.get("direction")
        if direction in ("up", "down"):
            # Volume Up / Down changes the volume by 10% by default.
            amount = action.get("amount")
            amount = self.volume.step if amount is None else abs(float(amount))
            return self._with_beep(
                category, self.volume.adjust(category, amount if direction == "up" else -amount))

        # Nothing to change, so the current level is reported instead.
        return {"success": True,
                "message": f"The {category} volume is at {self.volume.percent(category)} percent."}

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
        elif command == "dismiss":
            return self.clock.dismiss()
        elif command == "missed":
            return self.clock.missed()
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
        # This will want "chime": True once it works, since a light turning on is
        # its own confirmation and doesn't need saying out loud.
        return {"success": False, "message": "Home Assistant integration not yet implemented."}