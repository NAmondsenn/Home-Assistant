"""
Spotify Controller Module
Handles authentication and playback control via the Spotify Web API.

Playback is targeted at this machine's own Spotify Connect device (installed by
setup.sh as raspotify), so the assistant behaves like a standalone speaker rather
than remote-controlling whichever phone or laptop Spotify happened to list first.
"""

import os
import re
import difflib
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

        # Scopes needed for playback control.
        # playlist-read scopes are needed to look up the user's own playlists by name.
        scope = ("user-read-playback-state,user-modify-playback-state,"
                 "user-read-currently-playing,playlist-read-private,"
                 "playlist-read-collaborative")

        self.sp_oauth = SpotifyOAuth(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            scope=scope,
            cache_path=os.path.join(PROJECT_DIRECTORY, ".spotify_cache")
        )

        self.sp = None
        # Tracks whether the assistant paused the music itself,
        # only resuming playback it actually interrupted.
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

        # Extract the authorisation code and get the token
        code = self.sp_oauth.parse_response_code(redirect_response)
        self.sp_oauth.get_access_token(code, as_dict=False)

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

    def play(self, query: Optional[str] = None, search_type: Optional[str] = None) -> Dict:
        """
        Play music on this machine's Connect device.

        Args:
            query: Song or artist to search for. Resumes playback if omitted.
            search_type: 'track', 'artist' or 'playlist', when the caller knows
                         which the query refers to. Optional.

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
                return self._search_and_play(query, device_id, search_type)

            # No search term, so resume whatever was queued on the device.
            self.sp.start_playback(device_id=device_id)
            logger.info("Resumed playback")
            return {"success": True, "message": "Resumed playback", "chime": True}

        except Exception as e:
            logger.error(f"Play failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't start the music."}

    @staticmethod
    def _looks_like_match(query: str, *names: str) -> bool:
        """
        Loose check that a search result actually relates to what was asked for.

        Whisper mishears song and artist names fairly often, and Spotify's search
        always returns *something*, so without this an unrecognised query quietly
        plays an unrelated track.

        Args:
            query: What the user asked for.
            names: Track and artist names from the search result.

        Returns:
            True if any word of the query (3+ letters) appears in the result.
        """
        query_words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
        if not query_words:
            return True

        result_words = set()
        for name in names:
            result_words |= set(re.findall(r"[a-z0-9]+", name.lower()))

        return bool(query_words & result_words)

    def _search_and_play(self, query: str, device_id: str, search_type: Optional[str] = None) -> Dict:
        """
        Searches for what the user asked for and starts playing it.

        Playback is started with a context (a playlist, an artist's catalogue, or the
        album a track belongs to) rather than a single track URI, so the music keeps
        going instead of stopping dead at the end of one song.

        Args:
            query: Song, artist, genre, or "song by artist".
            device_id: The Connect device to play on.
            search_type: 'track', 'artist' or 'playlist' when known, which avoids
                         having to guess what kind of thing the query names.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        # A genre or mood ("something chill") is best served by an existing playlist.
        if search_type == "playlist":
            playlists = self.sp.search(q=query, limit=1, type='playlist').get('playlists', {}).get('items', [])
            playlists = [p for p in playlists if p]
            if playlists:
                self.sp.start_playback(device_id=device_id, context_uri=playlists[0]['uri'])
                logger.info(f"Playing playlist: {playlists[0]['name']}")
                return {"success": True, "message": f"Playing {playlists[0]['name']}"}
        # "song by artist" is turned into Spotify's field filters, which rank the
        # original recording first. As free text, "by" is just noise, and karaoke
        # covers (whose titles contain "by ...") often win instead.
        track_part = artist_part = None
        if " by " in query.lower():
            split_index = query.lower().rindex(" by ")
            track_part = query[:split_index].strip()
            artist_part = query[split_index + 4:].strip()

        # An artist name on its own ("play Drake") should play that artist rather
        # than one arbitrary song of theirs. An exact name match is required unless
        # the caller has already identified the query as an artist.
        if not track_part:
            artists = self.sp.search(q=query, limit=1, type='artist').get('artists', {}).get('items', [])
            if artists and (search_type == "artist" or artists[0]['name'].lower() == query.lower()):
                artist = artists[0]
                self.sp.start_playback(device_id=device_id, context_uri=artist['uri'])
                logger.info(f"Playing artist: {artist['name']}")
                return {"success": True, "message": f"Playing {artist['name']}"}

        search_query = f'track:"{track_part}" artist:"{artist_part}"' if track_part and artist_part else query
        items = self.sp.search(q=search_query, limit=1, type='track').get('tracks', {}).get('items', [])

        # Falls back to a plain text search if the strict track / artist search
        # found nothing, e.g. for titles which contain "by" themselves.
        if not items and search_query != query:
            items = self.sp.search(q=query, limit=1, type='track').get('tracks', {}).get('items', [])

        if not items:
            return {"success": False, "message": f"Sorry, I couldn't find {query}."}

        track = items[0]
        track_name = track['name']
        artist_name = track['artists'][0]['name']

        # Rejects results which have nothing in common with the query, rather than
        # playing something random when the query was misheard.
        if not self._looks_like_match(query, track_name, artist_name):
            logger.info(f"Rejected poor match for '{query}': {track_name} by {artist_name}")
            return {"success": False, "message": f"Sorry, I couldn't find {query}."}

        # Starts from the track within its album, so playback continues afterwards.
        album_uri = track.get('album', {}).get('uri')
        if album_uri:
            self.sp.start_playback(device_id=device_id, context_uri=album_uri,
                                   offset={"uri": track['uri']})
        else:
            self.sp.start_playback(device_id=device_id, uris=[track['uri']])

        logger.info(f"Playing: {track_name} by {artist_name}")
        return {"success": True, "message": f"Playing {track_name} by {artist_name}"}

    def pause(self) -> Dict:
        """Pause playback"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            self.sp.pause_playback()
            logger.info("Paused playback")
            # The message is only used when the chime is turned off.
            return {"success": True, "message": "Paused", "chime": True}
        except Exception as e:
            logger.error(f"Pause failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't pause the music."}

    def skip(self) -> Dict:
        """Skip to the next track"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            self.sp.next_track()
            logger.info("Skipped to next track")
            return {"success": True, "message": "That song was shit anyway"}
        except Exception as e:
            logger.error(f"Skip failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't skip the track."}

    def previous(self) -> Dict:
        """Go back to the previous track."""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            # With the player at position 0, "previous" steps back a track rather
            # than restarting the current one.
            try:
                self.sp.seek_track(0)
            except Exception as e:
                logger.warning(f"Could not seek before going back: {e}")

            self.sp.previous_track()
            logger.info("Went to previous track")
            return {"success": True, "message": "Previous track", "chime": True}
        except Exception as e:
            logger.error(f"Previous failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't go back a track."}

    def get_volume(self) -> Optional[int]:
        """
        The Connect device's current volume, as a percentage.

        Returns:
            The volume, or None if it can't be read (nothing playing, or the
            device doesn't report one).
        """
        if not self.sp:
            return None

        try:
            current = self.sp.current_playback()
            if current:
                return current.get("device", {}).get("volume_percent")
        except Exception as e:
            logger.warning(f"Could not read the Spotify volume: {e}")
        return None

    def set_volume(self, percent: int) -> Dict:
        """
        Set the volume of the Connect device.

        Called when the assistant's general volume changes, so music and speech
        stay in proportion rather than being adjusted separately.

        Args:
            percent: Volume from 0 to 100.

        Returns:
            Dict with 'success' and a 'message'.
        """
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        percent = max(0, min(100, int(percent)))

        try:
            device_id = self._find_device()
            # Falls back to whatever is active, so the volume still applies if the
            # assistant's own speaker isn't currently available.
            self.sp.volume(percent, device_id=device_id)
            logger.info(f"Spotify volume set to {percent}%")
            return {"success": True, "message": f"Volume {percent} percent."}
        except Exception as e:
            # Not being able to set the volume shouldn't fail whatever asked for it.
            logger.warning(f"Could not set Spotify volume: {e}")
            return {"success": False, "message": "Sorry, I couldn't change the music volume."}

    def restart(self) -> Dict:
        """Start the current track again from the beginning."""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            self.sp.seek_track(0)
            logger.info("Restarted the current track")
            return {"success": True, "message": "Starting it again", "chime": True}
        except Exception as e:
            logger.error(f"Restart failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't restart the track."}

    def repeat(self, mode: str) -> Dict:
        """
        Set the repeat mode.

        Args:
            mode: 'off', 'track' to repeat the current song, or 'all' / 'context'
                  to repeat the playlist or album.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        # Spotify calls repeating the whole playlist / album "context".
        api_mode = "context" if mode in ("all", "context", "playlist", "album") else mode
        if api_mode not in ("off", "track", "context"):
            return {"success": False, "message": f"I don't know how to repeat '{mode}'."}

        try:
            self.sp.repeat(api_mode)
            logger.info(f"Repeat set to {api_mode}")
            messages = {"off": "Repeat off", "track": "Repeating this track",
                        "context": "Repeating everything"}
            return {"success": True, "message": messages[api_mode], "chime": True}
        except Exception as e:
            logger.error(f"Repeat failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't change repeat."}

    def shuffle(self, enabled: bool) -> Dict:
        """
        Turn shuffle on or off.

        Args:
            enabled: True to shuffle, False to play in order.

        Returns:
            Dict with 'success' and a spoken 'message'.
        """
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            self.sp.shuffle(enabled)
            logger.info(f"Shuffle set to {enabled}")
            return {"success": True, "message": "Shuffle on" if enabled else "Shuffle off", "chime": True}
        except Exception as e:
            logger.error(f"Shuffle failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't change shuffle."}

    def _find_playlist(self, name: str) -> Optional[Dict]:
        """
        Finds one of the user's own playlists by name.

        Matching is deliberately loose, since the name comes from speech: an exact
        match wins, then one name containing the other, then the closest match by
        similarity. Comparing against the user's own few dozen playlists is far
        more forgiving than searching all of Spotify.

        Args:
            name: The playlist name as the user said it.

        Returns:
            The playlist dict, or None if nothing matched closely enough.
        """
        # Pages through the user's playlists, since the API returns 50 at a time.
        playlists = []
        offset = 0
        while True:
            page = self.sp.current_user_playlists(limit=50, offset=offset)
            items = [p for p in page.get("items", []) if p]
            playlists.extend(items)
            if len(items) < 50:
                break
            offset += 50

        wanted = name.strip().lower()

        for playlist in playlists:
            if playlist["name"].lower() == wanted:
                return playlist

        for playlist in playlists:
            playlist_name = playlist["name"].lower()
            if wanted in playlist_name or playlist_name in wanted:
                return playlist

        # Falls back to the closest name, as long as it's a reasonable match.
        best, best_score = None, 0.0
        for playlist in playlists:
            score = difflib.SequenceMatcher(None, wanted, playlist["name"].lower()).ratio()
            if score > best_score:
                best, best_score = playlist, score

        return best if best_score >= 0.6 else None

    def play_playlist(self, name: str) -> Dict:
        """
        Play one of the user's own playlists.

        Args:
            name: The playlist name as the user said it.

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

            playlist = self._find_playlist(name)
            if not playlist:
                return {"success": False, "message": f"I couldn't find a playlist called {name}."}

            self.sp.start_playback(device_id=device_id, context_uri=playlist["uri"])
            logger.info(f"Playing playlist: {playlist['name']}")
            return {"success": True, "message": f"Playing {playlist['name']}"}
        except Exception as e:
            logger.error(f"Playlist playback failed: {e}")
            return {"success": False, "message": "Sorry, I couldn't play that playlist."}

    def current_track(self) -> Dict:
        """Retrieves current track information."""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}

        try:
            current = self.sp.current_playback()
            # Checks for a loaded track rather than is_playing, since the assistant
            # pauses the music while it listens - so at this point what the user is
            # asking about is always paused.
            if current and current.get('item'):
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

        Only pauses if something is actually playing and remembers that it did,
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

    def cancel_resume(self):
        """
        Stops resume_after_speech() from restarting playback. Used when the user
        explicitly asked for the music to pause, which would otherwise be undone
        by the automatic resume at the end of the interaction.
        """
        self._paused_for_speech = False

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
