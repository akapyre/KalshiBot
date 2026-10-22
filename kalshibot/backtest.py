"""Replay your rules against past games using Kalshi's minute-by-minute
prices, to see how each rule would have done.

Usage:  python -m kalshibot.backtest nfl            (last 60 days)
        python -m kalshibot.backtest inspect        (raw prices behind the oldest bets)
        python -m kalshibot.backtest soccer --days 180
        python -m kalshibot.backtest all

For every finished game in the window:
  1. SportsGameOdds gives the finished games and their start times.
  2. The game's two Kalshi "<team> wins" markets are found the same way
     the bot finds them, and their price history is fetched. The favorite
     and its pregame odds are the Kalshi prices AT KICKOFF. (SportsGameOdds'
     "closing" line can't be used for old games: it ends up holding the
     line from the end of the game, which names the winner -- an earlier
     version of this backtest made that mistake and "won" 91%.)
  3. The price is sampled every 5 minutes (like the bot's polling) and the
     rule fires the first time Kalshi's price converts into its odds band.
  4. The market's result settles the $25 bet, fees included.

What it can't replay exactly -- read results with this in mind:
  - Live odds come from Kalshi's price, not sportsbooks (the live bot needs
    both to agree).
  - Quarter / inning are estimated from minutes since kickoff.
  - Score-based rules (NHL trailing, soccer first half) need a goal-by-goal
    timeline the data doesn't have, so they're skipped; soccer's "skip if
    down a goal after halftime" clause is ignored.

Downloads are cached in state/backtest_cache, so reruns are quick.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from .executor import american_odds
from .main import SPORTS, build_kalshi_client
from .matching import MarketMatcher
from .odds_providers.base import GameSnapshot
from .odds_providers.sportsgameodds import SportsGameOddsProvider
from .rules_engine import Rule, _invalidated, _pregame_ok, _trigger_ok, kalshi_band_ok, load_rules
from .state import PregameOddsStore

CACHE = Path("state/backtest_cache")
STAKE_CENTS = 2500
SAMPLE_EVERY_S = 300          # the bot polls every 5 minutes
# How long after the start to follow the price, by sport.
GAME_LENGTH_MIN = {"nfl": 240, "cfb": 270, "mlb": 240, "nhl": 210, "nba": 150, "cbb": 150, "soccer": 150, "tennis": 240}
# Estimated minute each quarter starts (NFL games ~3h, college ~3h20m).
QUARTER_STARTS = {"nfl": [0, 45, 110, 145], "cfb": [0, 50, 125, 165]}
MLB_MIN_PER_INNING = 18


def estimated_period(sport: str, minutes: float) -> int | None:
    if sport in QUARTER_STARTS:
        return sum(minutes >= start for start in QUARTER_STARTS[sport])
    if sport == "mlb":
        return 1 + int(minutes // MLB_MIN_PER_INNING)
    return None


def replayable(rule: Rule) -> bool:
    """Rules whose trigger is odds (plus, at most, quarter/inning)."""
    return rule.market == "game" and rule.trigger_scores is None and rule.late_period is None


def kalshi_fee(contracts: int, price_cents: int) -> float:
    """Kalshi's taker fee: 7% x contracts x P x (1-P), rounded up to the cent."""
    p = price_cents / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100


@dataclass
class Trade:
    rule: str
    sport: str
    game: str
    team: str
    start: str
    pregame: int
    minute: int
    price: int
    won: bool
    contracts: int
    cost: float
    pnl: float
    ticker: str = ""


def simulate_game(rules: list[Rule], sport: str, game: str, team: str, start: datetime,
                  pregame: int, prices: list[tuple[int, int]], won: bool, ticker: str = "") -> list[Trade]:
    """prices: (unix seconds, YES ask in cents), oldest first. Returns the
    bets each rule would have placed on this game."""
    trades: list[Trade] = []
    fired: set[str] = set()
    t0 = start.timestamp()
    next_sample = t0
    for ts, ask in prices:
        if ts < next_sample or not 1 <= ask <= 99:
            continue
        next_sample = ts + SAMPLE_EVERY_S
        minutes = (ts - t0) / 60
        snap = GameSnapshot(
            game_id=game, sport=sport, home_team="", away_team="", favorite_team=team,
            pregame_favorite_odds=pregame, live_favorite_odds=american_odds(ask),
            start_time_utc=start.isoformat(), is_live=True, is_final=False,
            period=estimated_period(sport, minutes),
        )
        for rule in rules:
            if rule.id in fired or rule.sport != sport or not replayable(rule):
                continue
            if not _pregame_ok(rule, pregame):
                continue
            if rule.requires_prior_rule and rule.requires_prior_rule not in fired:
                continue
            if _invalidated(rule, snap) or not _trigger_ok(rule, snap):
                continue
            if rule.kalshi_band is not None and not kalshi_band_ok(rule, ask):
                continue
            fired.add(rule.id)
            contracts = STAKE_CENTS // ask
            cost = contracts * ask / 100
            fee = kalshi_fee(contracts, ask)
            pnl = (contracts if won else 0) - cost - fee
            trades.append(Trade(rule.id, sport, game, team, start.isoformat(), pregame, round(minutes),
                                ask, won, contracts, round(cost, 2), round(pnl, 2), ticker))
    return trades


# ---------- data (cached) ----------

def _cached(name: str, fetch):
    """Saved result if there is one; empty results are never saved (they
    are usually a throttled or failed request), so they're retried."""
    path = CACHE / name
    if path.exists():
        data = json.loads(path.read_text())
        if data:
            return data
    data = fetch()
    if data:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data


def _kalshi_call(fn, *args, **kwargs):
    """One Kalshi read, paced, retrying when throttled (HTTP 429)."""
    for wait in (2, 5, 15, None):
        time.sleep(0.15)
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status != 429 or wait is None:
                raise
            time.sleep(wait)


def finished_games(provider: SportsGameOddsProvider, sport: str, days: int) -> list[GameSnapshot]:
    """Completed games with a closing line, from SportsGameOdds, a week at a time."""
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    snaps: list[GameSnapshot] = []
    while start < end:
        stop = min(start + timedelta(days=7), end)
        key = f"sgo-{sport}-{start:%Y%m%d%H}-{stop:%Y%m%d%H}.json"
        # Only cache windows that are fully in the past (results are final).
        fetch = lambda a=start, b=stop: provider.fetch_events(sport, window=(a, b))
        try:
            events = _cached(key, fetch) if stop < end - timedelta(days=1) else fetch()
        except Exception as e:
            # A failed week (network blip, rate limit) is skipped, not fatal;
            # it isn't cached, so the next run fetches it again.
            print(f"\n  (skipped {start:%b %d}-{stop:%b %d}: {e})")
            events = []
        for ev in events:
            if ev.get("type") != "match":
                continue
            snap = provider._parse_event(sport, ev)
            if snap is not None and snap.is_final:
                snaps.append(snap)
        start = stop
        print(f"  odds history: {len(snaps)} finished {sport} games so far", end="\r", flush=True)
    print()
    return snaps


class SettledMarkets:
    """Looks like the Kalshi client to MarketMatcher, but serves settled
    markets, so past games are matched exactly the way the bot matches."""

    def __init__(self, kalshi, days: int):
        self._kalshi = kalshi
        self._min_close = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        self._by_series: dict[str, list[dict[str, Any]]] = {}
        self.historical_ok = True

    def list_series(self, **params):
        return self._kalshi.list_series(**params)

    def iter_markets(self, series_ticker: str, **_ignored) -> list[dict[str, Any]]:
        if series_ticker not in self._by_series:
            markets = self._kalshi.iter_markets(series_ticker=series_ticker, status="settled",
                                                min_close_ts=self._min_close, max_pages=20)
            if self.historical_ok:
                try:
                    markets += self._kalshi.iter_historical_markets(series_ticker=series_ticker,
                                                                    min_close_ts=self._min_close)
                except Exception as e:
                    self.historical_ok = False
                    print(f"  (older Kalshi history unavailable: {e}; using the recent window only)")
            self._by_series[series_ticker] = markets
        return self._by_series[series_ticker]


_warned_shape = False


def ask_cents(candle: dict[str, Any]) -> int | None:
    """Closing YES ask of a candle, from either price format Kalshi uses."""
    global _warned_shape
    for block in ("yes_ask", "price"):
        part = candle.get(block) or {}
        for key in ("close_dollars", "close"):
            v = part.get(key)
            if v in (None, ""):
                continue
            try:
                num = float(v)
            except (TypeError, ValueError):
                continue
            # Dollar formats ("0.4500", 0.45) vs whole cents (45).
            dollars = key.endswith("dollars") or isinstance(v, str) or (isinstance(v, float) and num <= 1)
            return round(num * 100) if dollars else int(num)
    if not _warned_shape:
        _warned_shape = True
        print(f"\n  (unrecognised candle format, sample: {json.dumps(candle)[:300]})")
    return None


def candles_for(kalshi, ticker: str, start: datetime, minutes: int) -> list[dict[str, Any]]:
    """Minute candles from 10 minutes before the start to the end of play."""
    a = int(start.timestamp()) - 600
    b = a + minutes * 60 + 600

    def fetch():
        try:
            candles = _kalshi_call(kalshi.get_candlesticks, ticker, a, b)
        except Exception:
            candles = []
        if not candles:  # settled before Kalshi's cutoff: only in the archive
            try:
                candles = _kalshi_call(kalshi.get_candlesticks, ticker, a, b, historical=True)
            except Exception:
                candles = []
        return candles

    return _cached(f"candles-{ticker}.json", fetch)


def asks(candles: list[dict[str, Any]]) -> list[tuple[int, int]]:
    out = []
    for c in candles:
        ask = ask_cents(c)
        ts = c.get("end_period_ts")
        if ask is not None and ts is not None:
            out.append((int(ts), ask))
    return sorted(out)


def _cents(part: dict[str, Any] | None) -> float | None:
    v = (part or {}).get("close_dollars", (part or {}).get("close"))
    try:
        num = float(v)
    except (TypeError, ValueError):
        return None
    return num * 100 if (isinstance(v, str) or num <= 1) else num


def kickoff_cents(candles: list[dict[str, Any]], start: datetime) -> int | None:
    """The market's price at the start: bid/ask midpoint of the candle
    closest to kickoff (within 10 minutes)."""
    near = [c for c in candles if c.get("end_period_ts") is not None
            and abs(c["end_period_ts"] - start.timestamp()) <= 600]
    if not near:
        return None
    c = min(near, key=lambda c: abs(c["end_period_ts"] - start.timestamp()))
    bid, ask = _cents(c.get("yes_bid")), _cents(c.get("yes_ask"))
    if bid is not None and ask is not None and 0 < bid <= ask < 100:
        price = round((bid + ask) / 2)
    else:
        price = ask_cents(c)
    # A one-sided book (ask 100c / 0c) has no usable price -- and no odds.
    return price if price is not None and 1 <= price <= 99 else None


# ---------- reporting ----------

def summarize(trades: list[Trade], label: str) -> str:
    if not trades:
        return f"  {label:34} no bets"
    wins = sum(t.won for t in trades)
    net = sum(t.pnl for t in trades)
    outlay = sum(t.cost for t in trades)
    avg_price = sum(t.price for t in trades) / len(trades)
    return (f"  {label:34} {len(trades):4} bets  {wins:3}-{len(trades) - wins:<3}  "
            f"win {wins / len(trades):5.1%} (price implied {avg_price / 100:5.1%})  "
            f"net {net:+8.2f}  ROI {net / outlay:+6.1%}")


def report(sport: str, rules: list[Rule], trades: list[Trade], games: int, matched: int) -> None:
    print(f"\n=== {sport.upper()}: {games} finished games, {matched} with Kalshi prices at kickoff ===")
    for rule in rules:
        if rule.sport != sport:
            continue
        if not replayable(rule):
            print(f"  {rule.id:34} skipped (score-based -- can't be replayed from prices)")
            continue
        mine = [t for t in trades if t.rule == rule.id]
        print(summarize(mine, rule.id + ("" if rule.enabled else " (paused)")))
        # Where in the price band the money was made or lost.
        for lo in range(0, 100, 10):
            bucket = [t for t in mine if lo <= t.price < lo + 10]
            if bucket:
                print("    " + summarize(bucket, f"  bought at {lo}-{lo + 9}¢ ({american_odds(lo + 5):+d})").strip())


@dataclass
class GameData:
    """One finished game, ready to replay: the kickoff favorite, its price
    at kickoff and minute by minute after, and whether it won."""
    sport: str
    game: str
    team: str
    start: datetime
    kickoff: int                       # favorite's price at kickoff, cents
    prices: list[tuple[int, int]]      # (unix seconds, YES ask cents)
    won: bool
    ticker: str


def load_games(sport: str, days: int) -> tuple[list[GameData], int, dict[str, int]]:
    """Finished games with Kalshi prices: (games, games found, why others
    were left out). Uses and fills the download cache."""
    logging.getLogger("kalshibot.matching").setLevel(logging.ERROR)
    logging.getLogger("kalshibot.odds_providers.sportsgameodds").setLevel(logging.ERROR)
    kalshi = build_kalshi_client(demo=False)
    provider = SportsGameOddsProvider(pregame_store=PregameOddsStore(CACHE / "pregame.json"))
    settled = SettledMarkets(kalshi, days + 2)
    matcher = MarketMatcher(settled)

    print(f"\n{sport}: loading {days} days of games...")
    found = finished_games(provider, sport, days)
    games: list[GameData] = []
    skipped = {"no Kalshi event found": 0, "no Kalshi prices at kickoff": 0, "market not settled yes/no": 0}
    for i, g in enumerate(found, 1):
        print(f"  loading prices {i}/{len(found)}", end="\r", flush=True)
        event = matcher.resolve_event(g)
        if event is None:
            skipped["no Kalshi event found"] += 1
            continue
        start = datetime.fromisoformat(g.start_time_utc.replace("Z", "+00:00"))
        length = GAME_LENGTH_MIN.get(sport, 240)
        candles = {side: candles_for(kalshi, event[side]["ticker"], start, length) for side in ("home", "away")}
        kickoff = {side: kickoff_cents(c, start) for side, c in candles.items()}
        if None in kickoff.values() or kickoff["home"] == kickoff["away"]:
            skipped["no Kalshi prices at kickoff"] += 1
            continue
        fav = "home" if kickoff["home"] > kickoff["away"] else "away"
        result = event[fav].get("result")
        if result not in ("yes", "no"):
            skipped["market not settled yes/no"] += 1
            continue
        games.append(GameData(sport, f"{g.away_team} @ {g.home_team}",
                              g.home_team if fav == "home" else g.away_team, start, kickoff[fav],
                              asks(candles[fav]), result == "yes", event[fav]["ticker"]))
    print()
    return games, len(found), skipped


def run(sport: str, days: int) -> list[Trade]:
    rules, _ = load_rules()
    games, found, skipped = load_games(sport, days)
    trades: list[Trade] = []
    for g in games:
        trades += simulate_game(rules, sport, g.game, g.team, g.start, american_odds(g.kickoff),
                                g.prices, g.won, g.ticker)
    report(sport, rules, trades, found, len(games))
    print("  games left out: " + ", ".join(f"{n} {why}" for why, n in skipped.items()))
    return trades


def inspect(count: int = 4) -> None:
    """Show the raw price data behind a few simulated bets from the oldest
    games, to check the archived (pre-cutoff) prices make sense."""
    path = Path("state/backtest_trades.csv")
    if not path.exists():
        print("Run a backtest first.")
        return
    rows = sorted(csv.DictReader(path.open(encoding="utf-8")), key=lambda r: r["start"])
    seen: set[str] = set()
    for r in rows:
        ticker = r.get("ticker", "")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        if len(seen) > count:
            break
        start = datetime.fromisoformat(r["start"])
        cache = CACHE / f"candles-{ticker}.json"
        candles = json.loads(cache.read_text()) if cache.exists() else []
        print(f"\n{r['game']} | bet {r['team']} ({r['rule']}) | pregame {r['pregame']} | "
              f"start {start:%Y-%m-%d %H:%M} UTC | bought {r['price']}c at minute {r['minute']} | "
              f"{'WON' if r['won'] == 'True' else 'lost'} | {ticker}")
        if not candles:
            print("  no cached candles")
            continue
        times = [c.get("end_period_ts") for c in candles if c.get("end_period_ts")]
        rel = [(t - start.timestamp()) / 60 for t in times]
        print(f"  {len(candles)} candles, from minute {min(rel):.0f} to {max(rel):.0f} relative to the start")
        print(f"  first candle: {json.dumps(candles[0])[:600]}")
        marks = []
        for target in (-10, 0, 30, 60, 90, 120, 180):
            c = min(candles, key=lambda c: abs((c.get("end_period_ts", 0) - start.timestamp()) / 60 - target))
            marks.append(f"m{target}:{ask_cents(c)}")
        print("  ask by minute: " + "  ".join(marks))


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay your rules on past games")
    parser.add_argument("sport", choices=SPORTS + ["all", "inspect"])
    parser.add_argument("--days", type=int, default=60, help="how far back (default 60)")
    args = parser.parse_args()
    if args.sport == "inspect":
        inspect()
        return
    sports = SPORTS if args.sport == "all" else [args.sport]
    trades: list[Trade] = []
    for sport in sports:
        trades += run(sport, args.days)
    if trades:
        out = Path("state/backtest_trades.csv")
        with out.open("w", newline="", encoding="utf-8") as f:   # team names like "Gençlerbirliği"
            writer = csv.DictWriter(f, fieldnames=list(Trade.__dataclass_fields__))
            writer.writeheader()
            writer.writerows(t.__dict__ for t in trades)
        print(f"\nEvery simulated bet: {out}")
    print("\nReminder: a simulation, not a promise -- see the notes at the top of kalshibot/backtest.py.")


if __name__ == "__main__":
    sys.exit(main())
