"""Maps a live game to the Kalshi market to buy for the pregame favorite.

Kalshi game-winner markets live under a series ticker per league (e.g.
KXNFLGAME). The assumed layout -- unverified against real data at the time
of writing; run `python -m kalshibot.diagnose markets <sport>` to check --
is one market per team per game, all sharing an event_ticker, with the
team that "yes" pays out on named in yes_sub_title.

Safety rule: the bot only ever buys YES on a market that clearly names the
favorite as its yes outcome. It never infers the favorite's side by buying
NO on the other team's market (in soccer, NO on "underdog wins" also pays
on a draw, so that's a different bet), and when anything is ambiguous it
skips rather than guesses.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .kalshi_client import KalshiClient
from .odds_providers.base import GameSnapshot, Sport

logger = logging.getLogger("kalshibot.matching")

SERIES_BY_SPORT: dict[Sport, str] = {
    # All unverified guesses -- confirm with `diagnose markets <sport>`.
    "nfl": "KXNFLGAME",
    "soccer": "KXSOCCERGAME",
    "tennis": "KXTENNISGAME",
    "mlb": "KXMLBGAME",
    "cfb": "KXNCAAFGAME",
    "nhl": "KXNHLGAME",
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _team_tokens(team: str) -> tuple[list[str], list[str]]:
    """(specific, city) name forms: "Edmonton Oilers" ->
    (["edmontonoilers", "oilers"], ["edmonton"]). City forms are kept
    separate because they can collide (New York Rangers vs Islanders)."""
    words = team.split()
    specific = [_normalize(team)]
    city = []
    if len(words) > 1:
        specific.append(_normalize(words[-1]))
        city.append(_normalize(" ".join(words[:-1])))
    return [t for t in specific if len(t) >= 3], [t for t in city if len(t) >= 3]


def _mentions(text: str, team: str, include_city: bool = True) -> bool:
    specific, city = _team_tokens(team)
    tokens = specific + city if include_city else specific
    return any(t in text for t in tokens)


def _market_text(market: dict[str, Any]) -> str:
    fields = ("title", "subtitle", "yes_sub_title", "no_sub_title")
    return _normalize(" ".join(str(market.get(f) or "") for f in fields))


def _yes_team_is(market: dict[str, Any], team: str, other: str) -> bool:
    yes = _normalize(str(market.get("yes_sub_title") or ""))
    if not yes:
        return False
    # Prefer the nickname/full name; fall back to the city only when it
    # identifies exactly one of the two teams.
    if _mentions(yes, team, include_city=False) or _mentions(yes, other, include_city=False):
        return _mentions(yes, team, include_city=False) and not _mentions(yes, other, include_city=False)
    return _mentions(yes, team) and not _mentions(yes, other)


@dataclass
class ResolvedMarket:
    ticker: str
    side: str   # always "yes" -- see module docstring


class MarketMatcher:
    def __init__(self, kalshi_client: KalshiClient):
        self._kalshi = kalshi_client

    def game_markets(self, snapshot: GameSnapshot) -> list[dict[str, Any]]:
        """Open markets in the sport's series that mention both teams."""
        series_ticker = SERIES_BY_SPORT.get(snapshot.sport)
        if series_ticker is None:
            return []
        return [
            m for m in self._kalshi.iter_markets(series_ticker=series_ticker, status="open")
            if _mentions(_market_text(m), snapshot.home_team)
            and _mentions(_market_text(m), snapshot.away_team)
        ]

    def resolve(self, snapshot: GameSnapshot) -> ResolvedMarket | None:
        game = f"{snapshot.away_team} @ {snapshot.home_team}"
        if snapshot.sport not in SERIES_BY_SPORT:
            logger.warning("No Kalshi series mapped for sport=%s", snapshot.sport)
            return None

        markets = self.game_markets(snapshot)
        if not markets:
            logger.warning("No Kalshi market matched %s -- skipping, will not guess", game)
            return None

        events = {m.get("event_ticker") for m in markets}
        if len(events) > 1:
            logger.warning(
                "AMBIGUOUS: %s matches %d different Kalshi events %s -- skipping",
                game, len(events), sorted(e or "?" for e in events),
            )
            return None

        underdog = snapshot.away_team if snapshot.favorite_team == snapshot.home_team else snapshot.home_team
        favorite_yes = [m for m in markets if _yes_team_is(m, snapshot.favorite_team, underdog)]
        if len(favorite_yes) != 1:
            logger.warning(
                "Found %d Kalshi market(s) for %s but %d name %s as the YES outcome "
                "-- skipping rather than guess the side",
                len(markets), game, len(favorite_yes), snapshot.favorite_team,
            )
            return None

        return ResolvedMarket(ticker=favorite_yes[0]["ticker"], side="yes")
