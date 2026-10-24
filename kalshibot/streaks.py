"""Underdog Streaks math: 11 picks in a row (two to start, then one at a
time), about 1,000x the entry, with an early cash-out offer along the way.

Usage:
  python -m kalshibot.streaks odds -150 +120
      chance a pick hits, from a sportsbook's two prices for it (vig removed)
  python -m kalshibot.streaks cashout --prize 20000 --left 3 --offer 4500 --p 0.58
      take the cash-out, or keep going? (--p = your chance per remaining pick)
  python -m kalshibot.streaks value --entry 20 --prize 20000 --p 0.56
      what an entry is worth if every pick hits at that rate

All of this is risk-neutral expected value: it says which choice pays more
on average. Taking a smaller sure amount can still be the right call for
you -- the numbers just show what it costs.
"""
from __future__ import annotations

import argparse

PICKS = 11


def implied(american: int) -> float:
    """Sportsbook price -> chance including the book's margin."""
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def fair_probability(price: int, other_side: int) -> float:
    """Chance this side hits, with the book's margin (vig) removed."""
    a, b = implied(price), implied(other_side)
    return a / (a + b)


def finish_value(prize: float, picks_left: int, p: float) -> float:
    """Average worth of playing out the rest of the streak."""
    return prize * p ** picks_left


def break_even(prize: float, entry: float, picks: int = PICKS) -> float:
    """Chance per pick needed for an entry to pay for itself."""
    return (entry / prize) ** (1 / picks)


def cashout_verdict(prize: float, picks_left: int, offer: float, p: float) -> str:
    keep = finish_value(prize, picks_left, p)
    needed = (offer / prize) ** (1 / picks_left)
    if offer >= keep:
        return (f"TAKE THE CASH-OUT: ${offer:,.0f} beats ${keep:,.0f}, the average worth of playing "
                f"{picks_left} more at {p:.0%}. Keeping going only pays if your picks hit {needed:.1%}+.")
    return (f"KEEP GOING: playing {picks_left} more at {p:.0%} is worth ${keep:,.0f} on average, "
            f"more than the ${offer:,.0f} offer (chance of finishing: {p ** picks_left:.1%}). "
            f"The offer wins if your picks are below {needed:.1%}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Underdog Streaks math")
    sub = parser.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("odds", help="chance a pick hits from its two sportsbook prices")
    o.add_argument("price", type=int)
    o.add_argument("other_side", type=int)
    c = sub.add_parser("cashout", help="take the cash-out or keep going")
    c.add_argument("--prize", type=float, required=True)
    c.add_argument("--left", type=int, required=True, help="picks still to win")
    c.add_argument("--offer", type=float, required=True)
    c.add_argument("--p", type=float, required=True, help="chance per pick, e.g. 0.58")
    v = sub.add_parser("value", help="what an entry is worth")
    v.add_argument("--entry", type=float, required=True)
    v.add_argument("--prize", type=float, required=True)
    v.add_argument("--p", type=float, required=True)
    v.add_argument("--picks", type=int, default=PICKS)
    args = parser.parse_args()

    if args.cmd == "odds":
        p = fair_probability(args.price, args.other_side)
        print(f"{args.price:+d} vs {args.other_side:+d}: this side hits {p:.1%} "
              f"({'above' if p > break_even(1000, 1) else 'below'} the {break_even(1000, 1):.1%} "
              "break-even for a 1,000x streak)")
    elif args.cmd == "cashout":
        print(cashout_verdict(args.prize, args.left, args.offer, args.p))
    else:
        worth = finish_value(args.prize, args.picks, args.p)
        print(f"{args.picks} picks at {args.p:.0%}: finishes {args.p ** args.picks:.2%} of the time; "
              f"a ${args.entry:,.0f} entry is worth ${worth:,.2f} on average "
              f"(break-even {break_even(args.prize, args.entry, args.picks):.1%} per pick)")


if __name__ == "__main__":
    main()
