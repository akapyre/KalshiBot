import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.backtest import GameData
from kalshibot.sweep import Result, Setup, bet_pnl, evaluate, first_entry, samples, windows

START = datetime(2025, 10, 4, 17, 0, tzinfo=timezone.utc)
T0 = int(START.timestamp())


def game(kickoff, path, won, ticker="T"):
    """path: (minute, cents) held each minute until the next point (the
    last one for 30 more minutes)."""
    prices = []
    for (m1, c1), (m2, _) in zip(path, path[1:] + [(path[-1][0] + 30, 0)]):
        prices += [(T0 + m * 60, c1) for m in range(m1, m2)]
    return GameData("cfb", "A @ B", "Fav", START, kickoff, prices, won, ticker)


WINS = windows("cfb")


def test_samples_every_five_minutes_with_ten_minute_lookback():
    pts = samples(game(80, [(0, 80), (12, 45)], True))
    assert [(m, a) for m, a, _ in pts[:4]] == [(0, 80), (5, 80), (10, 80), (15, 45)]
    assert pts[3][2] == 80 and pts[1][2] is None


def test_entry_respects_strength_band_window_drop_and_steadiness():
    g = game(80, [(0, 80), (20, 48), (40, 46), (130, 44)], True)
    pts = samples(g)
    base = dict(pregame=-300, band=(40, 49), window="any time", drop=0, steady=False)
    assert first_entry(Setup(**base), g, pts, WINS) == 48
    assert first_entry(Setup(**{**base, "pregame": -500}), g, pts, WINS) is None    # 80c = -400, not -500
    assert first_entry(Setup(**{**base, "window": "2nd half"}), g, pts, WINS) == 46  # waits for minute 125+
    assert first_entry(Setup(**{**base, "drop": 35}), g, pts, WINS) == 44            # 80 -> 44 is 36c
    # steady: skips minute 20 (48c, down from 80c ten minutes earlier), buys at
    # minute 30 once 48c has held for ten minutes
    steady_pts = [p for p in pts if p[0] in (20.0, 30.0)]
    assert [p[1] >= p[2] for p in steady_pts] == [False, True]
    assert first_entry(Setup(**{**base, "steady": True}), g, pts, WINS) == 48


def test_evaluate_and_pnl():
    games = [game(80, [(0, 80), (20, 45)], True, "a"), game(80, [(0, 80), (20, 45)], False, "b")]
    pts = {g.ticker: samples(g) for g in games}
    r = evaluate(Setup(-300, (40, 49), "any time", 0, False), games, pts, WINS)
    assert (r.bets, r.wins) == (2, 1)
    win_cost, win_pnl = bet_pnl(45, True)
    assert round(r.net, 2) == round(win_pnl + bet_pnl(45, False)[1], 2)


def test_describe_reads_plainly():
    text = Setup(-300, (40, 49), "2nd half", 15, True).describe()
    assert text == ("favorite -300 or stronger | buy at 40-49¢ (+104 to +150) | 2nd half"
                    " | fallen 15¢+ since kickoff | stopped falling")
