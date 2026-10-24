import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.maker import POLL_S, Result, Side, maker_fee, replay

T0 = 1_800_000_000


def test_quotes_inside_the_spread_and_fills_on_crossing_trades():
    quotes = {T0: (40, 44)}                               # we bid 41, offer 43
    prints = [(T0 + 10, "no", 40, 30.0),                  # taker sold YES at 40 -> crosses our 41 bid
              (T0 + 20, "yes", 42, 10.0),                 # taker bought YES at 42 -> below our 43 offer: no fill
              (T0 + 30, "yes", 44, 20.0)]                 # bought at 44 -> fills our 43 offer
    yes, no = replay(quotes, prints, T0, T0 + 2 * POLL_S + 1)
    assert yes.fills == [(T0 + 10, 41, 30.0)]
    assert no.fills == [(T0 + 30, 57, 20.0)]              # selling YES at 43 = buying NO at 57


def test_no_quotes_when_the_spread_is_one_cent():
    yes, no = replay({T0: (40, 41)}, [(T0 + 10, "no", 30, 100.0)], T0, T0 + 2 * POLL_S + 1)
    assert not yes.fills and not no.fills


def test_each_side_stops_at_25_dollars():
    yes, _ = replay({T0: (40, 44)}, [(T0 + 10, "no", 40, 1000.0)], T0, T0 + 2 * POLL_S + 1)
    assert yes.spent() == pytest.approx(2500)


def test_both_sides_filled_locks_in_the_spread():
    yes = Side(True, [(T0, 41, 10.0)])
    no = Side(False, [(T0, 57, 10.0)])
    for yes_won in (True, False):
        pnl = Result("S", "T", T0, yes_won, yes, no).pnl()
        assert pnl == pytest.approx(10 * 0.02 - maker_fee(10, 41) - maker_fee(10, 57))
