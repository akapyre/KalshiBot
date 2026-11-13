"""Record the last minutes of Kalshi's 15-minute crypto markets, second by
second, and test whether a fair-price model would have beaten them.

Why: BTC/ETH/SOL/XRP 15-minute markets settle on the AVERAGE of the 60
BRTI prices in the final minute (checked with `last_minute --rules`,
2026-11-13). With 20 seconds left, two-thirds of that average is already
fixed, so the true chance can be worked out from the live price -- the
question is whether Kalshi's quotes in those seconds lag behind it.

Usage (on the PC; reads only, never trades):
  python -m kalshibot.recorder                record BTC ETH SOL XRP, last 3 min of every window
  python -m kalshibot.recorder --coins BTC ETH
  python -m kalshibot.recorder analyze        replay the recordings against the model

Recording: from 3 minutes before each quarter-hour close until just after
it, once a second per coin: Coinbase's spot price (stand-in for CF
Benchmarks' BRTI, which is built from the big exchanges and isn't free to
watch live) and Kalshi's best YES/NO prices with sizes. Lines go to
state/recordings/<date>.jsonl.

Model: the final value = average of the 60 seconds before close. Seconds
already past are known (from the recorded prices); the rest are a random
walk from the current price with the volatility measured over the last
two minutes, plus a small allowance for Coinbase-vs-BRTI differences.
That gives the chance YES wins, compared with Kalshi's ask.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .backtest import kalshi_fee

COINS = ["BTC", "ETH", "SOL", "XRP"]
SERIES = {c: f"KX{c}15M" for c in COINS}
COINBASE = "https://api.exchange.coinbase.com/products/{}-USD/ticker"
WINDOW_S = 15 * 60
RECORD_BEFORE_S = 180
RECORD_AFTER_S = 3
OUT_DIR = Path("state/recordings")

# Model settings (analyze).
AVG_SECONDS = 60
VOL_LOOKBACK_S = 120
BASIS_BP = 1.0          # allowance for Coinbase vs BRTI, in basis points of price
MIN_EDGE_CENTS = 3      # model chance minus ask (fee on top) needed to "buy"
STAKE_DOLLARS = 100


# ---------------------------------------------------------------- recording

def book_levels(resp: dict[str, Any], side: str) -> list[tuple[int, float]]:
    """Bids on one side as [(cents, contracts)], best first, from either
    orderbook format Kalshi sends."""
    book = resp.get("orderbook_fp") or resp.get("orderbook") or {}
    raw = book.get(f"{side}_dollars")
    levels = []
    if raw:
        for price, qty in raw:
            levels.append((round(float(price) * 100), float(qty)))
    else:
        for price, qty in book.get(side) or []:
            levels.append((int(price), float(qty)))
    return sorted(levels, reverse=True)


def top_of_book(resp: dict[str, Any]) -> dict[str, Any]:
    """Best YES/NO asks (100 - the other side's best bid) and their sizes."""
    yes, no = book_levels(resp, "yes"), book_levels(resp, "no")
    out: dict[str, Any] = {}
    if no:
        out["yes_ask"], out["yes_ask_qty"] = 100 - no[0][0], no[0][1]
    if yes:
        out["no_ask"], out["no_ask_qty"] = 100 - yes[0][0], yes[0][1]
    return out


def next_close(now: datetime) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % WINDOW_S + WINDOW_S, tz=timezone.utc)


def find_market(kalshi, series: str, close: datetime) -> dict[str, Any] | None:
    for m in kalshi.list_markets(series_ticker=series, status="open", limit=20).get("markets", []):
        try:
            if datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) == close:
                return m
        except (KeyError, ValueError):
            continue
    return None


def spot(coin: str) -> float | None:
    try:
        r = requests.get(COINBASE.format(coin), timeout=3)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def record(kalshi, coins: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pool = ThreadPoolExecutor(max_workers=2 * len(coins))
    print(f"Recording {', '.join(coins)}: last {RECORD_BEFORE_S // 60} minutes of every 15-minute window. "
          "Ctrl+C to stop.", flush=True)
    while True:
        close = next_close(datetime.now(timezone.utc))
        start = close.timestamp() - RECORD_BEFORE_S
        wait = start - time.time() - 10
        if wait > 0:
            time.sleep(wait)
        markets = {}
        for c in coins:
            try:
                m = find_market(kalshi, SERIES[c], close)
            except Exception as e:
                m = None
                print(f"  {c}: couldn't list markets ({e})")
            if m:
                markets[c] = m
        if not markets:
            print(f"  {close:%H:%M} UTC: no open markets found; skipping this window", flush=True)
            time.sleep(max(1.0, close.timestamp() - time.time() + 1))
            continue
        path = OUT_DIR / f"{close:%Y-%m-%d}.jsonl"
        while time.time() < start:
            time.sleep(0.05)
        print(f"  {close:%H:%M} UTC: recording {', '.join(m['ticker'] for m in markets.values())}", flush=True)
        with path.open("a", encoding="utf-8") as f:
            for c, m in markets.items():
                f.write(json.dumps({"type": "market", "coin": c, "ticker": m["ticker"],
                                    "close": close.isoformat(), "strike": m.get("floor_strike")}) + "\n")
            tick = math.ceil(time.time())
            while tick <= close.timestamp() + RECORD_AFTER_S:
                while time.time() < tick:
                    time.sleep(0.01)
                spots = {c: pool.submit(spot, c) for c in markets}
                books = {c: pool.submit(kalshi.get_orderbook, m["ticker"]) for c, m in markets.items()}
                for c, m in markets.items():
                    line: dict[str, Any] = {"type": "tick", "t": tick, "coin": c, "ticker": m["ticker"],
                                            "spot": spots[c].result()}
                    try:
                        line.update(top_of_book(books[c].result()))
                    except Exception:
                        pass
                    f.write(json.dumps(line) + "\n")
                f.flush()
                tick += 1


# ------------------------------------------------------------------ model

def walk_average_sd(sigma: float, n: int) -> float:
    """SD of the average of the next n points of a random walk with
    per-second SD sigma, starting from the current (known) price."""
    if n <= 0:
        return 0.0
    return sigma * math.sqrt(n * (n + 1) * (2 * n + 1) / 6) / n


def yes_chance(prices: dict[int, float], now: int, close: int, strike: float,
               basis_bp: float = BASIS_BP) -> float | None:
    """Chance the 60-second average before `close` ends >= strike, given
    second-by-second prices up to `now`."""
    current = prices.get(now)
    if current is None:
        return None
    window = range(close - AVG_SECONDS + 1, close + 1)
    known = [prices[t] for t in window if t <= now and t in prices]
    remaining = sum(1 for t in window if t > now)
    if len(known) + remaining < AVG_SECONDS * 0.8:
        return None  # too many gaps in the recording
    back = [prices[t] for t in range(now - VOL_LOOKBACK_S, now + 1) if t in prices]
    moves = [math.log(b / a) for a, b in zip(back, back[1:]) if a > 0 and b > 0]
    sigma = (statistics.pstdev(moves) if len(moves) > 10 else 0.0) * current
    n_known = len(known)
    total = n_known + remaining
    mean = (sum(known) + remaining * current) / total
    sd = math.hypot(walk_average_sd(sigma, remaining) * remaining / total, current * basis_bp / 10_000)
    if sd == 0:
        return 1.0 if mean >= strike else 0.0
    return 1 - statistics.NormalDist(mean, sd).cdf(strike)


# ---------------------------------------------------------------- analysis

@dataclass
class Fill:
    coin: str
    ticker: str
    seconds_left: int
    side: str
    price: int
    model: float
    won: bool

    @property
    def pnl(self) -> float:
        contracts = STAKE_DOLLARS * 100 // self.price
        gross = contracts * (100 - self.price) / 100 if self.won else -contracts * self.price / 100
        return round(gross - kalshi_fee(contracts, self.price), 2)


def load_recordings(paths: list[Path]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    markets: dict[str, dict] = {}
    ticks: dict[str, list[dict]] = {}
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "market":
                markets[row["ticker"]] = row
            elif row.get("type") == "tick":
                ticks.setdefault(row["ticker"], []).append(row)
    return markets, ticks


def simulate(market: dict, ticks: list[dict], result: str, min_edge: int = MIN_EDGE_CENTS) -> list[Fill]:
    """The first second (in the final minute) where the model's chance beat
    Kalshi's ask by min_edge cents after fees -- one buy per market."""
    close = int(datetime.fromisoformat(market["close"]).timestamp())
    strike = float(market["strike"])
    prices = {r["t"]: r["spot"] for r in ticks if r.get("spot")}
    for r in sorted(ticks, key=lambda r: r["t"]):
        left = close - r["t"]
        if not 1 <= left <= AVG_SECONDS:
            continue
        p = yes_chance(prices, r["t"], close, strike)
        if p is None:
            continue
        for side, chance, ask in (("yes", p, r.get("yes_ask")), ("no", 1 - p, r.get("no_ask"))):
            if ask is None or not 1 <= ask <= 99:
                continue
            fee_per = kalshi_fee(STAKE_DOLLARS * 100 // ask, ask) / max(1, STAKE_DOLLARS * 100 // ask) * 100
            if chance * 100 - ask - fee_per >= min_edge:
                return [Fill(market["coin"], market["ticker"], left, side, ask, chance, side == result)]
    return []


def analyze(kalshi) -> None:
    paths = sorted(OUT_DIR.glob("*.jsonl"))
    if not paths:
        print(f"No recordings in {OUT_DIR}/ yet -- run `python -m kalshibot.recorder` first.")
        return
    markets, ticks = load_recordings(paths)
    fills: list[Fill] = []
    settled = 0
    for ticker, m in markets.items():
        try:
            result = str(kalshi.get_market(ticker).get("market", {}).get("result") or "").lower()
        except Exception:
            result = ""
        if result not in ("yes", "no"):
            continue
        settled += 1
        fills += simulate(m, ticks.get(ticker, []), result)
    print(f"{len(markets)} recorded markets, {settled} settled.")
    if not fills:
        print(f"The model never saw Kalshi's price {MIN_EDGE_CENTS}¢+ off (after fees) in the final minute.")
        return
    for coin in sorted({f.coin for f in fills}) + ["ALL"]:
        fs = [f for f in fills if coin in ("ALL", f.coin)]
        wins = sum(f.won for f in fs)
        print(f"  {coin:4} {len(fs):4} buys, won {wins} ({wins / len(fs):.1%}), model said "
              f"{statistics.mean(f.model for f in fs):.1%} on average, "
              f"avg price {statistics.mean(f.price for f in fs):.1f}¢, net ${sum(f.pnl for f in fs):+,.2f} "
              f"at ${STAKE_DOLLARS}/buy")
    print("\nNote: sizes on offer aren't applied here yet -- check the qty columns before trusting a big number.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record / analyze the final minute of 15-minute crypto markets")
    parser.add_argument("mode", nargs="?", default="record", choices=["record", "analyze"])
    parser.add_argument("--coins", nargs="*", default=COINS, choices=COINS)
    args = parser.parse_args()
    from .main import build_kalshi_client
    kalshi = build_kalshi_client(demo=False)
    if args.mode == "analyze":
        analyze(kalshi)
    else:
        try:
            record(kalshi, args.coins)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
