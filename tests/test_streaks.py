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
