from datetime import datetime, timezone

from kalshibot.recorder import (book_levels, next_close, simulate, top_of_book, walk_average_sd,
                                yes_chance)

CLOSE = 1_700_000_100   # a multiple of 900? not needed for the model


def test_orderbook_both_formats():
    new = {"orderbook_fp": {"yes_dollars": [["0.4000", "10.00"], ["0.4500", "5.00"]], "no_dollars": [["0.5300", "7.00"]]}}
    old = {"orderbook": {"yes": [[40, 10], [45, 5]], "no": [[53, 7]]}}
    for book in (new, old):
        assert book_levels(book, "yes") == [(45, 5.0), (40, 10.0)]
        assert top_of_book(book) == {"yes_ask": 47, "yes_ask_qty": 7.0, "no_ask": 55, "no_ask_qty": 5.0}


def test_next_close_is_the_coming_quarter_hour():
    now = datetime(2026, 11, 13, 20, 47, 30, tzinfo=timezone.utc)
    assert next_close(now) == datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)


def test_walk_average_sd():
    assert walk_average_sd(1.0, 0) == 0
    assert walk_average_sd(1.0, 1) == 1.0
    assert round(walk_average_sd(1.0, 3), 4) == round(14 ** 0.5 / 3, 4)   # sqrt(1+4+9)/3


def flat_then(price_known, price_now, now_left):
    """Prices for 3 minutes before CLOSE: steady at 100 with tiny noise, then
    price_known through the averaging window up to `now`."""
    prices = {}
    for t in range(CLOSE - 180, CLOSE - now_left + 1):
        base = 100.0 if t <= CLOSE - 60 else price_known
        prices[t] = base + (0.01 if t % 2 else -0.01)
    prices[CLOSE - now_left] = price_now
    return prices


def test_chance_reflects_locked_in_average():
    # 50 of 60 seconds averaged ~101 vs a 100 strike: YES is near certain.
    prices = flat_then(101.0, 101.0, 10)
    assert yes_chance(prices, CLOSE - 10, CLOSE, 100.0) > 0.99
    # Same but averaging right at the strike: a coin flip.
    prices = flat_then(100.0, 100.0, 10)
    assert 0.3 < yes_chance(prices, CLOSE - 10, CLOSE, 100.0) < 0.7


def test_simulate_buys_when_kalshi_lags():
    close_iso = datetime.fromtimestamp(CLOSE, tz=timezone.utc).isoformat()
    market = {"coin": "BTC", "ticker": "KXBTC15M-X", "close": close_iso, "strike": 100.0}
    prices = flat_then(101.0, 101.0, 10)
    # Kalshi quotes only from 10 seconds out, still at 80c for YES.
    ticks = [{"t": t, "spot": p, **({"yes_ask": 80, "no_ask": 22} if t >= CLOSE - 10 else {})}
             for t, p in prices.items()]
    fills = simulate(market, ticks, "yes")
    assert len(fills) == 1 and fills[0].side == "yes" and fills[0].price == 80 and fills[0].won
    assert fills[0].pnl > 0
    # Kalshi already at 99c: nothing to gain, no buy.
    ticks = [{"t": t, "spot": p, **({"yes_ask": 99, "no_ask": 2} if t >= CLOSE - 10 else {})}
             for t, p in prices.items()]
    assert simulate(market, ticks, "yes") == []
