"""Show what the bot sees for one sport right now, game by game.

Usage: python -m kalshibot.diagnose nfl   (or soccer / mlb / cfb)

Runs the exact same query and parsing as the live bot, then prints each
event with the reason it is or isn't counted as a live game.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from .odds_providers.sportsgameodds import LEAGUE_IDS, SportsGameOddsProvider


def main() -> None:
    sport = sys.argv[1] if len(sys.argv) > 1 else "nfl"
    if sport not in LEAGUE_IDS:
        print(f"Unknown sport {sport!r}; pick one of: {', '.join(LEAGUE_IDS)}")
        return

    provider = SportsGameOddsProvider()
    events = provider.fetch_events(sport)
    print(f"{sport}: {len(events)} events returned (leagues {LEAGUE_IDS[sport]})\n")

    counts: dict[str, int] = {}
    for event in events:
        status = event.get("status", {})
        teams = event.get("teams", {})
        away = teams.get("away", {}).get("names", {}).get("long", "?")
        home = teams.get("home", {}).get("names", {}).get("long", "?")

        ml = {}
        for odd in event.get("odds", {}).values():
            if odd.get("periodID") == "game" and odd.get("betTypeID") == "ml":
                ml[odd.get("sideID")] = odd.get("bookOdds") or odd.get("fairOdds")

        if event.get("type") != "match":
            verdict = f"skipped (type={event.get('type')})"
        else:
            snap = provider._parse_event(sport, event)
            if snap is None:
                verdict = "DROPPED (no moneyline or unparseable)"
            elif snap.is_live:
                verdict = "COUNTED AS LIVE"
            elif snap.is_final:
                verdict = "finished"
            else:
                verdict = "upcoming (pregame line cached each cycle)"

        counts[verdict] = counts.get(verdict, 0) + 1
        print(
            f"{verdict:45} live={status.get('live')!s:5} period={status.get('currentPeriodID') or '-':4} "
            f"{away} @ {home}  ml home={ml.get('home')} away={ml.get('away')}"
        )

    print("\nSummary:")
    for verdict, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3}  {verdict}")


if __name__ == "__main__":
    main()
