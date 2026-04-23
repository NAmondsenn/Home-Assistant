"""
Spotify Controller for Jarvis
Handles authentication and playback control via Spotify Web API
"""

import os
import logging
from typing import Optional, Dict
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()
logger = logging.getLogger(__name__)


class SpotifyController:
    def __init__(self):
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
            cache_path=os.path.expanduser("~/.spotify_cache")
        )
        
        self.sp = None
        self._authenticate()
        
        logger.info("Spotify controller initialized")
    
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
        First-time authentication - prints URL for user to visit
        Run this once to get initial token, then tokens auto-refresh
        """
        auth_url = self.sp_oauth.get_authorize_url()
        print(f"\n🔗 Visit this URL to authorize Jarvis with Spotify:")
        print(f"{auth_url}\n")
        print(f"After authorizing, you'll be redirected to {self.redirect_uri}")
        print(f"The page will likely say 'This site can't be reached' - that's OK!")
        print(f"Copy the FULL URL from your browser's address bar and paste it here:\n")
        
        redirect_response = input("Paste the full redirect URL here: ").strip()
        
        # Extract the authorization code and get the token
        code = self.sp_oauth.parse_response_code(redirect_response)
        token_info = self.sp_oauth.get_access_token(code)
        
        self.sp = spotipy.Spotify(auth=token_info['access_token'])
        logger.info("✅ Authentication successful! Token cached for future use.")
        print("✅ Authentication successful!")
    
    def play(self, query: Optional[str] = None):
        """Play music (resume if no query, search and play if query provided)"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}
        
        try:
            if query:
                # Search for track
                results = self.sp.search(q=query, limit=1, type='track')
                if results['tracks']['items']:
                    track_uri = results['tracks']['items'][0]['uri']
                    self.sp.start_playback(uris=[track_uri])
                    track_name = results['tracks']['items'][0]['name']
                    artist = results['tracks']['items'][0]['artists'][0]['name']
                    logger.info(f"Playing: {track_name} by {artist}")
                    return {"success": True, "message": f"Playing {track_name} by {artist}"}
                else:
                    return {"success": False, "message": f"Couldn't find '{query}'"}
            else:
                # Resume playback
                self.sp.start_playback()
                logger.info("Resumed playback")
                return {"success": True, "message": "Resumed playback"}
        except Exception as e:
            logger.error(f"Play failed: {e}")
            return {"success": False, "message": str(e)}
    
    def pause(self):
        """Pause playback"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}
        
        try:
            self.sp.pause_playback()
            logger.info("Paused playback")
            return {"success": True, "message": "Paused"}
        except Exception as e:
            logger.error(f"Pause failed: {e}")
            return {"success": False, "message": str(e)}
    
    def skip(self):
        """Skip to next track"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}
        
        try:
            self.sp.next_track()
            logger.info("Skipped to next track")
            return {"success": True, "message": "Skipped"}
        except Exception as e:
            logger.error(f"Skip failed: {e}")
            return {"success": False, "message": str(e)}
    
    def previous(self):
        """Go to previous track"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}
        
        try:
            self.sp.previous_track()
            logger.info("Went to previous track")
            return {"success": True, "message": "Previous track"}
        except Exception as e:
            logger.error(f"Previous failed: {e}")
            return {"success": False, "message": str(e)}
    
    def current_track(self):
        """Get currently playing track info"""
        if not self.sp:
            return {"success": False, "message": "Not authenticated"}
        
        try:
            current = self.sp.current_playback()
            if current and current['is_playing']:
                track = current['item']['name']
                artist = current['item']['artists'][0]['name']
                return {"success": True, "track": track, "artist": artist}
            else:
                return {"success": False, "message": "Nothing playing"}
        except Exception as e:
            logger.error(f"Current track failed: {e}")
            return {"success": False, "message": str(e)}


if __name__ == "__main__":
    # Test authentication
    logging.basicConfig(level=logging.INFO)
    controller = SpotifyController()
    
    # If not authenticated, run first-time auth
    if not controller.sp:
        controller.authenticate_first_time()
    
    # Test playback
    print("\n🎵 Spotify Controller Ready!")
    print("Commands: play [song], pause, skip, previous, current")
