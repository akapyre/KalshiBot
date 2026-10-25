from kalshibot.streaks import break_even, cashout_verdict, fair_probability, finish_value


def test_vig_removed_probability():
    assert round(fair_probability(-150, 120), 3) == 0.569
    assert round(fair_probability(-110, -110), 3) == 0.5


def test_break_even_for_1000x_over_11_picks():
    assert round(break_even(20000, 20), 3) == 0.534


def test_finish_value_and_cashout():
    assert round(finish_value(20000, 3, 0.58)) == 3902
    assert cashout_verdict(20000, 3, 4500, 0.58).startswith("TAKE")
    assert cashout_verdict(20000, 3, 3000, 0.58).startswith("KEEP")


from datetime import datetime, timezone

from kalshibot.streaks import adjust_to_line, book_chances, find_picks, first_two, log_summary

NOW = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
FRESH = "2026-10-24T19:55:00.000Z"


def _q(odds, line, available=True, at=FRESH):
    return {"odds": odds, "overUnder": str(line), "available": available, "lastUpdatedAt": at}


def _event(event_id, over_books, under_books, ud_line="24.5", started=False, stat="points"):
    over = {"oddID": "o", "opposingOddID": "u", "betTypeID": "ou", "sideID": "over", "periodID": "game",
            "statID": stat, "playerID": "P1", "byBookmaker": dict(over_books, underdog=_q("+100", ud_line))}
    under = {"oddID": "u", "opposingOddID": "o", "betTypeID": "ou", "sideID": "under", "periodID": "game",
             "statID": stat, "playerID": "P1", "byBookmaker": dict(under_books, underdog=_q("+100", ud_line))}
    return {"eventID": event_id, "status": {"started": started, "startsAt": "2026-10-24T23:30:00.000Z"},
            "teams": {"home": {"names": {"short": "PHI"}}, "away": {"names": {"short": "MIL"}}},
            "players": {"P1": {"name": "Joe Player"}}, "odds": {"o": over, "u": under}}


def test_adjust_to_line_same_line_and_direction():
    assert adjust_to_line("points", 24.5, 0.6, 24.5) == 0.6
    assert adjust_to_line("points", 24.5, 0.5, 22.5) > 0.6     # lower line, easier over
    assert adjust_to_line("assists", 4.5, 0.5, 5.5) < 0.4       # Poisson count stat
    assert round(adjust_to_line("assists", 4.5, 0.5, 4.5), 3) == 0.5


def test_book_chances_skips_stale_mismatched_and_dfs():
    over = {"byBookmaker": {"fanduel": _q("-150", 24.5), "novig": _q("-163", 24.5),
                            "draftkings": _q("-120", 25.5), "prizepicks": _q("+100", 24.5),
                            "betmgm": _q("-110", 24.5, available=False)}}
    under = {"byBookmaker": {"fanduel": _q("+120", 24.5), "novig": _q("-2400", 24.5, at="2026-10-24T19:00:00Z"),
                             "draftkings": _q("-110", 24.5), "prizepicks": _q("+100", 24.5),
                             "betmgm": _q("-110", 24.5)}}
    got = book_chances(over, under, NOW)
    assert [b for b, _, _ in got] == ["fanduel"]
    assert round(got[0][2], 3) == 0.569


def test_find_picks_ranks_and_picks_side():
    books_o = {"fanduel": _q("+150", 24.5), "draftkings": _q("+140", 24.5)}
    books_u = {"fanduel": _q("-190", 24.5), "draftkings": _q("-175", 24.5)}
    picks, unpriced = find_picks([_event("g1", books_o, books_u)], NOW)
    assert unpriced == 0 and len(picks) == 1
    assert picks[0]["side"] == "Lower" and picks[0]["p"] > 0.6 and picks[0]["game"].startswith("MIL @ PHI")
    # started games are left out unless asked for; a single book isn't enough
    assert find_picks([_event("g1", books_o, books_u, started=True)], NOW)[0] == []
    one_o, one_u = {"fanduel": books_o["fanduel"]}, {"fanduel": books_u["fanduel"]}
    assert find_picks([_event("g1", one_o, one_u)], NOW) == ([], 1)


def test_first_two_in_different_games():
    picks = [{"game_id": "a"}, {"game_id": "a"}, {"game_id": "b"}]
    assert first_two(picks) == (0, 2)
    assert first_two([{"game_id": "a"}]) is None


def test_log_summary():
    assert log_summary([]).startswith("No graded")
    picks = [{"result": "hit", "p": 0.6}] * 70 + [{"result": "miss", "p": 0.6}] * 30 + [{"result": "push"}]
    text = log_summary(picks)
    assert "70/100 hit = 70.0%" in text and "clearly above" in text


from kalshibot.streaks import find_market, parse_candidate, score_candidate, two_streaks


def test_parse_candidate():
    c = parse_candidate("Devin Booker / rebounds / Lower / 4.5 / $20,000 / 21000")
    assert c["side"] == "Lower" and c["line"] == 4.5 and c["m"] == 1.05 and c["p"] is None
    assert parse_candidate("A / pts / higher / 20.5 / 100 / 80 / 62%")["p"] == 0.62
    for bad in ("A / pts / over / 1 / 1 / 1", "A / pts / higher / 1"):
        try:
            parse_candidate(bad)
            assert False, bad
        except ValueError:
            pass


def test_find_market_and_score_candidate():
    books_o = {"fanduel": _q("+150", 24.5), "draftkings": _q("+140", 24.5)}
    books_u = {"fanduel": _q("-190", 24.5), "draftkings": _q("-175", 24.5)}
    events = [_event("g1", books_o, books_u)]
    assert find_market(events, "joe player", "PTS")[3] == "Joe Player"
    try:
        find_market(events, "Nobody", "points")
        assert False
    except LookupError:
        pass
    lower = score_candidate(parse_candidate("Joe / points / lower / 24.5 / 100 / 90"), events, NOW)
    assert lower["p"] > 0.6 and lower["score"] == lower["p"] * 0.9 and lower["game"].startswith("MIL @ PHI")
    own = score_candidate(parse_candidate("Ghost / points / higher / 9.5 / 100 / 120 / 55"), events, NOW)
    assert own["score"] == 0.55 * 1.2 and own["own_p"]


def test_two_streaks_split_or_same():
    a = {"p": 0.80, "score": 0.80, "game_id": "a"}
    assert "SPLIT" in two_streaks(a, {"p": 0.78, "score": 0.78, "game_id": "b"})
    assert "SAME PICK" in two_streaks(a, {"p": 0.60, "score": 0.60, "game_id": "b"})
