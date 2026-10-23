import dataclasses

from kalshibot.odds_providers.base import GameSnapshot
from kalshibot.paper import (EvRule, PaperBook, PaperTrader, RunlineRule, expected_return, load_paper_rules,
                             win_chance)

RULE = RunlineRule(id="mlb_underdog_runline", sport="mlb", summary="", dog_gain_points=15, max_inning=5,
                   kalshi_band=(10, 29), min_runs_over=1.5)
EVENT = "KXMLBGAME-26APR101910NYYBOS"


def game(pregame=-200, live=+110, inning=3):
    """Yankees @ Red Sox, Boston the pregame favorite; Yankees the underdog."""
    return GameSnapshot(game_id="g1", sport="mlb", home_team="Boston Red Sox", away_team="New York Yankees",
                        favorite_team="Boston Red Sox", pregame_favorite_odds=pregame, live_favorite_odds=live,
                        start_time_utc="2026-04-10T23:10:00Z", is_live=True, is_final=False, period=inning,
                        away_score=3, home_score=0, started=True)


def spread(team, n, ask, title=None):
    return {"ticker": f"KXMLBSPREAD-26APR101910NYYBOS-{team}{n}", "event_ticker": "KXMLBSPREAD-26APR101910NYYBOS",
            "yes_sub_title": title or f"{team} wins by over {n - 0.5} runs", "yes_ask_dollars": f"{ask / 100:.4f}"}


class FakeKalshi:
    def __init__(self, markets, results=None):
        self.markets = markets
        self.results = results or {}
        self.orders = 0

    def iter_markets(self, **params):
        return [m for m in self.markets if m["event_ticker"] == params.get("event_ticker")]

    def get_market(self, ticker):
        return {"market": {"ticker": ticker, "result": self.results.get(ticker, "")}}

    def create_order(self, *a, **k):   # must never be called
        self.orders += 1


class FakeMatcher:
    def resolve_event(self, snapshot):
        return {"home": {"ticker": f"{EVENT}-BOS", "event_ticker": EVENT},
                "away": {"ticker": f"{EVENT}-NYY", "event_ticker": EVENT}}


def trader(tmp_path, markets, results=None):
    kalshi = FakeKalshi(markets, results)
    return PaperTrader(kalshi, FakeMatcher(), [RULE], 25, PaperBook(tmp_path / "paper.json")), kalshi


def test_win_chance():
    assert round(win_chance(-200), 3) == 0.667
    assert round(win_chance(+150), 3) == 0.4


def test_bets_lowest_underdog_line_in_band_and_never_orders(tmp_path):
    t, kalshi = trader(tmp_path, [spread("NYY", 2, 35), spread("NYY", 3, 24), spread("NYY", 4, 15),
                                  spread("BOS", 3, 12)])
    t.run([game()])   # Boston -200 (67%) -> +110 (48%): Yankees up 19 points
    [bet] = t.book.all_bets()
    assert bet["ticker"].endswith("-NYY3") and bet["price_cents"] == 24
    assert bet["contracts"] == 104 and bet["cost"] == 24.96 and bet["status"] == "open"
    assert kalshi.orders == 0


def test_one_bet_per_game(tmp_path):
    t, _ = trader(tmp_path, [spread("NYY", 3, 24)])
    t.run([game()])
    t.run([game()])
    assert len(t.book.all_bets()) == 1


def test_needs_underdog_lead_early_and_price_in_band(tmp_path):
    t, _ = trader(tmp_path, [spread("NYY", 3, 24)])
    t.run([game(live=-160)])      # only up 6 points
    t.run([game(inning=6)])       # too late
    t.run([dataclasses.replace(game(), pregame_favorite_odds=None)])
    assert t.book.all_bets() == []
    t2, _ = trader(tmp_path / "b", [spread("NYY", 3, 31), spread("NYY", 4, 8)])
    t2.run([game()])              # neither line is 10-29c
    assert t2.book.all_bets() == []


def test_settles_from_market_result(tmp_path):
    t, kalshi = trader(tmp_path, [spread("NYY", 3, 24)])
    t.run([game()])
    kalshi.results["KXMLBSPREAD-26APR101910NYYBOS-NYY3"] = "yes"
    t.run([])
    [bet] = t.book.all_bets()
    assert bet["status"] == "won" and bet["pnl"] == round(104 - 24.96 - bet["fee"], 2)
    assert "1-0" in t.book.record_line()


def test_config_loads():
    rules, stake = load_paper_rules()
    # mlb_underdog_runline is paused (enabled: false) until MLB is back
    assert [r.id for r in rules] == ["ev_moneyline"] and stake == 25
    assert isinstance(rules[0], EvRule) and rules[0].min_ev == 0.03


EV = EvRule(id="ev_moneyline", sports=("mlb",), summary="", min_ev=0.03, kalshi_band=(10, 90), pregame_hours=3)


class PricedKalshi(FakeKalshi):
    def __init__(self, asks, results=None):
        super().__init__([], results)
        self.asks = asks

    def get_market(self, ticker):
        m = super().get_market(ticker)["market"]
        if ticker in self.asks:
            m["yes_ask_dollars"] = f"{self.asks[ticker] / 100:.4f}"
            m["yes_bid_dollars"] = f"{(self.asks[ticker] - 2) / 100:.4f}"
        return {"market": m}


def test_expected_return():
    assert round(expected_return(0.60, 50), 4) == round((0.60 - 0.50 - 0.07 * 0.25) / 0.50, 4)
    assert expected_return(0.50, 50) < 0          # fair price loses the fee


def test_ev_bet_when_kalshi_beats_the_sharp_price(tmp_path):
    kalshi = PricedKalshi({f"{EVENT}-BOS": 52, f"{EVENT}-NYY": 50})
    t = PaperTrader(kalshi, FakeMatcher(), [EV], 25, PaperBook(tmp_path / "paper.json"), tmp_path / "obs.jsonl")
    snap = dataclasses.replace(game(), fair_win_chance={"home": 0.60, "away": 0.40}, fair_source="pinnacle")
    t.run([snap])
    t.run([snap])                                 # one per team per game per phase
    [bet] = t.book.all_bets()
    assert bet["label"] == "Boston Red Sox wins (live)" and bet["price_cents"] == 52 and bet["source"] == "pinnacle"
    assert kalshi.orders == 0
    lines = (tmp_path / "obs.jsonl").read_text().splitlines()
    assert len(lines) == 4 and '"side": "away"' in lines[1]


def test_ev_needs_a_fair_price_and_a_big_enough_gap(tmp_path):
    kalshi = PricedKalshi({f"{EVENT}-BOS": 59, f"{EVENT}-NYY": 41})
    t = PaperTrader(kalshi, FakeMatcher(), [EV], 25, PaperBook(tmp_path / "paper.json"), tmp_path / "obs.jsonl")
    t.run([game()])                               # no sharp price -> nothing, not even an observation
    t.run([dataclasses.replace(game(), fair_win_chance={"home": 0.60, "away": 0.40})])   # 1c gap < fee + 3%
    assert t.book.all_bets() == []


def test_fair_fields_devig_pinnacle_and_fall_back_to_consensus():
    from datetime import datetime, timezone
    from kalshibot.odds_providers.sportsgameodds import _fair_fields
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    def side(sid, pin, fair, available=True):
        return {"statID": "points", "periodID": "game", "betTypeID": "ml", "sideID": sid, "fairOdds": fair,
                "fairOddsAvailable": True,
                "byBookmaker": {"pinnacle": {"odds": pin, "available": available, "lastUpdatedAt": now}}}
    odds = {"h": side("home", "-150", "-140"), "a": side("away", "+130", "+140")}
    f = _fair_fields("nba", odds, live=True)
    raw_h, raw_a = 150 / 250, 100 / 230
    assert f["fair_source"] == "pinnacle" and abs(f["fair_win_chance"]["home"] - raw_h / (raw_h + raw_a)) < 1e-9
    odds = {"h": side("home", "-150", "-140", available=False), "a": side("away", "+130", "+140")}
    f = _fair_fields("nba", odds, live=True)
    assert f["fair_source"] == "consensus" and abs(f["fair_win_chance"]["home"] - 140 / 240 / (140 / 240 + 100 / 240)) < 1e-9
    assert _fair_fields("nba", {"h": odds["h"]}, live=True) == {}
