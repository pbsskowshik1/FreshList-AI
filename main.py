import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ==================== CONFIGURATION ====================
CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID", "YOUR_CLIENT_ID_HERE")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "YOUR_CLIENT_SECRET_HERE")
REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

SCOPES = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-modify-public "
    "playlist-modify-private "
    "playlist-read-private "
    "playlist-read-collaborative"
)

POLL_INTERVAL_SECONDS = 3
SKIP_THRESHOLD_SECONDS = 8
MAX_AGE_YEARS = 3


# ==================== DUMMY WEB SERVER (FOR RENDER) ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP server to pass Render's health checks."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"FreshList-AI Worker is running 24/7!")

    def log_message(self, format, *args):
        # Silence standard HTTP logs to keep Render console clean
        return


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"[Web Server] Health-check listener running on port {port}")
    server.serve_forever()


# ==================== SPOTIFY BACKGROUND AGENT ====================
def get_spotify_client():
    auth_manager = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_path=".cache-spotify_cleanup",
        open_browser=False,  # Set to False for headless cloud execution
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def spotify_agent_loop():
    print("[Spotify Agent] Connecting to Spotify...")
    sp = get_spotify_client()
    print("[Spotify Agent] Watching playback...")

    current_track_id = None
    current_track_uri = None
    current_track_name = None
    current_playlist_id = None
    current_track_duration_ms = None
    last_seen_progress_ms = 0

    while True:
        try:
            playback = sp.current_playback()
            if not playback or not playback.get("item"):
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            item = playback["item"]
            track_id = item["id"]
            track_uri = item["uri"]
            track_name = item["name"]
            artists = ", ".join(a["name"] for a in item["artists"])
            duration_ms = item["duration_ms"]
            progress_ms = playback.get("progress_ms", 0) or 0
            is_playing = playback.get("is_playing", False)

            context = playback.get("context") or {}
            playlist_id = None
            if context.get("type") == "playlist" and context.get("uri"):
                playlist_id = context["uri"].split(":")[-1]

            # Detect track change
            if track_id != current_track_id:
                # 1. Evaluate previous track for early skip
                if current_track_id is not None and current_playlist_id:
                    skipped_early = (
                        last_seen_progress_ms < SKIP_THRESHOLD_SECONDS * 1000
                        and current_track_duration_ms
                        and current_track_duration_ms > SKIP_THRESHOLD_SECONDS * 1000 * 3
                    )
                    if skipped_early:
                        print(f"[Skip Detected] '{current_track_name}' left early. Removing from playlist...")
                        try:
                            sp.playlist_remove_all_occurrences_of_items(current_playlist_id, [current_track_uri])
                        except Exception as e:
                            print(f"Failed to remove track: {e}")

                # 2. Evaluate new track for age limit
                release_date_str = item["album"].get("release_date", "")
                if release_date_str and playlist_id:
                    release_year = int(release_date_str.split("-")[0])
                    current_year = time.localtime().tm_year
                    if (current_year - release_year) >= MAX_AGE_YEARS:
                        print(f"[Age Flag] '{track_name}' is {current_year - release_year}y old. Removing...")
                        try:
                            sp.playlist_remove_all_occurrences_of_items(playlist_id, [track_uri])
                        except Exception as e:
                            print(f"Failed to remove track: {e}")

                current_track_id = track_id
                current_track_uri = track_uri
                current_track_name = track_name
                current_playlist_id = playlist_id
                current_track_duration_ms = duration_ms

            last_seen_progress_ms = progress_ms if is_playing else last_seen_progress_ms

        except Exception as e:
            print(f"[Agent Error] {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


# ==================== ENTRY POINT ====================
if __name__ == "__main__":
    # Start the HTTP server in a separate thread for Render
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # Run the Spotify polling agent on the main thread
    spotify_agent_loop()
