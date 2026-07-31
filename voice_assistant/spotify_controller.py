"""
Spotify Controller Module
Handles authentication and playback control via the Spotify Web API.

Playback is targeted at this machine's own Spotify Connect device (installed by
setup.sh as raspotify), so the assistant behaves like a standalone speaker rather
than remote-controlling whichever phone or laptop Spotify happened to list first.
"""

import os
import logging
from typing import Optional, Dict
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()
logger = logging.getLogger(__name__)

# Resolves paths relative to the project root (the parent of the voice_assistant
# package), so the token cache lives inside the project rather than at ~.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SpotifyController:
    """Controls Spotify playback, preferring this machine's own Connect device."""

    def __init__(self, config=None):
        """
        Args:
            config: Configs loaded from config.yaml. Used for the Connect device
                    name and the pause-while-speaking setting. Optional, so the
                    controller can still be used standalone for testing.
        """
        # Pulls the spotify section from config.yaml.
        spotify_config = config.section("spotify") if config else {}
        self.device_name = spotify_config.get("device_name", "Voice-Assistant")
        self.pause_while_speaking = spotify_config.get("pause_while_speaking", True)

        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI")

        if not all([self.client_id, self.client_secret, self.redirect_uri]):
            raise ValueError("Spotify credentials not found in .env")

        # Scopes needed for playback control
        scope = "user-read-playback-state,user-modify-playback-state,user-read-currently-playing"

        self.sp_oauth = SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope=scope,
            cache_path=os.path.join(PROJECT_DIRECTORY, ".spotify_cache")
        )

        self.sp = None
        # Tracks whether the assistant paused the music itself, so it only resumes
        # playback it actually interrupted.
        self._paused_for_speech = False
        self._authenticate()

        logger.info(f"Spotify controller initialised (target device: '{self.device_name}')")

    def _authenticate(self):
        """Authenticate with Spotify (uses cached token if available)"""
        token_info = self.sp_oauth.get_cached_token()

        if not token_info:
            logger.warning("No cached token found. Run authenticate_first_time() to get one.")
            return

        self.sp = spotipy.Spotify(auth=token_info['access_token'])
        logger.info("Authenticated with Spotify using cached token")

    def authenticate_first_time(self):
        """
        First-time authentication - prints a URL for the user to visit.
        Run this once to get the initial token, after which tokens auto-refresh.

        The redirect page is expected to fail to load; only the URL it redirects
        to matters, since that carries the authorisation code.
        """
        auth_url = self.sp_oauth.get_authorize_url()
        print("\nVisit this URL to authorise the assistant with Spotify:")
        print(f"{auth_url}\n")
        print(f"After authorising, you'll be redirected to {self.redirect_uri}")
        print("The page will likely say 'This site can't be reached' - that's expected.")
        print("Copy the FULL URL from your browser's address bar and paste it here:\n")

        redirect_response = input("Paste the full redirect URL here: ").strip()

        # Extract the authorization code and get the token
        code = self.sp_oauth.parse_response_code(redirect_response)
        self.sp_oauth.get_access_token(code)

        self._authenticate()
        print("Authentication successful! The token is cached for future runs.")

    def _find_device(self) -> Optional[str]:
        """
        Looks for this machine's Spotify Connect device in the account's device list.

        Returns:
            The device's ID, or None if it isn't currently advertising itself
            (e.g. the raspotify service isn't running).
        """
        devices = self.sp.devices().get("devices", [])

        # Matched case-insensitively, since the name is typed into config.yaml by hand.
        for device in devices:
            if device["name"].lower() == self.device_name.lower():
                return device["id"]

        # Logs what was actually available, which is the first thing worth knowing
        # when the assistant can't find its own speaker.
        available = ", ".join(d["name"] for d in devices) or "none"
        logger.warning(f"Connect device '{self.device_name}' not found. Available devices: {available}")
        return None

    def _activate_device(self) -> Optional[str]:
        """
        Finds this machine's Connect device and transfers playback to it if the
        music is currently coming out of something else.

        Returns:
            The device's ID, or None if it isn't available.
        """
        device_id = self._find_device()
        if not device_id:
            return None

        try:
            # Only transfers if playback is on a different device, to avoid an
            # unnecessary API call (and a brief audio stutter) every time.
            current = self.sp.current_playback()
            if current and current.get("device", {}).get("id") != device_id:
                logger.info(f"Transferring playback to '{self.device_name}'")
                self.sp.transfer_playback(device_id=device_id, force_play=False)
        except Exception as e:
            # A failed transfer isn't fatal - playback can still be started
            # directly on the device below.
            logger.warning(f"Could not transfer playback: {e}")

        return device_id

    def play(self, query: Optional[str] = None) -> Dict:
        """
        Play music on this machine's Connect device.

        Args:
            query: Song or artist to search for. Resumes playback if omitted.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            device_id = self._activate_device()
            if not device_id:
                return {"success": False,
                        "message": f"I can't find the {self.device_name} speaker. Is Spotify Connect running?"}

            # "spotify" on its own isn't a search term, it's just the user naming the app.
            if query and query.lower() != "spotify":
                results = self.sp.search(q=query, limit=1, type='track')
                items = results.get('tracks', {}).get('items', [])

                if not items:
                    return {"success": False, "message": f"Couldn't find '{query}'"}

                track = items[0]
                self.sp.start_playback(device_id=device_id, uris=[track['uri']])
                track_name = track['name']
                artist = track['artists'][0]['name']
                logger.info(f"Playing: {track_name} by {artist}")
                return {"success": True, "message": f"Playing {track_name} by {artist}"}

            # No search term, so resume whatever was queued on the device.
            self.sp.start_playback(device_id=device_id)
            logger.info("Resumed playback")
            return {"success": True, "message": "Resumed playback"}

        except Exception as e:
            logger.error(f"Play failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't start the music."}

    def pause(self) -> Dict:
        """Pause playback"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            self.sp.pause_playback()
            logger.info("Paused playback")
            return {"success": True, "message": "Paused"}
        except Exception as e:
            logger.error(f"Pause failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't pause the music."}

    def skip(self) -> Dict:
        """Skip to next track"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            self.sp.next_track()
            logger.info("Skipped to next track")
            return {"success": True, "message": "Skipped"}
        except Exception as e:
            logger.error(f"Skip failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't skip the track."}

    def previous(self) -> Dict:
        """Go to previous track"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            self.sp.previous_track()
            logger.info("Went to previous track")
            return {"success": True, "message": "Previous track"}
        except Exception as e:
            logger.error(f"Previous failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't go back a track."}

    def current_track(self) -> Dict:
        """Get currently playing track info"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            current = self.sp.current_playback()
            if current and current.get('is_playing'):
                track = current['item']['name']
                artist = current['item']['artists'][0]['name']
                return {"success": True, "track": track, "artist": artist}
            return {"success": False, "message": "Nothing playing"}
        except Exception as e:
            logger.error(f"Current track failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't check what's playing."}

    def pause_for_speech(self):
        """
        Pauses playback so the assistant's reply can be heard over the music.

        Only pauses if something is actually playing, and remembers that it did,
        so resume_after_speech() won't start music the user had already stopped.
        """
        if not self.sp or not self.pause_while_speaking:
            return

        try:
            current = self.sp.current_playback()
            if current and current.get("is_playing"):
                self.sp.pause_playback()
                self._paused_for_speech = True
                logger.info("Paused Spotify while speaking")
        except Exception as e:
            # Never let this stop the assistant from replying.
            logger.warning(f"Could not pause for speech: {e}")

    def resume_after_speech(self):
        """Resumes playback, but only if pause_for_speech() was what paused it."""
        if not self.sp or not self._paused_for_speech:
            return

        try:
            self.sp.start_playback()
            logger.info("Resumed Spotify after speaking")
        except Exception as e:
            logger.warning(f"Could not resume after speech: {e}")
        finally:
            # Cleared either way, so a failed resume doesn't leave the flag stuck on.
            self._paused_for_speech = False


# Runs a first-time authentication if this file is executed directly.
# This is the one-off step needed before the assistant can control Spotify.
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Allows this file to be run directly by putting the project
    # root on the path before importing from the package.
    import sys
    sys.path.insert(0, PROJECT_DIRECTORY)
    from voice_assistant.config import Config

    # Config is passed in for the device name.
    controller = SpotifyController(config=Config())

    # If there's no cached token yet, walk the user through authorising.
    if not controller.sp:
        controller.authenticate_first_time()
    else:
        print("\nAlready authenticated - nothing to do.")

    # Reports which devices Spotify can currently see, which is the quickest way
    # to check the Connect endpoint on this machine is running and named correctly.
    if controller.sp:
        devices = controller.sp.devices().get("devices", [])
        print("\nSpotify devices visible to this account:")
        for device in devices:
            marker = " <- target" if device["name"].lower() == controller.device_name.lower() else ""
            print(f"  {device['name']} ({device['type']}){marker}")
        if not devices:
            print("  none - start Spotify somewhere, or check the Connect service is running")
