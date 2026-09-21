"""Maps an odds-provider GameSnapshot to the Kalshi market/ticker + side to
buy for the favorite team.

Kalshi's game-winner markets are organized under a series ticker per league
(e.g. KXNFLGAME) with one market per game, each carrying a yes/no side tied
to a specific team. I have NOT verified the exact title/team-field format
against a live response (no live Kalshi key here) -- confirm the parsing in
`_market_matches_game` against real `list_markets` output for your sports
before trusting matches in anything but dry-run. When in doubt this raises
`AmbiguousMatch` / returns None rather than guessing a ticker wrong.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .kalshi_client import KalshiClient
from .odds_providers.base import GameSnapshot, Sport

logger = logging.getLogger("kalshibot.matching")

SERIES_BY_SPORT: dict[Sport, str] = {
    "nfl": "KXNFLGAME",
    "soccer": "KXSOCCERGAME",   # confirm exact series ticker per league on kalshi.com/browse
    "tennis": "KXTENNISGAME",   # confirm exact series ticker per tour
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


@dataclass
class ResolvedMarket:
    ticker: str
    side: str   # "yes" or "no" -- which side corresponds to the favorite team winning


class MarketMatcher:
    def __init__(self, kalshi_client: KalshiClient):
        self._kalshi = kalshi_client

    def resolve(self, snapshot: GameSnapshot) -> ResolvedMarket | None:
        series_ticker = SERIES_BY_SPORT.get(snapshot.sport)
        if series_ticker is None:
            logger.warning("No Kalshi series mapped for sport=%s", snapshot.sport)
            return None

        try:
            game_date = datetime.fromisoformat(snapshot.start_time_utc.replace("Z", "+00:00"))
        except ValueError:
            game_date = None

        markets = self._kalshi.list_markets(series_ticker=series_ticker, status="open")
        candidates = markets.get("markets", [])

        matches = [m for m in candidates if self._market_matches_game(m, snapshot, game_date)]

        if len(matches) == 1:
            return self._resolve_side(matches[0], snapshot)
        if len(matches) == 0:
            logger.warning(
                "No Kalshi market matched game %s (%s vs %s) -- skipping, will not guess",
                snapshot.game_id, snapshot.home_team, snapshot.away_team,
            )
            return None

        logger.warning(
            "AMBIGUOUS: %d Kalshi markets matched game %s (%s vs %s) -- skipping, "
            "needs a tighter matcher (event date/time disambiguation)",
            len(matches), snapshot.game_id, snapshot.home_team, snapshot.away_team,
        )
        return None

    def _market_matches_game(
        self, market: dict[str, Any], snapshot: GameSnapshot, game_date: datetime | None
    ) -> bool:
        title = market.get("title", "") + " " + market.get("subtitle", "")
        norm_title = _normalize(title)
        home_hit = _normalize(snapshot.home_team) in norm_title
        away_hit = _normalize(snapshot.away_team) in norm_title
        if not (home_hit and away_hit):
            return False

        if game_date is not None and market.get("close_time"):
            try:
                close_time = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
                if abs((close_time - game_date)) > timedelta(hours=12):
                    return False
            except ValueError:
                pass

        return True

    def _resolve_side(self, market: dict[str, Any], snapshot: GameSnapshot) -> ResolvedMarket:
        # Convention (confirm against real data): Kalshi's `yes_sub_title` /
        # `yes_team` style field names its "yes" outcome after one team.
        yes_team = market.get("yes_sub_title") or market.get("yes_team") or ""
        side = "yes" if _normalize(snapshot.favorite_team) in _normalize(yes_team) else "no"
        return ResolvedMarket(ticker=market["ticker"], side=side)
