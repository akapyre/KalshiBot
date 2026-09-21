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
