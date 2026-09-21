"""Example OddsProvider adapter for TheRundown (therundown.io).

TheRundown was picked as the reference implementation because it bundles
live odds and live game state (score, quarter/period, halftime) in one
subscription, matching your "same provider for both" choice. You can drop
in a different vendor (SportsDataIO, OddsJam, ...) by writing a new class
that implements OddsProvider -- nothing else in the bot needs to change.

IMPORTANT: I have not run this against a live TheRundown API key (I don't
have one), so the exact JSON field names below are my best read of their
public docs, not verified against a live response. Before trusting this in
dry-run, hit the endpoints with your key and confirm the field paths in
`_parse_game` match what actually comes back -- fix on the spot if not.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from .base import GameSnapshot, Sport

API_BASE = "https://therundown-therundown-v1.p.rapidapi.com"

_SPORT_IDS = {
    # TheRundown sport IDs -- confirm against GET /sports for your account.
    "nfl": 2,
    "soccer": 20,   # soccer has many league-specific sub-ids; adjust as needed
    "tennis": 21,
}


class TheRundownProvider:
    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ["THERUNDOWN_API_KEY"]
        self._pregame_cache: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "X-RapidAPI-Key": self._api_key,
            "X-RapidAPI-Host": "therundown-therundown-v1.p.rapidapi.com",
        }

    def list_live_games(self, sport: Sport) -> list[GameSnapshot]:
        sport_id = _SPORT_IDS[sport]
        resp = requests.get(
            f"{API_BASE}/sports/{sport_id}/events",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])
        snapshots = []
        for event in events:
            snapshot = self._parse_game(sport, event)
            if snapshot is None:
                continue
            if snapshot.pregame_favorite_odds is None and snapshot.game_id in self._pregame_cache:
                pass  # filled below
            if snapshot.game_id not in self._pregame_cache and not snapshot.is_live:
                self._pregame_cache[snapshot.game_id] = snapshot.live_favorite_odds
            snapshots.append(snapshot)
        return [s for s in snapshots if s.is_live]

    def get_pregame_odds(self, sport: Sport, game_id: str) -> int | None:
        return self._pregame_cache.get(game_id)

    def _parse_game(self, sport: Sport, event: dict[str, Any]) -> GameSnapshot | None:
        try:
            game_id = event["event_id"]
            teams = event["teams_normalized"]
            home = next(t for t in teams if t["is_home"])
            away = next(t for t in teams if t["is_away"])
            line = event.get("lines", {})
            # Pick any book's moneyline as the reference line; average/median
            # across books is worth doing once this is past prototype stage.
            moneyline = next(iter(line.values()))["moneyline"]
            home_ml = moneyline["moneyline_home"]
            away_ml = moneyline["moneyline_away"]
            favorite_is_home = home_ml < away_ml
            favorite_team = home["name"] if favorite_is_home else away["name"]
            live_favorite_odds = home_ml if favorite_is_home else away_ml

            score = event.get("score", {})
            period = score.get("game_period")
            home_score = score.get("score_home")
            away_score = score.get("score_away")
            past_halftime = None
            if sport == "soccer" and period is not None:
                past_halftime = period >= 2

            pregame = self._pregame_cache.get(game_id)

            return GameSnapshot(
                game_id=game_id,
                sport=sport,
                home_team=home["name"],
                away_team=away["name"],
                favorite_team=favorite_team,
                pregame_favorite_odds=pregame if pregame is not None else live_favorite_odds,
                live_favorite_odds=live_favorite_odds,
                start_time_utc=event.get("event_date", ""),
                is_live=score.get("event_status") == "STATUS_IN_PROGRESS",
                is_final=score.get("event_status") == "STATUS_FINAL",
                period=period,
                home_score=home_score,
                away_score=away_score,
                past_halftime=past_halftime,
            )
        except (KeyError, StopIteration):
            return None
