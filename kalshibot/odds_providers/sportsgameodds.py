"""OddsProvider adapter for SportsGameOdds (sportsgameodds.com).

Replaces the TheRundown adapter -- TheRundown's RapidAPI resale listing
returns placeholder (0.0001) odds values regardless of plan tier, so it
can never feed the rules engine real numbers. SportsGameOdds was chosen
instead because independent sources (not just their own marketing)
confirm real, bookmaker-sourced odds, including on the free tier.

IMPORTANT -- two things are NOT yet verified against a live account and
need a real API key + one real response to confirm:

1. `LEAGUE_IDS` below: "NFL" and "EPL" are confirmed correct from public
   docs. The rest (La Liga, Bundesliga, Serie A, Ligue 1, ATP, WTA) are my
   best guess at the naming convention and are UNCONFIRMED -- call
   GET /v2/leagues once you have a key and check the real leagueID
   strings, then fix this dict if any are wrong.

2. `_parse_event`: SportsGameOdds uses a flexible key-value "odds" object
   keyed by oddID strings shaped like
   {statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}-{bookmakerID}
   (moneyline = betTypeID "ml"), NOT fixed fields like TheRundown's
   `lines.moneyline_home`. I have not seen a real response yet, so the
   parsing below is a best-effort placeholder. Get one real /v2/events
   response (for a live or upcoming NFL game) and send it back -- I'll
   rewrite `_parse_event` against the actual field names in one pass
   instead of guessing wrong repeatedly like the TheRundown adapter did.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from .base import GameSnapshot, Sport

API_BASE = "https://api.sportsgameodds.com/v2"

LEAGUE_IDS: dict[Sport, list[str]] = {
    "nfl": ["NFL"],
    "tennis": ["ATP_TENNIS", "WTA_TENNIS"],           # UNCONFIRMED -- check /v2/leagues
    "soccer": ["EPL", "LA_LIGA", "BUNDESLIGA", "SERIE_A", "LIGUE_1"],  # UNCONFIRMED except EPL
}


class SportsGameOddsProvider:
    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ["SPORTSGAMEODDS_API_KEY"]
        self._pregame_cache: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key}

    def list_live_games(self, sport: Sport) -> list[GameSnapshot]:
        league_ids = ",".join(LEAGUE_IDS[sport])
        resp = requests.get(
            f"{API_BASE}/events",
            headers=self._headers(),
            params={"leagueID": league_ids, "live": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json().get("data", [])
        snapshots = []
        for event in events:
            snapshot = self._parse_event(sport, event)
            if snapshot is None:
                continue
            if snapshot.game_id not in self._pregame_cache and not snapshot.is_live:
                self._pregame_cache[snapshot.game_id] = snapshot.live_favorite_odds
            snapshots.append(snapshot)
        return [s for s in snapshots if s.is_live]

    def get_pregame_odds(self, sport: Sport, game_id: str) -> int | None:
        return self._pregame_cache.get(game_id)

    def _parse_event(self, sport: Sport, event: dict[str, Any]) -> GameSnapshot | None:
        # PLACEHOLDER -- rewrite once we have a real response. Current best
        # guess at the shape based on public docs (team info + an "odds"
        # dict keyed by oddID strings containing betTypeID "ml").
        try:
            game_id = event["eventID"]
            teams = event.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            home_name = home.get("name") or home.get("names", {}).get("long", "")
            away_name = away.get("name") or away.get("names", {}).get("long", "")

            odds = event.get("odds", {})
            home_ml = away_ml = None
            for odd_id, odd in odds.items():
                parts = odd_id.split("-")
                if len(parts) < 5 or parts[3] != "ml":
                    continue
                side = parts[4]
                price = odd.get("odds") or odd.get("americanOdds") or odd.get("price")
                if price is None:
                    continue
                if side == "home":
                    home_ml = int(price)
                elif side == "away":
                    away_ml = int(price)

            if home_ml is None or away_ml is None:
                return None

            favorite_is_home = home_ml < away_ml
            favorite_team = home_name if favorite_is_home else away_name
            live_favorite_odds = home_ml if favorite_is_home else away_ml

            status = event.get("status", {})
            results = event.get("results", {})
            period = status.get("period") or status.get("periodID")
            home_score = results.get("home", {}).get("points")
            away_score = results.get("away", {}).get("points")
            past_halftime = None
            if sport == "soccer" and period is not None:
                past_halftime = str(period) not in ("1", "1H", "first_half")

            pregame = self._pregame_cache.get(game_id)

            return GameSnapshot(
                game_id=game_id,
                sport=sport,
                home_team=home_name,
                away_team=away_name,
                favorite_team=favorite_team,
                pregame_favorite_odds=pregame if pregame is not None else live_favorite_odds,
                live_favorite_odds=live_favorite_odds,
                start_time_utc=event.get("status", {}).get("startsAt", ""),
                is_live=status.get("live", False) or status.get("started", False),
                is_final=status.get("completed", False) or status.get("ended", False),
                period=int(period) if isinstance(period, (int, str)) and str(period).isdigit() else None,
                home_score=home_score,
                away_score=away_score,
                past_halftime=past_halftime,
            )
        except (KeyError, ValueError, TypeError):
            return None
