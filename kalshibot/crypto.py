"""Crypto research: is Kalshi's hourly "Bitcoin above $X" market mispriced?
Paper only -- this module has no executor and only reads Kalshi and Coinbase.

Usage:  python -m kalshibot.crypto backtest btc               (last 14 days)
        python -m kalshibot.crypto backtest eth --days 30
        python -m kalshibot.crypto collect                    (log live quotes every minute)
        python -m kalshibot.crypto grade                      (score the collected log)

The markets (checked 2026-10-23): KXBTCD / KXETHD, one event per hour, each
with ~190 "above $X" strikes, open for the hour before it closes, on
exchange_index 2 (not 0 like most sports, not 3 like tennis/MLB). Each one
settles on the average of the last 60 seconds of CF Benchmarks' real-time
index (BRTI), not on any one exchange's price.

The idea is not to guess direction. With the spot price, the time left and
how much the coin has been moving, the chance it finishes above a strike is
a standard option-style calculation (fair_prob). A bet is only worth it when
Kalshi's ask is below that chance by more than the fee.

How the backtest works, for each settled hourly event in the window:
  1. Coinbase's minute closes stand in for the BRTI (it's one of the
     exchanges in the index; the gap is printed as "basis").
  2. Volatility is the standard deviation of the last few hours of minute
     returns -- only data from before the moment being priced.
  3. Strikes within BAND of the spot at the open are replayed. Every
     EVERY_S seconds the model's chance is compared with Kalshi's YES ask
     (buy YES) and NO ask = 100 - YES bid (buy NO), from minute candles.
  4. The first check with enough edge places a $25 bet, fees included;
     at most one per strike.

Read the results with this in mind:
  - Every strike in an hour rides on the same price move, so 40 bets in one
    hour are close to ONE bet. The "hours" count and the per-hour t-stat are
    the honest sample size, not the number of bets.
  - Candle quotes say nothing about size; $25 near the money is small next
    to the books seen so far (thousands of contracts), but the collector
    logs the real sizes.
  - Like sweep.py, results are split older / newer: a setting that only
    works on the older two-thirds is luck.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .backtest import _kalshi_call
from .main import build_kalshi_client
from .paper import kalshi_fee

ASSETS = {"btc": ("KXBTCD", "BTC-USD"), "eth": ("KXETHD", "ETH-USD")}
COINBASE = "https://api.exchange.coinbase.com"
CACHE = Path("state/crypto_cache")
LOG_DIR = Path("state/crypto")
STAKE_CENTS = 2500
BAND = 0.012                  # replay strikes within 1.2% of the spot at the open
EVERY_S = 120                 # the bot polls every 2 minutes
VOL_MIN = 240                 # volatility from the last 4 hours of minute returns
MIN_LEFT_S = 120              # no bets once the 60-second settlement average is near
EDGES = [2, 4, 6, 8, 10]      # cents of edge after fees
SCALES = [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2]   # volatility multipliers tried


# ---------- the model ----------

def fair_prob(spot: float, strike: float, left_s: float, sigma: float) -> float:
    """Chance the 60-second settlement average ends above strike, with the
    price a driftless random walk: sigma is the standard deviation of log
    returns per sqrt(second). Averaging the last 60s takes out 20s of
    variance (the average of a random walk over a window of w seconds is
    as uncertain as the walk w/3 seconds before the window ends)."""
    var = sigma ** 2 * max(left_s - 40, 1)
    sd = math.sqrt(var)
    z = (math.log(spot / strike) - var / 2) / sd
    return 0.5 * math.erfc(-z / math.sqrt(2))


def realized_sigma(closes: list[float]) -> float | None:
    """Per-sqrt(second) volatility from consecutive minute closes."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 30:
        return None
    return statistics.pstdev(rets) / math.sqrt(60)


def edge_cents(p: float, bid: int, ask: int) -> tuple[str, int, float]:
    """The better side to buy: (side, price in cents, expected cents per
    contract after the fee). YES costs the ask; NO costs 100 - bid."""
    best = ("none", 0, -100.0)
    for side, price, win in (("yes", ask, p), ("no", 100 - bid, 1 - p)):
        if not 1 <= price <= 99:
            continue
        contracts = STAKE_CENTS // price
        fee = kalshi_fee(contracts, price) * 100 / contracts
        ev = win * 100 - price - fee
        if ev > best[2]:
            best = (side, price, ev)
    return best


# ---------- data (cached) ----------

def _cached(name: str, fetch, keep: bool = True):
    path = CACHE / name
    if path.exists():
        data = json.loads(path.read_text())
        if data:
            return data
    data = fetch()
    if data and keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def coinbase_closes(product: str, start: int, end: int) -> dict[int, float]:
    """Minute closes keyed by the minute's END (unix seconds), so the price
    at time T is closes[T]. Fetched and cached one UTC day at a time."""
    out: dict[int, float] = {}
    day = datetime.fromtimestamp(start, timezone.utc).replace(hour=0, minute=0, second=0)
    while day.timestamp() < end:
        a = int(day.timestamp())
        b = a + 86400

        def fetch(a=a, b=b):
            rows: list[list[float]] = []
            for s in range(a, min(b, int(time.time())), 300 * 60):
                for wait in (1, 3, 10, None):
                    r = requests.get(f"{COINBASE}/products/{product}/candles", timeout=15,
                                     params={"granularity": 60, "start": s, "end": min(s + 300 * 60, b) - 60})
                    if r.status_code != 429 or wait is None:
                        break
                    time.sleep(wait)
                r.raise_for_status()
                rows += r.json()
                time.sleep(0.15)
            return {str(int(row[0]) + 60): row[4] for row in rows}

        done = b < time.time() - 120
        out.update({int(k): v for k, v in _cached(f"cb-{product}-{day:%Y%m%d}.json", fetch, keep=done).items()})
        day += timedelta(days=1)
    return out


def settled_events(kalshi, series: str, days: int) -> list[dict[str, Any]]:
    """Settled hourly events: close time, BRTI settlement value and the
    "above" strikes with their results, oldest first."""
    since = int(time.time()) - days * 86400
    markets = kalshi.iter_markets(series_ticker=series, status="settled", min_close_ts=since, max_pages=200)
    try:
        # Markets settled before the cutoff are only in the archive, which
        # ignores min_close_ts (it returned July with a 2-day window).
        cutoff = _ts(kalshi._request("GET", "/historical/cutoff")["market_settled_ts"])
        if since < cutoff:
            markets += kalshi.iter_historical_markets(series_ticker=series, max_pages=200)
    except Exception as e:
        print(f"  (older Kalshi history unavailable: {e})")
    events: dict[str, dict[str, Any]] = {}
    for m in markets:
        if m.get("strike_type") not in ("greater", "greater_or_equal") or m.get("result") not in ("yes", "no"):
            continue
        if _ts(m["close_time"]) < since:
            continue
        ev = events.setdefault(m["event_ticker"], {
            "event": m["event_ticker"], "close": _ts(m["close_time"]), "open": _ts(m["open_time"]),
            "value": float(m["expiration_value"]) if m.get("expiration_value") else None, "markets": {}})
        ev["markets"][m["ticker"]] = {"ticker": m["ticker"], "strike": float(m["floor_strike"]),
                                      "won": m["result"] == "yes"}
    return sorted(events.values(), key=lambda e: e["close"])


def quotes(kalshi, ticker: str, start: int, end: int) -> dict[int, tuple[int, int]]:
    """(YES bid, YES ask) in cents at the end of each minute."""
    def fetch():
        for historical in (False, True):
            try:
                c = _kalshi_call(kalshi.get_candlesticks, ticker, start, end, historical=historical)
            except Exception:
                c = []
            if c:
                return c
        return []

    out = {}
    for c in _cached(f"q-{ticker}.json", fetch):
        try:
            bid = round(float(c["yes_bid"]["close_dollars"]) * 100)
            ask = round(float(c["yes_ask"]["close_dollars"]) * 100)
        except (KeyError, TypeError, ValueError):
            continue
        out[int(c["end_period_ts"])] = (bid, ask)
    return out


# ---------- replay ----------

@dataclass
class Sample:
    """One strike at one check: what the model and Kalshi said, and the result."""
    event: str
    ticker: str
    strike: float
    close: int
    ts: int
    left_s: int
    spot: float
    sigma: float
    p: float
    bid: int
    ask: int
    won: bool


@dataclass
class Bet:
    event: str
    ticker: str
    close: int
    left_s: int
    side: str
    price: int
    p: float
    edge: float
    won: bool
    pnl: float


def load_samples(asset: str, days: int) -> tuple[list[Sample], list[float]]:
    """(samples, basis per event = BRTI settlement minus Coinbase, in %)."""
    series, product = ASSETS[asset]
    kalshi = build_kalshi_client(demo=False)
    print(f"\n{asset}: loading {days} days of settled {series} events...")
    events = settled_events(kalshi, series, days)
    if not events:
        return [], []
    closes = coinbase_closes(product, events[0]["open"] - VOL_MIN * 60 - 3600, events[-1]["close"] + 60)
    samples: list[Sample] = []
    basis: list[float] = []
    for i, ev in enumerate(events, 1):
        print(f"  loading prices {i}/{len(events)}", end="\r", flush=True)
        start = max(ev["open"], ev["close"] - 3600)    # the daily 5pm event opens early: last hour only
        spot0, last = closes.get(start), closes.get(ev["close"])
        if spot0 is None:
            continue
        if ev["value"] and last:
            basis.append((ev["value"] / last - 1) * 100)
        near = [m for m in ev["markets"].values() if abs(m["strike"] / spot0 - 1) <= BAND]
        for m in near:
            q = quotes(kalshi, m["ticker"], start, ev["close"])
            for ts in range(start + EVERY_S, ev["close"] - MIN_LEFT_S + 1, EVERY_S):
                if ts not in q or ts not in closes:
                    continue
                window = [closes[t] for t in range(ts - VOL_MIN * 60, ts + 1, 60) if t in closes]
                sigma = realized_sigma(window)
                if sigma is None:
                    continue
                bid, ask = q[ts]
                p = fair_prob(closes[ts], m["strike"], ev["close"] - ts, sigma)
                samples.append(Sample(ev["event"], m["ticker"], m["strike"], ev["close"], ts,
                                      ev["close"] - ts, closes[ts], sigma, p, bid, ask, m["won"]))
    print()
    return samples, basis


def simulate(samples: list[Sample], min_edge: float) -> list[Bet]:
    """The first check per strike with at least min_edge cents of expected
    profit per contract after fees places one $25 bet."""
    bets: list[Bet] = []
    fired: set[str] = set()
    for s in sorted(samples, key=lambda s: s.ts):
        if s.ticker in fired:
            continue
        side, price, ev = edge_cents(s.p, s.bid, s.ask)
        if ev < min_edge:
            continue
        fired.add(s.ticker)
        contracts = STAKE_CENTS // price
        won = s.won if side == "yes" else not s.won
        pnl = (contracts if won else 0) - contracts * price / 100 - kalshi_fee(contracts, price)
        bets.append(Bet(s.event, s.ticker, s.close, s.left_s, side, price, round(s.p, 4), round(ev, 2),
                        won, round(pnl, 2)))
    return bets


# ---------- reporting ----------

def brier(pairs: list[tuple[float, bool]]) -> float:
    return sum((p - won) ** 2 for p, won in pairs) / len(pairs)


def calibration(samples: list[Sample]) -> None:
    """Who predicts the result better, the model or Kalshi's mid price?
    Lower Brier is better. If Kalshi wins everywhere, the model has nothing
    to add, whatever the bet simulation says."""
    print("\n  Who predicts better (Brier score, lower is better):")
    print(f"    {'minutes left':14} {'checks':>7} {'model':>7} {'Kalshi':>7}")
    for lo, hi in ((2, 10), (10, 20), (20, 40), (40, 61)):
        part = [s for s in samples if lo * 60 <= s.left_s < hi * 60 and 0 < s.bid <= s.ask < 100]
        if part:
            print(f"    {f'{lo}-{hi - 1}':14} {len(part):7} {brier([(s.p, s.won) for s in part]):7.4f} "
                  f"{brier([((s.bid + s.ask) / 200, s.won) for s in part]):7.4f}")


def summarize(bets: list[Bet], label: str, unit: str = "hours") -> str:
    if not bets:
        return f"    {label:24} no bets"
    outlay = sum((STAKE_CENTS // b.price) * b.price / 100 for b in bets)
    net = sum(b.pnl for b in bets)
    hours: dict[str, float] = {}
    for b in bets:
        hours[b.event] = hours.get(b.event, 0) + b.pnl
    t = ""
    if len(hours) > 2:
        sd = statistics.stdev(hours.values())
        t = f"  t {statistics.mean(hours.values()) / sd * math.sqrt(len(hours)):+5.1f}" if sd else ""
    wins = sum(b.won for b in bets)
    return (f"    {label:24} {len(bets):5} bets in {len(hours):4} {unit}  win {wins / len(bets):5.1%} "
            f"(paid {sum(b.price for b in bets) / len(bets) / 100:5.1%})  net {net:+9.2f}  "
            f"ROI {net / outlay:+6.1%}{t}")


def rescale(samples: list[Sample], scale: float) -> list[Sample]:
    return [replace(s, sigma=s.sigma * scale, p=fair_prob(s.spot, s.strike, s.left_s, s.sigma * scale))
            for s in samples]


def fit_scale(samples: list[Sample], cut: str) -> float:
    """Minute-to-minute jitter makes realized volatility overstate the real
    moves (a live check 2026-10-23 found Kalshi pricing about half of it).
    Pick the volatility multiplier that predicted best on the OLDER
    two-thirds only, and show how it does on the newer third."""
    # Only two-sided books, so the model and Kalshi are scored on the same checks.
    quoted = [s for s in samples if 0 < s.bid <= s.ask < 100]
    older = [s for s in quoted if s.event < cut]
    newer = [s for s in quoted if s.event >= cut]
    if not older or not newer:
        return 1.0
    mid = lambda part: brier([((s.bid + s.ask) / 200, s.won) for s in part])
    print(f"\n  Volatility scale (Brier, lower is better; Kalshi's own: older {mid(older):.4f}, "
          f"newer {mid(newer):.4f}):")
    fits = {}
    for scale in SCALES:
        fits[scale] = brier([(s.p, s.won) for s in rescale(older, scale)])
        print(f"    x{scale:.1f}   older {fits[scale]:.4f}   newer "
              f"{brier([(s.p, s.won) for s in rescale(newer, scale)]):.4f}")
    best = min(fits, key=fits.get)
    print(f"  Using x{best:.1f} (best on the older games) from here on.")
    return best


def report(asset: str, samples: list[Sample], basis: list[float]) -> list[Bet]:
    events = sorted({s.event for s in samples})
    print(f"\n=== {asset.upper()}: {len(events)} hourly events, {len(samples)} checks ===")
    if not samples:
        return []
    if basis:
        print(f"  basis (Kalshi settlement vs Coinbase): median {statistics.median(basis):+.3f}%, "
              f"middle 80% {sorted(basis)[len(basis) // 10]:+.3f}% to {sorted(basis)[len(basis) * 9 // 10]:+.3f}%")
    cut = events[len(events) * 2 // 3]
    samples = rescale(samples, fit_scale(samples, cut))
    calibration(samples)
    print("\n  Bets by minimum edge (cents per contract, after fees). 't' = profit per hour / its")
    print("  spread x sqrt(hours): under ~2 is indistinguishable from luck.")
    for e in EDGES:
        bets = simulate(samples, e)
        print(summarize(bets, f"edge >= {e}c, all"))
        print(summarize([b for b in bets if b.event < cut], "  older 2/3"))
        print(summarize([b for b in bets if b.event >= cut], "  newer 1/3"))
    bets = simulate(samples, EDGES[1])
    print(f"\n  edge >= {EDGES[1]}c, by side / minutes left / price paid:")
    for side in ("yes", "no"):
        print(summarize([b for b in bets if b.side == side], f"buy {side.upper()}"))
    for lo, hi in ((2, 10), (10, 20), (20, 40), (40, 61)):
        print(summarize([b for b in bets if lo * 60 <= b.left_s < hi * 60], f"{lo}-{hi - 1} min left"))
    for lo, hi in ((1, 10), (10, 30), (30, 70), (70, 90), (90, 100)):
        print(summarize([b for b in bets if lo <= b.price < hi], f"paid {lo}-{hi - 1}c"))
    return bets


def backtest(asset: str, days: int) -> None:
    samples, basis = load_samples(asset, days)
    bets = report(asset, samples, basis)
    if bets:
        out = Path(f"state/crypto_backtest_{asset}.csv")
        with out.open("w", encoding="utf-8") as f:
            f.write(",".join(Bet.__dataclass_fields__) + "\n")
            for b in bets:
                f.write(",".join(str(v) for v in asdict(b).values()) + "\n")
        print(f"\n  Every simulated bet (edge >= {EDGES[1]}c): {out}")
    print("\nReminder: a simulation, not a promise -- see the notes at the top of kalshibot/crypto.py.")


# ---------- live collector (paper) ----------

def collect(every_s: int = 60) -> None:
    """Every minute: spot, volatility, and the real top-of-book (with sizes)
    for strikes near the money in the next event to close, one JSON line
    each in state/crypto/quotes-YYYYMMDD.jsonl. Nothing is bet. Ctrl+C stops."""
    kalshi = build_kalshi_client(demo=False)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Logging crypto quotes to {LOG_DIR} every {every_s}s (paper only). Ctrl+C to stop.")
    while True:
        now = int(time.time())
        for asset, (series, product) in ASSETS.items():
            try:
                rows = snapshot(kalshi, series, product, now)
            except Exception as e:
                print(f"  {asset}: {e}")
                continue
            path = LOG_DIR / f"quotes-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
            best = max(rows, key=lambda r: r["edge"], default=None)
            if best:
                print(f"  {datetime.now():%H:%M} {asset}: {len(rows)} strikes, spot {rows[0]['spot']:.2f}, "
                      f"best edge {best['edge']:+.1f}c ({best['side']} {best['ticker']})")
        time.sleep(max(1, every_s - (time.time() - now)))


def snapshot(kalshi, series: str, product: str, now: int) -> list[dict[str, Any]]:
    candles = requests.get(f"{COINBASE}/products/{product}/candles", timeout=10,
                           params={"granularity": 60, "start": now - VOL_MIN * 60, "end": now}).json()
    closes = [row[4] for row in sorted(candles)]
    sigma = realized_sigma(closes)
    spot = float(requests.get(f"{COINBASE}/products/{product}/ticker", timeout=10).json()["price"])
    if sigma is None:
        return []
    markets = kalshi.iter_markets(series_ticker=series, status="open", max_pages=2)
    if not markets:
        return []
    first = min(_ts(m["close_time"]) for m in markets)
    rows = []
    for m in markets:
        close = _ts(m["close_time"])
        if close != first or m.get("strike_type") not in ("greater", "greater_or_equal"):
            continue
        strike = float(m["floor_strike"])
        if abs(strike / spot - 1) > BAND or close - now < MIN_LEFT_S:
            continue
        bid = round(float(m["yes_bid_dollars"]) * 100)
        ask = round(float(m["yes_ask_dollars"]) * 100)
        p = fair_prob(spot, strike, close - now, sigma)
        side, price, ev = edge_cents(p, bid, ask)
        rows.append({"ts": now, "event": m["event_ticker"], "ticker": m["ticker"], "strike": strike,
                     "close": close, "left_s": close - now, "spot": spot, "sigma": sigma, "p": round(p, 4),
                     "bid": bid, "ask": ask, "bid_size": m.get("yes_bid_size_fp"),
                     "ask_size": m.get("yes_ask_size_fp"), "side": side, "price": price, "edge": round(ev, 2)})
    return rows


def grade() -> None:
    """Score the collected log against how each market settled, with the
    same report as the backtest (checks every 2 minutes)."""
    rows = [json.loads(line) for path in sorted(LOG_DIR.glob("quotes-*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        print(f"Nothing collected yet ({LOG_DIR}). Run: python -m kalshibot.crypto collect")
        return
    kalshi = build_kalshi_client(demo=False)
    results: dict[str, bool | None] = {}
    samples: dict[str, list[Sample]] = {}
    for r in rows:
        if r["close"] > time.time() - 300:
            continue
        if r["ticker"] not in results:
            m = _kalshi_call(kalshi.get_market, r["ticker"]).get("market", {})
            results[r["ticker"]] = {"yes": True, "no": False}.get(m.get("result"))
        won = results[r["ticker"]]
        if won is None or r["ts"] % EVERY_S >= 60:   # thin 1-minute logs to the bot's 2-minute pace
            continue
        asset = next(a for a, (series, _) in ASSETS.items() if r["ticker"].startswith(series + "-"))
        samples.setdefault(asset, []).append(Sample(r["event"], r["ticker"], r["strike"], r["close"], r["ts"],
                                                    r["left_s"], r["spot"], r["sigma"], r["p"], r["bid"],
                                                    r["ask"], won))
    for asset, part in samples.items():
        report(asset, part, [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto research (paper only, never bets)")
    parser.add_argument("command", choices=["backtest", "collect", "grade"])
    parser.add_argument("asset", nargs="?", choices=list(ASSETS) + ["all"], default="all")
    parser.add_argument("--days", type=int, default=14, help="backtest: how far back (default 14)")
    args = parser.parse_args()
    if args.command == "collect":
        collect()
    elif args.command == "grade":
        grade()
    else:
        for asset in ASSETS if args.asset == "all" else [args.asset]:
            backtest(asset, args.days)


if __name__ == "__main__":
    sys.exit(main())
