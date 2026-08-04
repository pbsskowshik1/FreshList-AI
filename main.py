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
MAX_TRACK_AGE_YEARS = 3  # Only remove tracks released 3+ years ago

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
    "is_authenticated": False,
    "last_cleaned": "None",
    "removed_count": 0,
    "removal_history": []
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


def parse_release_date(release_date_str):
    """Parses Spotify release date strings ('YYYY', 'YYYY-MM', or 'YYYY-MM-DD')."""
    try:
        parts = release_date_str.split("-")
        if len(parts) == 1:
            return datetime.strptime(release_date_str, "%Y")
        elif len(parts) == 2:
            return datetime.strptime(release_date_str, "%Y-%m")
        elif len(parts) == 3:
            return datetime.strptime(release_date_str, "%Y-%m-%d")
    except Exception:
        pass
    return datetime.now()


def get_similar_track(sp, seed_track_id, seed_artist_id):
    """Fetches a similar track using Spotify Recommendations API."""
    try:
        recs = sp.recommendations(seed_tracks=[seed_track_id], seed_artists=[seed_artist_id], limit=5)
        tracks = recs.get("tracks", [])
        if tracks:
            return tracks[0]
    except Exception as e:
        print(f"[Recommendation Error] {e}")
    return None


def is_track_older_than_limit(track_item, max_years=MAX_TRACK_AGE_YEARS):
    """Returns True if track release date is older than max_years."""
    release_date_str = track_item.get("album", {}).get("release_date", "")
    if not release_date_str:
        return False
    
    release_date = parse_release_date(release_date_str)
    age_days = (datetime.now() - release_date).days
    age_years = age_days / 365.25
    return age_years >= max_years, age_years


def process_and_replace_track(sp, playlist_id, track_item, playlist_name):
    """Archives old track, appends similar track, and removes old track from playlist."""
    track_uri = track_item["uri"]
    track_id = track_item["id"]
    track_name = track_item["name"]
    artist_name = track_item["artists"][0]["name"]
    artist_id = track_item["artists"][0]["id"]

    # 1. Add Similar Replacement
    similar_track = get_similar_track(sp, track_id, artist_id)
    added_similar_name = None
    if similar_track:
        sp.playlist_add_items(playlist_id=playlist_id, items=[similar_track["uri"]])
        added_similar_name = f"{similar_track['name']} - {similar_track['artists'][0]['name']}"

    # 2. Archive Old Track
    if ENABLE_ARCHIVE_BACKUP:
        user_id = sp.current_user()["id"]
        archive_id = get_or_create_archive_playlist(sp, user_id)
        if archive_id:
            sp.playlist_add_items(playlist_id=archive_id, items=[track_uri])

    # 3. Remove Old Track
    sp.playlist_remove_all_occurrences_of_items(playlist_id=playlist_id, items=[track_uri])

    timestamp = time.strftime("%H:%M:%S")
    playback_state["last_cleaned"] = timestamp
    playback_state["removed_count"] += 1

    images = track_item.get("album", {}).get("images", [])
    image_url = images[0]["url"] if images else None
    track_url = track_item.get("external_urls", {}).get("spotify", "#")

    removal_entry = {
        "track": track_name,
        "artist": artist_name,
        "playlist": playlist_name,
        "replacement": added_similar_name or "None",
        "image_url": image_url,
        "track_url": track_url,
        "time": timestamp
    }
    playback_state["removal_history"].insert(0, removal_entry)
    playback_state["removal_history"] = playback_state["removal_history"][:20]


def scan_entire_playlist(playlist_id):
    """Fetches all items in a playlist and cleans older tracks."""
    auth_manager = get_auth_manager()
    token_info = auth_manager.get_cached_token()
    if not token_info or not auth_manager.validate_token(token_info):
        return

    sp = spotipy.Spotify(auth_manager=auth_manager)
    playlist_info = sp.playlist(playlist_id, fields="name")
    playlist_name = playlist_info.get("name", "Playlist")

    if playlist_name.lower() == ARCHIVE_PLAYLIST_NAME.lower():
        return

    print(f"[FreshList-AI] Starting full scan for playlist: '{playlist_name}'...")
    
    # Get all tracks (handles pagination)
    results = sp.playlist_items(playlist_id)
    tracks = results.get("items", [])
    while results.get("next"):
        results = sp.next(results)
        tracks.extend(results.get("items", []))

    cleaned = 0
    for item in tracks:
        track = item.get("track")
        if not track or not track.get("id"):
            continue

        is_old, age = is_track_older_than_limit(track)
        if is_old:
            print(f"[FreshList-AI] Found track '{track['name']}' ({age:.1f} yrs old). Replacing and archiving...")
            process_and_replace_track(sp, playlist_id, track, playlist_name)
            cleaned += 1
            time.sleep(1) # Prevent hitting API rate limits

    print(f"[FreshList-AI] Completed full scan for '{playlist_name}'. Processed {cleaned} old tracks.")


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
                    track_uri = track_item["uri"]
                    
                    images = track_item.get("album", {}).get("images", [])
                    image_url = images[0]["url"] if images else None

                    playback_state["track"] = track_item["name"]
                    playback_state["artist"] = track_item["artists"][0]["name"]
                    playback_state["image_url"] = image_url
                    playback_state["status"] = "Playing"

                    context = current.get("context")
                    if context and context.get("type") == "playlist":
                        playlist_uri = context.get("uri")
                        playlist_id = playlist_uri.split(":")[-1]
                        playback_state["active_playlist_id"] = playlist_id

                        if track_uri != last_processed_track_uri:
                            playlist_info = sp.playlist(playlist_id, fields="name")
                            playlist_name = playlist_info.get("name", "Active Playlist")
                            playback_state["current_playlist"] = playlist_name

                            is_old, age = is_track_older_than_limit(track_item)
                            if is_old and playlist_name.lower() != ARCHIVE_PLAYLIST_NAME.lower():
                                process_and_replace_track(sp, playlist_id, track_item, playlist_name)
                            
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


@app.post("/scan-full-playlist")
async def scan_playlist_endpoint():
    playlist_id = playback_state.get("active_playlist_id")
    if playlist_id:
        # Run scan in background thread so HTTP response returns immediately
        thread = threading.Thread(target=scan_entire_playlist, args=(playlist_id,), daemon=True)
        thread.start()
        return {"status": "started", "message": "Full playlist scan initiated."}
    return {"status": "error", "message": "No active playlist detected."}


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
