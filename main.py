import os
import threading
import time
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import uvicorn

# --- CONFIGURATION ---
CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID", "YOUR_CLIENT_ID")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "https://freshlist-ai.onrender.com")

SCOPES = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "playlist-modify-public "
    "playlist-modify-private "
    "playlist-read-private "
    "playlist-read-collaborative"
)

CACHE_PATH = ".cache-spotify_cleanup"

# --- GLOBAL STATE ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")

playback_state = {
    "status": "Initializing...",
    "track": "None",
    "artist": "None",
    "current_playlist": "None",
    "is_authenticated": False,
    "last_cleaned": "None",
    "removed_count": 0,
}


def get_auth_manager():
    cache_data = os.environ.get("SPOTIPY_CACHE")
    if cache_data and not os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "w") as f:
            f.write(cache_data.strip())

    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=False,
    )


# --- PLAYLIST CLEANUP FUNCTION ---
def cleanup_from_active_playlist(sp, playlist_id, current_track_uri):
    """Removes the playing track from the current active playlist."""
    try:
        # Fetch playlist metadata to display the name on dashboard
        playlist_info = sp.playlist(playlist_id, fields="name")
        playlist_name = playlist_info.get("name", "Active Playlist")
        playback_state["current_playlist"] = playlist_name

        # Remove track from active playlist
        sp.playlist_remove_all_occurrences_of_items(
            playlist_id=playlist_id,
            items=[current_track_uri]
        )
        
        playback_state["last_cleaned"] = time.strftime("%H:%M:%S UTC")
        playback_state["removed_count"] += 1
        print(f"[FreshList-AI] Successfully removed {current_track_uri} from {playlist_name}")
    except Exception as e:
        print(f"[Cleanup Error] {e}")


# --- SPOTIFY BACKGROUND WORKER ---
def spotify_agent_loop():
    global playback_state
    last_processed_track_uri = None

    while True:
        try:
            auth_manager = get_auth_manager()
            token_info = auth_manager.get_cached_token()

            if token_info and auth_manager.validate_token(token_info):
                sp = spotipy.Spotify(auth_manager=auth_manager)
                playback_state["is_authenticated"] = True

                current = sp.current_user_playing_track()
                if current and current.get("is_playing"):
                    track_item = current["item"]
                    track_name = track_item["name"]
                    artist_name = track_item["artists"][0]["name"]
                    track_uri = track_item["uri"]

                    playback_state["track"] = track_name
                    playback_state["artist"] = artist_name
                    playback_state["status"] = "Playing"

                    # Check context to see if playback is coming from a playlist
                    context = current.get("context")
                    if context and context.get("type") == "playlist":
                        # URI format: spotify:playlist:<PLAYLIST_ID>
                        playlist_uri = context.get("uri")
                        playlist_id = playlist_uri.split(":")[-1]

                        # Clean up only once per track change
                        if track_uri != last_processed_track_uri:
                            cleanup_from_active_playlist(sp, playlist_id, track_uri)
                            last_processed_track_uri = track_uri
                    else:
                        playback_state["current_playlist"] = "Not playing from a playlist"

                else:
                    playback_state["track"] = "No track playing"
                    playback_state["artist"] = "-"
                    playback_state["current_playlist"] = "None"
                    playback_state["status"] = "Idle"
            else:
                playback_state["is_authenticated"] = False
                playback_state["status"] = "Action Required: Login Needed"

        except Exception as e:
            playback_state["status"] = f"Error: {str(e)}"

        time.sleep(10)


# --- FASTAPI WEB ROUTES ---
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"state": playback_state}
    )


@app.get("/login")
async def login():
    auth_manager = get_auth_manager()
    return RedirectResponse(auth_manager.get_authorize_url())


@app.get("/callback")
async def callback(code: str):
    auth_manager = get_auth_manager()
    auth_manager.get_access_token(code)
    playback_state["is_authenticated"] = True
    return RedirectResponse("/")


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- ENTRY POINT ---
if __name__ == "__main__":
    agent_thread = threading.Thread(target=spotify_agent_loop, daemon=True)
    agent_thread.start()

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
