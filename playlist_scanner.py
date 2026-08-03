"""
Playlist Scanner (scheduled)
=============================
Scans all Spotify playlists for duplicate tracks on a schedule.
Duplicates are written to pending_removals.jsonl for review.

Requirements:
    pip install spotipy rich schedule

Usage:
    python playlist_scanner.py              # every 1 minute (default)
    python playlist_scanner.py --interval 5 # every 5 minutes
    python playlist_scanner.py --once       # run once and exit
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import schedule
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich import box

CLIENT_ID     = os.environ.get("SPOTIPY_CLIENT_ID",     "YOUR_CLIENT_ID_HERE")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "YOUR_CLIENT_SECRET_HERE")
REDIRECT_URI  = os.environ.get("SPOTIPY_REDIRECT_URI",  "http://127.0.0.1:8888/callback")

SCOPES = (
    "playlist-read-private "
    "playlist-read-collaborative"
)

PENDING_FILE = "pending_removals.jsonl"
console = Console()


def get_spotify_client() -> spotipy.Spotify:
    auth_manager = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_path=".cache-spotify_cleanup",
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def load_pending() -> list[dict]:
    if not os.path.exists(PENDING_FILE):
        return []
    entries: list[dict] = []
    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def append_to_pending(entries: list[dict]) -> None:
    with open(PENDING_FILE, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def fetch_all_playlists(sp: spotipy.Spotify) -> list[dict]:
    all_playlists: list[dict] = []
    response = sp.current_user_playlists(limit=50)
    while response:
        all_playlists.extend(response["items"])
        response = sp.next(response) if response["next"] else None
    return all_playlists


def fetch_playlist_tracks(sp: spotipy.Spotify, playlist_id: str) -> list[dict]:
    tracks: list[dict] = []
    response = sp.playlist_tracks(playlist_id, fields="items(track(uri,name,artists)),next")
    position = 0
    while response:
        for item in response["items"]:
            t = item.get("track")
            if not t or not t.get("uri"):
                position += 1
                continue
            tracks.append({
                "track_uri":  t["uri"],
                "track_name": t["name"],
                "artists":    ", ".join(a["name"] for a in t.get("artists", [])),
                "position":   position,
            })
            position += 1
        response = sp.next(response) if response["next"] else None
    return tracks


def find_duplicates_in_playlist(tracks: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    duplicates: list[dict] = []
    for track in tracks:
        uri = track["track_uri"]
        if uri in seen:
            duplicates.append({**track, "duplicate_of_position": seen[uri]})
        else:
            seen[uri] = track["position"]
    return duplicates


def already_queued_keys(pending: list[dict]) -> set[str]:
    return {f"{e['playlist_id']}::{e['track_uri']}" for e in pending}


def run_scan(sp: spotipy.Spotify) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    console.print(Rule(f"[bold green]Scanning playlists[/] [dim]{now}[/]"))

    playlists = fetch_all_playlists(sp)
    console.print(f"[dim]Found {len(playlists)} playlist(s). Checking for duplicates…[/]\n")

    pending = load_pending()
    queued_keys = already_queued_keys(pending)
    new_entries: list[dict] = []

    summary_table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan")
    summary_table.add_column("Playlist", style="bold white")
    summary_table.add_column("Tracks", justify="right", style="dim")
    summary_table.add_column("Duplicates Found", justify="right", style="yellow")
    summary_table.add_column("Added to Queue", justify="right", style="green")

    for pl in playlists:
        try:
            tracks = fetch_playlist_tracks(sp, pl["id"])
            duplicates = find_duplicates_in_playlist(tracks)
            added = 0

            for dup in duplicates:
                key = f"{pl['id']}::{dup['track_uri']}"
                if key in queued_keys:
                    continue
                new_entries.append({
                    "track_name":    dup["track_name"],
                    "artists":       dup["artists"],
                    "playlist_name": pl["name"],
                    "playlist_id":   pl["id"],
                    "track_uri":     dup["track_uri"],
                    "reason":        f"Duplicate — also appears at position {dup['duplicate_of_position'] + 1}",
                    "timestamp":     now,
                })
                queued_keys.add(key)
                added += 1

            summary_table.add_row(pl["name"], str(len(tracks)), str(len(duplicates)), f"+{added}" if added else "—")

        except spotipy.exceptions.SpotifyException as exc:
            console.print(f"  [red]✗ Skipped '{pl['name']}': {exc}[/]")

    console.print(summary_table)

    if new_entries:
        append_to_pending(new_entries)
        console.print(f"\n[bold green]✓ {len(new_entries)} new duplicate(s) added to the queue.[/] Run [bold]python review_queue.py[/] to review them.\n")
    else:
        console.print("\n[dim]No new duplicates found.[/]\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Spotify Playlist Scanner")
    parser.add_argument("--interval", type=int, default=1, metavar="MINUTES", help="Scan interval in minutes (default: 1)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if "YOUR_CLIENT_ID_HERE" in CLIENT_ID or "YOUR_CLIENT_SECRET_HERE" in CLIENT_SECRET:
        console.print(Panel("[bold red]Spotify credentials not set![/]\n\nSet SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI", title="Setup Required", border_style="red"))
        sys.exit(1)

    with console.status("[bold green]Connecting to Spotify…[/]"):
        sp = get_spotify_client()

    me = sp.me()
    console.print(Panel(
        f"[bold green]Playlist Scanner[/]\n"
        f"[dim]Logged in as[/] [bold]{me['display_name']}[/]\n"
        f"[dim]Interval:[/]  every [bold]{args.interval}[/] minute(s)\n"
        f"[dim]Checking:[/]  duplicate tracks across all playlists",
        border_style="green", expand=False,
    ))

    if args.once:
        run_scan(sp)
        return

    run_scan(sp)
    schedule.every(args.interval).minutes.do(run_scan, sp)
    console.print(f"[dim]Next scan in {args.interval} minute(s). Press Ctrl+C to stop.[/]\n")

    try:
        while True:
            schedule.run_pending()
            time.sleep(10)
    except KeyboardInterrupt:
        console.print("\n[dim]Scanner stopped.[/]")


if __name__ == "__main__":
    main()
