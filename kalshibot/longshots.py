"""Long-shot NO scanner: non-sports markets where YES is close to impossible
and NO can be bought at 95-99c. Paper only -- this module has no executor and
only reads Kalshi.

Usage:  python -m kalshibot.longshots scan                (NO at 98-99c)
        python -m kalshibot.longshots scan --min-no 97 --max-days 14 --budget 500
        python -m kalshibot.longshots scan --stake 50 --budget 1000

The trade: buy NO at 95-99c, collect 1-5c per contract when nothing happens,
lose the whole stake when it does. At 97c one upset wipes out ~33 wins, so
this only pays if upsets are rarer than the price says.

Backtest 2026-10-23 (settled non-sports markets, last 18 months, NO bought at
the YES bid 30 / 7 / 1 days before close, $25, taker fee):
  - NO at 90-94c: YES won MORE often than priced at every checkpoint
    (14 vs 9, 24 vs 15, 20 vs 12); ROI about -5%.
  - NO at 95-97c: about even to -5%.
  - NO at 98-99c: 7 upsets vs 11 implied over the three checkpoints; ROI
    +1.3%, +0.5%, -0.1% -- small, and within luck.
So the default here is 98c+. Nothing beat the prices by enough to matter.

The basket (--budget) spreads $25 bets across different events -- at most one
per event, because the outcomes of one event move together -- and prefers the
best return per day held.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .backtest import _kalshi_call
from .main import build_kalshi_client

STAKE = 25.0
TAKER_FEE = 0.07
SKIP_CATEGORIES = {"Sports", "Crypto", "Financials", "Climate and Weather", "Commodities"}


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


@dataclass
class Pick:
    ticker: str
    event: str
    category: str
    title: str
    outcome: str
    no_cost: float          # dollars per NO contract
    depth: float            # NO contracts offered at that price
    days: float             # to expected settlement
    volume: float

    def profit(self, stake: float = STAKE) -> float:
        """Dollars won on `stake` if NO wins, after the taker fee."""
        n = stake / self.no_cost
        fee = TAKER_FEE * n * self.no_cost * (1 - self.no_cost)
        return n * (1 - self.no_cost) - fee

    def per_day(self) -> float:
        return self.profit() / STAKE / max(self.days, 1)


def candidates(events: list[dict[str, Any]], now: float, min_no: float = 0.98, max_no: float = 0.99,
               max_days: float = 45, min_volume: float = 5000) -> list[Pick]:
    out = []
    for e in events:
        category = e.get("category") or ""
        if category in SKIP_CATEGORIES or not category:
            continue
        for m in e.get("markets", []):
            if m.get("status") not in (None, "active") or "KXMVE" in m["ticker"]:
                continue
            no_cost = float(m.get("no_ask_dollars") or 1)
            depth = float(m.get("yes_bid_size_fp") or 0)   # YES bids are the NO offers
            if not (min_no <= no_cost <= max_no) or depth <= 0:
                continue
            volume = float(m.get("volume_fp") or 0)
            days = (_ts(m.get("expected_expiration_time") or m["close_time"]) - now) / 86400
            if volume < min_volume or days > max_days or days < 0:
                continue
            out.append(Pick(m["ticker"], e["event_ticker"], category, e.get("title") or "",
                            m.get("yes_sub_title") or "", no_cost, depth, days, volume))
    return out


def basket(picks: list[Pick], budget: float, stake: float = STAKE) -> list[Pick]:
    """`stake`-dollar bets, one per event, best return per day first, up to
    budget. Skips markets without `stake` dollars of NO offered at the price."""
    chosen, events = [], set()
    for p in sorted(picks, key=lambda p: -p.per_day()):
        if len(chosen) * stake + stake > budget:
            break
        if p.event in events or p.depth * p.no_cost < stake:
            continue
        chosen.append(p)
        events.add(p.event)
    return chosen


def open_events(kalshi) -> list[dict[str, Any]]:
    events, cursor = [], None
    for _ in range(300):
        page = _kalshi_call(kalshi._request, "GET", "/events",
                            params={"status": "open", "with_nested_markets": "true", "limit": 200,
                                    **({"cursor": cursor} if cursor else {})})
        events += page.get("events", [])
        cursor = page.get("cursor")
        if not cursor:
            break
    return events


def _line(p: Pick, stake: float = STAKE) -> str:
    return (f"  NO {p.no_cost * 100:5.1f}c  {p.days:4.0f}d  +${p.profit(stake):5.2f}  {p.category[:11]:11}  "
            f"{p.title[:48]:48} | {p.outcome[:32]}")


def scan(min_no: float, max_days: float, min_volume: float, budget: float, show: int, stake: float) -> None:
    kalshi = build_kalshi_client(demo=False)
    print("Reading open Kalshi events...", flush=True)
    picks = candidates(open_events(kalshi), time.time(), min_no=min_no, max_days=max_days, min_volume=min_volume)
    print(f"\n{len(picks)} markets: NO at {min_no * 100:.0f}-99c, settling within {max_days:.0f} days, "
          f"volume {min_volume:,.0f}+ (non-sports)."
          f"\nColumns: NO price, days to settle, profit on ${stake:.0f} if NO wins (after fee).\n")
    for p in sorted(picks, key=lambda p: p.days)[:show]:
        print(_line(p, stake))
    chosen = basket(picks, budget, stake)
    if chosen:
        win = sum(p.profit(stake) for p in chosen)
        print(f"\nBasket for ${budget:.0f} ({len(chosen)} x ${stake:.0f}, one per event, best return per day):")
        for p in chosen:
            print(_line(p, stake))
        print(f"\n  If every NO wins: +${win:.2f}.  Each upset costs ${stake:.0f} (= {stake / (win / len(chosen)):.0f} "
              f"average wins).  Paper only: nothing was bought.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Long-shot NO scanner (paper only, never bets)")
    parser.add_argument("command", choices=["scan"])
    parser.add_argument("--min-no", type=float, default=98, help="cheapest NO price in cents (default 98)")
    parser.add_argument("--max-days", type=float, default=45, help="settles within N days (default 45)")
    parser.add_argument("--min-volume", type=float, default=5000, help="contracts traded (default 5000)")
    parser.add_argument("--budget", type=float, default=500, help="basket size in dollars (default 500)")
    parser.add_argument("--stake", type=float, default=25, help="dollars per bet (default 25)")
    parser.add_argument("--show", type=int, default=40, help="markets listed, soonest first (default 40)")
    args = parser.parse_args()
    scan(args.min_no / 100, args.max_days, args.min_volume, args.budget, args.show, args.stake)


if __name__ == "__main__":
    sys.exit(main())
