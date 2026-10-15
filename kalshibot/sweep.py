"""Search for better versions of the "buy the fallen favorite" rules, with a
guard against fooling ourselves.

Usage:  python -m kalshibot.sweep cfb             (400 days by default)
        python -m kalshibot.sweep nfl --days 400

It replays thousands of rule variations over the same past games as the
backtest (cached data, so it's quick after a backtest has run), varying:
  - how strong the favorite was at kickoff  (e.g. -200, -300, -400 or stronger)
  - the price range it buys in             (e.g. 40-49c = +150..+104)
  - when in the game                        (any time, 1st half, 2nd half, 4th quarter...)
  - how far it has fallen since kickoff     (any, 15c+, 25c+)
  - whether it has stopped falling          (price not lower than 10 minutes earlier)

Every variation bets at most once per game, at the first 5-minute check
that matches, at Kalshi's ask, fees included.

Guard against luck: games are split by date. Variations are ranked using
only the OLDER two-thirds; the NEWER third is then used to check them. A
variation that only looked good by chance usually falls apart on the newer
games. Only trust ones that hold up there with a decent number of bets.
"""
from __future__ import annotations

import argparse
import math
import statistics
from dataclasses import dataclass, field
from itertools import product

from .backtest import (GAME_LENGTH_MIN, MLB_MIN_PER_INNING, QUARTER_STARTS, SAMPLE_EVERY_S, STAKE_CENTS,
                       GameData, kalshi_fee, load_games)
from .executor import american_odds
from .main import SPORTS

# How strong the favorite was at kickoff (its Kalshi price, as odds).
PREGAME_ODDS = [-150, -200, -250, -300, -400, -500]
# Price ranges to buy in (inclusive cents).
BANDS = [(30, 39), (35, 44), (40, 49), (45, 54), (50, 59), (35, 49), (40, 54), (45, 59), (30, 49), (40, 59)]
DROPS = [0, 15, 25]          # cents fallen since kickoff, at least
STEADY = [False, True]       # price not lower than 10 minutes earlier
MIN_TRAIN_BETS = 40
MIN_TEST_BETS = 15


def windows(sport: str) -> dict[str, tuple[float, float]]:
    """Named stretches of the game, in minutes since the start."""
    inf = float("inf")
    if sport in QUARTER_STARTS:
        q = QUARTER_STARTS[sport]
        return {"any time": (0, inf), "1st half": (0, q[2]), "2nd half": (q[2], inf),
                "Q1-Q3": (0, q[3]), "4th quarter": (q[3], inf)}
    if sport == "mlb":
        inning = lambda n: (n - 1) * MLB_MIN_PER_INNING
        return {"any time": (0, inf), "innings 1-5": (0, inning(6)), "inning 6+": (inning(6), inf),
                "inning 7+": (inning(7), inf)}
    if sport == "soccer":
        return {"any time": (0, inf), "1st half": (0, 50), "2nd half": (60, inf)}
    half = GAME_LENGTH_MIN.get(sport, 240) / 2
    return {"any time": (0, inf), "first half of play": (0, half), "second half of play": (half, inf)}


@dataclass(frozen=True)
class Setup:
    pregame: int
    band: tuple[int, int]
    window: str
    drop: int
    steady: bool

    def describe(self) -> str:
        lo, hi = self.band
        parts = [f"favorite {self.pregame:+d} or stronger",
                 f"buy at {lo}-{hi}¢ ({american_odds(hi):+d} to {american_odds(lo):+d})", self.window]
        if self.drop:
            parts.append(f"fallen {self.drop}¢+ since kickoff")
        if self.steady:
            parts.append("stopped falling")
        return " | ".join(parts)


def samples(game: GameData) -> list[tuple[float, int, int | None]]:
    """(minutes since start, ask, ask 10 minutes earlier) every 5 minutes,
    the way the live bot looks at a game."""
    out: list[tuple[float, int, int | None]] = []
    t0 = game.start.timestamp()
    next_sample = t0
    for ts, ask in game.prices:
        if ts < next_sample or not 1 <= ask <= 99:
            continue
        next_sample = ts + SAMPLE_EVERY_S
        out.append(((ts - t0) / 60, ask, out[-2][1] if len(out) >= 2 else None))
    return out


def bet_pnl(ask: int, won: bool) -> tuple[float, float]:
    """(cost, profit) of a $25 bet at this price."""
    contracts = STAKE_CENTS // ask
    cost = contracts * ask / 100
    return cost, (contracts if won else 0) - cost - kalshi_fee(contracts, ask)


def first_entry(setup: Setup, game: GameData, pts, wins: dict[str, tuple[float, float]]) -> int | None:
    if american_odds(game.kickoff) > setup.pregame:
        return None
    lo, hi = setup.band
    start, end = wins[setup.window]
    for minute, ask, before in pts:
        if not start <= minute < end or not lo <= ask <= hi:
            continue
        if game.kickoff - ask < setup.drop:
            continue
        if setup.steady and (before is None or ask < before):
            continue
        return ask
    return None


@dataclass
class Result:
    bets: int = 0
    wins: int = 0
    cost: float = 0.0
    net: float = 0.0
    implied: float = 0.0
    pnls: list[float] = field(default_factory=list)
    picks: list[tuple[str, int]] = field(default_factory=list)   # (game, price) -- spots duplicates

    def add(self, ask: int, won: bool, key: str = "") -> None:
        self.picks.append((key, ask))
        cost, pnl = bet_pnl(ask, won)
        self.bets += 1
        self.wins += won
        self.cost += cost
        self.net += pnl
        self.implied += ask / 100
        self.pnls.append(pnl)

    @property
    def roi(self) -> float:
        return self.net / self.cost if self.cost else 0.0

    @property
    def t_stat(self) -> float:
        """Average profit per bet in standard errors: how far from luck."""
        if self.bets < 3:
            return 0.0
        sd = statistics.stdev(self.pnls)
        return (self.net / self.bets) / (sd / math.sqrt(self.bets)) if sd else 0.0

    def line(self) -> str:
        if not self.bets:
            return "  no bets"
        return (f"{self.bets:4} bets  win {self.wins / self.bets:5.1%} vs {self.implied / self.bets:5.1%} priced"
                f"  net {self.net:+8.2f}  ROI {self.roi:+6.1%}")


def evaluate(setup: Setup, games: list[GameData], pts: dict[str, list], wins) -> Result:
    r = Result()
    for g in games:
        ask = first_entry(setup, g, pts[g.ticker], wins)
        if ask is not None:
            r.add(ask, g.won, g.ticker)
    return r


def sweep(sport: str, days: int, top: int = 12) -> None:
    games, found, _ = load_games(sport, days)
    games.sort(key=lambda g: g.start)
    if len(games) < 60:
        print(f"Only {len(games)} games with prices -- too few to search. Try more --days.")
        return
    cut = len(games) * 2 // 3
    train, test = games[:cut], games[cut:]
    pts = {g.ticker: samples(g) for g in games}
    wins = windows(sport)
    setups = [Setup(p, b, w, d, s) for p, b, w, d, s in product(PREGAME_ODDS, BANDS, wins, DROPS, STEADY)]

    print(f"\n=== {sport.upper()} sweep: {len(games)} games -- ranked on the older {len(train)} "
          f"({train[0].start:%b %Y}-{train[-1].start:%b %Y}), checked on the newer {len(test)} "
          f"({test[0].start:%b %Y}-{test[-1].start:%b %Y}) ===")
    print(f"  {len(setups)} variations tested. With that many, some will look great by luck alone --")
    print("  only the 'newer games' column is a fair test.\n")

    scored = []
    for i, setup in enumerate(setups, 1):
        if i % 200 == 0:
            print(f"  testing {i}/{len(setups)}", end="\r", flush=True)
        tr = evaluate(setup, train, pts, wins)
        if tr.bets >= MIN_TRAIN_BETS and tr.net > 0:
            scored.append((tr.t_stat, setup, tr))
    print(" " * 40, end="\r")
    scored.sort(key=lambda x: x[0], reverse=True)
    # Different settings often pick exactly the same bets; show each set once.
    seen, unique = set(), []
    for item in scored:
        picks = tuple(item[2].picks)
        if picks not in seen:
            seen.add(picks)
            unique.append(item)
    scored = unique

    if not scored:
        print("  No variation made money on the older games with enough bets. That itself is the answer:")
        print("  none of these 'buy the fallen favorite' setups shows an edge in this sport.")
        return

    held = 0
    for rank, (t, setup, tr) in enumerate(scored[:top], 1):
        te = evaluate(setup, test, pts, wins)
        verdict = ("HELD UP" if te.bets >= MIN_TEST_BETS and te.net > 0
                   else "too few newer bets" if te.bets < MIN_TEST_BETS else "did NOT hold up")
        held += verdict == "HELD UP"
        print(f"{rank:2}. {setup.describe()}")
        print(f"      older games: {tr.line()}")
        print(f"      newer games: {te.line()}   -> {verdict}")
    print(f"\n  {held} of the top {min(top, len(scored))} held up on newer games.")
    # Luck gauge: of every variation that made money on the older games, how
    # many also did on the newer ones? With no real pattern it's about half.
    checked = [evaluate(setup, test, pts, wins) for _, setup, _ in scored]
    checked = [te for te in checked if te.bets >= MIN_TEST_BETS]
    if checked:
        share = sum(te.net > 0 for te in checked) / len(checked)
        print(f"  Luck gauge: {share:.0%} of the {len(checked)} variations that made money on older games "
              f"also made money on newer ones")
        print("  (about 50% = probably luck; clearly above 60-65% = more likely a real pattern).")
    if not held:
        print("  Nothing held up: the best-looking variations were most likely luck.")
    else:
        print("  A variation that held up is a candidate, not a proven edge. Adopting one means")
        print("  updating rules.yaml -- tell Claude which number you want and it will translate it.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Search rule variations with an out-of-sample check")
    parser.add_argument("sport", choices=SPORTS)
    parser.add_argument("--days", type=int, default=400, help="how far back (default 400)")
    parser.add_argument("--top", type=int, default=12, help="how many variations to show")
    args = parser.parse_args()
    sweep(args.sport, args.days, args.top)


if __name__ == "__main__":
    main()
