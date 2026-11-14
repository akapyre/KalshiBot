"""Re-test rule candidates from a spreadsheet (rule_candidates.csv) at the
prices Kalshi really charged.

The sheet's in-game "Winner" rules were priced from ESPN's win model, which
underrates big favorites who fall behind -- exactly what most of them bet
on. Here every rule is replayed on real games instead:

  - games: Kalshi's settled game markets (one "<team> wins" market per team)
  - game state: ESPN's play-by-play -- score, period, clock and the real
    time of every play -- so the score at any moment is known
  - price: Kalshi's 1-minute candles at that same moment, bought at the ask,
    Kalshi's fee included; "pregame odds" = Kalshi's price at tip-off/kickoff
  - timing: checked every 2 minutes, like the live bot; one bet per game

Usage (on the PC -- needs Kalshi and ESPN):
  python -m kalshibot.candidates path/to/rule_candidates.csv
  python -m kalshibot.candidates rule_candidates.csv --sports nba cbb --days 365

Writes the results next to the sheet as <name>_kalshi.csv. Pregame
spread/total rows are listed as not tested yet.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from .backtest import (GAME_LENGTH_MIN, SettledMarkets, _cached, ask_cents, candles_for, kalshi_fee,
                       kickoff_cents)
from .executor import american_odds
from .matching import SERIES_BY_SPORT, _label_side, event_date

LEAGUE_TO_SPORT = {"NBA": "nba", "NCAAB": "cbb", "NCAAF": "cfb", "NFL": "nfl"}
ESPN_PATH = {"nba": "basketball/nba", "cbb": "basketball/mens-college-basketball",
             "cfb": "football/college-football", "nfl": "football/nfl"}
# ESPN scoreboard "groups": all of Division I basketball; FBS + FCS football.
ESPN_GROUPS = {"cbb": ["50"], "cfb": ["80", "81"]}
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# (minutes per regulation period, regulation periods, minutes per overtime)
PERIODS = {"nba": (12, 4, 5), "cbb": (20, 2, 5), "cfb": (15, 4, 15), "nfl": (15, 4, 10)}
SAMPLE_S = 120          # the live bot polls every 2 minutes
STALE_PLAY_S = 15 * 60  # no play for this long: don't trust the score
STALE_PRICE_S = 5 * 60
STAKE = 100


# ------------------------------------------------------------------ the sheet

@dataclass
class Candidate:
    row: dict[str, str]
    settings: dict[str, Any]

    @property
    def sport(self) -> str | None:
        leagues = self.settings.get("leagues") or []
        return LEAGUE_TO_SPORT.get(leagues[0]) if leagues else None

    @property
    def in_game(self) -> bool:
        return "game_minutes" in self.settings and "live_min" in self.settings


def read_sheet(path: Path) -> list[Candidate]:
    rows = list(csv.reader(path.open(encoding="utf-8-sig")))
    start = next(i for i, r in enumerate(rows) if r and r[0].strip() == "Add to bot")
    header = rows[start]
    out = []
    for r in rows[start + 1:]:
        row = dict(zip(header, r))
        try:
            settings = json.loads(row.get("Rule settings") or "")
        except ValueError:
            continue
        out.append(Candidate(row, settings))
    return out


def in_range(value: float, lo: float | None, hi: float | None) -> bool:
    return (lo is None or value >= lo) and (hi is None or value <= hi)


# ------------------------------------------------------------------ ESPN

def _espn_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    for wait in (2, 5, None):
        time.sleep(0.2)
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if wait is None:
                raise
            time.sleep(wait)
    return {}


def espn_games(sport: str, day: date) -> list[dict[str, Any]]:
    """[{id, start, home, away}] for one ESPN date."""
    out: list[dict[str, Any]] = []
    for group in ESPN_GROUPS.get(sport, [None]):
        params = {"dates": f"{day:%Y%m%d}", "limit": 500, **({"groups": group} if group else {})}
        key = f"espn-sb-{sport}-{day:%Y%m%d}-{group}.json"
        data = _cached(key, lambda: _espn_get(f"{ESPN_BASE}/{ESPN_PATH[sport]}/scoreboard", params))
        for ev in (data or {}).get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            teams = {c.get("homeAway"): c.get("team", {}) for c in comp.get("competitors", [])}
            if "home" in teams and "away" in teams:
                out.append({"id": ev.get("id"), "start": ev.get("date"),
                            "home": teams["home"].get("displayName") or "",
                            "away": teams["away"].get("displayName") or "",
                            "home_short": teams["home"].get("location") or "",
                            "away_short": teams["away"].get("location") or ""})
    return out


def collect_plays(node: Any) -> list[dict[str, Any]]:
    """Every play object anywhere in an ESPN summary (basketball keeps them
    in "plays"; football nests them in drives)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if all(k in node for k in ("period", "clock", "homeScore", "awayScore")):
            found.append(node)
        for v in node.values():
            found += collect_plays(v)
    elif isinstance(node, list):
        for v in node:
            found += collect_plays(v)
    return found


def clock_minutes(display: str) -> float | None:
    """"5:32" -> 5.53; "32.5" (final-minute format) -> 0.54."""
    try:
        if ":" in display:
            m, s = display.split(":", 1)
            return int(m) + float(s) / 60
        return float(display) / 60
    except (TypeError, ValueError):
        return None


def elapsed_minutes(sport: str, period: int, clock_left: float) -> float:
    length, regs, ot = PERIODS[sport]
    if period <= regs:
        return (period - 1) * length + (length - clock_left)
    return regs * length + (period - regs - 1) * ot + (ot - clock_left)


@dataclass
class Moment:
    ts: float
    minute: float
    home: int
    away: int


def game_states(sport: str, summary: dict[str, Any]) -> list[Moment]:
    seen = set()
    out = []
    for p in collect_plays(summary):
        try:
            ts = datetime.fromisoformat(str(p["wallclock"]).replace("Z", "+00:00")).timestamp()
            period = int((p.get("period") or {}).get("number"))
            left = clock_minutes(str((p.get("clock") or {}).get("displayValue")))
            home, away = int(p["homeScore"]), int(p["awayScore"])
        except (KeyError, TypeError, ValueError):
            continue
        if left is None:
            continue
        key = (ts, period, left, home, away)
        if key not in seen:
            seen.add(key)
            out.append(Moment(ts, elapsed_minutes(sport, period, left), home, away))
    return sorted(out, key=lambda m: m.ts)


def espn_summary(sport: str, event_id: str) -> dict[str, Any]:
    return _cached(f"espn-sum-{sport}-{event_id}.json",
                   lambda: _espn_get(f"{ESPN_BASE}/{ESPN_PATH[sport]}/summary", {"event": event_id}))


# ------------------------------------------------------------------ games

@dataclass
class Game:
    sport: str
    label: str
    start: datetime
    states: list[Moment]
    # per side ("home"/"away"): Kalshi ticker, won?, [(ts, ask)], kickoff cents
    ticker: dict[str, str] = field(default_factory=dict)
    won: dict[str, bool] = field(default_factory=dict)
    asks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    kickoff: dict[str, int] = field(default_factory=dict)


def match_espn(sport: str, home_label: str, away_label: str, day: date) -> dict[str, Any] | None:
    """The ESPN game on (or a day either side of) the Kalshi date whose two
    teams match the two Kalshi labels."""
    for d in (day, day - timedelta(days=1), day + timedelta(days=1)):
        hits = []
        for g in espn_games(sport, d):
            a = _label_side(home_label, g["home"], g["away"])
            b = _label_side(away_label, g["home"], g["away"])
            if (a, b) in (("home", "away"), ("away", "home")):
                hits.append((g, a == "away"))
        if len(hits) == 1:
            return {**hits[0][0], "swapped": hits[0][1]}
    return None


def load_games(kalshi, sport: str, days: int) -> tuple[list[Game], dict[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    settled = SettledMarkets(kalshi, days)
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for series in SERIES_BY_SPORT.get(sport, []):
        for m in settled.iter_markets(series):
            by_event[m.get("event_ticker") or ""].append(m)
    games = []
    for n, (event, ms) in enumerate(sorted(by_event.items())):
        print(f"  {sport}: {n + 1}/{len(by_event)} Kalshi games, {len(games)} lined up with ESPN", end="\r", flush=True)
        counts["kalshi events"] += 1
        ms = [m for m in ms if str(m.get("result") or "").lower() in ("yes", "no")]
        day = event_date(event)
        if len(ms) != 2 or day is None:
            counts["not a two-team settled event"] += 1
            continue
        a, b = ms
        try:
            g = match_espn(sport, str(a.get("yes_sub_title") or ""), str(b.get("yes_sub_title") or ""), day)
        except requests.RequestException:
            g = None
        if g is None:
            counts["no matching ESPN game"] += 1
            continue
        # a is "home" unless the labels came out the other way round.
        sides = {"home": b, "away": a} if g["swapped"] else {"home": a, "away": b}
        try:
            start = datetime.fromisoformat(str(g["start"]).replace("Z", "+00:00"))
            states = game_states(sport, espn_summary(sport, g["id"]))
        except (requests.RequestException, ValueError):
            counts["ESPN play-by-play failed"] += 1
            continue
        if len(states) < 20:
            counts["no timed play-by-play"] += 1
            continue
        game = Game(sport, f"{g['away']} @ {g['home']}", start, states)
        for side, m in sides.items():
            candles = candles_for(kalshi, m["ticker"], start, GAME_LENGTH_MIN.get(sport, 240) + 30)
            game.ticker[side] = m["ticker"]
            game.won[side] = str(m.get("result")).lower() == "yes"
            game.asks[side] = sorted((int(c["end_period_ts"]), a) for c in candles
                                     if c.get("end_period_ts") is not None and (a := ask_cents(c)) is not None)
            k = kickoff_cents(candles, start)
            if k is not None:
                game.kickoff[side] = k
        if len(game.kickoff) != 2:
            counts["no Kalshi price at the start"] += 1
            continue
        games.append(game)
    print()
    counts["games tested"] = len(games)
    return games, counts


# ------------------------------------------------------------------ replay

@dataclass
class Bet:
    game: str
    side: str
    minute: float
    price: int
    won: bool

    @property
    def pnl(self) -> float:
        contracts = STAKE * 100 // self.price
        gross = contracts * (100 - self.price) / 100 if self.won else -contracts * self.price / 100
        return round(gross - kalshi_fee(contracts, self.price), 2)


def _last_before(items: list, times: list[float], ts: float):
    """The last item at or before ts (times = each item's time, sorted)."""
    i = bisect.bisect_right(times, ts)
    return items[i - 1] if i else None


def replay(c: Candidate, game: Game) -> Bet | None:
    s = c.settings
    lo_min, hi_min = s["game_minutes"]
    state_times = [m.ts for m in game.states]
    for side in ("home", "away"):
        pregame = american_odds(game.kickoff[side])
        if not in_range(pregame, s.get("pregame_min"), s.get("pregame_max")):
            continue
        quotes = game.asks[side]
        quote_times = [q[0] for q in quotes]
        start, end = game.start.timestamp(), game.states[-1].ts
        for now in range(int(start), int(end) + 1, SAMPLE_S):
            state = _last_before(game.states, state_times, now)
            quote = _last_before(quotes, quote_times, now)
            if state is None or quote is None or now - state.ts > STALE_PLAY_S or now - quote[0] > STALE_PRICE_S:
                continue
            diff = (state.home - state.away) * (1 if side == "home" else -1)
            if not (lo_min <= state.minute <= hi_min and in_range(diff, s.get("min_score_diff"), s.get("max_score_diff"))):
                continue
            ask = quote[1]
            if not 1 <= ask <= 99 or not in_range(american_odds(ask), s.get("live_min"), s.get("live_max")):
                continue
            return Bet(game.label, side, state.minute, ask, game.won[side])
    return None


def summarize(bets: list[Bet]) -> dict[str, Any]:
    if not bets:
        return {"bets": 0}
    cost = sum(STAKE * 100 // b.price * b.price / 100 for b in bets)
    net = sum(b.pnl for b in bets)
    return {"bets": len(bets), "win": sum(b.won for b in bets) / len(bets),
            "price": sum(b.price for b in bets) / len(bets), "net": net, "roi": net / cost}


def verdict(sheet_bets: int, real: dict[str, Any]) -> str:
    n = real["bets"]
    if n == 0:
        return "never fired at real Kalshi prices"
    if n < 20:
        return "too few real bets to judge"
    # Rough luck check: is the average profit more than ~2 standard errors above zero?
    roi = real["roi"]
    if roi <= 0:
        return "LOSES at real prices"
    se = math.sqrt(real["win"] * (1 - real["win"]) / n) / max(real["price"] / 100, 0.01)
    return "holds up at real prices" if roi > 2 * se else "positive but could be luck"


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-test rule candidates at real Kalshi prices")
    parser.add_argument("sheet", type=Path)
    parser.add_argument("--sports", nargs="*", default=["nba", "cbb", "cfb", "nfl"], choices=list(ESPN_PATH))
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    candidates = read_sheet(args.sheet)
    in_game = [c for c in candidates if c.in_game and c.sport in args.sports]
    print(f"{len(candidates)} candidates in the sheet; {len(in_game)} in-game rules to test "
          f"({', '.join(args.sports)}); {sum(not c.in_game for c in candidates)} pregame spread/total rules not tested yet.")

    from .main import build_kalshi_client
    kalshi = build_kalshi_client(demo=False)
    games_by_sport: dict[str, list[Game]] = {}
    for sport in sorted({c.sport for c in in_game}):
        games, counts = load_games(kalshi, sport, args.days)
        games_by_sport[sport] = games
        print(f"  {sport}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))

    out_rows = []
    print(f"\n{'Ticked':6} {'Sheet ROI':>9} {'Sheet bets':>10} | {'Real bets':>9} {'Win':>6} {'Price':>6} {'Real ROI':>8} | Verdict / rule")
    for c in in_game:
        bets = [b for g in games_by_sport.get(c.sport, []) if (b := replay(c, g))]
        real = summarize(bets)
        v = verdict(int(c.row.get("Bets") or 0), real)
        line = (f"{c.row.get('Add to bot', ''):6} {c.row.get('Est. ROI', ''):>9} {c.row.get('Bets', ''):>10} | "
                f"{real['bets']:>9} " + (f"{real['win']:>6.1%} {real['price']:>5.1f}c {real['roi']:>+8.1%}"
                                         if real["bets"] else f"{'':>6} {'':>6} {'':>8}")
                + f" | {v}: {c.row.get('Rule', '')[:90]}")
        print(line)
        out_rows.append({**c.row, "Kalshi bets": real["bets"],
                         "Kalshi win %": f"{real['win']:.1%}" if real["bets"] else "",
                         "Kalshi avg price": f"{real['price']:.1f}c" if real["bets"] else "",
                         "Kalshi ROI": f"{real['roi']:+.1%}" if real["bets"] else "",
                         "Kalshi verdict": v})
    for c in candidates:
        if not c.in_game:
            out_rows.append({**c.row, "Kalshi verdict": "not tested yet (pregame spread/total)"})
        elif c.sport not in args.sports:
            out_rows.append({**c.row, "Kalshi verdict": "not tested in this run (sport not selected)"})

    out = args.sheet.with_name(args.sheet.stem + "_kalshi.csv")
    fields = list(out_rows[0].keys()) if out_rows else []
    for r in out_rows:
        fields += [k for k in r if k not in fields]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nSaved {out} (open it in Excel; the last columns are the real-price results).")


if __name__ == "__main__":
    main()
