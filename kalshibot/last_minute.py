"""How often does a near-certain side of Kalshi's 15-minute markets lose?

Idea being tested: just before a 15-minute crypto (or other) market closes,
buy whichever side is priced 99¢ (or 95-99¢) and collect the last cent or
few. That only works if the "sure" side almost never flips at the end.

Usage:
  python -m kalshibot.last_minute                 (last 1 day, every 15-minute series found)
  python -m kalshibot.last_minute --days 7        (more markets = a firmer answer)
  python -m kalshibot.last_minute --series KXBTC15M KXETH15M
  python -m kalshibot.last_minute --rules         (how BTC/ETH/SOL/XRP 15-minute markets settle;
                                                   add --series ... for others)

For every settled market it reads Kalshi's 1-minute price candles and, at
1, 2, 3 and 5 minutes before close, checks whether either side could be
bought at 95-99¢ (YES at the YES ask, NO at 100 - YES bid). Each such
would-be buy is settled with the market's real result, Kalshi's fee
included, at $100 per bet.

Limits: candles are 1-minute, so "the last second" is approximated by the
quote 1 minute before close; and they don't show how many contracts were
on offer at 99¢ -- a bigger bet may not fill there.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .backtest import _cached, _cents, _kalshi_call, kalshi_fee

CHECKPOINTS_MIN = [1, 2, 3, 5]
THRESHOLDS = [99, 98, 97, 95]
STAKE_DOLLARS = 100
FIFTEEN_MIN = re.compile(r"15M$|15 ?min", re.IGNORECASE)
OTHER_ASSETS = re.compile(r"oil|wti|crude|brent|gold|silver|nasdaq|s&p|spx|inx", re.IGNORECASE)


@dataclass
class Entry:
    series: str
    ticker: str
    close: datetime
    minutes_before: int
    side: str        # "yes" / "no"
    price: int       # cents paid
    won: bool

    @property
    def pnl(self) -> float:
        contracts = STAKE_DOLLARS * 100 // self.price
        fee = kalshi_fee(contracts, self.price)
        gross = contracts * (100 - self.price) / 100 if self.won else -contracts * self.price / 100
        return round(gross - fee, 2)


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def quote_at(candles: list[dict[str, Any]], when: int) -> tuple[int | None, int | None]:
    """(YES bid, YES ask) in cents from the last candle ending at or before `when`."""
    best = None
    for c in candles:
        end = c.get("end_period_ts")
        if end is not None and end <= when and (best is None or end > best["end_period_ts"]):
            best = c
    if best is None:
        return None, None
    bid, ask = _cents(best.get("yes_bid")), _cents(best.get("yes_ask"))
    return (round(bid) if bid is not None else None, round(ask) if ask is not None else None)


def favorite_buy(bid: int | None, ask: int | None, threshold: int) -> tuple[str, int] | None:
    """The side buyable at threshold..99¢, if either is: ("yes", ask) or ("no", 100 - bid)."""
    if ask is not None and threshold <= ask <= 99:
        return "yes", ask
    if bid is not None and threshold <= 100 - bid <= 99:
        return "no", 100 - bid
    return None


def entries_for_market(series: str, market: dict[str, Any], candles: list[dict[str, Any]],
                       threshold: int) -> list[Entry]:
    close = _ts(market.get("close_time"))
    result = str(market.get("result") or "").lower()
    if close is None or result not in ("yes", "no"):
        return []
    out = []
    for k in CHECKPOINTS_MIN:
        bid, ask = quote_at(candles, int(close.timestamp()) - 60 * k)
        buy = favorite_buy(bid, ask, threshold)
        if buy:
            side, price = buy
            out.append(Entry(series, market["ticker"], close, k, side, price, side == result))
    return out


def find_series(kalshi) -> tuple[list[str], list[str]]:
    """(15-minute series tickers, other short-window-looking series worth naming)."""
    fifteen, others = [], []
    for s in kalshi.list_series().get("series", []):
        ticker, title = str(s.get("ticker") or ""), str(s.get("title") or "")
        if FIFTEEN_MIN.search(ticker) or FIFTEEN_MIN.search(title):
            fifteen.append(ticker)
        elif OTHER_ASSETS.search(ticker + " " + title):
            others.append(f"{ticker} ({title})")
    return sorted(fifteen), sorted(others)


def load(kalshi, series: str, days: float) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    markets = _kalshi_call(kalshi.iter_markets, series_ticker=series, status="settled",
                           min_close_ts=int(since.timestamp()), max_close_ts=int(now.timestamp()))
    out = []
    for m in markets:
        close = _ts(m.get("close_time"))
        if close is None or not since <= close <= now:
            continue
        a, b = int(close.timestamp()) - 10 * 60, int(close.timestamp())

        def fetch(ticker=m["ticker"]):
            try:
                return _kalshi_call(kalshi.get_candlesticks, ticker, a, b)
            except Exception:
                try:
                    return _kalshi_call(kalshi.get_candlesticks, ticker, a, b, historical=True)
                except Exception:
                    return []

        out.append((m, _cached(f"lastmin-{m['ticker']}.json", fetch)))
    return out


def report(name: str, entries: list[Entry], markets: int, threshold: int) -> None:
    print(f"\n{name}: {markets} markets, buying the side priced {threshold}-99¢")
    for k in CHECKPOINTS_MIN:
        es = [e for e in entries if e.minutes_before == k]
        if not es:
            print(f"  {k} min before close:    0 chances")
            continue
        losses = [e for e in es if not e.won]
        net = sum(e.pnl for e in es)
        avg_price = sum(e.price for e in es) / len(es)
        # Loss rate at which the bets break even, at these prices.
        win_gain = sum(e.pnl for e in es if e.won) / max(1, len(es) - len(losses))
        loss_cost = STAKE_DOLLARS
        break_even = win_gain / (win_gain + loss_cost) if win_gain > 0 else 0.0
        print(f"  {k} min before close: {len(es):4} bets, {len(losses):3} reversed ({len(losses) / len(es):5.2%}; "
              f"break-even {break_even:.2%})  avg {avg_price:.1f}¢  net ${net:+,.2f} at ${STAKE_DOLLARS}/bet")


RULES_DEFAULT = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]
RULE_FIELDS = ("title", "subtitle", "yes_sub_title", "open_time", "close_time", "expiration_time",
               "floor_strike", "cap_strike", "strike_type", "rules_primary", "rules_secondary")


def print_rules(kalshi, series: list[str]) -> None:
    """Kalshi's own settlement rules for one current market of each series:
    whether it settles on one price or an average, and from which source."""
    for s in series:
        print(f"\n===== {s} =====")
        try:
            info = kalshi._request("GET", f"/series/{s}").get("series", {})
            print(f"Series: {info.get('title')}")
            for src in info.get("settlement_sources") or []:
                print(f"Settlement source: {src.get('name')} {src.get('url') or ''}")
            if info.get("contract_url"):
                print(f"Full contract terms: {info['contract_url']}")
        except Exception as e:
            print(f"(series info unavailable: {e})")
        market = None
        for status in ("open", "settled"):
            try:
                ms = kalshi.list_markets(series_ticker=s, status=status, limit=1).get("markets", [])
            except Exception:
                ms = []
            if ms:
                market = ms[0]
                break
        if market is None:
            print("No market found.")
            continue
        print(f"Example market: {market.get('ticker')}")
        for f in RULE_FIELDS:
            if market.get(f) not in (None, ""):
                print(f"  {f}: {market[f]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="How often near-certain 15-minute markets reverse")
    parser.add_argument("--days", type=float, default=1, help="how far back (default 1 day)")
    parser.add_argument("--series", nargs="*", help="series tickers (default: every 15-minute series found)")
    parser.add_argument("--rules", action="store_true", help="print how the markets settle, then stop")
    args = parser.parse_args()

    from .main import build_kalshi_client
    kalshi = build_kalshi_client(demo=False)
    if args.rules:
        print_rules(kalshi, args.series or RULES_DEFAULT)
        return
    if args.series:
        series = args.series
    else:
        series, others = find_series(kalshi)
        print(f"15-minute series on Kalshi: {', '.join(series) or 'none found'}")
        if others:
            print(f"Other oil/metals/index series (not 15-minute; add with --series): {', '.join(others[:15])}")

    per_series: dict[str, tuple[int, list[dict], list]] = {}
    for s in series:
        print(f"  loading {s}...", flush=True)
        per_series[s] = load(kalshi, s, args.days)

    for threshold in THRESHOLDS:
        print(f"\n===== Side priced {threshold}-99¢, last {args.days:g} day(s) =====")
        everything: list[Entry] = []
        total_markets = 0
        for s, rows in per_series.items():
            es = [e for m, c in rows for e in entries_for_market(s, m, c, threshold)]
            everything += es
            total_markets += len(rows)
            report(s, es, len(rows), threshold)
        if len(per_series) > 1:
            report("ALL SERIES", everything, total_markets, threshold)
        if threshold == 99:
            flips = [e for e in everything if not e.won]
            if flips:
                print("\n  Every reversal at 99¢:")
                for e in sorted(flips, key=lambda e: e.close):
                    print(f"    {e.close:%b %d %H:%M} UTC  {e.ticker}  bought {e.side.upper()} at {e.price}¢ "
                          f"{e.minutes_before} min before close -- lost ${-e.pnl:,.2f}")


if __name__ == "__main__":
    main()
