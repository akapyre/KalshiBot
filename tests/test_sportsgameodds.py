import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json

import pytest

from kalshibot.odds_providers.sportsgameodds import (
    SportsGameOddsProvider,
    _parse_clock_seconds,
    _parse_period_number,
)
from kalshibot.state import PregameOddsStore


def event(home_ml, away_ml, started, live, home_score=0, away_score=0, close=None, extra_odds=None):
    odds = {
        "h": {"statID": "points", "periodID": "game", "betTypeID": "ml", "sideID": "home", "bookOdds": str(home_ml)},
        "a": {"statID": "points", "periodID": "game", "betTypeID": "ml", "sideID": "away", "bookOdds": str(away_ml)},
    }
    if close:
        odds["h"]["closeBookOdds"], odds["a"]["closeBookOdds"] = str(close[0]), str(close[1])
    odds.update(extra_odds or {})
    return {
        "eventID": "g1",
        "type": "match",
        "teams": {
            "home": {"names": {"long": "Chiefs"}, "score": home_score},
            "away": {"names": {"long": "Dolphins"}, "score": away_score},
        },
        "status": {"started": started, "live": live, "currentPeriodID": "2q" if live else ""},
        "odds": odds,
    }


@pytest.fixture
def provider(tmp_path):
    return SportsGameOddsProvider(api_key="x", pregame_store=PregameOddsStore(tmp_path / "p.json"))


def test_favorite_stays_locked_after_the_line_flips(provider):
    # Pregame: Chiefs are the -330 favorite.
    provider.fetch_events = lambda sport: [event(-330, 260, started=False, live=False)]
    provider.list_live_games("nfl")

    # Live: Chiefs fall behind and the line flips -- Dolphins now favored.
    provider.fetch_events = lambda sport: [event(+150, -180, started=True, live=True, home_score=0, away_score=10)]
    [snap] = provider.list_live_games("nfl")

    # Must still track the Chiefs at their own (now positive) price.
    assert snap.favorite_team == "Chiefs"
    assert snap.pregame_favorite_odds == -330
    assert snap.live_favorite_odds == 150


def test_game_first_seen_live_has_no_baseline(provider):
    provider.fetch_events = lambda sport: [event(-200, 170, started=True, live=True)]
    [snap] = provider.list_live_games("nfl")
    assert snap.pregame_favorite_odds is None


SHOTS_ON_GOAL = {
    "sh": {"statID": "shots_onGoal", "periodID": "game", "betTypeID": "ml", "sideID": "home", "bookOdds": "-770"},
    "sa": {"statID": "shots_onGoal", "periodID": "game", "betTypeID": "ml", "sideID": "away", "bookOdds": "+470"},
}


def test_other_stat_moneylines_are_ignored(provider):
    # Real Oilers-Canucks data: the shots-on-goal "moneyline" is -770 while
    # the actual game line is +100 with Edmonton trailing.
    provider.fetch_events = lambda sport: [
        event(+100, -128, started=True, live=True, close=(-307, +250), extra_odds=SHOTS_ON_GOAL)
    ]
    [snap] = provider.list_live_games("nhl")
    assert snap.live_favorite_odds == 100
    assert snap.pregame_favorite_odds == -307


def test_closing_line_beats_a_stale_stored_pregame(provider):
    provider._pregame_store.set("g1", "home", -770)
    provider.fetch_events = lambda sport: [event(+100, -128, started=True, live=True, close=(-307, +250))]
    [snap] = provider.list_live_games("nhl")
    assert (snap.favorite_team, snap.pregame_favorite_odds) == ("Chiefs", -307)


def test_closing_line_gives_a_baseline_when_started_mid_game(provider):
    provider.fetch_events = lambda sport: [event(+150, -180, started=True, live=True, close=(+120, -140))]
    [snap] = provider.list_live_games("nfl")
    assert (snap.favorite_team, snap.pregame_favorite_odds, snap.live_favorite_odds) == ("Dolphins", -140, -180)


def test_started_but_not_live_does_not_overwrite_pregame(provider):
    provider.fetch_events = lambda sport: [event(-330, 260, started=False, live=False)]
    provider.list_live_games("nfl")
    # Intermission/delay: started but momentarily not live, line has moved.
    provider.fetch_events = lambda sport: [event(+150, -180, started=True, live=False)]
    provider.list_live_games("nfl")
    assert provider._pregame_store.get("g1") == ("home", -330)


def test_legacy_pregame_entry_without_side_is_ignored(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"g1": -220}))
    assert PregameOddsStore(path).get("g1") is None


@pytest.mark.parametrize(
    "period_id, expected",
    [("1q", 1), ("4q", 4), ("2h", 2), ("3p", 3), ("7t", 7), ("10t", 10), ("ot", None), ("", None)],
)
def test_parse_period_number(period_id, expected):
    assert _parse_period_number(period_id) == expected


@pytest.mark.parametrize(
    "status, expected",
    [
        ({"clock": 345}, 345),
        ({"clock": "5:45"}, 345),
        ({"displayShort": "3P 12:04"}, 724),
        ({"displayShort": "Final"}, None),
        ({}, None),
    ],
)
def test_parse_clock_seconds(status, expected):
    assert _parse_clock_seconds(status) == expected


def test_soccer_uses_the_3way_regulation_line(provider):
    ev = event(-999, -999, started=True, live=True)   # 2-way placeholders, should be ignored
    ev["odds"].update({
        "h3": {"statID": "points", "periodID": "reg", "betTypeID": "ml3way", "sideID": "home",
               "bookOdds": "+140", "closeBookOdds": "-210"},
        "d3": {"statID": "points", "periodID": "reg", "betTypeID": "ml3way", "sideID": "draw", "bookOdds": "+230"},
        "a3": {"statID": "points", "periodID": "reg", "betTypeID": "ml3way", "sideID": "away",
               "bookOdds": "+190", "closeBookOdds": "+550"},
    })
    provider.fetch_events = lambda sport: [ev]
    [snap] = provider.list_live_games("soccer")
    assert (snap.favorite_team, snap.pregame_favorite_odds, snap.live_favorite_odds) == ("Chiefs", -210, 140)


def test_soccer_falls_back_to_2way_when_no_3way(provider):
    provider.fetch_events = lambda sport: [event(+120, -150, started=True, live=True, close=(-220, +180))]
    [snap] = provider.list_live_games("soccer")
    assert (snap.pregame_favorite_odds, snap.live_favorite_odds) == (-220, 120)


def test_fetch_follows_next_cursor(monkeypatch):
    from kalshibot.odds_providers import sportsgameodds as sgo
    pages = iter([{"data": [1, 2], "nextCursor": "c"}, {"data": [3]}])
    seen = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    monkeypatch.setattr(sgo.requests, "get", lambda url, headers, params, timeout: seen.append(params.get("cursor")) or Resp(next(pages)))
    p = sgo.SportsGameOddsProvider(api_key="x", pregame_store=None)
    assert p.fetch_events("soccer") == [1, 2, 3]
    assert seen == [None, "c"]
