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


def test_tennis_heavy_favorite_triggers_below_100(engine):
    eng, _ = engine
    snap = make_snapshot(sport="tennis", pregame_favorite_odds=-300, live_favorite_odds=50)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["tennis_heavy_favorite"]


def test_tennis_heavy_favorite_skips_at_100(engine):
    eng, _ = engine
    snap = make_snapshot(sport="tennis", pregame_favorite_odds=-300, live_favorite_odds=100)
    assert eng.evaluate(snap) == []


def test_nfl_favorite_fade_triggers(engine):
    eng, _ = engine
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=-115)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["nfl_favorite_fade"]


def test_nfl_moderate_favorite_triggers_for_lighter_pregame_favorite(engine):
    eng, _ = engine
    # This is the exact scenario that surfaced the gap: a -220 pregame
    # favorite doesn't meet nfl_favorite_fade's -250-or-steeper bar.
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-220, live_favorite_odds=125)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["nfl_moderate_favorite"]


def test_nfl_moderate_favorite_skips_out_of_band(engine):
    eng, _ = engine
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-220, live_favorite_odds=151)
    assert eng.evaluate(snap) == []


def test_nfl_moderate_favorite_does_not_apply_to_steep_favorite(engine):
    eng, _ = engine
    # A -260 pregame favorite belongs to nfl_favorite_fade's band, not this
    # one -- confirms the two NFL rules don't overlap on the same game.
    snap = make_snapshot(sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=125)
    decisions = eng.evaluate(snap)
    assert [d.rule_id for d in decisions] == ["nfl_favorite_fade"]


def test_nfl_fourth_quarter_reentry_requires_prior_fire(engine):
    eng, store = engine
    snap_q4 = make_snapshot(
        sport="nfl", pregame_favorite_odds=-260, live_favorite_odds=130, period=4
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


def test_no_other_rule_fires_after_one_has_for_same_game(engine):
    eng, store = engine
    # soccer_heavy_favorite fires and is recorded.
    store.record("g1", "soccer_heavy_favorite")
    # Even a snapshot that would otherwise satisfy soccer_moderate_favorite's
    # pregame band shouldn't matter here since it's a different game state,
    # but on the SAME game_id no second soccer rule should be allowed to fire.
    snap = make_snapshot(pregame_favorite_odds=-170, live_favorite_odds=150)
    assert eng.evaluate(snap) == []
