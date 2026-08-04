import os
import threading
import time
from datetime import datetime
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

ENABLE_ARCHIVE_BACKUP = os.environ.get("ENABLE_ARCHIVE_BACKUP", "true").lower() == "true"
ARCHIVE_PLAYLIST_NAME = "FreshList Archive"
MAX_TRACK_AGE_YEARS = 3
MIN_POPULARITY_SCORE = 20

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
    "active_playlist_id": None,
    "is_smart_shuffle": False,
    "is_authenticated": False,
    "last_cleaned": "None",
    "removed_count": 0,
    "removal_history": [],
    "added_history": []
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


def get_playlist_track_uris(sp, playlist_id):
    """Fetches all track URIs currently inside the active playlist."""
    track_uris = set()
    try:
        results = sp.playlist_items(playlist_id, fields="items(track(uri)),next")
        while results:
            for item in results.get("items", []):
                t = item.get("track")
                if t and t.get("uri"):
                    track_uris.add(t["uri"])
            if results.get("next"):
                results = sp.next(results)
            else:
                break
    except Exception as e:
        print(f"[Playlist Fetch Error] {e}")
    return track_uris


def handle_smart_shuffle_track(sp, playlist_id, track_item, playlist_name):
    """Appends Smart Shuffle track ONLY if it's genuinely missing from the playlist."""
    track_id = track_item.get("id")
    track_uri = track_item["uri"]
    track_name = track_item["name"]
    artist_name = track_item["artists"][0]["name"]

    # 1. Check if track is already in playlist
    existing_uris = get_playlist_track_uris(sp, playlist_id)
    if track_uri in existing_uris:
        print(f"[Smart Shuffle Skip] Track '{track_name}' is already in playlist '{playlist_name}'.")
        return

    # 2. Check real popularity
    popularity = track_item.get("popularity", 0)
    if popularity == 0 and track_id:
        try:
            full_track = sp.track(track_id)
            popularity = full_track.get("popularity", 0)
        except Exception:
            pass

    if popularity < MIN_POPULARITY_SCORE:
        print(f"[Smart Shuffle Skip] Track '{track_name}' popularity ({popularity}) below threshold ({MIN_POPULARITY_SCORE}).")
        return

    # 3. Add genuine new Smart Shuffle track to playlist
    try:
        sp.playlist_add_items(playlist_id=playlist_id, items=[track_uri])
        print(f"🎉 [Smart Shuffle SUCCESS] Added '{track_name}' by {artist_name} to '{playlist_name}'!")

        timestamp = time.strftime("%H:%M:%S")
        images = track_item.get("album", {}).get("images", [])
        image_url = images[0]["url"] if images else None
        track_url = track_item.get("external_urls", {}).get("spotify", "#")

        entry = {
            "track": track_name,
            "artist": artist_name,
            "playlist": playlist_name,
            "image_url": image_url,
            "track_url": track_url,
            "time": timestamp
        }

        playback_state["added_history"].insert(0, entry)
        playback_state["added_history"] = playback_state["added_history"][:20]

    except Exception as e:
        print(f"[Smart Shuffle Add Error] {e}")


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
                    track_item = current.get("item")
                    if not track_item or not track_item.get("id"):
                        time.sleep(5)
                        continue

                    track_uri = track_item["uri"]
                    images = track_item.get("album", {}).get("images", [])
                    image_url = images[0]["url"] if images else None

                    playback_state["track"] = track_item["name"]
                    playback_state["artist"] = track_item["artists"][0]["name"]
                    playback_state["image_url"] = image_url
                    playback_state["status"] = "Playing"

                    # Strict Smart Shuffle Detection Logic
                    shuffle_state = current.get("shuffle_state", False)
                    smart_shuffle = (
                        current.get("smart_shuffle", False) or 
                        current.get("is_smart_shuffle", False) or 
                        (shuffle_state and "recommendation" in str(current).lower())
                    )
                    playback_state["is_smart_shuffle"] = smart_shuffle

                    context = current.get("context")
                    if context and context.get("type") == "playlist":
                        playlist_uri = context.get("uri")
                        playlist_id = playlist_uri.split(":")[-1]
                        playback_state["active_playlist_id"] = playlist_id

                        if track_uri != last_processed_track_uri:
                            playlist_info = sp.playlist(playlist_id, fields="name")
                            playlist_name = playlist_info.get("name", "Active Playlist")
                            playback_state["current_playlist"] = playlist_name

                            # ONLY run insertion if Smart Shuffle is actively detected AND not the Archive playlist
                            if smart_shuffle and playlist_name.lower() != ARCHIVE_PLAYLIST_NAME.lower():
                                handle_smart_shuffle_track(sp, playlist_id, track_item, playlist_name)

                            last_processed_track_uri = track_uri
                    else:
                        playback_state["current_playlist"] = "Not playing from a playlist"
                        playback_state["active_playlist_id"] = None

                else:
                    playback_state["track"] = "No track playing"
                    playback_state["artist"] = "-"
                    playback_state["image_url"] = None
                    playback_state["current_playlist"] = "None"
                    playback_state["active_playlist_id"] = None
                    playback_state["status"] = "Idle"
            else:
                playback_state["is_authenticated"] = False
                playback_state["status"] = "Action Required: Login Needed"

        except Exception as e:
            print(f"[Worker Exception] {e}")

        time.sleep(5)


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
    auth_manager.get_access_token(code, as_dict=False)
    playback_state["is_authenticated"] = True
    return RedirectResponse("/")


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    agent_thread = threading.Thread(target=spotify_agent_loop, daemon=True)
    agent_thread.start()

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
