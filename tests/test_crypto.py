import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.crypto import Sample, edge_cents, fair_prob, realized_sigma, simulate
from kalshibot.paper import kalshi_fee

SIGMA = 0.0004 / math.sqrt(60)    # 0.04% per minute


def test_at_the_money_is_a_coin_flip_and_moves_with_spot():
    assert fair_prob(80000, 80000, 1800, SIGMA) == pytest.approx(0.5, abs=0.01)
    assert fair_prob(80400, 80000, 1800, SIGMA) > 0.75
    assert fair_prob(79600, 80000, 1800, SIGMA) < 0.25


def test_less_time_left_means_more_certain():
    far, near = fair_prob(80200, 80000, 3000, SIGMA), fair_prob(80200, 80000, 180, SIGMA)
    assert 0.5 < far < near < 1


def test_realized_sigma_needs_enough_data_and_measures_moves():
    assert realized_sigma([100.0] * 10) is None
    closes = [100 * (1.001 if i % 2 else 1) for i in range(200)]   # +-0.1% every minute
    assert realized_sigma(closes) * math.sqrt(60) == pytest.approx(0.001, rel=0.01)


def test_edge_picks_the_cheaper_side_net_of_fees():
    side, price, ev = edge_cents(0.70, bid=55, ask=60)
    fee = kalshi_fee(2500 // 60, 60) * 100 / (2500 // 60)
    assert (side, price) == ("yes", 60) and ev == pytest.approx(70 - 60 - fee)
    side, price, _ = edge_cents(0.20, bid=40, ask=45)          # NO costs 100 - 40 = 60c, wins 80%
    assert (side, price) == ("no", 60)


def _sample(ticker, ts, p, bid, ask, won, event="E1"):
    return Sample(event, ticker, 80000, 3600, ts, 3600 - ts, 80000, SIGMA, p, bid, ask, won)


def test_simulate_bets_once_per_strike_at_first_edge():
    samples = [_sample("A", 120, 0.52, 48, 50, True),     # no edge yet
               _sample("A", 240, 0.70, 55, 57, True),     # edge -> YES at 57
               _sample("A", 360, 0.90, 60, 62, True),     # already bet
               _sample("B", 240, 0.10, 30, 32, False)]    # NO at 70c
    bets = simulate(samples, min_edge=4)
    assert [(b.ticker, b.side, b.price, b.won) for b in bets] == [("A", "yes", 57, True), ("B", "no", 70, True)]
    a = bets[0]
    assert a.pnl == round(43 - 43 * 0.57 - kalshi_fee(43, 57), 2)
