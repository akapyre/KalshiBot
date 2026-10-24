import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.weather import Check, bucket_bounds, bucket_prob, event_date, fit, simulate


def test_bucket_bounds_follow_kalshis_wording():
    assert bucket_bounds({"strike_type": "less", "cap_strike": 59}) == (None, 58)        # "58 or below"
    assert bucket_bounds({"strike_type": "greater", "floor_strike": 66}) == (67, None)   # "67 or above"
    assert bucket_bounds({"strike_type": "between", "floor_strike": 59, "cap_strike": 60}) == (59, 60)


def test_bucket_probabilities_cover_every_outcome():
    buckets = [(None, 58), (59, 60), (61, 62), (63, 64), (65, 66), (67, None)]
    probs = [bucket_prob(61.3, 2.5, lo, hi) for lo, hi in buckets]
    assert sum(probs) == pytest.approx(1.0)
    assert max(probs) == probs[2]          # the bucket holding the forecast is most likely


def test_event_date():
    assert str(event_date("KXHIGHNY-26OCT24")) == "2026-10-24"


def _check(day, forecast, actual, lo, hi, bid, ask, won):
    return Check("KXHIGHNY", f"KXHIGHNY-{day}", day, f"KXHIGHNY-{day}-{lo}", lo, hi, forecast, actual, bid, ask, won)


def test_fit_uses_only_days_before_the_cut():
    old = [_check(f"2026-09-{d:02d}", 70, 72, 71, 72, 40, 41, True) for d in range(1, 13)]   # forecast 2F low
    new = [_check(f"2026-10-{d:02d}", 70, 60, 59, 60, 40, 41, True) for d in range(1, 13)]   # would say 10F high
    bias, sd = fit(old + new, "2026-10-01")["KXHIGHNY"]
    assert bias == pytest.approx(2) and sd == 1.5      # spread floors at MIN_SD


def test_simulate_buys_the_side_with_edge():
    c = _check("2026-10-01", 70, 71, 71, 72, 30, 32, True)
    c.p = 0.60
    [bet] = simulate([c], min_edge=4)
    assert (bet.side, bet.price, bet.won) == ("yes", 32, True)
    c.p = 0.05
    [bet] = simulate([c], min_edge=4)
    assert (bet.side, bet.price, bet.won) == ("no", 70, False)
