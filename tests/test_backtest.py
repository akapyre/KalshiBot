import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.backtest import ask_cents, estimated_period, kalshi_fee, kickoff_cents, replayable, simulate_game
from kalshibot.rules_engine import load_rules

RULES, _ = load_rules(Path(__file__).resolve().parents[1] / "config" / "rules.yaml")
START = datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)
T0 = int(START.timestamp())


def path(*points):
    """(minute, cents) -> (unix ts, cents)."""
    return [(T0 + minute * 60, cents) for minute, cents in points]


def test_nfl_moderate_favorite_fires_once_in_band_and_settles():
    prices = path((0, 62), (30, 55), (60, 44), (65, 43), (90, 41))
    trades = simulate_game(RULES, "nfl", "DET @ CAR", "Detroit", START, -225, prices, won=True)
    assert [t.rule for t in trades] == ["nfl_moderate_favorite"]
    t = trades[0]
    assert (t.price, t.contracts, t.minute) == (44, 34, 60)
    assert t.pnl == round(34 - 34 * 0.44 - kalshi_fee(34, 44), 2)


def test_steep_nfl_favorite_can_stack_fade_then_q4_reentry():
    # -300 favorite: fades to 52c (-108) in Q2, then +140 (42c) in the 4th quarter.
    prices = path((0, 75), (60, 52), (120, 50), (150, 42))
    trades = simulate_game(RULES, "nfl", "g", "Fav", START, -300, prices, won=False)
    assert [(t.rule, t.minute) for t in trades] == [
        ("nfl_favorite_fade", 60), ("nfl_fourth_quarter_reentry", 150), ("nfl_moderate_favorite", 150)]
    assert all(t.pnl < 0 for t in trades)


def test_mlb_rule_waits_for_the_7th_inning():
    early = simulate_game(RULES, "mlb", "g", "Fav", START, -220, path((30, 45)), won=True)
    late = simulate_game(RULES, "mlb", "g", "Fav", START, -220, path((30, 70), (115, 45)), won=True)
    assert early == [] and [t.rule for t in late] == ["mlb_favorite_fade"]


def test_prices_are_sampled_every_five_minutes_like_the_bot():
    # A one-minute dip at minute 2 is between samples (0 and 5): not seen.
    prices = path((0, 62), (2, 44), (3, 62), (5, 62))
    assert simulate_game(RULES, "nfl", "g", "Fav", START, -225, prices, won=True) == []


def test_score_based_rules_are_not_replayed():
    ids = {r.id for r in RULES if not replayable(r)}
    assert {"nhl_favorite_trails_by_one", "soccer_first_half_favorite"} <= ids


@pytest.mark.parametrize("candle, cents", [
    ({"yes_ask": {"close_dollars": "0.4500"}}, 45),
    ({"yes_ask": {"close": 45}}, 45),
    ({"yes_ask": {"close": "0.45"}}, 45),
    ({"yes_ask": {}, "price": {"close": 0.45}}, 45),
    ({"volume": 3}, None),
])
def test_candle_price_formats(candle, cents):
    assert ask_cents(candle) == cents


def test_estimated_periods():
    assert [estimated_period("nfl", m) for m in (0, 50, 120, 150)] == [1, 2, 3, 4]
    assert estimated_period("mlb", 110) == 7
    assert estimated_period("soccer", 50) is None


def test_fee_rounds_up():
    assert kalshi_fee(34, 44) == 0.59   # 0.07*34*0.44*0.56 = 0.5864


def candle(minute, bid, ask):
    return {"end_period_ts": T0 + minute * 60,
            "yes_bid": {"close": f"{bid / 100:.4f}"}, "yes_ask": {"close": f"{ask / 100:.4f}"}}


def test_kickoff_price_is_the_midpoint_nearest_the_start():
    # Real archive shape (Bills-Ravens, Sep 2025): coin flip at kickoff,
    # even though the game's final "closing line" said -2000.
    candles = [candle(-10, 48, 49), candle(0, 48, 50), candle(60, 20, 22)]
    assert kickoff_cents(candles, START) == 49


def test_no_kickoff_price_without_a_candle_near_the_start():
    assert kickoff_cents([candle(30, 40, 42)], START) is None


def test_empty_downloads_are_not_cached(tmp_path, monkeypatch):
    import kalshibot.backtest as bt
    monkeypatch.setattr(bt, "CACHE", tmp_path)
    assert bt._cached("x.json", lambda: []) == []
    assert not (tmp_path / "x.json").exists()
    (tmp_path / "y.json").write_text("[]")          # left over from an earlier run
    assert bt._cached("y.json", lambda: [1]) == [1]  # retried, then saved
    assert bt._cached("y.json", lambda: [2]) == [1]


def test_throttled_calls_are_retried(monkeypatch):
    import kalshibot.backtest as bt
    monkeypatch.setattr(bt.time, "sleep", lambda s: None)
    calls = []

    class Throttled(Exception):
        response = type("R", (), {"status_code": 429})()

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise Throttled()
        return ["ok"]

    assert bt._kalshi_call(fn) == ["ok"] and len(calls) == 3
