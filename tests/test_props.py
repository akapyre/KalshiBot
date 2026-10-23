import dataclasses
from kalshibot.main import run_props
from kalshibot.odds_providers.base import GameSnapshot
from kalshibot.odds_providers.sportsgameodds import _player_names
from kalshibot.props import (PropPregameStore, PropRule, PropWatcher, load_prop_rules, mid_cents, parse_line,
                             parse_market, parse_player)
from kalshibot.rules_engine import load_rules
from kalshibot.state import BetStateStore

# bet_line "lower" (the rule as first asked for) -- most tests below;
# SAME is how it runs on Kalshi.
RULE = PropRule(id="nhl_prop_lower_line", sport="nhl", stats=["points", "shots"], pregame_max_odds=-200,
                band=(30, 58), max_bets_per_game=2, bet_line="lower")
SAME = PropRule(id="nhl_prop_favorite_drift", sport="nhl", stats=["points", "assists", "goals"],
                pregame_max_odds=-200, band=(30, 58), max_bets_per_game=2, bet_line="same")


def game(game_id="g1", live=True, players=("Connor McDavid", "Leon Draisaitl", "Zach Hyman")):
    return GameSnapshot(game_id=game_id, sport="nhl", home_team="Vancouver Canucks", away_team="Edmonton Oilers",
                        favorite_team="Edmonton Oilers", pregame_favorite_odds=-150, live_favorite_odds=-150,
                        start_time_utc="2026-10-16T02:00:00Z", is_live=live, is_final=False,
                        player_names=players, started=live)


def mkt(player, line, ask=None, bid=None, event="KXNHLSOG-26OCT15EDMVAN", stat="shots"):
    m = {"ticker": f"{event}-{player.split()[-1].upper()}{line}", "event_ticker": event,
         "yes_sub_title": f"{player}: {line}+", "exchange_index": 0}
    if ask is not None:
        m["yes_ask_dollars"] = f"{ask / 100:.4f}"
    if bid is not None:
        m["yes_bid_dollars"] = f"{bid / 100:.4f}"
    return parse_market(stat, m)


def watcher(tmp_path, pregame: dict[str, int]):
    store = PropPregameStore(tmp_path / "p.json")
    store.set_many(pregame)
    return PropWatcher(kalshi=None, rules=[RULE], store=store)


def test_parse_line_and_player():
    assert parse_line("Connor McDavid: 2+") == 2
    assert parse_line("3 or more shots") == 3
    assert parse_line("Over 1.5 shots on goal") == 2
    assert parse_line("Connor McDavid") is None
    assert parse_player("Connor McDavid: 2+") == "Connor McDavid"
    assert parse_player("J.T. Miller 3+ shots") == "J.T. Miller"
    assert parse_player("Edmonton: 30+") is None   # one word -- a team, not a player


def test_minus_200_is_67_cents():
    assert RULE.pregame_min_cents == 67


def test_mid_price():
    assert mid_cents({"yes_bid_dollars": "0.66", "yes_ask_dollars": "0.70"}) == 68
    assert mid_cents({"last_price_dollars": "0.55"}) == 55
    assert mid_cents({}) is None


def test_bets_line_below_the_highest_minus_200_line(tmp_path):
    ms = [mkt("Connor McDavid", 2, ask=57), mkt("Connor McDavid", 3, ask=30), mkt("Connor McDavid", 4, ask=10)]
    # Puck drop: 2+ at 90¢, 3+ at 70¢ (-233), 4+ at 45¢. The -200 line is 3+, so bet 2+.
    w = watcher(tmp_path, {ms[0].market["ticker"]: 90, ms[1].market["ticker"]: 70, ms[2].market["ticker"]: 45})
    bets = w.candidates(ms, game())
    assert [(b.player, b.main_line, b.bet.line, b.ask) for b in bets] == [("Connor McDavid", 3, 2, 57)]
    assert bets[0].label == "Connor McDavid 2+ shots on goal"


def test_no_bet_until_the_lower_line_is_minus_140_or_longer(tmp_path):
    ms = [mkt("Connor McDavid", 2, ask=60), mkt("Connor McDavid", 3, ask=35)]
    w = watcher(tmp_path, {ms[0].market["ticker"]: 90, ms[1].market["ticker"]: 70})
    assert w.candidates(ms, game()) == []


def test_floor_skips_a_collapsed_price(tmp_path):
    ms = [mkt("Connor McDavid", 2, ask=12), mkt("Connor McDavid", 3, ask=3)]
    w = watcher(tmp_path, {ms[0].market["ticker"]: 90, ms[1].market["ticker"]: 70})
    assert w.candidates(ms, game()) == []


def test_lowest_line_has_nothing_below_it(tmp_path):
    ms = [mkt("Connor McDavid", 1, ask=50, stat="points"), mkt("Connor McDavid", 2, ask=20, stat="points")]
    w = watcher(tmp_path, {ms[0].market["ticker"]: 75, ms[1].market["ticker"]: 40})
    assert w.candidates(ms, game()) == []


def test_player_not_minus_200_anywhere_is_ignored(tmp_path):
    ms = [mkt("Zach Hyman", 1, ask=50), mkt("Zach Hyman", 2, ask=20)]
    w = watcher(tmp_path, {ms[0].market["ticker"]: 64, ms[1].market["ticker"]: 35})
    assert w.candidates(ms, game()) == []


def test_no_puck_drop_prices_means_no_bet(tmp_path):
    ms = [mkt("Connor McDavid", 2, ask=55), mkt("Connor McDavid", 3, ask=30)]
    assert watcher(tmp_path, {}).candidates(ms, game()) == []


def test_only_roster_players_and_the_games_date(tmp_path):
    ms = [mkt("Connor McDavid", 2, ask=55), mkt("Connor McDavid", 3, ask=30),
          mkt("Sidney Crosby", 2, ask=55), mkt("Sidney Crosby", 3, ask=30),
          mkt("Leon Draisaitl", 2, ask=55, event="KXNHLSOG-26OCT17EDMCGY"),
          mkt("Leon Draisaitl", 3, ask=30, event="KXNHLSOG-26OCT17EDMCGY")]
    w = watcher(tmp_path, {m.market["ticker"]: 90 if m.line == 2 else 70 for m in ms})
    assert {b.player for b in w.candidates(ms, game())} == {"Connor McDavid"}


def test_pregame_prices_saved_only_for_upcoming_games(tmp_path):
    ms = [mkt("Connor McDavid", 2, ask=92, bid=88)]
    w = watcher(tmp_path, {})
    w.record_pregame(ms, [game(live=False)])
    assert PropPregameStore(tmp_path / "p.json").get(ms[0].market["ticker"]) == 90


class FakeProvider:
    def list_upcoming_games(self, sport):
        return []


class FakeExecutor:
    def __init__(self, store):
        self.store, self.orders = store, []

    def execute(self, decision, **kw):
        self.orders.append((decision.team, kw["ticker"], kw["yes_price_cents"]))
        self.store.record(decision.game_id, decision.rule_id)


class FakeKalshi:
    def get_positions(self):
        return {}


def test_run_props_bets_once_per_player_and_caps_per_game(tmp_path):
    names = ("Connor McDavid", "Leon Draisaitl", "Zach Hyman")
    ms = [m for n in names for m in (mkt(n, 2, ask=55), mkt(n, 3, ask=30))]
    w = watcher(tmp_path, {m.market["ticker"]: 90 if m.line == 2 else 70 for m in ms})
    w.fetch = lambda: ms
    store = BetStateStore(tmp_path / "bets.json")
    ex = FakeExecutor(store)
    for _ in range(3):   # three cycles
        run_props(w, FakeProvider(), [game()], store, ex, FakeKalshi(), 25.0)
    assert len(ex.orders) == 2   # max 2 per game, no repeats
    assert all(o[0].endswith("2+ shots on goal") and o[2] == 55 for o in ex.orders)


def test_prop_rule_is_loaded_apart_from_game_rules():
    assert [(r.id, r.bet_line, r.stats) for r in load_prop_rules()] == [
        ("nhl_prop_favorite_drift", "same", ["points", "assists", "goals"]),
        ("nba_prop_heavy_favorite_even", "same", ["points", "rebounds", "assists", "threes"]),
        ("nba_prop_favorite_drift", "same", ["points", "rebounds", "assists", "threes"])]
    assert load_prop_rules()[0].band == (39, 58)   # no hockey bet past +160 (2026-10-22)
    heavy, drift = load_prop_rules()[1:]
    assert (heavy.pregame_min_cents, heavy.band, heavy.stake_usd, heavy.max_period) == (75, (46, 50), 50, 2)
    assert (drift.stake_usd, drift.max_period) == (None, 2)
    assert "nhl_prop_favorite_drift" not in [r.id for r in load_rules()[0]]


def test_series_discovery_picks_player_point_and_shot_series(tmp_path):
    class Kalshi:
        def list_series(self, **_):
            return {"series": [
                {"ticker": "KXNHLGAME", "title": "NHL Game"},
                {"ticker": "KXNHLPTS", "title": "NHL Player Points"},
                {"ticker": "KXNHLSOG", "title": "NHL Shots on Goal"},
                {"ticker": "KXNHLTEAMSOG", "title": "NHL Team Shots on Goal"},
                {"ticker": "KXNHLTOTAL", "title": "NHL Total Goals"},
                {"ticker": "KXNBAPTS", "title": "NBA Points"},
            ]}
    w = PropWatcher(Kalshi(), [RULE], PropPregameStore(tmp_path / "p.json"))
    assert w.series() == [("points", "KXNHLPTS"), ("shots", "KXNHLSOG")]


def test_sportsgameodds_roster_names():
    event = {"players": {"A": {"name": "Connor McDavid"}, "B": {"firstName": "Leon", "lastName": "Draisaitl"}}}
    assert _player_names(event) == ("Connor McDavid", "Leon Draisaitl")


def test_season_long_series_and_wide_spreads_are_ignored(tmp_path):
    class Kalshi:
        def list_series(self, **_):
            return {"series": [{"ticker": "KXNHLPTS", "title": "Pro Hockey Points"},
                               {"ticker": "KXNHLSEASONPPTS", "title": "Season Player Points"}]}
    w = PropWatcher(Kalshi(), [RULE], PropPregameStore(tmp_path / "p.json"))
    assert w.series() == [("points", "KXNHLPTS")]
    assert mid_cents({"yes_bid_dollars": "0.39", "yes_ask_dollars": "0.70"}) == 39   # wide: the bid


def test_same_line_rule_bets_the_minus_200_line_itself(tmp_path):
    ms = [mkt("Connor McDavid", 1, ask=55, stat="points"), mkt("Connor McDavid", 2, ask=20, stat="points")]
    store = PropPregameStore(tmp_path / "p.json")
    store.set_many({ms[0].market["ticker"]: 67, ms[1].market["ticker"]: 32})
    w = PropWatcher(kalshi=None, rules=[SAME], store=store)
    bets = w.candidates(ms, game())
    assert [(b.label, b.ask, b.main_pregame_cents) for b in bets] == [("Connor McDavid 1+ points", 55, 67)]


def test_same_line_waits_for_minus_140(tmp_path):
    ms = [mkt("Connor McDavid", 1, ask=62, stat="points")]
    store = PropPregameStore(tmp_path / "p.json")
    store.set_many({ms[0].market["ticker"]: 70})
    assert PropWatcher(kalshi=None, rules=[SAME], store=store).candidates(ms, game()) == []


def test_real_kalshi_series_list(tmp_path):
    names = {"KXNHLSEASONGOALS": "Player to score X Goals in Season", "KXNHLPTS": "Pro Hockey Points",
             "KXNHLF10G": "NHL Goal in First 10 Minutes", "KXNHLSAVES": "Pro Hockey Saves",
             "KXNHLFIRSTGOAL": "NHL First Goal", "KXNHLAST": "Pro Hockey Assists", "KXNHLEAST": "Eastern Conference Championship",
             "KXNHLPSEASONGOALS": "Player Goals Scored in Season", "KXNHLSEASONPPTS": "Season Player Points",
             "KXNHLANYGOAL": "Pro Hockey Goalscorer", "KXNHLTOTAL": "NHL Total Goals", "KXNHLGAME": "NHL Game",
             "KXNHLSAVE": "Goaltender Saves", "KXNHLGOAL": "NHL Goalscorer", "KXNHLSEASONPTS": "NHL Season Point Totals",
             "KXNHLTEAMTOTAL": "NHL Team Goals Scored During Game", "KXNHLSERIESTOTALGOALS": "NHL Series Total Goals",
             "KXNHLPLAYOFFGOALS": "NHL Playoffs Goal Leader (by Round)", "KXNHL1PBTTS": "NHL Both Teams to Score in 1st Period"}

    class Kalshi:
        def list_series(self, **_):
            return {"series": [{"ticker": t, "title": n} for t, n in names.items()]}
    w = PropWatcher(Kalshi(), [SAME], PropPregameStore(tmp_path / "p.json"))
    assert w.series() == [("points", "KXNHLPTS"), ("assists", "KXNHLAST"),
                          ("goals", "KXNHLANYGOAL"), ("goals", "KXNHLGOAL")]


def test_real_kalshi_points_market_and_strike_fallback():
    m = {"ticker": "KXNHLPTS-26OCT14DALCOL-COLCMAKAR8-2", "event_ticker": "KXNHLPTS-26OCT14DALCOL",
         "title": "Cale Makar: 2+ points", "yes_sub_title": "Cale Makar: 2+", "floor_strike": 1.5, "strike_type": "greater"}
    pm = parse_market("points", m)
    assert (pm.player, pm.line) == ("Cale Makar", 2)
    goal = {"event_ticker": "KXNHLANYGOAL-26OCT14DALCOL", "yes_sub_title": "Cale Makar", "title": "Will Cale Makar score?",
            "floor_strike": 0.5, "strike_type": "greater"}
    pm = parse_market("goals", goal)
    assert (pm.player, pm.line) == ("Cale Makar", 1)


def test_two_goal_series_for_one_player_use_one(tmp_path):
    a = mkt("Connor McDavid", 1, ask=50, stat="goals", event="KXNHLGOAL-26OCT15EDMVAN")
    b = mkt("Connor McDavid", 1, ask=50, stat="goals", event="KXNHLANYGOAL-26OCT15EDMVAN")
    b2 = mkt("Connor McDavid", 2, ask=10, stat="goals", event="KXNHLANYGOAL-26OCT15EDMVAN")
    ladders = PropWatcher.for_game([a, b, b2], game())
    assert {line: pm.market["event_ticker"] for line, pm in ladders[("goals", "Connor McDavid")].items()} == {
        1: "KXNHLANYGOAL-26OCT15EDMVAN", 2: "KXNHLANYGOAL-26OCT15EDMVAN"}


def test_nba_prop_rule_uses_the_named_nba_series_and_bets_plus_money(tmp_path):
    rule = next(r for r in load_prop_rules() if r.id == "nba_prop_favorite_drift")
    assert rule.sport == "nba" and rule.pregame_min_cents == 67 and rule.band == (36, 50)

    w = PropWatcher(kalshi=None, rules=[rule], store=PropPregameStore(tmp_path / "p.json"))
    assert sorted(t for _, t in w.series()) == ["KXNBA3PT", "KXNBAAST", "KXNBAPTS", "KXNBAREB"]

    snap = dataclasses.replace(game(players=("Jayson Tatum", "Jaylen Brown")), sport="nba",
                               home_team="Detroit Pistons", away_team="Boston Celtics", period=2)
    ev = "KXNBAPTS-26OCT15BOSDET"
    w._store.set_many({f"{ev}-TATUM20": 70, f"{ev}-TATUM25": 52})
    for ask, bets in ((55, False), (50, True), (36, True), (35, False)):   # -122 / +100 / +178 / +186
        markets = [mkt("Jayson Tatum", 20, ask=ask, event=ev, stat="points"),
                   mkt("Jayson Tatum", 25, ask=20, event=ev, stat="points")]
        found = w.candidates(markets, snap)
        assert bool(found) == bets, ask
        if bets:
            assert found[0].label == "Jayson Tatum 20+ points"


HEAVY = PropRule(id="nba_prop_heavy_favorite_even", sport="nba", stats=["points"], pregame_max_odds=-300,
                 band=(46, 50), max_bets_per_game=2, max_period=2, stake_usd=50)
DRIFT = PropRule(id="nba_prop_favorite_drift", sport="nba", stats=["points"], pregame_max_odds=-200,
                 band=(36, 50), max_bets_per_game=2, max_period=2)


def nba_game(period=2):
    return dataclasses.replace(game(), sport="nba", home_team="Boston Celtics", away_team="Detroit Pistons",
                               player_names=("Jayson Tatum",), period=period)


class StakeExecutor(FakeExecutor):
    def execute(self, decision, **kw):
        self.orders.append((decision.rule_id, decision.stake_usd, kw["yes_price_cents"]))
        self.store.record(decision.game_id, decision.rule_id)


def nba_setup(tmp_path, ask, pregame=80):
    ms = [mkt("Jayson Tatum", 25, ask=ask, event="KXNBAPTS-26OCT15DETBOS", stat="points")]
    store = PropPregameStore(tmp_path / "p.json")
    store.set_many({ms[0].market["ticker"]: pregame})
    w = PropWatcher(kalshi=None, rules=[HEAVY, DRIFT], store=store)
    w.fetch = lambda: ms
    return w, ms


def test_minus_300_line_at_even_money_bets_double_once(tmp_path):
    w, ms = nba_setup(tmp_path, ask=48)
    store = BetStateStore(tmp_path / "bets.json")
    ex = StakeExecutor(store)
    run_props(w, FakeProvider(), [nba_game()], store, ex, FakeKalshi(), 25.0)
    ms[0].market["yes_ask_dollars"] = "0.4000"    # later drifts into the -200 rule's range
    run_props(w, FakeProvider(), [nba_game()], store, ex, FakeKalshi(), 25.0)
    assert ex.orders == [("nba_prop_heavy_favorite_even", 50, 48)]   # one bet per player and stat


def test_minus_300_rule_band_is_plus_100_to_plus_120(tmp_path):
    for ask, rule in ((50, "nba_prop_heavy_favorite_even"), (46, "nba_prop_heavy_favorite_even"),
                      (45, "nba_prop_favorite_drift"), (51, None)):
        w, ms = nba_setup(tmp_path / str(ask), ask=ask)
        bets = w.candidates(ms, nba_game())
        assert [b.rule.id for b in bets] == ([rule] if rule else [])


def test_minus_200_only_line_bets_the_normal_stake(tmp_path):
    w, ms = nba_setup(tmp_path, ask=48, pregame=70)     # -233 at tip: not -300
    store = BetStateStore(tmp_path / "bets.json")
    ex = StakeExecutor(store)
    run_props(w, FakeProvider(), [nba_game()], store, ex, FakeKalshi(), 25.0)
    assert ex.orders == [("nba_prop_favorite_drift", 25.0, 48)]


def test_nba_props_stop_after_the_first_half(tmp_path):
    w, ms = nba_setup(tmp_path, ask=48)
    assert [b.rule.id for b in w.candidates(ms, nba_game(period=1))] == ["nba_prop_heavy_favorite_even"]
    assert w.candidates(ms, nba_game(period=3)) == []
    assert w.candidates(ms, nba_game(period=None)) == []   # halftime / unknown: no bet
