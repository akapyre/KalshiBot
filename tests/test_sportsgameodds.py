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


def event(home_ml, away_ml, started, live, home_score=0, away_score=0):
    return {
        "eventID": "g1",
        "type": "match",
        "teams": {
            "home": {"names": {"long": "Chiefs"}, "score": home_score},
            "away": {"names": {"long": "Dolphins"}, "score": away_score},
        },
        "status": {"started": started, "live": live, "currentPeriodID": "2q" if live else ""},
        "odds": {
            "h": {"periodID": "game", "betTypeID": "ml", "sideID": "home", "bookOdds": str(home_ml)},
            "a": {"periodID": "game", "betTypeID": "ml", "sideID": "away", "bookOdds": str(away_ml)},
        },
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
