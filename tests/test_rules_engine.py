import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.odds_providers.base import GameSnapshot
from kalshibot.rules_engine import RulesEngine, load_rules
from kalshibot.state import BetStateStore

RULES_PATH = Path(__file__).resolve().parents[1] / "config" / "rules.yaml"


def make_snapshot(**overrides):
    base = dict(
        game_id="g1",
        sport="soccer",
        home_team="Home FC",
        away_team="Away FC",
        favorite_team="Home FC",
        pregame_favorite_odds=-250,
        live_favorite_odds=150,
        start_time_utc="2026-01-01T00:00:00Z",
        is_live=True,
        is_final=False,
        period=None,
        home_score=None,
        away_score=None,
        past_halftime=None,
    )
    base.update(overrides)
    return GameSnapshot(**base)


@pytest.fixture
def unpaused_engine(tmp_path):
    """The rule set as it was before the 2026-10-12 backtest changes (every
    rule on, no fade caps) -- keeps the stacking / prior-rule mechanics
    tested while those rules are paused in the live config."""
    rules, stake = load_rules(RULES_PATH)
    rules = [dataclasses.replace(r, enabled=True,
                                 trigger_max=None if r.id in ("nfl_favorite_fade", "cfb_favorite_fade") else r.trigger_max)
             for r in rules]
    store = BetStateStore(tmp_path / "bets.json")
    return RulesEngine(rules, stake, store), store


@pytest.fixture
def engine(tmp_path):
    rules, stake = load_rules(RULES_PATH)
    store = BetStateStore(tmp_path / "bets.json")
    return RulesEngine(rules, stake, store), store


def test_soccer_heavy_favorite_triggers_in_band(engine):
    eng, _ = engine
    snap = make_snapshot(pregame_favorite_odds=-250, live_favorite_odds=150)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["soccer_heavy_favorite"]


def test_soccer_heavy_favorite_skips_past_ceiling(engine):
    eng, _ = engine
    snap = make_snapshot(pregame_favorite_odds=-250, live_favorite_odds=201)
    assert eng.evaluate(snap) == []


def test_soccer_heavy_favorite_skips_below_band(engine):
    eng, _ = engine
    snap = make_snapshot(pregame_favorite_odds=-250, live_favorite_odds=99)
    assert eng.evaluate(snap) == []


def test_soccer_moderate_favorite_requires_strictly_past_115(engine):
    eng, _ = engine
    snap_at_115 = make_snapshot(pregame_favorite_odds=-170, live_favorite_odds=115)
    assert eng.evaluate(snap_at_115) == []

    snap_past_115 = make_snapshot(pregame_favorite_odds=-170, live_favorite_odds=116)
    decisions = eng.evaluate(snap_past_115)
    assert [d.rule_id for d in decisions] == ["soccer_moderate_favorite"]


def test_soccer_moderate_favorite_skips_if_trailing_after_half(engine):
    eng, _ = engine
    snap = make_snapshot(
        pregame_favorite_odds=-170,
        live_favorite_odds=150,
        past_halftime=True,
        home_score=0,
        away_score=1,   # favorite (Home FC) trailing by 1 after halftime
    )
    assert eng.evaluate(snap) == []


@pytest.mark.parametrize("live, fires", [
    (-300, False), (-200, False), (-111, False),   # still clearly favored
    (-110, True), (-105, True), (100, True),       # faded to near even
    (101, False), (150, False),                    # became a real underdog
])
def test_tennis_heavy_favorite_bets_near_even(engine, live, fires):
    eng, _ = engine
    snap = make_snapshot(sport="tennis", pregame_favorite_odds=-300, live_favorite_odds=live)
    assert [d.rule_id for d in eng.evaluate(snap)] == (["tennis_heavy_favorite"] if fires else [])


def test_tennis_needs_a_minus_200_favorite(engine):
    eng, _ = engine
    assert eng.evaluate(make_snapshot(sport="tennis", pregame_favorite_odds=-199, live_favorite_odds=-105)) == []
    assert [d.rule_id for d in eng.evaluate(
        make_snapshot(sport="tennis", pregame_favorite_odds=-200, live_favorite_odds=-105))] == ["tennis_heavy_favorite"]


def test_nfl_favorite_fade_triggers(engine):
    eng, _ = engine
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=-115)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["nfl_favorite_fade"]


def test_nfl_moderate_favorite_triggers_for_lighter_pregame_favorite(unpaused_engine):
    eng, _ = unpaused_engine
    # This is the exact scenario that surfaced the gap: a -220 pregame
    # favorite doesn't meet nfl_favorite_fade's -250-or-steeper bar.
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-220, live_favorite_odds=125)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["nfl_moderate_favorite"]


def test_nfl_moderate_favorite_skips_out_of_band(engine):
    eng, _ = engine
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-220, live_favorite_odds=151)
    assert eng.evaluate(snap) == []


def test_nfl_moderate_favorite_deliberately_overlaps_steep_favorite(unpaused_engine):
    eng, store = unpaused_engine
    # Per an explicit user call: a steep (-250+) favorite that drifts into
    # the +100-150 band fires BOTH NFL rules for the same game -- two
    # separate wagers, on purpose. Confirm both fire in the same cycle...
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=125)
    decisions = eng.evaluate(snap)
    assert {d.rule_id for d in decisions} == {"nfl_favorite_fade", "nfl_moderate_favorite"}

    # ...and confirm neither blocks the other across cycles either (each
    # rule's own max_bets_per_game=3 accounts for the other two NFL rules).
    store.record("g1", "nfl_favorite_fade")
    decisions_next_cycle = eng.evaluate(snap)
    assert [d.rule_id for d in decisions_next_cycle] == ["nfl_moderate_favorite"]


def test_nfl_all_three_rules_can_fire_on_one_steep_favorite_game(unpaused_engine):
    eng, store = unpaused_engine
    # Confirmed intentional: a steep favorite can rack up all three NFL
    # wagers across a game -- fade + moderate early, reentry in the 4th.
    store.record("g1", "nfl_favorite_fade")
    store.record("g1", "nfl_moderate_favorite")
    snap_q4 = make_snapshot(
        sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=130, period=4
    )
    decisions = eng.evaluate(snap_q4)
    assert [d.rule_id for d in decisions] == ["nfl_fourth_quarter_reentry"]


def test_nfl_fourth_quarter_reentry_requires_prior_fire(unpaused_engine):
    eng, store = unpaused_engine
    # live=160 is deliberately outside nfl_moderate_favorite's 100-150 band,
    # so this isolates the reentry-requires-prior-fire behavior without
    # incidentally also triggering the (separately tested) NFL rule overlap.
    snap_q4 = make_snapshot(
        sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=160, period=4
    )
    # The reentry band (110-165) sits inside nfl_favorite_fade's own trigger
    # (odds >= -115), so on a game where nfl_favorite_fade hasn't fired yet,
    # ONLY nfl_favorite_fade should fire -- reentry needs it recorded first.
    first_pass = eng.evaluate(snap_q4)
    assert [d.rule_id for d in first_pass] == ["nfl_favorite_fade"]

    store.record("g1", "nfl_favorite_fade")
    decisions = eng.evaluate(snap_q4)
    assert [d.rule_id for d in decisions] == ["nfl_fourth_quarter_reentry"]


def test_no_double_fire_of_same_rule(engine):
    eng, store = engine
    snap = make_snapshot(pregame_favorite_odds=-250, live_favorite_odds=150)
    first = eng.evaluate(snap)
    assert len(first) == 1
    store.record(snap.game_id, first[0].rule_id)

    second = eng.evaluate(snap)
    assert second == []


def test_mlb_favorite_fade_triggers_past_6th_inning(engine):
    eng, _ = engine
    snap = make_snapshot(
        sport="mlb", pregame_favorite_odds=-220, live_favorite_odds=150, period=7
    )
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["mlb_favorite_fade"]


def test_mlb_favorite_fade_skips_before_7th_inning(engine):
    eng, _ = engine
    snap = make_snapshot(
        sport="mlb", pregame_favorite_odds=-220, live_favorite_odds=150, period=6
    )
    assert eng.evaluate(snap) == []


def test_mlb_favorite_fade_skips_unknown_inning(engine):
    eng, _ = engine
    snap = make_snapshot(
        sport="mlb", pregame_favorite_odds=-220, live_favorite_odds=150, period=None
    )
    assert eng.evaluate(snap) == []


def test_mlb_favorite_fade_skips_out_of_band(engine):
    eng, _ = engine
    snap = make_snapshot(
        sport="mlb", pregame_favorite_odds=-220, live_favorite_odds=250, period=8
    )
    assert eng.evaluate(snap) == []


def test_cfb_favorite_fade_triggers(engine):
    eng, _ = engine
    snap = make_snapshot(sport="cfb", pregame_favorite_odds=-260, live_favorite_odds=-115)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["cfb_favorite_fade"]


def test_cfb_moderate_favorite_deliberately_overlaps_steep_favorite(unpaused_engine):
    eng, store = unpaused_engine
    # Mirrors the NFL overlap behavior: a steep CFB favorite can fire both
    # cfb_favorite_fade and cfb_moderate_favorite for the same game.
    snap = make_snapshot(sport="cfb", pregame_favorite_odds=-260, live_favorite_odds=125)
    decisions = eng.evaluate(snap)
    assert {d.rule_id for d in decisions} == {"cfb_favorite_fade", "cfb_moderate_favorite"}


def test_cfb_fourth_quarter_reentry_requires_prior_fire(unpaused_engine):
    eng, store = unpaused_engine
    snap_q4 = make_snapshot(
        sport="cfb", pregame_favorite_odds=-260, live_favorite_odds=160, period=4
    )
    first_pass = eng.evaluate(snap_q4)
    assert [d.rule_id for d in first_pass] == ["cfb_favorite_fade"]

    store.record("g1", "cfb_favorite_fade")
    decisions = eng.evaluate(snap_q4)
    assert [d.rule_id for d in decisions] == ["cfb_fourth_quarter_reentry"]


def test_no_other_rule_fires_after_one_has_for_same_game(engine):
    eng, store = engine
    # soccer_heavy_favorite fires and is recorded.
    store.record("g1", "soccer_heavy_favorite")
    # Even a snapshot that would otherwise satisfy soccer_moderate_favorite's
    # pregame band shouldn't matter here since it's a different game state,
    # but on the SAME game_id no second soccer rule should be allowed to fire.
    snap = make_snapshot(pregame_favorite_odds=-170, live_favorite_odds=150)
    assert eng.evaluate(snap) == []


def nhl(**overrides):
    base = dict(
        sport="nhl", pregame_favorite_odds=-180, live_favorite_odds=110,
        favorite_team="Home FC", home_score=0, away_score=1,
        period=2, seconds_remaining=600,
    )
    base.update(overrides)
    return make_snapshot(**base)


@pytest.mark.parametrize("fav, dog", [(0, 1), (1, 2)])
def test_nhl_fires_when_favorite_trails_one_nil_or_two_one(engine, fav, dog):
    eng, _ = engine
    decisions = eng.evaluate(nhl(home_score=fav, away_score=dog))
    assert [(d.rule_id, d.team) for d in decisions] == [("nhl_favorite_trails_by_one", "Home FC")]


@pytest.mark.parametrize("fav, dog", [(1, 1), (0, 2), (2, 3), (1, 0), (0, 0)])
def test_nhl_skips_other_scores(engine, fav, dog):
    eng, _ = engine
    assert eng.evaluate(nhl(home_score=fav, away_score=dog)) == []


def test_nhl_uses_favorite_side_when_favorite_is_away(engine):
    eng, _ = engine
    snap = nhl(favorite_team="Away FC", home_score=1, away_score=0)
    assert [d.team for d in eng.evaluate(snap)] == ["Away FC"]


def test_nhl_needs_minus_170_favorite(engine):
    eng, _ = engine
    assert eng.evaluate(nhl(pregame_favorite_odds=-165)) == []
    assert eng.evaluate(nhl(pregame_favorite_odds=None)) == []


@pytest.mark.parametrize(
    "period, seconds, fires",
    [
        (3, 361, True),     # 6:01 left in the 3rd
        (3, 360, False),    # 6:00 left -- last 6 minutes
        (3, 90, False),
        (3, None, False),   # clock unreadable in the 3rd -- skip
        (None, None, False),  # overtime ("ot" has no period number)
        (1, None, True),    # early periods don't need the clock
    ],
)
def test_nhl_last_six_minutes_blocked(engine, period, seconds, fires):
    eng, _ = engine
    decisions = eng.evaluate(nhl(period=period, seconds_remaining=seconds))
    assert bool(decisions) is fires


@pytest.mark.parametrize(
    "rule_id, ask, ok",
    [
        ("mlb_favorite_fade", 41, True),        # +144, inside +100..+200
        ("mlb_favorite_fade", 26, False),       # +285, past +200 (the real Dodgers case)
        ("mlb_favorite_fade", 33, False),       # +203, just past +200
        ("mlb_favorite_fade", 34, True),        # +194
        ("mlb_favorite_fade", 52, True),        # -108, within the 2c spread allowance of +100
        ("mlb_favorite_fade", 55, False),       # -122, clearly shorter than +100
        ("nfl_moderate_favorite", 40, True),    # +150, the real Detroit fill
        ("nfl_favorite_fade", 55, True),        # -122 vs -115 entry (53.5c + 2c)
        ("nfl_favorite_fade", 60, False),       # -150
        ("nfl_favorite_fade", 10, False),       # capped at -100 since the backtest
        ("nfl_favorite_fade", 52, True),        # -108, inside -115..-100
        ("cfb_favorite_fade", 41, True),        # +144, inside -115..+150
        ("cfb_favorite_fade", 30, False),       # +233, past the +150 cap
        ("nhl_favorite_trails_by_one", 90, True),  # score-based rule, no odds band
        ("soccer_first_half_favorite", 55, True),   # -122, inside -130..+120
        ("soccer_first_half_favorite", 60, False),  # -150, not drifted enough
        ("soccer_first_half_favorite", 44, False),  # +127, past +120
        ("tennis_heavy_favorite", 52, True),    # -108, near even
        ("tennis_heavy_favorite", 45, False),   # +122, past +100
        ("tennis_heavy_favorite", 60, False),   # -150, still clearly favored
    ],
)
def test_kalshi_price_must_respect_the_rule_band(tmp_path, rule_id, ask, ok):
    rules, stake = load_rules(RULES_PATH)
    engine = RulesEngine(rules, stake, BetStateStore(tmp_path / "b.json"))
    assert (engine.kalshi_price_problem(rule_id, ask) is None) == ok


def test_watched_rules_lists_rules_whose_pregame_condition_is_met(engine):
    eng, _ = engine
    assert eng.watched_rules(make_snapshot(sport="tennis", pregame_favorite_odds=-400, live_favorite_odds=-250)) \
        == ["tennis_heavy_favorite"]
    assert eng.watched_rules(make_snapshot(sport="tennis", pregame_favorite_odds=-150)) == []
    assert eng.watched_rules(make_snapshot(sport="tennis", pregame_favorite_odds=None)) == []



def first_half(**kw):
    base = dict(sport="soccer", pregame_favorite_odds=-320, live_favorite_odds=-180,
                first_half_favorite_odds=-120, period=1, home_score=0, away_score=0, past_halftime=False)
    base.update(kw)
    return make_snapshot(**base)


@pytest.mark.parametrize("kw, fires", [
    ({}, True),                                          # 0-0, 1H, -120 first-half odds
    ({"first_half_favorite_odds": -130}, True),          # edge of the band
    ({"first_half_favorite_odds": 120}, True),           # other edge
    ({"first_half_favorite_odds": -140}, False),         # not drifted far enough
    ({"first_half_favorite_odds": 125}, False),          # past +120
    ({"first_half_favorite_odds": None}, False),         # no first-half line quoted
    ({"home_score": 1}, False),                          # no longer 0-0
    ({"period": 2, "past_halftime": True}, False),       # second half
    ({"period": None}, False),                           # halftime break / unknown
    ({"pregame_favorite_odds": -290}, False),            # favorite not -300 or steeper
])
def test_soccer_first_half_rule(engine, kw, fires):
    eng, _ = engine
    decisions = [d for d in eng.evaluate(first_half(**kw)) if d.rule_id == "soccer_first_half_favorite"]
    assert bool(decisions) == fires
    if fires:
        assert decisions[0].market == "first_half"
        assert decisions[0].live_odds == first_half(**kw).first_half_favorite_odds


def test_first_half_bet_does_not_block_the_full_match_rule(engine):
    eng, store = engine
    store.record("g1", "soccer_first_half_favorite")
    # later the full-match line drifts to +150 -> soccer_heavy_favorite still bets
    snap = first_half(period=2, past_halftime=True, home_score=0, away_score=1, live_favorite_odds=150)
    assert [d.rule_id for d in eng.evaluate(snap)] == ["soccer_heavy_favorite"]



def test_backtest_changes_in_the_live_rules(engine):
    eng, store = engine
    nfl = dict(sport="nfl", pregame_favorite_odds=-260)
    # paused: NFL moderate favorite and 4th-quarter re-bet, CFB moderate favorite
    assert eng.evaluate(make_snapshot(**nfl, live_favorite_odds=120)) == []
    store.record("g1", "nfl_favorite_fade")
    assert eng.evaluate(make_snapshot(**nfl, live_favorite_odds=130, period=4)) == []
    assert [d.rule_id for d in eng.evaluate(make_snapshot(sport="cfb", pregame_favorite_odds=-160,
                                                          live_favorite_odds=120))] == []


def test_nfl_fade_only_between_minus_115_and_minus_100(engine):
    eng, _ = engine
    fires = lambda live: [d.rule_id for d in eng.evaluate(
        make_snapshot(game_id=str(live), sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=live))]
    assert fires(-108) == ["nfl_favorite_fade"]
    assert fires(-100) == ["nfl_favorite_fade"]
    assert fires(105) == [] and fires(-130) == []


def test_cfb_fade_capped_at_plus_150(engine):
    eng, _ = engine
    fires = lambda live: [d.rule_id for d in eng.evaluate(
        make_snapshot(game_id=str(live), sport="cfb", pregame_favorite_odds=-260, live_favorite_odds=live))]
    assert fires(140) == ["cfb_favorite_fade"]
    assert fires(160) == []


def test_paused_rules_are_not_watched(engine):
    eng, _ = engine
    assert "nfl_moderate_favorite" not in eng.watched_rules(make_snapshot(sport="nfl", pregame_favorite_odds=-160))
