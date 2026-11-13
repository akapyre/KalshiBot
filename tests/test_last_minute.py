from kalshibot.last_minute import Entry, entries_for_market, favorite_buy, find_series, quote_at

CLOSE = 1_700_000_000


def candle(minutes_before, bid, ask):
    return {"end_period_ts": CLOSE - 60 * minutes_before,
            "yes_bid": {"close_dollars": f"{bid / 100:.4f}"}, "yes_ask": {"close_dollars": f"{ask / 100:.4f}"}}


MARKET = {"ticker": "KXBTC15M-X", "close_time": "2023-11-14T22:13:20Z", "result": "no"}


def test_quote_uses_last_candle_at_or_before_the_moment():
    cs = [candle(6, 40, 42), candle(3, 97, 99), candle(1, 0, 2)]
    assert quote_at(cs, CLOSE - 120) == (97, 99)
    assert quote_at(cs, CLOSE - 60) == (0, 2)
    assert quote_at(cs, CLOSE - 600) == (None, None)


def test_favorite_side_yes_or_no():
    assert favorite_buy(97, 99, 99) == ("yes", 99)
    assert favorite_buy(1, 3, 97) == ("no", 99)
    assert favorite_buy(50, 52, 95) is None
    assert favorite_buy(99, 100, 99) is None   # no one selling YES below $1, NO costs 1¢ -- not a favorite


def test_entries_settle_with_the_real_result():
    cs = [candle(5, 96, 97), candle(3, 97, 99), candle(2, 98, 99), candle(1, 98, 99)]
    es = entries_for_market("KXBTC15M", MARKET, cs, 99)
    # YES was the 99¢ favorite 1-3 minutes out, but the market settled NO: three reversals.
    assert [(e.minutes_before, e.side, e.won) for e in es] == [(1, "yes", False), (2, "yes", False), (3, "yes", False)]


def test_pnl_includes_fee():
    win = Entry("S", "T", None, 1, "yes", 99, True)
    loss = Entry("S", "T", None, 1, "yes", 99, False)
    assert win.pnl == round(101 * 0.01 - 0.07, 2)     # 101 contracts, 1¢ each, 7¢ fee
    assert loss.pnl == round(-101 * 0.99 - 0.07, 2)


def test_find_series():
    class K:
        def list_series(self):
            return {"series": [{"ticker": "KXBTC15M", "title": "Bitcoin price up or down in 15 minutes"},
                               {"ticker": "KXETH15M", "title": "ETH 15 min"},
                               {"ticker": "KXWTI", "title": "WTI oil price"},
                               {"ticker": "KXNHLGAME", "title": "NHL Game"}]}
    fifteen, others = find_series(K())
    assert fifteen == ["KXBTC15M", "KXETH15M"]
    assert others == ["KXWTI (WTI oil price)"]


def test_print_rules(capsys):
    from kalshibot.last_minute import print_rules

    class K:
        def _request(self, method, path):
            return {"series": {"title": "BTC 15 min", "settlement_sources": [{"name": "CF Benchmarks", "url": "https://cfb"}]}}

        def list_markets(self, **params):
            return {"markets": [{"ticker": "KXBTC15M-X", "rules_primary": "If the average of the BRTI over the 60 seconds..."}]
                    if params["status"] == "open" else []}
    print_rules(K(), ["KXBTC15M"])
    out = capsys.readouterr().out
    assert "CF Benchmarks" in out and "rules_primary: If the average" in out
