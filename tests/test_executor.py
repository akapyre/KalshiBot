import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.executor import Executor, american_odds, yes_ask_cents
from kalshibot.risk import RiskConfig, RiskManager
from kalshibot.rules_engine import BetDecision
from kalshibot.state import BetStateStore


class FakeKalshi:
    def __init__(self, response):
        self.response = response
        self.orders = []

    def create_order(self, **order):
        self.orders.append(order)
        return self.response


def make(tmp_path, monkeypatch, response=None, dry_run=False):
    monkeypatch.setenv("TRADING_ENABLED", "true")
    kalshi = FakeKalshi(response or {"order": {"fill_count": 21}})
    state = BetStateStore(tmp_path / "bets.json")
    risk = RiskManager(RiskConfig(15.0, None, None, "TRADING_ENABLED"))
    return Executor(kalshi, risk, state, dry_run=dry_run), kalshi, state


def decision():
    return BetDecision(rule_id="r", game_id="g", sport="nhl", team="Edmonton Oilers", live_odds=-770, stake_usd=15.0)


def run(executor, price=70):
    executor.execute(decision(), ticker="T", side="yes", yes_price_cents=price,
                     open_position_count=0, realized_pnl_today_usd=0.0)


@pytest.mark.parametrize(
    "market, expected",
    [({"yes_ask_dollars": "0.5600"}, 56), ({"yes_ask": 41}, 41), ({"yes_ask_dollars": "0.0000"}, None),
     ({"yes_ask_dollars": "1.0000"}, None), ({"yes_bid": 50}, None), ({}, None)],
)
def test_yes_ask_cents(market, expected):
    assert yes_ask_cents(market) == expected


def test_american_odds():
    assert american_odds(50) == -100
    assert american_odds(80) == -400
    assert american_odds(40) == 150


def test_live_order_is_ioc_limit_at_ask_within_stake(tmp_path, monkeypatch):
    executor, kalshi, state = make(tmp_path, monkeypatch)
    run(executor, price=70)
    [order] = kalshi.orders
    assert order["side"] == "bid" and order["time_in_force"] == "immediate_or_cancel"
    assert order["price_dollars"] == "0.7000" and order["count"] == 21   # 21 x 70¢ = $14.70 <= $15
    assert state.has_fired("g", "r")


@pytest.mark.parametrize("response", [{"order": {"fill_count": 0}}, {"order_id": "o", "fill_count_fp": "0.00"}])
def test_unfilled_order_is_not_recorded(tmp_path, monkeypatch, response):
    executor, kalshi, state = make(tmp_path, monkeypatch, response=response)
    run(executor)
    assert kalshi.orders and not state.has_fired("g", "r")


def test_dry_run_records_without_ordering(tmp_path, monkeypatch):
    executor, kalshi, state = make(tmp_path, monkeypatch, dry_run=True)
    run(executor)
    assert not kalshi.orders and state.has_fired("g", "r")


def test_v2_order_request_body(monkeypatch):
    from kalshibot.kalshi_client import KalshiClient
    client = KalshiClient(None, base_url="https://example.com/trade-api/v2")
    sent = {}
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: sent.update(method=method, path=path, **kw) or {})
    client.create_order(ticker="T", count=34, price_dollars="0.4400")
    assert (sent["method"], sent["path"]) == ("POST", "/portfolio/events/orders")
    body = sent["json_body"]
    assert {k: body[k] for k in ("ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type")} == {
        "ticker": "T", "side": "bid", "count": "34.00", "price": "0.4400", "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross"}
