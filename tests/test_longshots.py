import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.longshots import Pick, basket, candidates

NOW = 1_800_000_000


def _market(ticker, no_ask, depth=500, volume=10000, days=10):
    from datetime import datetime, timezone
    exp = datetime.fromtimestamp(NOW + days * 86400, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"ticker": ticker, "status": "active", "no_ask_dollars": str(no_ask), "yes_bid_size_fp": str(depth),
            "volume_fp": str(volume), "expected_expiration_time": exp, "close_time": exp, "yes_sub_title": ticker}


def test_candidates_filter_price_category_volume_and_date():
    events = [
        {"event_ticker": "E1", "category": "Politics", "title": "t",
         "markets": [_market("OK", 0.97), _market("CHEAP", 0.90), _market("THIN", 0.97, volume=10),
                     _market("LATE", 0.97, days=400), _market("NOBID", 0.97, depth=0)]},
        {"event_ticker": "E2", "category": "Sports", "title": "t", "markets": [_market("SPORT", 0.97)]},
    ]
    assert [p.ticker for p in candidates(events, NOW, min_no=0.95)] == ["OK"]


def test_profit_after_fee():
    p = Pick("T", "E", "Politics", "", "", 0.97, 1000, 10, 1)
    n = 25 / 0.97
    assert p.profit() == pytest.approx(n * 0.03 - 0.07 * n * 0.97 * 0.03)


def test_basket_takes_one_per_event_and_stops_at_budget():
    picks = [Pick(f"T{i}", f"E{i // 2}", "Politics", "", "", 0.97, 1000, 1 + i, 1) for i in range(10)]
    chosen = basket(picks, 100)
    assert len(chosen) == 4
    assert len({p.event for p in chosen}) == 4
    assert [p.ticker for p in chosen] == ["T0", "T2", "T4", "T6"]    # soonest = best per day


def test_basket_at_50_skips_markets_too_thin_to_fill():
    picks = [Pick("THIN", "E1", "Politics", "", "", 0.98, 30, 1, 1),     # $29 offered
             Pick("DEEP", "E2", "Politics", "", "", 0.98, 1000, 2, 1)]
    assert [p.ticker for p in basket(picks, 500)] == ["THIN", "DEEP"]
    assert [p.ticker for p in basket(picks, 500, stake=50)] == ["DEEP"]
    p = Pick("T", "E", "", "", "", 0.98, 1, 1, 1)
    assert p.profit(50) == pytest.approx(2 * p.profit(25))
