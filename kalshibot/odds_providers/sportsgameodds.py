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

TENNIS: not on the original plan; the upgraded plan lists TENNIS leagues
ATP and WTA (`diagnose leagues`, 2026-10-07). Tennis events parse like
the team sports: players under teams.home/away, the match moneyline under
statID "points", score in sets.

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

REQUEST `limit` MATTERS A LOT: confirmed 2026-09-26 that a busy CFB
Saturday has 100+ total events in the startsAfter/startsBefore window
across all divisions, and the API's ordering does NOT put currently-live
games first -- every live: true game that day was past position 79. At
the old limit of 25, the bot was structurally incapable of ever seeing a
live CFB game, no matter what was actually happening; it silently looked
fine (0 live games, no errors) while being blind to all of them. Raised
to 100, which covered that day's slate, but there's no guarantee 100 is
always enough on the busiest days -- if this happens again, the real fix
is paginating via the response's `nextCursor` rather than raising this
number indefinitely (each unit of `limit` costs against the monthly
object quota on every poll cycle, for every sport, so this isn't free).
"""
from __future__ import annotations

import logging
import os
import re
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
    "tennis": ["ATP", "WTA"],
    "mlb": ["MLB"],  # confirmed present in GET /v2/leagues
    "cfb": ["NCAAF"],  # confirmed present in GET /v2/leagues (early exploration, not the MLB/NFL session)
    "nhl": ["NHL"],  # confirmed present in GET /v2/leagues
}


class SportsGameOddsProvider:
    def __init__(self, api_key: str | None = None, pregame_store: PregameOddsStore | None = None):
        self._api_key = api_key or os.environ["SPORTSGAMEODDS_API_KEY"]
        self._pregame_store = pregame_store or PregameOddsStore()
        # Some games (e.g. small-college matchups with no real betting
        # market) will NEVER get a moneyline -- warn about each one once,
        # not every single poll cycle forever.
        self._warned_no_odds: set[str] = set()

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key}

    def fetch_events(self, sport: Sport) -> list[dict[str, Any]]:
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

        resp = requests.get(
            f"{API_BASE}/events",
            headers=self._headers(),
            params={
                "leagueID": ",".join(league_ids),
                "limit": 100,
                "startsAfter": starts_after,
                "startsBefore": starts_before,
            },
            # limit=100 responses (with full rosters and odds per event) are
            # large enough that 10s read timeouts were happening regularly.
            timeout=30,
        )
        resp.raise_for_status()
        events = resp.json().get("data", [])
        logger.debug("%s: %d raw events returned for leagues %s", sport, len(events), league_ids)
        return events

    def list_live_games(self, sport: Sport) -> list[GameSnapshot]:
        snapshots = []
        for event in self.fetch_events(sport):
            if event.get("type") != "match":
                continue  # skip prop-only/novelty entries (e.g. Puppy Bowl)
            snapshot = self._parse_event(sport, event)
            if snapshot is None:
                continue
            if not _has_started(event.get("status", {})):
                side = "home" if snapshot.favorite_team == snapshot.home_team else "away"
                self._pregame_store.set(snapshot.game_id, side, snapshot.live_favorite_odds)
            snapshots.append(snapshot)
        return [s for s in snapshots if s.is_live]

    def get_pregame_odds(self, sport: Sport, game_id: str) -> int | None:
        stored = self._pregame_store.get(game_id)
        return stored[1] if stored else None

    def _parse_event(self, sport: Sport, event: dict[str, Any]) -> GameSnapshot | None:
        try:
            game_id = event["eventID"]
            teams = event["teams"]
            home = teams["home"]
            away = teams["away"]
            home_name = home["names"]["long"]
            away_name = away["names"]["long"]

            home_ml = away_ml = None
            close: dict[str, int] = {}
            for odd in event.get("odds", {}).values():
                # Only the game-winner line. Other stats share the "ml" bet
                # type -- "Shots On Goal Moneyline" (statID shots_onGoal) and
                # "First Goal Moneyline" were being read as the moneyline,
                # which is where Edmonton's frozen -770 came from.
                if (odd.get("statID") != "points" or odd.get("periodID") != "game"
                        or odd.get("betTypeID") != "ml"):
                    continue
                side = odd.get("sideID")
                closing = odd.get("closeBookOdds") or odd.get("closeFairOdds")
                if closing is not None and side in ("home", "away"):
                    close[side] = int(closing)
                price = odd.get("bookOdds") or odd.get("fairOdds")
                if price is None:
                    continue
                if side == "home":
                    home_ml = int(price)
                elif side == "away":
                    away_ml = int(price)

            if home_ml is None or away_ml is None:
                if game_id not in self._warned_no_odds:
                    self._warned_no_odds.add(game_id)
                    logger.warning(
                        "No game-moneyline odds for %s @ %s (eventID=%s) -- dropping "
                        "this game from live_games (won't repeat this warning for "
                        "the same game)",
                        away_name, home_name, game_id,
                    )
                return None

            status = event.get("status", {})

            # Before the game starts, the favorite is whoever the current
            # line favors and that line IS the pregame line. Once it starts,
            # the favorite is locked to the stored pregame side, and "live
            # odds" means that same team's current price -- even after the
            # other team becomes favored.
            if not _has_started(status):
                fav_side = "home" if home_ml < away_ml else "away"
                pregame_odds = home_ml if fav_side == "home" else away_ml
            elif len(close) == 2:
                # The provider's own closing line -- the true pregame price,
                # available even when the bot was started mid-game.
                fav_side = "home" if close["home"] < close["away"] else "away"
                pregame_odds = close[fav_side]
            else:
                stored = self._pregame_store.get(game_id)
                if stored is not None:
                    fav_side, pregame_odds = stored
                else:
                    # Never seen pregame: no baseline, so rules skip it.
                    fav_side = "home" if home_ml < away_ml else "away"
                    pregame_odds = None

            favorite_team = home_name if fav_side == "home" else away_name
            live_favorite_odds = home_ml if fav_side == "home" else away_ml

            current_period = status.get("currentPeriodID", "")
            period = _parse_period_number(current_period)
            started_periods = status.get("periods", {}).get("started", [])
            past_halftime = "2h" in started_periods if sport == "soccer" else None

            return GameSnapshot(
                game_id=game_id,
                sport=sport,
                home_team=home_name,
                away_team=away_name,
                favorite_team=favorite_team,
                pregame_favorite_odds=pregame_odds,
                live_favorite_odds=live_favorite_odds,
                start_time_utc=status.get("startsAt", ""),
                is_live=bool(status.get("live", False)),
                is_final=bool(status.get("completed", False) or status.get("finalized", False)),
                period=period,
                seconds_remaining=_parse_clock_seconds(status),
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


def _has_started(status: dict[str, Any]) -> bool:
    return bool(
        status.get("started") or status.get("live")
        or status.get("completed") or status.get("finalized")
    )


def _parse_period_number(period_id: str) -> int | None:
    """"1q" -> 1, "4q" -> 4, "2h" -> 2, "3p" -> 3, "10t" -> 10;
    "ot"/"so"/"game"/"reg"/"" -> None. Reads every leading digit, so extra
    innings ("10t") don't collapse to 1. MLB/NHL formats are assumed from
    the NFL/soccer pattern, not yet confirmed against real responses."""
    match = re.match(r"\d+", period_id or "")
    return int(match.group()) if match else None


_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _parse_clock_seconds(status: dict[str, Any]) -> int | None:
    """Seconds left in the current period. UNVERIFIED: no live NHL response
    has been inspected yet, so this tries a numeric/"mm:ss" `clock` field,
    then any "mm:ss" in the display strings. Returns None if none found --
    the NHL rule treats an unknown clock in the 3rd period as "too late"."""
    clock = status.get("clock")
    if isinstance(clock, (int, float)) and not isinstance(clock, bool):
        return int(clock)
    for text in (clock, status.get("displayShort"), status.get("displayLong")):
        if isinstance(text, str):
            match = _CLOCK_RE.search(text)
            if match:
                return int(match.group(1)) * 60 + int(match.group(2))
    return None
