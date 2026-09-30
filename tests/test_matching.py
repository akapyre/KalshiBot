import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.kalshi_client import KalshiClient
from kalshibot.matching import MarketMatcher
from kalshibot.odds_providers.base import GameSnapshot


class FakeKalshi:
    def __init__(self, markets):
        self.markets = markets

    def iter_markets(self, **params):
        return self.markets


def market(ticker, event, title, yes):
    return {"ticker": ticker, "event_ticker": event, "title": title, "yes_sub_title": yes}


def snap(home, away, favorite, sport="nhl"):
    return GameSnapshot(
        game_id="g", sport=sport, home_team=home, away_team=away, favorite_team=favorite,
        pregame_favorite_odds=-770, live_favorite_odds=-400, start_time_utc="",
        is_live=True, is_final=False,
    )


VAN_EDM = [
    market("NHL-VANEDM-VAN", "NHL-VANEDM", "Vancouver vs Edmonton Winner?", "Vancouver"),
    market("NHL-VANEDM-EDM", "NHL-VANEDM", "Vancouver vs Edmonton Winner?", "Edmonton"),
    market("NHL-CGYEDM-EDM", "NHL-CGYEDM", "Calgary vs Edmonton Winner?", "Edmonton"),
]


def resolve(markets, game):
    return MarketMatcher(FakeKalshi(markets)).resolve(game)


def test_buys_yes_on_the_favorites_own_market():
    r = resolve(VAN_EDM, snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers"))
    assert (r.ticker, r.side) == ("NHL-VANEDM-EDM", "yes")


def test_away_favorite():
    r = resolve(VAN_EDM, snap("Edmonton Oilers", "Vancouver Canucks", "Vancouver Canucks"))
    assert (r.ticker, r.side) == ("NHL-VANEDM-VAN", "yes")


def test_skips_when_only_the_underdogs_market_exists():
    only_van = [VAN_EDM[0]]
    assert resolve(only_van, snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers")) is None


def test_skips_when_yes_side_is_unnamed():
    unnamed = [market("X", "E", "Vancouver vs Edmonton Winner?", "")]
    assert resolve(unnamed, snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers")) is None


def test_skips_when_two_different_events_match():
    two = VAN_EDM[:2] + [market("NHL-VANEDM2-EDM", "NHL-VANEDM2", "Vancouver vs Edmonton Winner?", "Edmonton")]
    assert resolve(two, snap("Edmonton Oilers", "Vancouver Canucks", "Edmonton Oilers")) is None


def test_soccer_tie_market_is_ignored():
    markets = [
        market("S-ARS", "S-1", "Arsenal vs Chelsea", "Arsenal"),
        market("S-TIE", "S-1", "Arsenal vs Chelsea", "Tie"),
        market("S-CHE", "S-1", "Arsenal vs Chelsea", "Chelsea"),
    ]
    r = resolve(markets, snap("Arsenal", "Chelsea", "Chelsea", sport="soccer"))
    assert r.ticker == "S-CHE"


def test_same_city_teams_use_nickname():
    markets = [
        market("R", "NY", "New York R vs New York I", "Rangers"),
        market("I", "NY", "New York R vs New York I", "Islanders"),
    ]
    r = resolve(markets, snap("New York Islanders", "New York Rangers", "New York Rangers"))
    assert r.ticker == "R"


def test_same_city_ambiguous_yes_is_skipped():
    markets = [market("X", "NY", "Rangers vs Islanders", "New York")]
    assert resolve(markets, snap("New York Islanders", "New York Rangers", "New York Rangers")) is None


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
