"""Replay "buy YES just before the start" on Kalshi's full-game totals and
both-teams-to-score markets.

    python -m kalshibot.totals_backtest nba nfl cfb soccer btts

For every settled Over (or BTTS) market that was listed before the game,
the YES ask in the last minute before the start is compared with how often
it actually won. Same question the NHL Over 3.5 rule came from
(state/nhl_totals_backtest.txt): do high-priced Overs win more often than
their price says? Kalshi doesn't give the start time directly; see
SERIES for the offsets, checked against ESPN 2026-11-01.

Candles are cached in state/totals_cache/, so a re-run is quick.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .kalshi_client import KalshiClient

_LEAGUES = ["EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL", "MLS", "LIGAMX"]
# sport -> (series, hours from the start to occurrence_datetime, hours from
# the start to expected_expiration_time for older markets that have no
# occurrence_datetime, or None to skip those). US sports: +3h on both
# (NY @ GS 2026-01-16: tip 03:00Z, expiration 06:00Z). Soccer: +4h
# (Hull v Ipswich 2026-10-31: kickoff 15:00Z, occurrence 19:00Z); its older
# markets aren't checked, so they're skipped.
SERIES = {
    "nba": (["KXNBATOTAL"], 3, 3), "nfl": (["KXNFLTOTAL"], 3, 3), "cfb": (["KXNCAAFTOTAL"], 3, 3),
    "nhl": (["KXNHLTOTAL"], 3, 3),
    "soccer": ([f"KX{lg}TOTAL" for lg in _LEAGUES], 4, None),
    "btts": ([f"KX{lg}BTTS" for lg in _LEAGUES], 4, None),
}
CACHE = Path("state/totals_cache")
_OVER = re.compile(r"over\s+([\d.]+)", re.I)
BANDS = [(80, 85), (85, 88), (88, 90), (90, 92), (92, 94), (94, 97)]
ALL_BANDS = [(1, 30), (30, 40), (40, 45), (45, 50), (50, 55), (55, 60), (60, 65), (65, 70), (70, 80), (80, 100)]


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fee(contracts: int, cents: int) -> float:
    p = cents / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100


def _pages(k: KalshiClient, path: str, **params) -> list[dict]:
    """Every market from a paged Kalshi listing, retrying 429s per page."""
    markets, cursor = [], None
    while True:
        page = _get(lambda: k._request("GET", path, params={"limit": 1000, **params,
                                                             **({"cursor": cursor} if cursor else {})}))
        if page is None:
            raise RuntimeError(f"Kalshi kept refusing {path} for {params}")
        markets.extend(page.get("markets", []))
        cursor = page.get("cursor")
        if not cursor:
            return markets


def settled_markets(k: KalshiClient, series: str, occ_hours: float, exp_hours: float | None) -> list[dict]:
    seen, out = set(), []
    for m in _pages(k, "/markets", series_ticker=series, status="settled") + \
            _pages(k, "/historical/markets", series_ticker=series):
        if m.get("occurrence_datetime"):
            start = _ts(m["occurrence_datetime"]) - timedelta(hours=occ_hours)
        elif exp_hours is not None and m.get("expected_expiration_time"):   # markets before ~Apr 2026
            start = _ts(m["expected_expiration_time"]) - timedelta(hours=exp_hours)
        else:
            continue
        if m["ticker"] in seen or m.get("result") not in ("yes", "no"):
            continue
        title = str(m.get("yes_sub_title") or "")
        line = _OVER.search(title)
        if not line and "both teams" not in title.lower():
            continue
        if _ts(m["open_time"]) > start - timedelta(minutes=10):
            continue   # line added during the game
        seen.add(m["ticker"])
        out.append(dict(ticker=m["ticker"], event=m["event_ticker"], line=float(line.group(1)) if line else 0.0,
                        result=m["result"], start=start))
    return out


def _get(fn):
    """fn(), retrying Kalshi's 429s; None on any other HTTP error (e.g. 404)."""
    for attempt in range(6):
        try:
            return fn()
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 429:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    return None


def _last_ask(candles: list[dict]) -> int | None:
    for c in reversed(candles):
        ask = c.get("yes_ask") or {}
        close = ask.get("close_dollars", ask.get("close"))   # /historical candles drop "_dollars"
        if close not in (None, ""):
            cents = round(float(close) * 100)
            return cents if 1 <= cents <= 99 else None
    return None


def ask_at_start(k: KalshiClient, m: dict) -> int | None:
    """YES ask (cents) at the last 1-minute candle before the start."""
    path = CACHE / f"{m['ticker']}.json"
    if path.exists():
        return _last_ask(json.loads(path.read_text()))
    end = int(m["start"].timestamp())
    candles = None
    for historical in (False, True):   # 404 on the first: settled before the cutoff
        candles = _get(lambda: k.get_candlesticks(m["ticker"], end - 30 * 60, end, interval_min=1,
                                                  historical=historical))
        if candles is not None:
            break
    if candles is None:
        return None
    path.write_text(json.dumps(candles))
    return _last_ask(candles)


def event_asks(k: KalshiClient, series: str, event: str, start: datetime) -> dict[str, int | None] | None:
    """Every line's ask at the start from one per-game request; None for
    games before Kalshi's archive cutoff, which that endpoint doesn't serve."""
    path = CACHE / f"event-{event}.json"
    if path.exists():
        data = json.loads(path.read_text())
    else:
        end = int(start.timestamp())
        data = _get(lambda: k._request("GET", f"/series/{series}/events/{event}/candlesticks",
                                       params={"start_ts": end - 30 * 60, "end_ts": end, "period_interval": 1}))
        if not data or not data.get("market_tickers"):
            return None   # older games come back empty rather than 404
        path.write_text(json.dumps(data))
    return {t: _last_ask(c) for t, c in zip(data.get("market_tickers", []), data.get("market_candlesticks", []))}


def game_rows(k: KalshiClient, lines: list[dict]) -> list[dict]:
    """This game's Over lines with their ask at the start: all of them from
    the per-game endpoint, else a binary search for the ones priced 80-97c
    (a higher line is always cheaper), about 6-8 requests instead of ~40.
    A one-market game (BTTS) is priced whatever its ask."""
    lines = sorted(lines, key=lambda m: m["line"])
    if len(lines) == 1:
        a = ask_at_start(k, lines[0])
        return [dict(lines[0], ask=a)] if a is not None else []
    series = lines[0]["ticker"].split("-")[0]
    asks = event_asks(k, series, lines[0]["event"], lines[0]["start"])
    if asks is not None:
        return [dict(m, ask=asks[m["ticker"]]) for m in lines if asks.get(m["ticker"])]
    memo: dict[int, int | None] = {}

    def ask(i: int) -> int | None:
        if i not in memo:
            memo[i] = ask_at_start(k, lines[i])
        return memo[i]

    lo, hi = 0, len(lines)   # -> first line priced under 97c
    while lo < hi:
        mid = (lo + hi) // 2
        a = ask(mid)
        if a is not None and a >= 97:
            lo = mid + 1
        else:
            hi = mid
    out = []
    for i in range(lo, len(lines)):
        a = ask(i)
        if a is not None and a < 80:
            break
        if a is not None:
            out.append(dict(lines[i], ask=a))
    return out


def _row(label: str, bets: list[tuple[int, bool]], stake: float = 25) -> str:
    if not bets:
        return f"  {label:34} 0 bets"
    pnls = []
    for ask, won in bets:
        n = int(stake * 100 // ask)
        pnls.append((n if won else 0) - n * ask / 100 - fee(n, ask))
    staked = sum(int(stake * 100 // a) * a / 100 for a, _ in bets)
    sd = statistics.pstdev(pnls) if len(pnls) > 1 else 0
    t = (statistics.mean(pnls) / (sd / math.sqrt(len(pnls)))) if sd else 0
    hit = 100 * sum(w for _, w in bets) / len(bets)
    price = sum(a for a, _ in bets) / len(bets)
    return (f"  {label:34} {len(bets):5} bets  hit {hit:5.1f}%  price {price:5.1f}c  "
            f"net $ {sum(pnls):+9.2f}  ROI {100 * sum(pnls) / staked:+6.2f}%  t {t:+.1f}")


def report_btts(rows: list[dict]) -> str:
    first, last = min(r["start"] for r in rows), max(r["start"] for r in rows)
    out = [f"=== BTTS: {len(rows)} games priced at kickoff ({first:%Y-%m-%d} to {last:%Y-%m-%d}) ===",
           " YES (both teams score), by its ask at kickoff:"]
    for lo, hi in ALL_BANDS:
        out.append(_row(f"{lo}-{hi - 1}c", [(r["ask"], r["result"] == "yes") for r in rows if lo <= r["ask"] < hi]))
    out.append(_row("all", [(r["ask"], r["result"] == "yes") for r in rows]))
    out.append(" By league:")
    for series in sorted({r["ticker"].split("-")[0] for r in rows}):
        out.append(_row(series, [(r["ask"], r["result"] == "yes") for r in rows if r["ticker"].startswith(series + "-")]))
    return "\n".join(out)


def report(sport: str, rows: list[dict]) -> str:
    games = {r["event"] for r in rows}
    first, last = min(r["start"] for r in rows), max(r["start"] for r in rows)
    out = [f"=== {sport.upper()}: {len(games)} games, {len(rows)} Over lines priced at the start "
           f"({first:%Y-%m-%d} to {last:%Y-%m-%d}) ==="]
    out.append(" Every pregame Over line, by its ask at the start:")
    for lo, hi in BANDS:
        out.append(_row(f"{lo}-{hi - 1}c", [(r["ask"], r["result"] == "yes") for r in rows if lo <= r["ask"] < hi]))
    out.append(" One bet per game -- the Over priced closest to 91c, if it is in the band:")
    for lo, hi in ((85, 88), (88, 95), (88, 91), (91, 95)):
        pick = {}
        for r in rows:
            if lo <= r["ask"] < hi and (r["event"] not in pick or abs(r["ask"] - 91) < abs(pick[r["event"]]["ask"] - 91)):
                pick[r["event"]] = r
        picked = sorted(pick.values(), key=lambda r: r["start"])
        out.append(_row(f"{lo}-{hi - 1}c", [(r["ask"], r["result"] == "yes") for r in picked]))
        half = len(picked) // 2
        out.append(_row(f"    earlier half", [(r["ask"], r["result"] == "yes") for r in picked[:half]]))
        out.append(_row(f"    later half", [(r["ask"], r["result"] == "yes") for r in picked[half:]]))
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sports", nargs="+", choices=sorted(SERIES))
    p.add_argument("--workers", type=int, default=3)   # Kalshi rate-limits hard; the live bot shares the limit
    args = p.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    k = KalshiClient(None)
    for sport in args.sports:
        series_list, occ_hours, exp_hours = SERIES[sport]
        games: dict[str, list[dict]] = {}
        for series in series_list:
            for m in settled_markets(k, series, occ_hours, exp_hours):
                games.setdefault(m["event"], []).append(m)
        print(f"{sport}: {len(games)} games -- fetching start prices...", file=sys.stderr, flush=True)
        with ThreadPoolExecutor(args.workers) as pool:
            rows = [r for game in pool.map(lambda g: game_rows(k, g), games.values()) for r in game]
        if sport == "btts":
            text = report_btts(rows) if rows else "=== BTTS: no priced games ==="
        else:
            rows = [r for r in rows if r["ask"] >= 80]   # older games only have the 80c+ lines (see game_rows)
            text = report(sport, rows) if rows else f"=== {sport.upper()}: no priced lines ==="
        print(text, flush=True)
        Path(f"state/{sport}_totals_backtest.txt").write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
