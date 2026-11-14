"""Paper rule kind `cbb_dip`: men's college basketball favorite dips live.

A team priced 90c+ (YES ask) at tip-off whose live YES ask later falls to
`dip_cents` or lower -> one paper bet on it, at that ask. Kalshi only: no
sportsbook feed or ESPN is needed. Tip-off = occurrence_datetime (else
expected_expiration_time) minus 3h, the same offset the backtest used.

Backtest (2026-11-08, 1-minute Kalshi prices, 1,124 men's games with a
90c+ favorite, Nov 2025 - Nov 2026, after the taker fee), first live ask at
or below: 85c +3.1% (370 bets), 75c +7.9% (232), 60c +12.2% (129); 30c and
below lost. Women's games: no edge. Not proven (t ~2 at best, after trying
many cut-offs), so this is paper only.

Like the rest of paper.py, nothing here can place an order.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .executor import american_odds, yes_ask_cents
from .kalshi_client import KalshiClient
from .paper import PaperBook, kalshi_fee

logger = logging.getLogger(__name__)

START_OFFSET = timedelta(hours=3)   # occurrence_datetime is ~3h after tip-off
TIP_WINDOW_S = 20 * 60              # record tip-off prices in the 20 min before tip-off
LIVE_AFTER_S = 60                   # live = at least a minute after tip-off
LIVE_FOR_S = 4 * 3600               # stop looking 4h after tip-off


@dataclass(frozen=True)
class CbbDipRule:
    id: str
    summary: str
    series: str            # KXNCAAMBGAME
    tip_min_cents: int     # favorite's ask at tip-off, at least
    dip_cents: int         # bet the first live ask at or below this...
    floor_cents: int       # ...but not below this
    paper: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CbbDipRule":
        return cls(id=d["id"], summary=" ".join(str(d.get("summary", "")).split()),
                   series=d.get("series", "KXNCAAMBGAME"), tip_min_cents=int(d.get("tip_min_cents", 90)),
                   dip_cents=int(d["dip_cents"]), floor_cents=int(d.get("floor_cents", 40)))


def load_cbb_dip_rules(path: str | Path = "config/paper_rules.yaml") -> list[CbbDipRule]:
    p = Path(path)
    raw = (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
    return [CbbDipRule.from_dict(r) for r in raw.get("rules", [])
            if r.get("kind") == "cbb_dip" and r.get("enabled", True) is not False]


def tip_off(market: dict[str, Any]) -> float | None:
    when = market.get("occurrence_datetime") or market.get("expected_expiration_time")
    try:
        return (datetime.fromisoformat(str(when).replace("Z", "+00:00")) - START_OFFSET).timestamp()
    except ValueError:
        return None


class CbbDipWatcher:
    """Each cycle: one listing of the series' open markets. Before tip-off it
    saves each team's ask (the last one seen counts); once live, it paper-bets
    the tip-off favorite when its ask dips into a rule's range."""

    def __init__(self, kalshi: KalshiClient, rules: list[CbbDipRule], stake_usd: float, book: PaperBook,
                 tips_path: str | Path = "state/cbb_tip_prices.json"):
        self._kalshi = kalshi
        self.rules = rules
        self._stake = stake_usd
        self.book = book
        self._tips_path = Path(tips_path)
        self._tips: dict[str, dict[str, Any]] = (json.loads(self._tips_path.read_text())
                                                 if self._tips_path.exists() else {})

    def _save_tips(self, now: float) -> None:
        self._tips = {e: t for e, t in self._tips.items() if now - t["tip_off"] < 2 * LIVE_FOR_S}
        self._tips_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tips_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tips, indent=1))
        os.replace(tmp, self._tips_path)

    def run(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for series in sorted({r.series for r in self.rules}):
            events: dict[str, list[dict[str, Any]]] = {}
            for m in self._kalshi.iter_markets(series_ticker=series, status="open"):
                events.setdefault(str(m.get("event_ticker")), []).append(m)
            changed = False
            for event, markets in events.items():
                start = tip_off(markets[0])
                if start is None or len(markets) != 2:
                    continue
                if start - TIP_WINDOW_S <= now <= start:
                    asks = {str(m["ticker"]): yes_ask_cents(m) for m in markets}
                    self._tips[event] = dict(tip_off=start, asks=asks)
                    changed = True
                elif start + LIVE_AFTER_S <= now <= start + LIVE_FOR_S and event in self._tips:
                    self._check(event, markets, self._tips[event])
            if changed:
                self._save_tips(now)

    def _check(self, event: str, markets: list[dict[str, Any]], tip: dict[str, Any]) -> None:
        asks = {t: a for t, a in tip["asks"].items() if a is not None}
        if not asks:
            return
        fav_ticker = max(asks, key=asks.get)
        fav = next((m for m in markets if m["ticker"] == fav_ticker), None)
        if fav is None:
            return
        ask = yes_ask_cents(fav)
        other = next(m for m in markets if m is not fav)
        game = f"{fav.get('yes_sub_title')} vs {other.get('yes_sub_title')}"
        for rule in self.rules:
            if (rule.series not in event or asks[fav_ticker] < rule.tip_min_cents or ask is None
                    or not rule.floor_cents <= ask <= rule.dip_cents or self.book.has(event, rule.id)):
                continue
            contracts = int(self._stake * 100 // ask)
            self.book.add(dict(
                rule=rule.id, game_id=event, game=game, ticker=fav_ticker,
                label=f"{fav.get('yes_sub_title')} wins (was {asks[fav_ticker]}c at tip-off)", price_cents=ask,
                contracts=contracts, cost=round(contracts * ask / 100, 2), fee=kalshi_fee(contracts, ask),
                tip_cents=asks[fav_ticker], bid_cents=round(float(fav.get("yes_bid_dollars") or 0) * 100),
                placed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), status="open", pnl=None))
            logger.info("PAPER BET (no order placed): %s -- %s, %s at Kalshi %d¢ (%+d); %d¢ at tip-off",
                        rule.id, game, fav.get("yes_sub_title"), ask, american_odds(ask), asks[fav_ticker])
