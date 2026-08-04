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
REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080/callback")

ENABLE_ARCHIVE_BACKUP = os.environ.get("ENABLE_ARCHIVE_BACKUP", "true").lower() == "true"
ARCHIVE_PLAYLIST_NAME = "FreshList Archive"

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
    "image_url": None,
    "current_playlist": "None",
    "is_authenticated": False,
    "last_cleaned": "None",
    "removed_count": 0,
    "removal_history": []  # List of removed tracks with rich metadata
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


def get_or_create_archive_playlist(sp, user_id):
    try:
        user_playlists = sp.current_user_playlists()
        for playlist in user_playlists.get("items", []):
            if playlist["name"].lower() == ARCHIVE_PLAYLIST_NAME.lower():
                return playlist["id"]
        
        new_playlist = sp.user_playlist_create(
            user=user_id,
            name=ARCHIVE_PLAYLIST_NAME,
            public=False,
            description="Automated backup playlist created by FreshList-AI"
        )
        return new_playlist["id"]
    except Exception as e:
        print(f"[Archive Error] {e}")
        return None


# --- PLAYLIST CLEANUP FUNCTION ---
def cleanup_from_active_playlist(sp, playlist_id, current_track_uri, track_name, artist_name, image_url, track_url):
    try:
        playlist_info = sp.playlist(playlist_id, fields="name")
        playlist_name = playlist_info.get("name", "Active Playlist")
        playback_state["current_playlist"] = playlist_name

        if playlist_name.lower() == ARCHIVE_PLAYLIST_NAME.lower():
            return

        if ENABLE_ARCHIVE_BACKUP:
            user_id = sp.current_user()["id"]
            archive_id = get_or_create_archive_playlist(sp, user_id)
            if archive_id:
                sp.playlist_add_items(playlist_id=archive_id, items=[current_track_uri])

        # Remove track from active playlist
        sp.playlist_remove_all_occurrences_of_items(
            playlist_id=playlist_id,
            items=[current_track_uri]
        )

        timestamp = time.strftime("%H:%M:%S")
        playback_state["last_cleaned"] = timestamp
        playback_state["removed_count"] += 1

        # Store full track record for dashboard display
        removal_entry = {
            "track": track_name,
            "artist": artist_name,
            "playlist": playlist_name,
            "image_url": image_url,
            "track_url": track_url,
            "time": timestamp
        }
        playback_state["removal_history"].insert(0, removal_entry)
        playback_state["removal_history"] = playback_state["removal_history"][:15]

        print(f"[FreshList-AI] Successfully removed '{track_name}' from {playlist_name}")
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
                    
                    # Extract album art thumbnail and Spotify web link
                    images = track_item.get("album", {}).get("images", [])
                    image_url = images[0]["url"] if images else None
                    track_url = track_item.get("external_urls", {}).get("spotify", "#")

                    playback_state["track"] = track_name
                    playback_state["artist"] = artist_name
                    playback_state["image_url"] = image_url
                    playback_state["status"] = "Playing"

                    context = current.get("context")
                    if context and context.get("type") == "playlist":
                        playlist_uri = context.get("uri")
                        playlist_id = playlist_uri.split(":")[-1]

                        if track_uri != last_processed_track_uri:
                            cleanup_from_active_playlist(
                                sp=sp,
                                playlist_id=playlist_id,
                                current_track_uri=track_uri,
                                track_name=track_name,
                                artist_name=artist_name,
                                image_url=image_url,
                                track_url=track_url
                            )
                            last_processed_track_uri = track_uri
                    else:
                        playback_state["current_playlist"] = "Not playing from a playlist"

                else:
                    playback_state["track"] = "No track playing"
                    playback_state["artist"] = "-"
                    playback_state["image_url"] = None
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
