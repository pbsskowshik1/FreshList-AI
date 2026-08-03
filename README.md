# FreshList-AI 🎵

A terminal-based Spotify playlist manager that lets you review a queue of pending track removals and search & add new songs to any of your playlists.

## Features

- **Review removal queue** — work through `pending_removals.jsonl` one track at a time: remove, discard, skip, or quit
- **Add songs to any playlist** — search Spotify and add tracks to one or more playlists in a single flow
- **All playlists, always** — full pagination so you never miss a playlist no matter how many you have
- **Rich terminal UI** — colored tables, panels, and prompts powered by the `rich` library

## Requirements

- Python 3.10+
- A [Spotify Developer app](https://developer.spotify.com/dashboard) (free)

## Setup

### 1. Install dependencies
pip install -r requirements.txt

### 2. Create a Spotify app
1. Go to Spotify Developer Dashboard
2. Create a new app
3. Add `http://127.0.0.1:8888/callback` as a Redirect URI
4. Copy your Client ID and Client Secret

### 3. Set environment variables
export SPOTIPY_CLIENT_ID="your_client_id"
export SPOTIPY_CLIENT_SECRET="your_client_secret"
export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8888/callback"

### 4. Run
python review_queue.py

## Pending Removals File

`pending_removals.jsonl` — each line is one track queued for removal:
{"track_name": "Song Title", "artists": "Artist Name", "playlist_name": "My Playlist", ...}

## Usage

  1  Review pending removal queue
  2  Search & add songs to a playlist
  q  Quit

| Key | Action |
|-----|--------|
| y   | Remove the track from the playlist |
| n   | Discard from queue, leave track in playlist |
| s   | Skip for now, keep in queue |
| q   | Stop reviewing, save remaining queue |

## License
MIT
