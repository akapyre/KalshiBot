"""Odds/game-state provider interface.

Any live-odds + live-score vendor (TheRundown, SportsDataIO, OddsJam, ...)
can be wired in by implementing this interface. The rules engine only
depends on this shape, never on a specific vendor's response format.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Sport = Literal["soccer", "tennis", "nfl", "mlb", "cfb"]


@dataclass
class GameSnapshot:
    game_id: str                 # provider's stable id for this game
    sport: Sport
    home_team: str
    away_team: str
    favorite_team: str           # team favored PREGAME (fixed once the game starts)
    pregame_favorite_odds: int   # American odds for favorite_team, captured pregame
    live_favorite_odds: int      # current American odds for favorite_team
    start_time_utc: str
    is_live: bool
    is_final: bool
    # game-state fields, only populated where the sport/rule needs them
    period: int | None = None            # e.g. NFL quarter (1-4, 5=OT) or MLB inning
    home_score: int | None = None
    away_score: int | None = None
    past_halftime: bool | None = None


class OddsProvider(Protocol):
    def list_live_games(self, sport: Sport) -> list[GameSnapshot]:
        """Return current snapshots for all in-progress games in a sport."""
        ...

    def get_pregame_odds(self, sport: Sport, game_id: str) -> int | None:
        """Return the favorite's American odds captured before kickoff/tipoff,
        or None if the game hasn't been seen pregame yet."""
        ...
