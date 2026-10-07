import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot import notify
from kalshibot.executor import Executor, order_cost
from kalshibot.ledger import BetLedger, record_line
from kalshibot.risk import RiskConfig, RiskManager
from kalshibot.rules_engine import BetDecision
from kalshibot.state import BetStateStore


def bet(ledger, order_id="o1", contracts=37, cost=14.80, fee=0.62):
    return ledger.record(order_id=order_id, ticker="KXNFLGAME-26OCT04DETCAR-DET", team="Detroit Lions",
                         sport="nfl", rule="r", contracts=contracts, cost=cost, fee=fee)


def test_win_loss_and_record(tmp_path):
    ledger = BetLedger(tmp_path / "ledger.json")
    bet(ledger, "o1")
    bet(ledger, "o2")
    bet(ledger, "o3")
    assert ledger.settle("o1", "yes")["pnl"] == 21.58     # 37 - 14.80 - 0.62
    assert ledger.settle("o2", "no")["pnl"] == -15.42
    assert ledger.settle("o2", "yes") is None              # already settled
    assert ledger.summary() == {"wins": 1, "losses": 1, "net": 6.16, "open": 1}
    assert record_line(ledger.summary()) == "Record 1-1 · Net +6.16 USD · 1 open"
    # survives a restart
    assert BetLedger(tmp_path / "ledger.json").summary()["wins"] == 1


def test_duplicate_order_is_recorded_once(tmp_path):
    ledger = BetLedger(tmp_path / "ledger.json")
    bet(ledger, "o1")
    bet(ledger, "o1")
    assert len(ledger.all_bets()) == 1


def test_order_cost_reads_kalshi_order_fields():
    order = {"order": {"taker_fill_cost_dollars": "14.800000", "maker_fill_cost_dollars": "0.000000",
                       "taker_fees_dollars": "0.621600", "maker_fees_dollars": "0.000000"}}
    assert order_cost(order) == (14.8, 0.6216)
    assert order_cost({}) == (None, None)


def test_notify_is_off_without_a_topic(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    assert notify.send("t", "m") is False


def test_notify_sends_json_and_never_raises(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "kalshibot-abc")
    sent = {}

    class Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: sent.update(url=url, **json) or Resp())
    assert notify.send("Bet placed: Marin Čilić", "37 at 40¢", tags=["moneybag"]) is True
    assert sent["topic"] == "kalshibot-abc" and sent["title"] == "Bet placed: Marin Čilić"

    def boom(*a, **k):
        raise ConnectionError("offline")

    monkeypatch.setattr(notify.requests, "post", boom)
    assert notify.send("t", "m") is False


class FakeKalshi:
    def create_order(self, **order):
        return {"order_id": "o9", "fill_count": "21.00"}

    def get_order(self, order_id):
        return {"order": {"taker_fill_cost_dollars": "14.700000", "taker_fees_dollars": "0.300000"}}


def test_filled_live_bet_goes_in_the_ledger_and_to_the_phone(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_ENABLED", "true")
    pushed = []
    monkeypatch.setattr(notify, "send", lambda title, message, **k: pushed.append(title) or True)
    ledger = BetLedger(tmp_path / "ledger.json")
    executor = Executor(FakeKalshi(), RiskManager(RiskConfig(15.0, None, None, "TRADING_ENABLED")),
                        BetStateStore(tmp_path / "bets.json"), dry_run=False, ledger=ledger)
    decision = BetDecision(rule_id="r", game_id="g", sport="nhl", team="Edmonton Oilers", live_odds=-120, stake_usd=15)
    executor.execute(decision, ticker="T", side="yes", yes_price_cents=70,
                     open_position_count=0, realized_pnl_today_usd=0.0)
    [entry] = ledger.all_bets()
    assert (entry["order_id"], entry["contracts"], entry["cost"], entry["fee"]) == ("o9", 21.0, 14.7, 0.3)
    assert pushed == ["Bet placed: Edmonton Oilers"]


def test_settlement_pass_marks_bets_and_notifies(tmp_path, monkeypatch):
    from kalshibot import main as bot
    pushed = []
    monkeypatch.setattr(notify, "send", lambda title, message, **k: pushed.append((title, message)) or True)
    ledger = BetLedger(tmp_path / "ledger.json")
    bet(ledger, "o1")

    class K:
        def get_portfolio(self, kind, **params):
            return {"settlements": [{"ticker": "KXNFLGAME-26OCT04DETCAR-DET", "market_result": "no"}]}

    bot.settle_finished_bets(K(), ledger)
    assert ledger.all_bets()[0]["status"] == "lost"
    assert pushed == [("LOST: Detroit Lions -$15.42", "Record 0-1 · Net -15.42 USD · 0 open")]
    bot.settle_finished_bets(K(), ledger)   # nothing open: no second notification
    assert len(pushed) == 1
