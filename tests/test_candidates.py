import json
from datetime import datetime, timezone

from kalshibot.candidates import (Candidate, Game, Moment, clock_minutes, collect_plays, elapsed_minutes,
                                  game_states, read_sheet, replay, summarize, verdict)

START = datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)
T0 = START.timestamp()


def test_clock_and_elapsed():
    assert round(clock_minutes("5:30"), 2) == 5.5
    assert round(clock_minutes("30.0"), 2) == 0.5
    assert elapsed_minutes("nba", 2, 6.0) == 18          # Q2 with 6:00 left
    assert elapsed_minutes("cbb", 2, 10.0) == 30         # 2nd half, 10:00 left
    assert elapsed_minutes("nfl", 4, 15.0) == 45
    assert elapsed_minutes("nba", 5, 5.0) == 48          # start of OT


def test_plays_found_in_basketball_and_football_layouts():
    play = {"period": {"number": 1}, "clock": {"displayValue": "11:00"}, "homeScore": 2, "awayScore": 0,
            "wallclock": "2026-01-10T00:01:00Z"}
    basketball = {"plays": [play]}
    football = {"drives": {"previous": [{"plays": [play]}]}}
    assert collect_plays(basketball) == [play] and collect_plays(football) == [play]
    (m,) = game_states("nba", basketball)
    assert (m.minute, m.home, m.away) == (1.0, 2, 0)


def test_read_sheet(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text('Title,,\n,,\nAdd to bot,Rule,Rule settings\n'
                 'TRUE,NBA rule,"{""leagues"": [""NBA""], ""pregame_min"": null, ""pregame_max"": -300, '
                 '""live_min"": 251, ""live_max"": 400, ""min_score_diff"": -12, ""max_score_diff"": -9, '
                 '""game_minutes"": [12, 24]}"\n'
                 'FALSE,Spread,"{""type"": ""line"", ""leagues"": [""NFL""], ""market"": ""spread""}"\n')
    cs = read_sheet(p)
    assert [(c.sport, c.in_game) for c in cs] == [("nba", True), ("nfl", False)]


RULE = Candidate({"Bets": "276"}, {"leagues": ["NBA"], "pregame_min": None, "pregame_max": -300, "live_min": 251,
                                   "live_max": 400, "min_score_diff": -12, "max_score_diff": -9,
                                   "game_minutes": [12, 24]})


def game(home_price_mid_game, home_won=True):
    # Home is an 80c (-400) favorite; by minute 18 it trails by 10.
    states = [Moment(T0 + 60 * i, i * 0.5, 0, 0) for i in range(0, 30)]           # minutes 0-14.5, tied
    states += [Moment(T0 + 60 * i, i * 0.5, 30, 40) for i in range(30, 60)]       # minutes 15-29.5, down 10
    g = Game("nba", "A @ H", START, states)
    g.won = {"home": home_won, "away": not home_won}
    g.kickoff = {"home": 80, "away": 21}
    g.asks = {"home": [(int(T0 + 60 * i), 80 if i < 30 else home_price_mid_game) for i in range(60)],
              "away": [(int(T0 + 60 * i), 21 if i < 30 else 100 - home_price_mid_game) for i in range(60)]}
    return g


def test_replay_bets_the_favorite_when_kalshi_price_is_in_range():
    bet = replay(RULE, game(25))          # 25c = +300, inside +251..+400
    assert bet and bet.side == "home" and bet.price == 25 and bet.won and 15 <= bet.minute <= 24
    assert bet.pnl > 0


def test_replay_skips_when_kalshi_price_is_higher_than_the_sheet_assumed():
    # ESPN might call it +300, but Kalshi has the favorite at 35c (+186): no bet.
    assert replay(RULE, game(35)) is None


def test_summary_and_verdict():
    s = summarize([])
    assert verdict(100, s) == "never fired at real Kalshi prices"
    bets = [replay(RULE, game(25, home_won=i % 3 == 0)) for i in range(30)]
    s = summarize(bets)
    assert s["bets"] == 30 and round(s["win"], 2) == 0.33
    assert verdict(276, s) in ("holds up at real prices", "positive but could be luck")
