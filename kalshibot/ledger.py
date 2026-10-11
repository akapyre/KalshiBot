"""The bot's own bets and how they turned out, persisted to
state/ledger.json. Only orders the bot placed are tracked -- bets made by
hand in the Kalshi app are left out of the record."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BetLedger:
    def __init__(self, path: str | Path = "state/ledger.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._bets: list[dict[str, Any]] = (
            json.loads(self._path.read_text()) if self._path.exists() else []
        )

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._bets, indent=2))

    def has(self, order_id: str) -> bool:
        return any(b["order_id"] == order_id for b in self._bets)

    def record(self, *, order_id: str, ticker: str, team: str, sport: str, rule: str,
               contracts: float, cost: float, fee: float | None, placed_at: str | None = None,
               **details: Any) -> dict[str, Any]:
        """`details` adds context for the results page: game ("Away @ Home"),
        pregame_odds, live_odds, price_cents."""
        bet = {
            "order_id": order_id, "ticker": ticker, "team": team, "sport": sport, "rule": rule,
            "contracts": contracts, "cost": round(cost, 4), "fee": None if fee is None else round(fee, 4),
            "placed_at": placed_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "open", "pnl": None, "settled_at": None,
            **{k: v for k, v in details.items() if v is not None},
        }
        with self._lock:
            if not self.has(order_id):
                self._bets.append(bet)
                self._save()
        return bet

    def open_bets(self) -> list[dict[str, Any]]:
        return [b for b in self._bets if b["status"] == "open"]

    def all_bets(self) -> list[dict[str, Any]]:
        return list(self._bets)

    def settle(self, order_id: str, market_result: str) -> dict[str, Any] | None:
        """We only ever buy YES: a "yes" result pays $1 per contract, "no"
        pays nothing. Anything else (a voided market) is a push at $0."""
        with self._lock:
            bet = next((b for b in self._bets if b["order_id"] == order_id and b["status"] == "open"), None)
            if bet is None:
                return None
            outlay = bet["cost"] + (bet["fee"] or 0)
            if market_result == "yes":
                bet["status"], bet["pnl"] = "won", round(bet["contracts"] - outlay, 2)
            elif market_result == "no":
                bet["status"], bet["pnl"] = "lost", round(-outlay, 2)
            else:
                bet["status"], bet["pnl"] = "void", 0.0
            bet["settled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save()
            return bet

    def reset(self) -> Path:
        """Start the record over at 0-0. The old ledger is kept as a dated
        backup next to it; bets still open stay, so they count once they
        settle. Returns the backup path."""
        with self._lock:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self._path.with_name(f"{self._path.stem}-backup-{stamp}.json")
            backup.write_text(json.dumps(self._bets, indent=2))
            self._bets = [b for b in self._bets if b["status"] == "open"]
            self._save()
            return backup

    def summary(self) -> dict[str, Any]:
        done = [b for b in self._bets if b["status"] in ("won", "lost")]
        return {
            "wins": sum(b["status"] == "won" for b in done),
            "losses": sum(b["status"] == "lost" for b in done),
            "net": round(sum(b["pnl"] for b in done), 2),
            "open": len(self.open_bets()),
        }


def record_line(summary: dict[str, Any]) -> str:
    return (f"Record {summary['wins']}-{summary['losses']} · Net {summary['net']:+.2f} USD"
            f" · {summary['open']} open")
