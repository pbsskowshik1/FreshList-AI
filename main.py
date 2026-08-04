import os
from datetime import datetime
from fastapi import FastAPI, Request
import spotipy
from spotipy.oauth2 import SpotifyOAuth

app = FastAPI()

# Global Cache State
PLAYLIST_TRACK_CACHE = {
    "playlist_id": None,
    "track_ids": set()
}

def get_spotify_client():
    """Initializes and returns the Spotipy client using environment variables."""
    auth_manager = SpotifyOAuth(
        client_id=os.getenv("SPOTIPY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
        scope="playlist-modify-public playlist-modify-private playlist-read-private user-read-currently-playing user-read-playback-state"
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def load_playlist_cache(sp, playlist_id):
    """
    Loads all existing track IDs into memory using Spotify's updated items -> item schema.
    Prevents duplicate additions to the target FreshList playlist.
    """
    global PLAYLIST_TRACK_CACHE

    if PLAYLIST_TRACK_CACHE["playlist_id"] == playlist_id and PLAYLIST_TRACK_CACHE["track_ids"]:
        return PLAYLIST_TRACK_CACHE["track_ids"]

    print(f"[Cache Load] Syncing track list for playlist ID: {playlist_id}")
    track_ids = set()

    try:
        playlist_data = sp.playlist(playlist_id)
        if not isinstance(playlist_data, dict):
            print(f"[Cache Load Error] Unexpected payload shape: {type(playlist_data)}")
            return track_ids

        # 1. Primary: Updated Spotify schema (top-level 'items')
        items = playlist_data.get("items", [])
        
        # 2. Legacy Fallback: Old 'tracks.items' schema
        if not items and isinstance(playlist_data.get("tracks"), dict):
            items = playlist_data.get("tracks", {}).get("items", [])

        print(f"[Cache Load] Processing {len(items)} playlist items...")

        def extract_track_id(wrapper):
            """Extracts track ID across both new 'item' and old 'track' keys."""
            if not isinstance(wrapper, dict):
                return None

            # New API schema: wrapper["item"] | Old API schema: wrapper["track"]
            track_obj = wrapper.get("item") or wrapper.get("track") or wrapper

            if isinstance(track_obj, dict):
                tid = track_obj.get("id")
                if tid and isinstance(tid, str) and not tid.startswith("spotify:"):
                    return tid

                uri = track_obj.get("uri", "")
                if uri and "spotify:track:" in uri:
                    return uri.split(":")[-1]

            return None

        for item_wrapper in items:
            tid = extract_track_id(item_wrapper)
            if tid:
                track_ids.add(tid)

        # Pagination for playlists with >100 tracks
        next_url = playlist_data.get("next") or (
            playlist_data.get("tracks", {}).get("next")
            if isinstance(playlist_data.get("tracks"), dict)
            else None
        )

        while next_url:
            next_page = sp.next({"next": next_url})
            if not next_page or not isinstance(next_page, dict):
                break

            page_items = next_page.get("items", [])
            for item_wrapper in page_items:
                tid = extract_track_id(item_wrapper)
                if tid:
                    track_ids.add(tid)

            next_url = next_page.get("next")

    except Exception as e:
        print(f"[Cache Load Error] Failed to load playlist tracks: {e}")

    PLAYLIST_TRACK_CACHE["playlist_id"] = playlist_id
    PLAYLIST_TRACK_CACHE["track_ids"] = track_ids
    print(f"[Cache Sync Complete] Found {len(track_ids)} unique tracks in playlist.")
    return track_ids


def is_track_viable(track_obj):
    """
    Evaluates track quality/viability.
    Falls back to album release recency because 'popularity' is deprecated.
    """
    if not isinstance(track_obj, dict):
        return False

    # Check direct popularity if Spotify returns it in your context
    raw_pop = track_obj.get("popularity")
    if raw_pop is not None and isinstance(raw_pop, int) and raw_pop > 0:
        return raw_pop >= 10

    # Fallback: Check album release recency (tracks within 5 years pass)
    album = track_obj.get("album", {})
    release_date = album.get("release_date", "")

    if release_date:
        try:
            release_year = int(release_date.split("-")[0])
            current_year = datetime.now().year
            return (current_year - release_year) <= 5
        except ValueError:
            pass

    # Default pass if metadata structure is limited
    return True


@app.get("/")
def sync_freshlist():
    """Main execution loop called by cron or web triggers."""
    try:
        sp = get_spotify_client()
        target_playlist_id = os.getenv("TARGET_PLAYLIST_ID")

        if not target_playlist_id:
            return {"status": "error", "message": "TARGET_PLAYLIST_ID environment variable not set"}

        # 1. Sync cache
        existing_tracks = load_playlist_cache(sp, target_playlist_id)

        # 0-track safety guard to prevent accidental additions/deletions on sync failure
        if len(existing_tracks) == 0:
            print("[Cache Warning] Playlist cache returned 0 tracks. Skipping evaluation to prevent duplicate additions.")
            return {"status": "skipped", "reason": "Cache returned 0 tracks"}

        # 2. Get currently playing item
        current_playback = sp.current_playback()
        if not current_playback or not current_playback.get("is_playing"):
            return {"status": "idle", "message": "No active playback detected"}

        item = current_playback.get("item")
        if not item:
            return {"status": "idle", "message": "No track item in current playback"}

        track_id = item.get("id")
        track_name = item.get("name", "Unknown")

        # 3. Check duplicate status
        if track_id in existing_tracks:
            print(f"[Skip] '{track_name}' ({track_id}) is already in the target playlist.")
            return {"status": "ignored", "reason": "Already in playlist", "track": track_name}

        # 4. Viability evaluation
        if not is_track_viable(item):
            print(f"[Skip] '{track_name}' failed viability evaluation.")
            return {"status": "ignored", "reason": "Failed viability check", "track": track_name}

        # 5. Add track to playlist & update memory cache
        sp.playlist_add_items(target_playlist_id, [f"spotify:track:{track_id}"])
        PLAYLIST_TRACK_CACHE["track_ids"].add(track_id)
        print(f"[Success] Added '{track_name}' ({track_id}) to FreshList!")

        return {"status": "success", "added_track": track_name, "track_id": track_id}

    except Exception as e:
        print(f"[Runtime Error] Execution failed: {e}")
        return {"status": "error", "detail": str(e)}
