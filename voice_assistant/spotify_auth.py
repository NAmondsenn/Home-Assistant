"""
One-time Spotify authentication with local server
"""

import os
from dotenv import load_dotenv
from flask import Flask, request
import webbrowser
import threading
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

# Resolves paths relative to the project root.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)
auth_code = None

# Spotify OAuth setup
sp_oauth = SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
    scope="user-read-playback-state,user-modify-playback-state,user-read-currently-playing",
    cache_path=os.path.join(PROJECT_DIRECTORY, ".spotify_cache")
)

@app.route('/callback')
def callback():
    global auth_code
    auth_code = request.args.get('code')
    return """
    <html>
        <body>
            <h1>Authentication Successful!</h1>
            <p>You can close this window and return to the terminal.</p>
        </body>
    </html>
    """

def run_server():
    app.run(host='0.0.0.0', port=8888, debug=False)

if __name__ == "__main__":
    # Start server in background
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    # Get auth URL
    auth_url = sp_oauth.get_authorize_url()
    print(f"\nOpening authorization URL in your browser...")
    print(f"{auth_url}\n")
    print("After you authorize, you'll be redirected back and the token will be saved automatically.")
    print("Waiting for callback...\n")
    
    # Wait for callback
    import time
    while auth_code is None:
        time.sleep(1)
    
    # Exchange code for token
    token_info = sp_oauth.get_access_token(auth_code)
    print("\nAuthentication successful! Token saved to the project's .spotify_cache")
    print("You can now use Spotify controller in Jarvis!")
