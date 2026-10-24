"""Maker research: would resting limit orders, re-posted every 2 minutes,
have made money? Paper only -- this module has no executor and only reads
Kalshi.

Usage:  python -m kalshibot.maker backtest                  (markets that closed 11-40 days ago)
        python -m kalshibot.maker backtest --skip 0 --days 10

Where this came from (2026-10-23): scoring every public trade from the
maker's side, over markets that closed in the last 10 days, found makers
ahead after fees in NHL game winners, WTA matches and NBA player props
(t 2.3-2.8) and about even elsewhere. That could be luck from testing 21
market types, so this replays a DIFFERENT window (default: markets that
closed 11-40 days ago) with the orders the bot itself could place.

The strategy, replayed over the last HOURS before each market closes:
  - Every POLL_S seconds, read the book (YES bid / ask, minute candles).
  - If the spread is 2c or more, post one cent inside it on both sides:
    buy YES at bid + 1, and sell YES (= buy NO) at ask - 1.
  - Those orders sit until the next poll. A side fills when a real trade in
    that window crossed its price: a taker sold YES at or below our bid, or
    bought YES at or above our ask. Partial fills add up.
  - Each side buys at most $25 per market, held to settlement; a market
    where both sides fill has locked in the spread.
  - Maker fee: 1.75% x contracts x P x (1-P) (a quarter of the taker fee),
    charged per fill, rounded up to the cent -- conservative, as Kalshi
    charges makers nothing on some series.

Result 2026-10-23 (markets that closed 11-40 days ago, 60 per type): the
leads did not hold up -- NHL games -4.9%, WTA -0.5%, NBA props -8.4% (only
12 markets: the season had just started) -- and the controls were +2.1% (NBA
games) and +10.5% (ATP), all within luck (|t| under 1.5). Even with the
optimistic fills below, quoting inside the spread every 2 minutes showed no
edge.

Read the results with this in mind:
  - It assumes we'd be first in line at our price. We'd be alone at a new
    best price, but real traders react to new orders; this is the most
    optimistic part.
  - The trades that "fill" us really happened at a worse price for the
    taker; a taker might not have traded with us at all.
  - A market where we hold a position is one bet, whatever the number of
    fills: the t-stat is over markets.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .backtest import _kalshi_call
from .main import build_kalshi_client

CACHE = Path("state/maker_cache")
STAKE_CENTS = 2500
POLL_S = 120
HOURS = 24
MAKER_FEE = 0.0175
LEADS = ["KXNHLGAME", "KXWTAMATCH", "KXNBAPTS"]
CONTROLS = ["KXNBAGAME", "KXATPMATCH"]


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def maker_fee(contracts: float, price_cents: int) -> float:
    p = price_cents / 100
    return math.ceil(MAKER_FEE * contracts * p * (1 - p) * 100) / 100


# ---------- data (cached) ----------

def _cached(name: str, fetch):
    path = CACHE / name
    if path.exists():
        return json.loads(path.read_text())
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def trades(kalshi, ticker: str) -> list[tuple[float, str, int, float]]:
    """(unix seconds, taker side, YES price in cents, contracts), oldest first."""
    def fetch():
        out, cursor = [], None
        for _ in range(50):
            d = _kalshi_call(kalshi._request, "GET", "/markets/trades",
                             params={"ticker": ticker, "limit": 1000, **({"cursor": cursor} if cursor else {})})
            out += [(t["created_time"], t["taker_side"], float(t["yes_price_dollars"]), float(t["count_fp"]))
                    for t in d.get("trades", [])]
            cursor = d.get("cursor")
            if not cursor:
                break
        return out
    # The earlier analysis cached trades under the same name and format.
    return sorted((_ts(t), side, round(p * 100), n) for t, side, p, n in _cached(f"{ticker}.json", fetch))


def book(kalshi, ticker: str, start: int, end: int) -> dict[int, tuple[int, int]]:
    """{minute end: (YES bid, YES ask) in cents}."""
    def fetch():
        try:
            return _kalshi_call(kalshi.get_candlesticks, ticker, start, end)
        except Exception:
            return []
    out = {}
    for c in _cached(f"book-{ticker}-{start}.json", fetch):
        try:
            out[int(c["end_period_ts"])] = (round(float(c["yes_bid"]["close_dollars"]) * 100),
                                            round(float(c["yes_ask"]["close_dollars"]) * 100))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---------- the replay ----------

@dataclass
class Side:
    """One side of our quoting in one market: what filled, at what price."""
    buys_yes: bool
    fills: list[tuple[float, int, float]] = field(default_factory=list)   # (ts, price paid cents, contracts)

    def spent(self) -> float:
        return sum(price * n for _, price, n in self.fills)

    def room(self) -> float:
        """Cents left to spend before this side reaches $25."""
        return 0.0 if self.spent() >= STAKE_CENTS else (STAKE_CENTS - self.spent())

    def pnl(self, yes_won: bool) -> float:
        win = yes_won if self.buys_yes else not yes_won
        return sum(n * ((100 if win else 0) - price) / 100 - maker_fee(n, price) for _, price, n in self.fills)


def replay(quotes: dict[int, tuple[int, int]], prints: list[tuple[float, str, int, float]],
           start: int, end: int) -> tuple[Side, Side]:
    """Quote one cent inside the spread every POLL_S seconds; fill against
    the trades that happened while each quote was up."""
    yes, no = Side(True), Side(False)
    minutes = sorted(quotes)
    i = j = 0
    for t in range(start, end - POLL_S, POLL_S):
        while i + 1 < len(minutes) and minutes[i + 1] <= t:
            i += 1
        if not minutes or minutes[i] > t:
            continue
        bid, ask = quotes[minutes[i]]
        if ask - bid < 2:
            continue
        my_bid, my_ask = bid + 1, ask - 1           # buy YES at my_bid; buy NO at 100 - my_ask
        while j < len(prints) and prints[j][0] <= t:
            j += 1
        k = j
        while k < len(prints) and prints[k][0] <= t + POLL_S:
            ts, taker, price, n = prints[k]
            if taker == "no" and price <= my_bid and 1 <= my_bid <= 99 and yes.room():
                yes.fills.append((ts, my_bid, min(n, yes.room() / my_bid)))
            if taker == "yes" and price >= my_ask and 1 <= my_ask <= 99 and no.room():
                no.fills.append((ts, 100 - my_ask, min(n, no.room() / (100 - my_ask))))
            k += 1
    return yes, no


@dataclass
class Result:
    series: str
    ticker: str
    close: float
    yes_won: bool
    yes: Side
    no: Side

    def pnl(self, since: float = 0, until: float = float("inf")) -> float:
        total = 0.0
        for side in (self.yes, self.no):
            part = Side(side.buys_yes, [f for f in side.fills if since <= f[0] < until])
            total += part.pnl(self.yes_won)
        return total

    def outlay(self) -> float:
        return (self.yes.spent() + self.no.spent()) / 100


def run_series(kalshi, series: str, days: int, skip: int, per_series: int) -> list[Result]:
    now = int(time.time())
    markets = [m for m in kalshi.iter_markets(series_ticker=series, status="settled",
                                              min_close_ts=now - (skip + days) * 86400,
                                              max_close_ts=now - skip * 86400, max_pages=20)
               if m.get("result") in ("yes", "no") and float(m.get("volume_fp") or 0) > 0]
    random.seed(series)
    markets = random.sample(markets, min(per_series, len(markets)))
    out = []
    for i, m in enumerate(markets, 1):
        print(f"  {series}: {i}/{len(markets)}", end="\r", flush=True)
        end = int(_ts(m["close_time"]))
        start = end - HOURS * 3600
        quotes = book(kalshi, m["ticker"], start, end)
        prints = [p for p in trades(kalshi, m["ticker"]) if start <= p[0] <= end]
        yes, no = replay(quotes, prints, start, end)
        out.append(Result(series, m["ticker"], end, m["result"] == "yes", yes, no))
    print()
    return out


def summarize(label: str, results: list[Result], since_h: float | None = None, until_h: float | None = None) -> str:
    """since_h / until_h: only fills that many hours before close."""
    pnls, outlay, both = [], 0.0, 0
    for r in results:
        a = r.close - since_h * 3600 if since_h is not None else 0
        b = r.close - until_h * 3600 if until_h is not None else float("inf")
        filled = [any(a <= f[0] < b for f in s.fills) for s in (r.yes, r.no)]
        if any(filled):
            pnls.append(r.pnl(a, b))
            outlay += sum(f[1] * f[2] for s in (r.yes, r.no) for f in s.fills if a <= f[0] < b) / 100
            both += all(filled)
    if not pnls:
        return f"  {label:28} no fills"
    t = ""
    if len(pnls) > 2 and statistics.stdev(pnls):
        t = f"  t {statistics.mean(pnls) / statistics.stdev(pnls) * math.sqrt(len(pnls)):+5.1f}"
    return (f"  {label:28} filled in {len(pnls):3}/{len(results):3} markets (both sides {both:3})  "
            f"bought ${outlay:8.2f}  net {sum(pnls):+8.2f}  ROI {sum(pnls) / outlay:+6.1%}{t}")


def backtest(days: int, skip: int, per_series: int) -> None:
    kalshi = build_kalshi_client(demo=False)
    print(f"\nMaker replay: markets that closed {skip + 1}-{skip + days} days ago, last {HOURS}h of each, "
          f"re-quoting every {POLL_S}s, $25 per side.")
    for group, names in (("LEADS (looked good 2026-10-23)", LEADS), ("CONTROLS (looked about even)", CONTROLS)):
        print(f"\n=== {group} ===")
        for series in names:
            results = run_series(kalshi, series, days, skip, per_series)
            print(summarize(series, results))
            print(summarize("  more than 3h before close", results, until_h=3))
            print(summarize("  last 3h", results, since_h=3))
    print("\nReminder: optimistic about fills -- see the notes at the top of kalshibot/maker.py.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Maker research (paper only, never bets)")
    parser.add_argument("command", choices=["backtest"])
    parser.add_argument("--days", type=int, default=30, help="window length in days (default 30)")
    parser.add_argument("--skip", type=int, default=10, help="skip the most recent N days (default 10)")
    parser.add_argument("--markets", type=int, default=60, help="markets sampled per series (default 60)")
    args = parser.parse_args()
    backtest(args.days, args.skip, args.markets)


if __name__ == "__main__":
    sys.exit(main())
