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
MIN_POPULARITY_SCORE = 10

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

# Persistent in-memory cache for the active playlist's track IDs
PLAYLIST_TRACK_CACHE = {
    "playlist_id": None,
    "track_ids": set()
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


def load_playlist_cache(sp, playlist_id):
    """Loads all existing track IDs into memory using the primary sp.playlist endpoint."""
    global PLAYLIST_TRACK_CACHE

    if PLAYLIST_TRACK_CACHE["playlist_id"] == playlist_id and PLAYLIST_TRACK_CACHE["track_ids"]:
        return PLAYLIST_TRACK_CACHE["track_ids"]

    print(f"[Cache Load] Syncing track list for playlist ID: {playlist_id}")
    track_ids = set()
    try:
        playlist_data = sp.playlist(playlist_id)
        tracks_data = playlist_data.get("tracks", {})
        items = tracks_data.get("items", [])

        while items:
            for item in items:
                if not item:
                    continue
                t = item.get("track")
                if t:
                    tid = t.get("id")
                    if not tid and t.get("uri") and t["uri"].startswith("spotify:track:"):
                        tid = t["uri"].split(":")[-1]
                    if tid:
                        track_ids.add(tid)

            if tracks_data.get("next"):
                tracks_data = sp.next(tracks_data)
                items = tracks_data.get("items", [])
            else:
                break

    except Exception as e:
        print(f"[Cache Load Error] {e}")

    PLAYLIST_TRACK_CACHE["playlist_id"] = playlist_id
    PLAYLIST_TRACK_CACHE["track_ids"] = track_ids
    print(f"[Cache Sync Complete] Found {len(track_ids)} unique tracks in playlist.")
    return track_ids


def process_playing_track(sp, playlist_id, track_item, playlist_name):
    """Evaluates if the current track should be added as a Smart Shuffle recommendation."""
    global PLAYLIST_TRACK_CACHE

    track_uri = track_item.get("uri", "")
    track_name = track_item.get("name", "Unknown Track")
    artist_name = track_item.get("artists", [{}])[0].get("name", "Unknown Artist")

    track_id = track_item.get("id")
    if not track_id and track_uri.startswith("spotify:track:"):
        track_id = track_uri.split(":")[-1]

    if not track_id:
        return

    # Load local cache
    existing_ids = load_playlist_cache(sp, playlist_id)

    # 0. Safety Guard: Skip if cache failed to sync tracks
    if len(existing_ids) == 0:
        print(f"[Cache Warning] Playlist cache returned 0 tracks. Skipping evaluation to prevent duplicate additions.")
        return

    # 1. Exact Duplicate Check
    if track_id in existing_ids:
        print(f"[Skip] '{track_name}' already exists in '{playlist_name}'.")
        playback_state["is_smart_shuffle"] = False
        return

    # 2. Track is NOT in original playlist -> Smart Shuffle recommendation detected
    playback_state["is_smart_shuffle"] = True

    # 3. Resolve Popularity
    popularity = track_item.get("popularity", 0)
    if popularity == 0:
        try:
            full_track = sp.track(track_id)
            popularity = full_track.get("popularity", 0)
        except Exception:
            pass

    if popularity == 0:
        popularity = 50

    if popularity < MIN_POPULARITY_SCORE:
        print(f"[Skip] '{track_name}' popularity ({popularity}) below threshold ({MIN_POPULARITY_SCORE}).")
        return

    # 4. Add to Playlist and update in-memory cache immediately
    try:
        sp.playlist_add_items(playlist_id=playlist_id, items=[track_uri])

        # Add ID directly to local memory so it skips on the next check pass
        PLAYLIST_TRACK_CACHE["track_ids"].add(track_id)

        print(f"🎉 [SUCCESS] Added Smart Shuffle track '{track_name}' by {artist_name} to '{playlist_name}'!")

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
        print(f"[Add Track Error] {e}")


# --- SPOTIFY BACKGROUND WORKER ---
def spotify_agent_loop():
    global playback_state
    last_processed_track_id = None

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
                    if not track_item:
                        time.sleep(5)
                        continue

                    track_uri = track_item.get("uri", "")
                    track_id = track_item.get("id") or (
                        track_uri.split(":")[-1] if track_uri.startswith("spotify:track:") else None
                    )

                    images = track_item.get("album", {}).get("images", [])
                    image_url = images[0]["url"] if images else None

                    playback_state["track"] = track_item.get("name", "Unknown")
                    playback_state["artist"] = track_item.get("artists", [{}])[0].get("name", "Unknown")
                    playback_state["image_url"] = image_url
                    playback_state["status"] = "Playing"

                    context = current.get("context")
                    if context and context.get("type") == "playlist":
                        playlist_uri = context.get("uri")
                        playlist_id = playlist_uri.split(":")[-1]
                        playback_state["active_playlist_id"] = playlist_id

                        # Process when a new track plays
                        if track_id and track_id != last_processed_track_id:
                            playlist_info = sp.playlist(playlist_id, fields="name")
                            playlist_name = playlist_info.get("name", "Active Playlist")
                            playback_state["current_playlist"] = playlist_name

                            if playlist_name.lower() != ARCHIVE_PLAYLIST_NAME.lower():
                                process_playing_track(sp, playlist_id, track_item, playlist_name)

                            last_processed_track_id = track_id
                    else:
                        playback_state["current_playlist"] = "Not playing from a playlist"
                        playback_state["active_playlist_id"] = None
                        playback_state["is_smart_shuffle"] = False

                else:
                    playback_state["track"] = "No track playing"
                    playback_state["artist"] = "-"
                    playback_state["image_url"] = None
                    playback_state["current_playlist"] = "None"
                    playback_state["active_playlist_id"] = None
                    playback_state["status"] = "Idle"
                    playback_state["is_smart_shuffle"] = False
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
