"""
Review Queue & Add Songs  (improved)
======================================
1. Processes the queue of pending removals (SAFE_MODE).
2. Allows searching for and adding new songs to ANY of your playlists.

Requirements:
    pip install spotipy rich

Usage:
    python review_queue.py
"""

import json
import os
import sys

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich import box
from rich.text import Text
from rich.columns import Columns
from rich.rule import Rule

CLIENT_ID     = os.environ.get("SPOTIPY_CLIENT_ID",     "YOUR_CLIENT_ID_HERE")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "YOUR_CLIENT_SECRET_HERE")
REDIRECT_URI  = os.environ.get("SPOTIPY_REDIRECT_URI",  "http://127.0.0.1:8888/callback")

SCOPES = (
    "playlist-modify-public "
    "playlist-modify-private "
    "playlist-read-private "
    "playlist-read-collaborative"
)

PENDING_FILE = "pending_removals.jsonl"
REMOVE_ALL_OCCURRENCES_IN_PLAYLIST = True

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


def save_pending(entries: list[dict]) -> None:
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def remove_track(sp: spotipy.Spotify, playlist_id: str, track_uri: str) -> bool:
    try:
        if REMOVE_ALL_OCCURRENCES_IN_PLAYLIST:
            sp.playlist_remove_all_occurrences_of_items(playlist_id, [track_uri])
        else:
            sp.playlist_remove_specific_occurrences_of_items(
                playlist_id, [{"uri": track_uri, "positions": [0]}]
            )
        return True
    except spotipy.exceptions.SpotifyException as exc:
        console.print(f"  [bold red]✗ Failed to remove:[/] {exc}")
        return False


def fetch_all_playlists(sp: spotipy.Spotify) -> list[dict]:
    all_playlists: list[dict] = []
    response = sp.current_user_playlists(limit=50)
    while response:
        all_playlists.extend(response["items"])
        response = sp.next(response) if response["next"] else None
    return all_playlists


def review_pending_removals(sp: spotipy.Spotify) -> None:
    entries = load_pending()
    console.print(Rule("[bold green]Pending Removal Queue[/]"))

    if not entries:
        console.print("[dim]No pending removals in the queue.[/]\n")
        return

    console.print(
        f"[bold]{len(entries)}[/] track(s) waiting for your decision.\n"
        "[dim]Commands:[/]  [bold green]y[/]=remove  [bold red]n[/]=discard from queue  "
        "[yellow]s[/]=skip (keep in queue)  [bold red]q[/]=quit review\n"
    )

    remaining: list[dict] = []
    for i, e in enumerate(entries, 1):
        console.print(
            Panel(
                f"[bold white]{e['track_name']}[/]  [dim]by[/]  [cyan]{e['artists']}[/]\n"
                f"[dim]Playlist:[/]  [green]{e['playlist_name']}[/]\n"
                f"[dim]Reason  :[/]  {e['reason']}\n"
                f"[dim]Queued  :[/]  {e['timestamp']}",
                title=f"[bold]{i}/{len(entries)}[/]",
                border_style="blue",
                expand=False,
            )
        )

        choice = Prompt.ask(
            "    Decision",
            choices=["y", "n", "s", "q"],
            default="s",
            show_choices=True,
        ).lower()

        if choice == "y":
            if remove_track(sp, e["playlist_id"], e["track_uri"]):
                console.print("    [bold green]✓ Removed.[/]\n")
            else:
                console.print("    [red]✗ Failed — keeping in queue.[/]\n")
                remaining.append(e)
        elif choice == "n":
            console.print("    [dim]Left in playlist, discarded from queue.[/]\n")
        elif choice == "q":
            console.print("[yellow]Stopping review. Remaining entries stay in the queue.[/]")
            remaining.append(e)
            remaining.extend(entries[i:])
            break
        else:
            console.print("    [dim]Skipped — still in queue.[/]\n")
            remaining.append(e)

    save_pending(remaining)
    console.print(
        f"[bold]Review complete.[/] [green]{len(entries) - len(remaining)}[/] removed, "
        f"[yellow]{len(remaining)}[/] still in queue.\n"
    )


def add_new_songs(sp: spotipy.Spotify) -> None:
    console.print(Rule("[bold green]Add Songs to a Playlist[/]"))
    console.print("[dim]Fetching all your playlists…[/]")
    playlists = fetch_all_playlists(sp)

    if not playlists:
        console.print("[red]No editable playlists found.[/]")
        return

    pl_table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", title="Your Playlists", title_style="bold")
    pl_table.add_column("#", style="dim", width=4, justify="right")
    pl_table.add_column("Playlist Name", style="bold white")
    pl_table.add_column("Tracks", justify="right", style="green")
    pl_table.add_column("Owner", style="dim")

    for idx, pl in enumerate(playlists, 1):
        pl_table.add_row(str(idx), pl["name"], str(pl.get("tracks", {}).get("total", "?")), pl["owner"]["display_name"])

    console.print(pl_table)

    pl_input = Prompt.ask("\nSelect playlist number (or [bold]q[/] to skip)").strip()
    if pl_input.lower() == "q" or not pl_input.isdigit():
        return

    idx = int(pl_input) - 1
    if not (0 <= idx < len(playlists)):
        console.print("[red]Invalid selection.[/]")
        return

    selected_pl = playlists[idx]
    console.print(f"\n[bold green]Selected:[/] {selected_pl['name']} ([dim]{selected_pl['tracks']['total']} tracks[/])\n")

    while True:
        query = Prompt.ask(f"Search a song to add to [bold]{selected_pl['name']}[/] (or press [bold]Enter[/] to finish)").strip()
        if not query:
            break

        with console.status("[dim]Searching Spotify…[/]"):
            results = sp.search(q=query, limit=8, type="track")["tracks"]["items"]

        if not results:
            console.print("[yellow]No tracks found.[/]\n")
            continue

        result_table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
        result_table.add_column("#", style="dim", width=3, justify="right")
        result_table.add_column("Title", style="bold white")
        result_table.add_column("Artists", style="cyan")
        result_table.add_column("Album", style="dim")
        result_table.add_column("Year", style="green", width=6, justify="right")
        result_table.add_column("Duration", justify="right", width=8)

        for i, track in enumerate(results, 1):
            artists = ", ".join(a["name"] for a in track["artists"])
            year = track["album"]["release_date"][:4]
            ms = track["duration_ms"]
            duration = f"{ms // 60000}:{(ms % 60000) // 1000:02d}"
            result_table.add_row(str(i), track["name"], artists, track["album"]["name"], year, duration)

        console.print(result_table)

        track_input = Prompt.ask("Select track number to add (or [bold]c[/] to cancel)").strip()
        if track_input.lower() == "c":
            console.print("[dim]Cancelled.[/]\n")
            continue

        if track_input.isdigit() and 1 <= int(track_input) <= len(results):
            chosen = results[int(track_input) - 1]
            artists = ", ".join(a["name"] for a in chosen["artists"])
            add_to_another = True
            target_playlists = [selected_pl]

            while add_to_another:
                add_to_another = Confirm.ask(f"Also add '[bold]{chosen['name']}[/]' to [bold]another[/] playlist?", default=False)
                if add_to_another:
                    console.print(pl_table)
                    extra_input = Prompt.ask("Select another playlist number (or [bold]q[/] to skip)").strip()
                    if extra_input.isdigit() and 1 <= int(extra_input) <= len(playlists):
                        extra_pl = playlists[int(extra_input) - 1]
                        if extra_pl not in target_playlists:
                            target_playlists.append(extra_pl)
                    else:
                        add_to_another = False

            for pl in target_playlists:
                try:
                    sp.playlist_add_items(pl["id"], [chosen["uri"]])
                    console.print(f"  [bold green]✓[/] Added [bold]{chosen['name']}[/] by {artists} → [green]{pl['name']}[/]")
                except spotipy.exceptions.SpotifyException as exc:
                    console.print(f"  [bold red]✗ Failed adding to {pl['name']}:[/] {exc}")
            console.print()
        else:
            console.print("[yellow]Invalid choice.[/]\n")


def main_menu(sp: spotipy.Spotify) -> None:
    me = sp.me()
    console.print(Panel(f"[bold green]Spotify Playlist Manager[/]\n[dim]Logged in as[/] [bold]{me['display_name']}[/]", border_style="green", expand=False))

    while True:
        console.print(Rule())
        console.print("[bold]What would you like to do?[/]\n")
        console.print("  [bold cyan]1[/]  Review pending removal queue")
        console.print("  [bold cyan]2[/]  Search & add songs to a playlist")
        console.print("  [bold cyan]q[/]  Quit\n")

        choice = Prompt.ask("Choose", choices=["1", "2", "q"], default="1")

        if choice == "1":
            review_pending_removals(sp)
        elif choice == "2":
            add_new_songs(sp)
        elif choice == "q":
            console.print("[dim]Goodbye![/]")
            break


def main() -> None:
    if "YOUR_CLIENT_ID_HERE" in CLIENT_ID or "YOUR_CLIENT_SECRET_HERE" in CLIENT_SECRET:
        console.print(Panel("[bold red]Spotify credentials not set![/]\n\nSet the following environment variables:\n\n  [green]SPOTIPY_CLIENT_ID[/]\n  [green]SPOTIPY_CLIENT_SECRET[/]\n  [green]SPOTIPY_REDIRECT_URI[/]", title="Setup Required", border_style="red"))
        sys.exit(1)

    with console.status("[bold green]Connecting to Spotify…[/]"):
        sp = get_spotify_client()

    main_menu(sp)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")
