from kalshibot.cbb_dip import CbbDipWatcher, load_cbb_dip_rules, tip_off
from kalshibot.paper import PaperBook

OCC = "2026-11-13T03:00:00Z"   # tip-off = 00:00Z


class FakeKalshi:
    def __init__(self):
        self.asks = {"FAV": "0.9300", "DOG": "0.0900"}

    def iter_markets(self, **params):
        assert params == {"series_ticker": "KXNCAAMBGAME", "status": "open"}
        return [dict(ticker=t, event_ticker="KXNCAAMBGAME-26NOV12DOGFAV", yes_sub_title=t, occurrence_datetime=OCC,
                     yes_ask_dollars=a, yes_bid_dollars="0.01") for t, a in self.asks.items()]


def test_rules_load():
    assert [(r.id, r.dip_cents) for r in load_cbb_dip_rules()] == [
        ("cbb_fav_dip_85", 85), ("cbb_fav_dip_75", 75), ("cbb_fav_dip_60", 60)]


def test_paper_bets_once_per_rule_after_dip(tmp_path):
    k = FakeKalshi()
    book = PaperBook(tmp_path / "bets.json")
    w = CbbDipWatcher(k, load_cbb_dip_rules(), 25, book, tmp_path / "tips.json")
    tip = tip_off(dict(occurrence_datetime=OCC))
    w.run(now=tip - 300)                      # pregame: saves 93c as the tip-off price
    k.asks["FAV"] = "0.8000"
    w.run(now=tip + 600)                      # live dip to 80c -> only the 85c rule
    k.asks["FAV"] = "0.7400"
    w.run(now=tip + 900)                      # 74c -> 75c rule; 85c rule already bet
    w.run(now=tip + 1000)
    bets = book.all_bets()
    assert [(b["rule"], b["price_cents"], b["ticker"]) for b in bets] == [
        ("cbb_fav_dip_85", 80, "FAV"), ("cbb_fav_dip_75", 74, "FAV")]
    assert bets[0]["contracts"] == 31 and bets[0]["tip_cents"] == 93


def test_no_bet_without_tip_price_or_weak_favorite(tmp_path):
    k = FakeKalshi()
    book = PaperBook(tmp_path / "bets.json")
    w = CbbDipWatcher(k, load_cbb_dip_rules(), 25, book, tmp_path / "tips.json")
    tip = tip_off(dict(occurrence_datetime=OCC))
    k.asks["FAV"] = "0.7000"
    w.run(now=tip + 600)                      # bot started mid-game: no tip-off price, no bet
    k.asks["FAV"] = "0.8800"
    w2 = CbbDipWatcher(k, load_cbb_dip_rules(), 25, PaperBook(tmp_path / "b2.json"), tmp_path / "t2.json")
    w2.run(now=tip - 60)                      # 88c at tip: not a 90c+ favorite
    k.asks["FAV"] = "0.7000"
    w2.run(now=tip + 600)
    assert book.all_bets() == [] and w2.book.all_bets() == []
