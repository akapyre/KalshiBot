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
    """Persists each game's captured pregame line, so a bot restart (not
    just a sleep/wake, an actual process restart) doesn't lose the baseline
    for a game already in progress -- it would otherwise fall back to
    treating "pregame odds" as "whatever the live odds are right now,"
    which can never trigger a rule that depends on the line having moved."""

    def __init__(self, path: str | Path = "state/pregame_odds.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if self._path.exists():
            return json.loads(self._path.read_text())
        return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def get(self, game_id: str) -> int | None:
        with self._lock:
            return self._data.get(game_id)

    def set(self, game_id: str, odds: int) -> None:
        """Overwrites unconditionally -- called every cycle a game is still
        pregame, so the stored value tracks the line right up to kickoff
        (closing line), not just the first value ever seen (opening line).
        Once a game goes live, the caller stops calling this for it, so the
        last pregame value written is what persists."""
        with self._lock:
            self._data[game_id] = odds
            self._save()
