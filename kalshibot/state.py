"""Per-game bet-state tracking, persisted to disk so a restart never causes
a duplicate wager on a game the bot already bet on."""
from __future__ import annotations

import json
import threading
from pathlib import Path


class BetStateStore:
    def __init__(self, path: str | Path = "state/bets.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, list[str]] = self._load()

    def _load(self) -> dict[str, list[str]]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def fired_rules(self, game_id: str) -> list[str]:
        with self._lock:
            return list(self._data.get(game_id, []))

    def has_fired(self, game_id: str, rule_id: str) -> bool:
        return rule_id in self.fired_rules(game_id)

    def record(self, game_id: str, rule_id: str) -> None:
        with self._lock:
            self._data.setdefault(game_id, [])
            if rule_id not in self._data[game_id]:
                self._data[game_id].append(rule_id)
            self._save()


class PregameOddsStore:
    """Persists each game's pregame favorite (which side, and its odds), so
    a process restart doesn't lose the baseline for a game in progress.

    The side matters as much as the odds: once a game is live, the rules
    follow THIS team's price. Re-deriving "the favorite" from live odds
    would silently switch to the other team the moment the favorite fell
    behind -- and then bet on the wrong team."""

    def __init__(self, path: str | Path = "state/pregame_odds.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def get(self, game_id: str) -> tuple[str, int] | None:
        """Returns (side, odds) -- side is "home" or "away" -- or None."""
        with self._lock:
            entry = self._data.get(game_id)
        # Older versions stored a bare odds number with no side; without the
        # side we can't know which team it belonged to, so treat as absent.
        if not isinstance(entry, dict):
            return None
        return entry["side"], entry["odds"]

    def set(self, game_id: str, side: str, odds: int) -> None:
        """Overwrites unconditionally -- called every cycle a game is still
        pregame, so the stored value tracks the line right up to kickoff
        (closing line). Once a game starts, the caller stops calling this."""
        with self._lock:
            self._data[game_id] = {"side": side, "odds": odds}
            self._save()
