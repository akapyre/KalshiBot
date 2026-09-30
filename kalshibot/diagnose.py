"""Show what the bot sees for one sport right now, game by game.

Usage: python -m kalshibot.diagnose nfl   (or soccer / mlb / cfb)
       python -m kalshibot.diagnose kalshi

Runs the exact same query and parsing as the live bot, then prints each
event with the reason it is or isn't counted as a live game. "kalshi"
instead checks which Kalshi API address accepts your credentials.
"""
from __future__ import annotations

import json
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

from .kalshi_client import KalshiClient
from .main import build_kalshi_client
from .odds_providers.sportsgameodds import LEAGUE_IDS, SportsGameOddsProvider

KALSHI_CANDIDATE_URLS = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
]


def _try(label: str, fn) -> str:
    try:
        fn()
        return f"{label} OK"
    except requests.HTTPError as e:
        return f"{label} HTTP {e.response.status_code} {e.response.text[:200]}"
    except Exception as e:
        return f"{label} {type(e).__name__}"


def check_kalshi() -> None:
    creds = build_kalshi_client(demo=False)._creds
    print("Credentials loaded:", "yes" if creds else "NO -- check .env")
    for url in KALSHI_CANDIDATE_URLS:
        client = KalshiClient(creds, base_url=url, timeout=15)
        markets = _try("markets", lambda: client.list_markets(limit=1))
        balance = _try("balance", client.get_balance) if creds else "balance skipped"
        print(f"{url}\n    {markets} | {balance}")
    print(
        "\nUse the address where BOTH say OK: add it to .env as\n"
        "KALSHI_BASE_URL=<that address>"
    )


def main() -> None:
    sport = sys.argv[1] if len(sys.argv) > 1 else "nfl"
    if sport == "kalshi":
        check_kalshi()
        return
    if sport not in LEAGUE_IDS:
        print(f"Unknown sport {sport!r}; pick one of: {', '.join(LEAGUE_IDS)}")
        return

    provider = SportsGameOddsProvider()
    events = provider.fetch_events(sport)
    print(f"{sport}: {len(events)} events returned (leagues {LEAGUE_IDS[sport]})\n")

    counts: dict[str, int] = {}
    sample_live_status = None
    for event in events:
        status = event.get("status", {})
        teams = event.get("teams", {})
        away = teams.get("away", {}).get("names", {}).get("long", "?")
        home = teams.get("home", {}).get("names", {}).get("long", "?")

        snap = provider._parse_event(sport, event) if event.get("type") == "match" else None
        if event.get("type") != "match":
            verdict = f"skipped (type={event.get('type')})"
        elif snap is None:
            verdict = "DROPPED (no moneyline or unparseable)"
        elif snap.is_live:
            verdict = "COUNTED AS LIVE"
            sample_live_status = sample_live_status or status
        elif snap.is_final:
            verdict = "finished"
        else:
            verdict = "upcoming (pregame line cached each cycle)"

        counts[verdict] = counts.get(verdict, 0) + 1
        line = f"{verdict:42} {away} @ {home}"
        if snap is not None:
            pregame = snap.pregame_favorite_odds
            line += (
                f" | fav {snap.favorite_team} pregame {pregame if pregame is not None else 'NONE'}"
                f" live {snap.live_favorite_odds:+d} | score {snap.away_score}-{snap.home_score}"
                f" period {snap.period} clock {snap.seconds_remaining}s"
            )
        print(line)

    print("\nSummary:")
    for verdict, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3}  {verdict}")

    # Raw status of one live game, to check how period and clock come
    # through for this sport (the NHL clock format is not yet verified).
    if sample_live_status is not None:
        print("\nRaw status of one live game:")
        print(json.dumps(sample_live_status, indent=2))


if __name__ == "__main__":
    main()
