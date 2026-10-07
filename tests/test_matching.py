import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from kalshibot.kalshi_client import KalshiClient
from kalshibot.matching import MarketMatcher, event_date, game_date
from kalshibot.odds_providers.base import GameSnapshot


class FakeKalshi:
    def __init__(self, markets):
        self.markets = markets

    def iter_markets(self, **params):
        series = params.get("series_ticker")
        return [m for m in self.markets if not series or m["event_ticker"].startswith(series + "-")]


def game_markets(event, *teams):
    """Kalshi's real layout: one "<Team> wins" market per team per event."""
    return [
        {"ticker": f"{event}-{abbr}", "event_ticker": event, "title": f"{name} wins",
         "yes_sub_title": name, "no_sub_title": name}
        for abbr, name in teams
    ]


def snap(home, away, favorite, start="2026-10-07T02:00:00Z", sport="nhl"):
    return GameSnapshot(
        game_id="g", sport=sport, home_team=home, away_team=away, favorite_team=favorite,
        pregame_favorite_odds=-770, live_favorite_odds=-400, start_time_utc=start,
        is_live=True, is_final=False,
    )


# Oct 6 (a 10pm ET start is 02:00 UTC on Oct 7) plus a later rematch and an
# unrelated Edmonton game.
MARKETS = (
    game_markets("KXNHLGAME-26OCT06VANEDM", ("VAN", "Vancouver"), ("EDM", "Edmonton"))
    + game_markets("KXNHLGAME-26NOV02VANEDM", ("VAN", "Vancouver"), ("EDM", "Edmonton"))
    + game_markets("KXNHLGAME-26OCT06CGYEDM", ("CGY", "Calgary"), ("EDM", "Edmonton"))
)


def resolve(markets, game):
    return MarketMatcher(FakeKalshi(markets)).resolve(game)


def test_buys_yes_on_favorite_in_the_event_on_the_games_date():
    r = resolve(MARKETS, snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers"))
    assert (r.ticker, r.side) == ("KXNHLGAME-26OCT06VANEDM-EDM", "yes")


def test_away_favorite():
    r = resolve(MARKETS, snap("Edmonton Oilers", "Vancouver Canucks", "Vancouver Canucks"))
    assert r.ticker == "KXNHLGAME-26OCT06VANEDM-VAN"


def test_later_rematch_is_not_bought_for_todays_game():
    game = snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers", start="2026-09-30T02:00:00Z")
    assert resolve(MARKETS, game) is None


def test_skips_when_only_one_teams_market_is_open():
    one = game_markets("KXNHLGAME-26OCT06VANEDM", ("EDM", "Edmonton"))
    assert resolve(one, snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers")) is None


def test_skips_two_events_on_the_same_date():
    doubled = MARKETS + game_markets("KXNHLGAME-26OCT06VANEDM2", ("VAN", "Vancouver"), ("EDM", "Edmonton"))
    assert resolve(doubled, snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers")) is None


@pytest.mark.parametrize(
    "label, team",
    [("Vegas", "Vegas Golden Knights"), ("St. Louis", "St. Louis Blues"),
     ("Minnesota", "Minnesota Wild"), ("NY Rangers", "New York Rangers"),
     ("Tampa Bay", "Tampa Bay Lightning"), ("Toronto", "Toronto Maple Leafs")],
)
def test_kalshi_short_names_match_full_names(label, team):
    markets = game_markets("KXNHLGAME-26OCT06XXXYYY", ("A", label), ("B", "Chicago"))
    r = resolve(markets, snap(team, "Chicago Blackhawks", team))
    assert r is not None and r.ticker == "KXNHLGAME-26OCT06XXXYYY-A"


def test_shared_city_label_is_ambiguous():
    markets = game_markets("KXNHLGAME-26OCT06NYRNYI", ("NY", "New York"), ("NYI", "NY Islanders"))
    assert resolve(markets, snap("New York Islanders", "New York Rangers", "New York Rangers")) is None


def test_soccer_tie_market_is_ignored():
    markets = game_markets("KXSOCCERGAME-26OCT06ARSCHE", ("ARS", "Arsenal"), ("TIE", "Tie"), ("CHE", "Chelsea"))
    game = snap("Arsenal", "Chelsea", "Chelsea", start="2026-10-06T14:00:00Z", sport="soccer")
    assert resolve(markets, game).ticker == "KXSOCCERGAME-26OCT06ARSCHE-CHE"


def test_mlb_layout_with_start_time_and_short_city_letter():
    markets = (
        game_markets("KXMLBGAME-26SEP292200CHCSD", ("SD", "San Diego"), ("CHC", "Chicago C"))
        + game_markets("KXMLBGAME-26SEP302200CHCSD", ("SD", "San Diego"), ("CHC", "Chicago C"))
    )
    game = snap("San Diego Padres", "Chicago Cubs", "Chicago Cubs", start="2026-09-30T02:00:00Z", sport="mlb")
    assert resolve(markets, game).ticker == "KXMLBGAME-26SEP292200CHCSD-CHC"
    white_sox = snap("San Diego Padres", "Chicago White Sox", "San Diego Padres", start="2026-09-30T02:00:00Z", sport="mlb")
    assert resolve(markets, white_sox) is None


def test_event_and_game_dates():
    assert event_date("KXNHLGAME-26OCT06MINBUF") == date(2026, 10, 6)
    assert event_date("no-date-here") is None
    # 7pm ET game on Oct 6 is 23:00 UTC; a 10:30pm ET game is 02:30 UTC Oct 7.
    assert game_date(snap("a", "b", "a", start="2026-10-06T23:00:00Z")) == date(2026, 10, 6)
    assert game_date(snap("a", "b", "a", start="2026-10-07T02:30:00.000Z")) == date(2026, 10, 6)
    # An EPL 7:30am ET kickoff is 11:30 UTC.
    assert game_date(snap("a", "b", "a", start="2026-10-06T11:30:00Z")) == date(2026, 10, 6)


def test_iter_markets_follows_cursor(monkeypatch):
    client = KalshiClient(None, base_url="https://example.com/trade-api/v2")
    pages = iter([{"markets": [1, 2], "cursor": "c1"}, {"markets": [3], "cursor": ""}])
    seen_cursors = []

    def fake_list(**params):
        seen_cursors.append(params.get("cursor"))
        return next(pages)

    monkeypatch.setattr(client, "list_markets", fake_list)
    assert client.iter_markets(series_ticker="S") == [1, 2, 3]
    assert seen_cursors == [None, "c1"]


def test_tennis_searches_both_tours_with_a_day_of_date_slack():
    # A Shanghai match at 03:00 UTC Oct 8 is Oct 7 by the US-date rule, but
    # Kalshi may date it by the local day.
    markets = (
        game_markets("KXATPMATCH-26OCT08SINALC", ("SIN", "Sinner"), ("ALC", "Alcaraz"))
        + game_markets("KXWTAMATCH-26OCT08SABGAU", ("SAB", "Sabalenka"), ("GAU", "Gauff"))
    )
    men = snap("Jannik Sinner", "Carlos Alcaraz", "Jannik Sinner", start="2026-10-08T03:00:00Z", sport="tennis")
    women = snap("Aryna Sabalenka", "Coco Gauff", "Coco Gauff", start="2026-10-08T03:00:00Z", sport="tennis")
    assert resolve(markets, men).ticker == "KXATPMATCH-26OCT08SINALC-SIN"
    assert resolve(markets, women).ticker == "KXWTAMATCH-26OCT08SABGAU-GAU"


def test_no_date_slack_outside_tennis():
    markets = game_markets("KXNHLGAME-26OCT08VANEDM", ("VAN", "Vancouver"), ("EDM", "Edmonton"))
    game = snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers", start="2026-10-08T02:00:00Z")
    assert resolve(markets, game) is None   # game date Oct 7, event Oct 8


def test_event_with_several_markets_naming_one_team_is_skipped():
    # e.g. score markets "Sinner 2-0", "Sinner 2-1" -- not a plain winner market.
    markets = game_markets(
        "KXATPMATCH-26OCT07SINALC", ("SIN20", "Sinner 2-0"), ("SIN21", "Sinner 2-1"), ("ALC", "Alcaraz"))
    game = snap("Jannik Sinner", "Carlos Alcaraz", "Jannik Sinner", start="2026-10-07T12:00:00Z", sport="tennis")
    assert resolve(markets, game) is None
