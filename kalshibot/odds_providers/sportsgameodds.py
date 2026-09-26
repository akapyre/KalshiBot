"""OddsProvider adapter for SportsGameOdds (sportsgameodds.com).

Replaces the TheRundown adapter -- TheRundown's RapidAPI resale listing
returns placeholder (0.0001) odds values regardless of plan tier, so it
could never feed the rules engine real numbers. SportsGameOdds gives real,
bookmaker-sourced odds, confirmed against a live paid-tier response on
2026-09-22.

Field mapping confirmed against a real /v2/events response (see that date's
debugging session, event mXCZTRJnbX8ib64z1h3D -- a finished Super Bowl LVIII
game used purely to inspect the schema, not live data):

- `teams.home/away.names.long` -- team name
- `teams.home/away.score` -- current score
- `odds` is a dict keyed by oddID strings shaped
  {statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}. We want
  periodID == "game" (full-game moneyline, not the 1st-quarter/2nd-half
  sub-markets that also exist) and betTypeID == "ml".
- Each odds entry's `bookOdds` field is the actual sportsbook-quoted
  American-odds string (e.g. "-170") -- preferred over `fairOdds`, which is
  a synthetic no-vig price, not what a bettor actually sees.
- `status.live` / `status.completed` -- game state flags.
- `status.currentPeriodID` (e.g. "4q", "2h") -- current quarter/half while
  live; used to detect the NFL 4th-quarter rule and soccer halftime.

TENNIS IS NOT COVERED: confirmed via GET /v2/leagues on both free and paid
tiers -- SportsGameOdds' sportID list is BASEBALL, BASKETBALL, FOOTBALL,
HANDBALL, HOCKEY, MMA, SOCCER. No tennis at all. Per your call, the tennis
rule in config/rules.yaml is left in place but will simply never fire,
since main.py no longer polls for it.

IMPORTANT DESIGN NOTE: we deliberately do NOT filter the /v2/events request
to live-only. If we did, we'd never see a game in its pregame state and
could never cache the pregame line your rules compare against. Instead we
pull a window of events every cycle, cache the line for anything not yet
live, and only return snapshots for games that ARE currently live (using
the pregame line cached from an earlier, pre-kickoff poll of the same
game). This means the bot needs to be running and polling *before* a
game's kickoff to catch its pregame line -- if you start the bot mid-game,
it has no pregame baseline for that game and its rules won't fire for it
until the next game.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .base import GameSnapshot, Sport
from ..state import PregameOddsStore

logger = logging.getLogger("kalshibot.odds_providers.sportsgameodds")

API_BASE = "https://api.sportsgameodds.com/v2"

LEAGUE_IDS: dict[Sport, list[str]] = {
    "nfl": ["NFL"],
    "soccer": ["EPL", "LA_LIGA", "BUNDESLIGA", "IT_SERIE_A", "FR_LIGUE_1"],
    "tennis": [],  # not covered by this provider -- see module docstring
    "mlb": ["MLB"],  # confirmed present in GET /v2/leagues
    "cfb": ["NCAAF"],  # confirmed present in GET /v2/leagues (early exploration, not the MLB/NFL session)
}


class SportsGameOddsProvider:
    def __init__(self, api_key: str | None = None, pregame_store: PregameOddsStore | None = None):
        self._api_key = api_key or os.environ["SPORTSGAMEODDS_API_KEY"]
        self._pregame_store = pregame_store or PregameOddsStore()

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key}

    def list_live_games(self, sport: Sport) -> list[GameSnapshot]:
        league_ids = LEAGUE_IDS.get(sport) or []
        if not league_ids:
            return []

        # Without an explicit date window, /v2/events does not default to
        # "now" -- every unfiltered pull we did while building this adapter
        # came back with events from 2024. Bound the query to "anything that
        # started recently enough to still plausibly be live" through
        # "anything starting soon enough to be worth caching a pregame line
        # for" instead.
        now = datetime.now(timezone.utc)
        starts_after = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        starts_before = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

        snapshots = []
        resp = requests.get(
            f"{API_BASE}/events",
            headers=self._headers(),
            params={
                "leagueID": ",".join(league_ids),
                "limit": 25,
                "startsAfter": starts_after,
                "startsBefore": starts_before,
            },
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json().get("data", [])
        logger.debug("%s: %d raw events returned for leagues %s", sport, len(events), league_ids)
        for event in events:
            if event.get("type") != "match":
                continue  # skip prop-only/novelty entries (e.g. Puppy Bowl)
            snapshot = self._parse_event(sport, event)
            if snapshot is None:
                continue
            if not snapshot.is_live:
                self._pregame_store.set(snapshot.game_id, snapshot.live_favorite_odds)
            snapshots.append(snapshot)
        return [s for s in snapshots if s.is_live]

    def get_pregame_odds(self, sport: Sport, game_id: str) -> int | None:
        return self._pregame_store.get(game_id)

    def _parse_event(self, sport: Sport, event: dict[str, Any]) -> GameSnapshot | None:
        try:
            game_id = event["eventID"]
            teams = event["teams"]
            home = teams["home"]
            away = teams["away"]
            home_name = home["names"]["long"]
            away_name = away["names"]["long"]

            home_ml = away_ml = None
            for odd in event.get("odds", {}).values():
                if odd.get("periodID") != "game" or odd.get("betTypeID") != "ml":
                    continue
                price = odd.get("bookOdds") or odd.get("fairOdds")
                if price is None:
                    continue
                if odd.get("sideID") == "home":
                    home_ml = int(price)
                elif odd.get("sideID") == "away":
                    away_ml = int(price)

            if home_ml is None or away_ml is None:
                logger.warning(
                    "No game-moneyline odds for %s @ %s (eventID=%s) -- dropping "
                    "this game from live_games this cycle",
                    away_name, home_name, game_id,
                )
                return None

            favorite_is_home = home_ml < away_ml
            favorite_team = home_name if favorite_is_home else away_name
            live_favorite_odds = home_ml if favorite_is_home else away_ml

            status = event.get("status", {})
            current_period = status.get("currentPeriodID", "")
            period = _parse_period_number(current_period)
            started_periods = status.get("periods", {}).get("started", [])
            past_halftime = "2h" in started_periods if sport == "soccer" else None

            pregame = self._pregame_store.get(game_id)

            return GameSnapshot(
                game_id=game_id,
                sport=sport,
                home_team=home_name,
                away_team=away_name,
                favorite_team=favorite_team,
                pregame_favorite_odds=pregame if pregame is not None else live_favorite_odds,
                live_favorite_odds=live_favorite_odds,
                start_time_utc=status.get("startsAt", ""),
                is_live=bool(status.get("live", False)),
                is_final=bool(status.get("completed", False) or status.get("finalized", False)),
                period=period,
                home_score=home.get("score"),
                away_score=away.get("score"),
                past_halftime=past_halftime,
            )
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "Failed to parse event (eventID=%s) -- dropping from live_games "
                "this cycle", event.get("eventID", "?"), exc_info=True,
            )
            return None


def _parse_period_number(period_id: str) -> int | None:
    """"1q" -> 1, "4q" -> 4, "2h" -> 2; "ot"/"game"/"reg"/"" -> None.
    Assumed (not yet confirmed against a real MLB response) that innings
    follow the same leading-digit convention, e.g. "7t"/"7b" -> 7."""
    if period_id and period_id[0].isdigit():
        return int(period_id[0])
    return None
