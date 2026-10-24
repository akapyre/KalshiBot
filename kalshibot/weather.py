"""Weather research: are Kalshi's daily high-temperature markets mispriced
against a weather forecast? Paper only -- this module has no executor and
only reads Kalshi and Open-Meteo.

Usage:  python -m kalshibot.weather backtest              (all cities, last 60 days)
        python -m kalshibot.weather backtest --days 150   (older days come from Kalshi's archive, slower)

The markets (checked 2026-10-23): KXHIGHNY, KXHIGHCHI, ... one event per city
per day, split into 2-degree buckets ("61-62F") plus an "or below" and an
"or above". They open about 10am ET the day before, are on exchange_index 0,
and settle on the high in the NWS climate report for one station
(Central Park for NYC, Midway for Chicago, ...).

The forecast is Open-Meteo's "previous runs" archive, which keeps what the
models said N days ahead -- so the backtest only uses a forecast that
existed at the time it bets (no peeking at later model runs):
  - day before, 2pm ET:  the forecast made two days ahead
  - morning of, 8am ET:  the forecast made one day ahead
Both are older than what traders had at that moment, so the model starts
at a disadvantage; if it still wins, the edge is real.

Result 2026-10-23 (60 days, 20 cities): Kalshi predicted far better (Brier
0.10-0.11 vs the model's 0.15-0.16) and every setting lost 35-43%. Open-Meteo's
grid forecast misses the station high by 2-5F on a typical day; traders use
station-level NWS forecasts. A better forecast (e.g. archived GFS MOS for the
station) would be the only reason to revisit this.

The model: tomorrow's high = forecast high + the city's usual miss, give or
take the city's usual spread (a bell curve), both measured on the OLDER
two-thirds of days. Bets are only judged on the NEWER third, which the fit
never saw. One check per bucket, $25 at Kalshi's ask, fees included.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .backtest import _kalshi_call
from .crypto import STAKE_CENTS, Bet, brier, edge_cents, summarize
from .main import build_kalshi_client
from .paper import kalshi_fee

CACHE = Path("state/weather_cache")
OPEN_METEO = "https://previous-runs-api.open-meteo.com/v1/forecast"
MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
EDGES = [2, 4, 6, 8, 10]
MIN_SD = 1.5
# series -> (city, settlement station lat/lon, local time zone)
CITIES = {
    "KXHIGHNY": ("New York", 40.7794, -73.9692, "America/New_York"),        # Central Park
    "KXHIGHCHI": ("Chicago", 41.7860, -87.7524, "America/Chicago"),          # Midway
    "KXHIGHMIA": ("Miami", 25.7881, -80.3169, "America/New_York"),
    "KXHIGHLAX": ("Los Angeles", 33.9381, -118.3889, "America/Los_Angeles"),
    "KXHIGHAUS": ("Austin", 30.1831, -97.6799, "America/Chicago"),
    "KXHIGHDEN": ("Denver", 39.8467, -104.6562, "America/Denver"),
    "KXHIGHPHIL": ("Philadelphia", 39.8733, -75.2268, "America/New_York"),
    "KXHIGHTATL": ("Atlanta", 33.6297, -84.4422, "America/New_York"),
    "KXHIGHTBOS": ("Boston", 42.3606, -71.0097, "America/New_York"),
    "KXHIGHTDAL": ("Dallas", 32.8998, -97.0403, "America/Chicago"),           # DFW
    "KXHIGHTDC": ("Washington DC", 38.8472, -77.0345, "America/New_York"),    # Reagan National
    "KXHIGHTHOU": ("Houston", 29.6375, -95.2825, "America/Chicago"),          # Hobby
    "KXHIGHTLV": ("Las Vegas", 36.0719, -115.1634, "America/Los_Angeles"),
    "KXHIGHTMIN": ("Minneapolis", 44.8831, -93.2289, "America/Chicago"),
    "KXHIGHTNOLA": ("New Orleans", 29.9934, -90.2580, "America/Chicago"),
    "KXHIGHTOKC": ("Oklahoma City", 35.3889, -97.6006, "America/Chicago"),
    "KXHIGHTPHX": ("Phoenix", 33.4278, -112.0037, "America/Phoenix"),
    "KXHIGHTSATX": ("San Antonio", 29.5443, -98.4839, "America/Chicago"),
    "KXHIGHTSFO": ("San Francisco", 37.6197, -122.3647, "America/Los_Angeles"),
    "KXHIGHTSEA": ("Seattle", 47.4447, -122.3144, "America/Los_Angeles"),
}
# (label, hours from midnight UTC of the target day, Open-Meteo variable).
# 2pm ET the day before = 18:00 UTC (EDT); 8am ET on the day = 12:00 UTC.
CHECKS = [("day before 2pm ET", -6, "temperature_2m_previous_day2"),
          ("morning of 8am ET", 12, "temperature_2m_previous_day1")]


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


# ---------- the model ----------

def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def bucket_prob(mean: float, sd: float, lo: float | None, hi: float | None) -> float:
    """Chance the reported (whole-degree) high lands in [lo, hi]; None = open end."""
    a = norm_cdf((lo - 0.5 - mean) / sd) if lo is not None else 0.0
    b = norm_cdf((hi + 0.5 - mean) / sd) if hi is not None else 1.0
    return max(0.0, b - a)


def bucket_bounds(m: dict[str, Any]) -> tuple[float | None, float | None] | None:
    """Kalshi's strikes as inclusive whole degrees: "less" 59 = 58 or below,
    "greater" 66 = 67 or above, "between" 59/60 = 59 to 60."""
    kind, floor, cap = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    if kind == "less" and cap is not None:
        return None, cap - 1
    if kind == "greater" and floor is not None:
        return floor + 1, None
    if kind == "between" and floor is not None and cap is not None:
        return floor, cap
    return None


# ---------- data ----------

def event_date(event_ticker: str) -> date | None:
    code = event_ticker.split("-")[1]           # "26OCT22"
    try:
        return date(2000 + int(code[:2]), MON[code[2:5]], int(code[5:7]))
    except (KeyError, ValueError, IndexError):
        return None


def settled_days(kalshi, series: str, days: int) -> list[dict[str, Any]]:
    """Settled events, oldest first: date, reported high, and each bucket."""
    since = int(time.time()) - days * 86400
    markets = kalshi.iter_markets(series_ticker=series, status="settled", min_close_ts=since, max_pages=20)
    cutoff = datetime.fromisoformat(
        kalshi._request("GET", "/historical/cutoff")["market_settled_ts"].replace("Z", "+00:00"))
    if since < cutoff.timestamp():
        try:   # the archive ignores min_close_ts; filtered below
            markets += kalshi.iter_historical_markets(series_ticker=series, max_pages=50)
        except Exception as e:
            print(f"  (older Kalshi history unavailable: {e})")
    events: dict[str, dict[str, Any]] = {}
    for m in markets:
        d = event_date(m["event_ticker"])
        bounds = bucket_bounds(m)
        if d is None or bounds is None or m.get("result") not in ("yes", "no") or not m.get("expiration_value"):
            continue
        if datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() < since:
            continue
        ev = events.setdefault(m["event_ticker"], {"event": m["event_ticker"], "date": d,
                                                   "actual": float(m["expiration_value"]), "markets": []})
        ev["markets"].append({"ticker": m["ticker"], "lo": bounds[0], "hi": bounds[1], "won": m["result"] == "yes",
                              "archived": m["close_time"] < cutoff.isoformat()})
    return sorted(events.values(), key=lambda e: e["date"])


def forecast_highs(series: str, first: date, last: date) -> dict[str, dict[str, float]]:
    """{variable: {"YYYY-MM-DD": forecast high F}} for each CHECKS variable,
    the high over the local calendar day."""
    _, lat, lon, tz = CITIES[series]
    variables = [v for _, _, v in CHECKS]

    def fetch():
        r = requests.get(OPEN_METEO, timeout=30, params={
            "latitude": lat, "longitude": lon, "hourly": ",".join(variables), "temperature_unit": "fahrenheit",
            "timezone": tz, "start_date": first.isoformat(), "end_date": last.isoformat()})
        r.raise_for_status()
        h = r.json()["hourly"]
        out: dict[str, dict[str, float]] = {v: {} for v in variables}
        for v in variables:
            by_day: dict[str, list[float]] = {}
            for t, x in zip(h["time"], h[v]):
                if x is not None:
                    by_day.setdefault(t[:10], []).append(x)
            out[v] = {d: max(xs) for d, xs in by_day.items() if len(xs) >= 20}
        return out

    done = last < date.today() - timedelta(days=2)
    return _cached(f"fc-{series}-{first}-{last}.json", fetch, keep=done)


def prices_at(kalshi, ev: dict[str, Any], ts: int) -> dict[str, tuple[int, int]]:
    """{ticker: (YES bid, YES ask)} from the last hourly candle at or before ts."""
    tickers = [m["ticker"] for m in ev["markets"]]

    def fetch():
        out: dict[str, list] = {}
        live = [m["ticker"] for m in ev["markets"] if not m["archived"]]
        if live:
            d = _kalshi_call(kalshi._request, "GET", "/markets/candlesticks", params={
                "market_tickers": ",".join(live), "start_ts": ts - 3 * 3600, "end_ts": ts, "period_interval": 60})
            out.update({x["market_ticker"]: x.get("candlesticks", []) for x in d.get("markets", [])})
        for t in tickers:
            if t not in out:
                try:
                    out[t] = _kalshi_call(kalshi.get_candlesticks, t, ts - 3 * 3600, ts, 60, historical=True)
                except Exception:
                    out[t] = []
        return out if any(out.values()) else {}

    quotes = {}
    for t, candles in _cached(f"px-{ev['event']}-{ts}.json", fetch).items():
        candles = [c for c in candles if c.get("end_period_ts", 0) <= ts]
        if not candles:
            continue
        c = max(candles, key=lambda c: c["end_period_ts"])
        try:
            quotes[t] = (round(float(c["yes_bid"]["close_dollars"]) * 100),
                         round(float(c["yes_ask"]["close_dollars"]) * 100))
        except (KeyError, TypeError, ValueError):
            continue
    return quotes


# ---------- replay ----------

@dataclass
class Check:
    """One bucket at one check time."""
    series: str
    event: str
    day: str
    ticker: str
    lo: float | None
    hi: float | None
    forecast: float
    actual: float
    bid: int
    ask: int
    won: bool
    p: float = 0.0


def load_checks(days: int) -> dict[str, list[Check]]:
    """{check label: checks across all cities}, and the misses per city."""
    kalshi = build_kalshi_client(demo=False)
    out: dict[str, list[Check]] = {label: [] for label, _, _ in CHECKS}
    for i, series in enumerate(CITIES, 1):
        print(f"  {i}/{len(CITIES)} {CITIES[series][0]}...", end="\r", flush=True)
        events = settled_days(kalshi, series, days)
        if not events:
            continue
        fc = forecast_highs(series, events[0]["date"], events[-1]["date"])
        for ev in events:
            midnight = int(datetime(ev["date"].year, ev["date"].month, ev["date"].day, tzinfo=timezone.utc).timestamp())
            for label, hours, var in CHECKS:
                f = fc.get(var, {}).get(ev["date"].isoformat())
                if f is None:
                    continue
                quotes = prices_at(kalshi, ev, midnight + hours * 3600)
                for m in ev["markets"]:
                    if m["ticker"] in quotes:
                        bid, ask = quotes[m["ticker"]]
                        out[label].append(Check(series, ev["event"], ev["date"].isoformat(), m["ticker"], m["lo"],
                                                m["hi"], f, ev["actual"], bid, ask, m["won"]))
    print()
    return out


def misses(checks: list[Check]) -> dict[tuple[str, str], float]:
    """{(series, day): reported high - forecast}, one per event."""
    return {(c.series, c.day): c.actual - c.forecast for c in checks}


def fit(checks: list[Check], cut: str) -> dict[str, tuple[float, float]]:
    """{series: (usual miss, spread)} from days before cut only."""
    by_city: dict[str, list[float]] = {}
    for (series, day), miss in misses(checks).items():
        if day < cut:
            by_city.setdefault(series, []).append(miss)
    return {s: (statistics.mean(x), max(MIN_SD, statistics.pstdev(x))) for s, x in by_city.items() if len(x) >= 10}


def simulate(checks: list[Check], min_edge: float) -> list[Bet]:
    bets = []
    for c in checks:
        side, price, ev = edge_cents(c.p, c.bid, c.ask)
        if ev < min_edge:
            continue
        contracts = STAKE_CENTS // price
        won = c.won if side == "yes" else not c.won
        pnl = (contracts if won else 0) - contracts * price / 100 - kalshi_fee(contracts, price)
        bets.append(Bet(c.event, c.ticker, 0, 0, side, price, round(c.p, 4), round(ev, 2), won, round(pnl, 2)))
    return bets


def report(label: str, checks: list[Check]) -> None:
    days = sorted({c.day for c in checks})
    print(f"\n=== {label}: {len({c.event for c in checks})} city-days, {len(checks)} bucket prices ===")
    if len(days) < 15:
        print("  not enough days")
        return
    cut = days[len(days) * 2 // 3]
    params = fit(checks, cut)
    checks = [c for c in checks if c.series in params]
    for c in checks:
        bias, sd = params[c.series]
        c.p = bucket_prob(c.forecast + bias, sd, c.lo, c.hi)
    newer = [c for c in checks if c.day >= cut]
    print(f"  fit on {days[0]}..{cut} (older 2/3), judged on {cut}..{days[-1]} (newer 1/3)")
    quoted = [c for c in newer if 0 < c.bid <= c.ask < 100]
    if quoted:
        print(f"  who predicts better on the newer days (Brier, lower is better): model "
              f"{brier([(c.p, c.won) for c in quoted]):.4f}, Kalshi {brier([((c.bid + c.ask) / 200, c.won) for c in quoted]):.4f}")
    print("  Bets on the NEWER days only ('t' under ~2 is indistinguishable from luck):")
    for e in EDGES:
        print(summarize(simulate(newer, e), f"edge >= {e}c", "city-days"))
    bets = simulate(newer, EDGES[1])
    print(f"  edge >= {EDGES[1]}c by city / price paid:")
    for series in CITIES:
        mine = [b for b in bets if b.ticker.startswith(series + "-")]
        if mine:
            print(summarize(mine, CITIES[series][0], "days"))
    for lo, hi in ((1, 10), (10, 30), (30, 70), (70, 90), (90, 100)):
        print(summarize([b for b in bets if lo <= b.price < hi], f"paid {lo}-{hi - 1}c", "city-days"))


def backtest(days: int) -> None:
    print(f"\nWeather: loading {days} days of settled daily-high markets for {len(CITIES)} cities...")
    by_check = load_checks(days)
    first = next(iter(by_check.values()))
    if first:
        m = misses(first)
        cities = {}
        for (series, _), miss in m.items():
            cities.setdefault(series, []).append(miss)
        print("  forecast miss (reported - forecast, day-before forecast), all days:")
        for series, x in cities.items():
            print(f"    {CITIES[series][0]:14} {len(x):4} days  average {statistics.mean(x):+5.1f}F  "
                  f"typical miss {statistics.pstdev(x):4.1f}F")
    for label, checks in by_check.items():
        report(label, checks)
    print("\nReminder: a simulation, not a promise -- see the notes at the top of kalshibot/weather.py.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Weather research (paper only, never bets)")
    parser.add_argument("command", choices=["backtest"])
    parser.add_argument("--days", type=int, default=60, help="how far back (default 60)")
    args = parser.parse_args()
    backtest(args.days)


if __name__ == "__main__":
    sys.exit(main())
