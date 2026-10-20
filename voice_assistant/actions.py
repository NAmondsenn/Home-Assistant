"""
Action Executor Module
Executes actions detected by the LLM (e.g. Spotify, Home Assistant).
It then dispatches to the correct handler based on the action's type.
"""

import logging
from typing import Optional, Dict, List, Tuple

from voice_assistant import network

# Logger setup
logger = logging.getLogger(__name__)

# Said straight away for anything Spotify, rather than after its timeouts, when
# there's no network - see network.py.
SPOTIFY_OFFLINE = "Spotify needs the internet, and I can't reach it right now."


def confirm(results: List[Dict], chime_enabled: bool = True,
            fallback: Optional[str] = None) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Works out how the actions a request ran should be confirmed to the user.

    An action can confirm itself in three ways - the music changing ("silent"), a
    sound of its own, such as the volume beep, or the success chime - and otherwise
    its message is said out loud. A self-confirming action's message is only ever
    logged, never spoken, even when there's something else to say as well: a
    reminder offer tacked onto a skipped song used to come out as "That song was
    shit anyway. Also, your reminder is still going."

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
        if result.get("success") and result.get("silent"):
            continue
        if result.get("success") and result.get("sound"):
            sound = (result["sound"], result.get("sound_category", "general"))
        elif result.get("success") and result.get("chime") and chime_enabled:
            sound = ("success_chime.wav", "general")

        if sound:
            if sound not in sounds:
                sounds.append(sound)
            continue

        message = result.get("message") or fallback
        if message and message not in messages:
            messages.append(message)
    return sounds, messages

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
        self.weather = weather

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
        elif action_type == "weather":
            return self._execute_weather(action)
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
        if not network.online():
            return {"success": False, "message": SPOTIFY_OFFLINE}

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
                return {"success": True, "message": f"This is {result['track']} by {result['artist']}"}
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
        if not network.online():
            return {"success": False, "message": SPOTIFY_OFFLINE}

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

        # Confirmed with the same beep as the other volumes. It plays at the
        # assistant's own level rather than the music's, since it comes out of the
        # assistant and not through Spotify - the music itself demonstrates the new
        # level well enough.
        return self._with_beep("general", self.spotify.set_volume(target))

    def _unnamed_volume_category(self) -> str:
        """
        Which volume a request like "turn it down" means when it doesn't say.

        The music, if it was playing when the user spoke - that's what they'll have
        been listening to - and the assistant's own voice otherwise. Before this the
        model always chose the assistant's voice, so "turn it down" with music on
        changed nothing the user could actually hear.
        """
        if self.spotify and self.spotify.was_playing():
            return "music"
        return "general"

    def _change_volume(self, action: Dict) -> Dict:
        """
        Changes a volume level.

        Args:
            action: Dict with a 'category' (or None when the user didn't say which),
                    and either 'level' to set an exact value or 'direction'
                    ('up' / 'down') to step it, optionally with an 'amount' to
                    override the standard step.

        Returns:
            Dict with 'success' and a relevant message.
        """
        if not self.volume:
            return {"success": False, "message": "Volume control isn't available right now."}

        category = action.get("category") or self._unnamed_volume_category()

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

    def _execute_weather(self, action: Dict) -> Dict:
        """
        Gets the forecast.

        Args:
            action: Dict with 'day' - "today" or "tomorrow".

        Returns:
            Dict with 'success' and the forecast as the message.
        """
        if not self.weather:
            return {"success": False, "message": "The weather isn't available right now."}
        return self.weather.forecast(action.get("day") or "today")

    def _execute_clock(self, action: Dict) -> Dict:
        """
        Runs a timer, reminder or alarm command (set / list / cancel).

        Args:
            action: Dict with a 'command', plus whatever that command needs -
                    'duration_seconds' or 'at', 'label' and 'is_reminder' for set_timer,
                    'label' for cancel_timer, 'hour'/'minute'/'repeat' for
                    set_alarm, 'label'/'cancel_all'/'scope' for cancel_alarm.

        Returns:
            Dict with 'success' and a relevant message.
        """
        if not self.clock:
            return {"success": False, "message": "Timers aren't available right now."}

        command = action.get("command")
        if command == "set_timer":
            return self.clock.set_timer(action.get("duration_seconds"), action.get("label"),
                                        action.get("is_reminder", False), at=action.get("at"))
        elif command == "name_reminder":
            return self.clock.name_pending_reminder(action.get("label"))
        elif command == "list_timers":
            return self.clock.list_timers()
        elif command == "dismiss":
            return self.clock.dismiss()
        elif command == "missed":
            return self.clock.missed()
        elif command == "cancel_timer":
            return self.clock.cancel_timer(action.get("label"), action.get("cancel_all", False))
        elif command == "set_alarm":
            return self.clock.set_alarm(action.get("hour"), action.get("minute"), action.get("repeat"))
        elif command == "list_alarms":
            return self.clock.list_alarms()
        elif command == "next_alarm":
            return self.clock.next_alarm()
        elif command == "cancel_alarm":
            return self.clock.cancel_alarm(action.get("label"), action.get("cancel_all", False),
                                           action.get("scope"))

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