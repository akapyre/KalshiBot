"""Maps a live game to the Kalshi market to buy for the pregame favorite.

Kalshi's game-winner layout, confirmed against real KXNHLGAME data on
2026-09-29 (`python -m kalshibot.diagnose markets <sport>` shows it):

    event_ticker   KXNHLGAME-26OCT06MINBUF        one event per game, dated
    ticker         KXNHLGAME-26OCT06MINBUF-MIN    one market per team
    title          "Minnesota wins"
    yes_sub_title  "Minnesota"                    short name, one team only

So a game is found by grouping markets into events, identifying which
team each market's yes_sub_title names, and requiring the date in the
event ticker to match the game's date -- the same two teams can have
several open events at once. Other sports are assumed to follow the same
layout until checked with the diagnostic.

Safety rule: the bot only ever buys YES on the market that clearly names
the favorite. It never buys NO on the other team (in soccer that also
pays on a draw), and when anything is ambiguous it skips.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .kalshi_client import KalshiClient
from .odds_providers.base import GameSnapshot, Sport

logger = logging.getLogger("kalshibot.matching")

# Each sport's Kalshi game-winner series. A sport can span several series
# (tennis: men's and women's tours are separate).
SERIES_BY_SPORT: dict[Sport, list[str]] = {
    "nhl": ["KXNHLGAME"],      # confirmed 2026-09-29
    "mlb": ["KXMLBGAME"],      # confirmed 2026-09-29 (tickers add a start time: 26SEP292000)
    "nfl": ["KXNFLGAME"],      # confirmed 2026-10-04 (Detroit order filled)
    "cfb": ["KXNCAAFGAME"],    # seen in the account's settlements 2026-10-04
    # Picked from Kalshi's series list ("ATP Tennis Match", "WTA Tennis
    # Match") 2026-10-07; market layout not yet checked.
    "tennis": ["KXATPMATCH", "KXWTAMATCH"],
    # Unverified guess -- confirm with `diagnose markets soccer`.
    "soccer": ["KXSOCCERGAME"],
}

# Sports whose Kalshi event date may be a day off from the game date
# computed here: tennis is played worldwide (e.g. Shanghai in October), so
# a match's local date and the US date can differ.
DATE_SLACK_DAYS: dict[Sport, int] = {"tennis": 1}

_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _label_names(label: str, team: str) -> bool:
    """Does a Kalshi short name ("Vegas", "St. Louis", "NY Rangers") refer
    to this full team name ("Vegas Golden Knights")?"""
    lab = _normalize(label)
    if len(lab) < 3:
        return False
    words = team.split()
    specific = [_normalize(team)] + ([_normalize(words[-1])] if len(words) > 1 else [])
    return lab in _normalize(team) or any(len(t) >= 3 and t in lab for t in specific)


def _label_side(label: str, home: str, away: str) -> str | None:
    """"home"/"away" if the label names exactly one of the two teams."""
    h, a = _label_names(label, home), _label_names(label, away)
    if h and not a:
        return "home"
    if a and not h:
        return "away"
    return None


def event_date(event_ticker: str) -> date | None:
    match = _EVENT_DATE_RE.search(event_ticker or "")
    if not match or match.group(2) not in _MONTHS:
        return None
    yy, mon, dd = match.groups()
    try:
        return date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None


def game_date(snapshot: GameSnapshot) -> date | None:
    """The game's US calendar date. Kickoff times are UTC; shifting back 6
    hours puts every US evening game (up to ~midnight Eastern) and every
    European daytime match on its local date."""
    try:
        start = datetime.fromisoformat(snapshot.start_time_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (start - timedelta(hours=6)).date()


@dataclass
class ResolvedMarket:
    ticker: str
    side: str   # always "yes" -- see module docstring


class MarketMatcher:
    def __init__(self, kalshi_client: KalshiClient):
        self._kalshi = kalshi_client

    def candidate_events(self, snapshot: GameSnapshot) -> dict[str, dict[str, dict[str, Any]]]:
        """Open events between these two teams, on any date:
        {event_ticker: {"home": market, "away": market}}."""
        by_event: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for series_ticker in SERIES_BY_SPORT.get(snapshot.sport, []):
            for m in self._kalshi.iter_markets(series_ticker=series_ticker, status="open"):
                side = _label_side(str(m.get("yes_sub_title") or ""), snapshot.home_team, snapshot.away_team)
                if side is not None:
                    by_event[m.get("event_ticker") or ""][side].append(m)
        # Exactly one market per team, or the event isn't a plain
        # "<team> wins" game (e.g. several score markets naming one team).
        return {
            e: {side: ms[0] for side, ms in sides.items()}
            for e, sides in by_event.items()
            if len(sides.get("home", [])) == 1 and len(sides.get("away", [])) == 1
        }

    def resolve(self, snapshot: GameSnapshot) -> ResolvedMarket | None:
        game = f"{snapshot.away_team} @ {snapshot.home_team}"
        if snapshot.sport not in SERIES_BY_SPORT:
            logger.warning("No Kalshi series mapped for sport=%s", snapshot.sport)
            return None

        events = self.candidate_events(snapshot)
        if not events:
            logger.warning("No Kalshi market matched %s -- skipping, will not guess", game)
            return None

        day = game_date(snapshot)
        same_day = [e for e in events if day is not None and event_date(e) == day]
        slack = DATE_SLACK_DAYS.get(snapshot.sport, 0)
        if not same_day and slack and day is not None:
            same_day = [
                e for e in events
                if event_date(e) is not None and abs((event_date(e) - day).days) <= slack
            ]
        if len(same_day) != 1:
            logger.warning(
                "%s: need exactly one Kalshi event dated %s, found %d (open events for "
                "these teams: %s) -- skipping",
                game, day, len(same_day), sorted(events),
            )
            return None

        fav_side = "home" if snapshot.favorite_team == snapshot.home_team else "away"
        return ResolvedMarket(ticker=events[same_day[0]][fav_side]["ticker"], side="yes")
