"""Underdog Streaks math: 11 picks in a row (two to start, then one at a
time), about 1,000x the entry, with an early cash-out offer along the way.

Usage:
  python -m kalshibot.streaks odds -150 +120
      chance a pick hits, from a sportsbook's two prices for it (vig removed)
  python -m kalshibot.streaks cashout --prize 20000 --left 3 --offer 4500 --p 0.58
      take the cash-out, or keep going? (--p = your chance per remaining pick)
  python -m kalshibot.streaks value --entry 20 --prize 20000 --p 0.56
      what an entry is worth if every pick hits at that rate
  python -m kalshibot.streaks picks NBA [--top 20] [--live]
      Underdog's current lines scored against the sportsbooks' prices for
      the same player and stat; only picks above the 53.4% break-even
  python -m kalshibot.streaks compare NBA "Devin Booker / rebounds / lower / 4.5 / 20000 / 21000" ...
      rank candidates by hit % x prize change (the app's prize after the
      pick / before), and say whether to split them across two streaks
  python -m kalshibot.streaks log [add|result|show]
      record your picks and results (state/streak_picks.json) and see your
      real hit rate against 53.4%

All of this is risk-neutral expected value: it says which choice pays more
on average. Taking a smaller sure amount can still be the right call for
you -- the numbers just show what it costs.

Only suggests picks; you place them yourself in the Underdog app.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PICKS = 11
BREAK_EVEN = 0.534  # break_even(1000, 1): ~1,000x for 11 in a row

# Pick'em apps quote a line with no real price, so they say nothing about
# the chance; "unknown" is an unnamed feed.
NOT_SPORTSBOOKS = {"underdog", "prizepicks", "sleeper", "dabble", "betr", "parlayplay", "chalkboard", "unknown"}
# A book's over and under must both be live and quoted within this long of
# each other, or the pair is stale (e.g. over -163 / under -2400).
PAIR_MAX_GAP = timedelta(minutes=10)
QUOTE_MAX_AGE = timedelta(hours=2)
# Books whose line is further than this from Underdog's are left out rather
# than stretched with the line-adjustment model.
MAX_LINE_GAP = 0.2


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


# ---- Line adjustment ---------------------------------------------------
# A book prices "over 4.5" but Underdog asks about 3.5: fit a distribution
# for the stat to the book's vig-free chance at its line, then read off the
# chance at Underdog's line. Small counts (assists, steals, threes, TDs)
# use a Poisson; big totals (points, PRA) and yardage use a normal whose
# spread is a share of the line.

SD_SHARE = {"passing_yards": 0.25, "rushing_yards": 0.45, "receiving_yards": 0.55}
SD_SHARE_YARDS = 0.45
SD_SHARE_DEFAULT = 0.30


def _uses_normal(stat: str, line: float) -> bool:
    return "yards" in stat or "fantasy" in stat.lower() or line >= 12


def _poisson_over(line: float, lam: float) -> float:
    """P(stat > line); on a whole-number line, a push doesn't count either way."""
    k = math.floor(line)
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k + 1):
        term *= lam / i
        cdf += term
    if line == k:  # P(X = k) is `term`
        return (1 - cdf) / (1 - term) if term < 1 else 0.0
    return 1 - cdf


def _solve_lambda(line: float, p_over: float) -> float:
    lo, hi = 1e-6, 4 * line + 20
    for _ in range(80):
        mid = (lo + hi) / 2
        if _poisson_over(line, mid) < p_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _normal_sd(stat: str, line: float) -> float:
    share = SD_SHARE.get(stat, SD_SHARE_YARDS if "yards" in stat else SD_SHARE_DEFAULT)
    return max(share * line, 5.0 if "yards" in stat else 1.5)


def adjust_to_line(stat: str, book_line: float, p_over: float, target_line: float) -> float:
    """Chance of going over `target_line`, given a vig-free `p_over` at `book_line`."""
    if book_line == target_line:
        return p_over
    p_over = min(max(p_over, 0.005), 0.995)
    if _uses_normal(stat, book_line):
        sd = _normal_sd(stat, book_line)
        mean = book_line + sd * statistics.NormalDist().inv_cdf(p_over)
        return 1 - statistics.NormalDist(mean, sd).cdf(target_line)
    return _poisson_over(target_line, _solve_lambda(book_line, p_over))


# ---- Pick finder -------------------------------------------------------

STAT_NAMES = {
    "points": "Points", "rebounds": "Rebounds", "assists": "Assists", "steals": "Steals", "blocks": "Blocks",
    "threePointersMade": "3-Pointers Made", "turnovers": "Turnovers",
    "points+rebounds+assists": "Pts + Rebs + Asts", "points+rebounds": "Points + Rebounds",
    "points+assists": "Points + Assists", "rebounds+assists": "Rebounds + Assists",
    "blocks+steals": "Blocks + Steals",
}


def stat_name(stat: str) -> str:
    if stat in STAT_NAMES:
        return STAT_NAMES[stat]
    words = []
    for part in stat.replace("+", " + ").replace("_", " ").split(" "):
        spaced = "".join(" " + c.lower() if c.isupper() else c for c in part)
        words.append(spaced.strip())
    return " ".join(w for w in words if w).title().replace(" + ", " + ")


def _stamp(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def book_chances(over: dict[str, Any], under: dict[str, Any], now: datetime) -> list[tuple[str, float, float]]:
    """(book, line, vig-free chance of over) for each sportsbook quoting both
    sides of a player's over/under live and fresh, at the same line."""
    out = []
    unders = under.get("byBookmaker") or {}
    for book, o in (over.get("byBookmaker") or {}).items():
        u = unders.get(book)
        if book in NOT_SPORTSBOOKS or not u or not (o.get("available") and u.get("available")):
            continue
        try:
            line, line_u = float(o["overUnder"]), float(u["overUnder"])
            o_odds, u_odds = int(float(o["odds"])), int(float(u["odds"]))
        except (KeyError, TypeError, ValueError):
            continue
        t_o, t_u = _stamp(o.get("lastUpdatedAt")), _stamp(u.get("lastUpdatedAt"))
        if line != line_u or not t_o or not t_u or abs(t_o - t_u) > PAIR_MAX_GAP or now - min(t_o, t_u) > QUOTE_MAX_AGE:
            continue
        overround = implied(o_odds) + implied(u_odds)
        if not 1.0 <= overround <= 1.25:  # an arb or a huge margin = a bad quote
            continue
        out.append((book, line, fair_probability(o_odds, u_odds)))
    return out


def game_label(event: dict[str, Any]) -> str:
    teams = event.get("teams") or {}
    away = ((teams.get("away") or {}).get("names") or {}).get("short", "?")
    home = ((teams.get("home") or {}).get("names") or {}).get("short", "?")
    start = _stamp((event.get("status") or {}).get("startsAt"))
    when = start.astimezone().strftime("%a %I:%M%p").replace(" 0", " ") if start else ""
    return f"{away} @ {home} {when}".strip()


def chances_at_line(over: dict[str, Any], under: dict[str, Any], line: float,
                    now: datetime) -> list[tuple[str, float, float]]:
    """(book, book's line, chance of over `line`) for each usable sportsbook."""
    stat = over.get("statID", "")
    return [(b, book_line, adjust_to_line(stat, book_line, p, line))
            for b, book_line, p in book_chances(over, under, now)
            if abs(book_line - line) <= max(1.0, MAX_LINE_GAP * line)]


def find_picks(events: list[dict[str, Any]], now: datetime, live: bool = False,
               min_books: int = 2) -> tuple[list[dict[str, Any]], int]:
    """Every Underdog player line that the sportsbooks also price, scored:
    the median (over the books) of the vig-free chance of the better side,
    adjusted to Underdog's line. Best first, plus how many Underdog lines
    had too few sportsbook prices to score."""
    picks = []
    unpriced = 0
    for event in events:
        status = event.get("status") or {}
        started = bool(status.get("started") or status.get("live"))
        if status.get("completed") or (started and not live):
            continue
        odds = event.get("odds") or {}
        players = event.get("players") or {}
        for over in odds.values():
            if over.get("betTypeID") != "ou" or over.get("sideID") != "over" or not over.get("playerID"):
                continue
            if over.get("periodID", "game") != "game" or over.get("ended") or over.get("cancelled"):
                continue
            under = odds.get(over.get("opposingOddID", ""))
            ud = (over.get("byBookmaker") or {}).get("underdog") or {}
            if not under or not ud.get("available") or "overUnder" not in ud:
                continue
            ud_line = float(ud["overUnder"])
            stat = over.get("statID", "")
            used = chances_at_line(over, under, ud_line, now)
            if len(used) < min_books:
                unpriced += 1
                continue
            p_higher = statistics.median(p for _, _, p in used)
            side, p = ("Higher", p_higher) if p_higher >= 0.5 else ("Lower", 1 - p_higher)
            player = (players.get(over["playerID"]) or {}).get("name") or over["playerID"]
            picks.append({
                "player": player, "stat": stat, "stat_name": stat_name(stat), "side": side, "line": ud_line,
                "p": p, "books": len(used), "same_line": sum(1 for _, line, _ in used if line == ud_line),
                "game": game_label(event), "game_id": event.get("eventID", ""), "live": started,
            })
    picks.sort(key=lambda x: x["p"], reverse=True)
    return picks, unpriced


def first_two(picks: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Indexes of the best two picks in different games."""
    if not picks:
        return None
    for j in range(1, len(picks)):
        if picks[j]["game_id"] != picks[0]["game_id"]:
            return 0, j
    return None


def fetch_events(league: str, hours: int = 36) -> list[dict[str, Any]]:
    from dotenv import load_dotenv
    from .odds_providers.sportsgameodds import SportsGameOddsProvider
    load_dotenv(".env")
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return SportsGameOddsProvider()._fetch_leagues(
        [league], (now - timedelta(hours=4)).strftime(fmt), (now + timedelta(hours=hours)).strftime(fmt))


def show_picks(league: str, top: int, live: bool, min_books: int) -> None:
    now = datetime.now(timezone.utc)
    picks, unpriced = find_picks(fetch_events(league), now, live=live, min_books=min_books)
    good = [x for x in picks if x["p"] > BREAK_EVEN]
    print(f"{league}: {len(picks)} Underdog lines priced by {min_books}+ sportsbooks; "
          f"{len(good)} above the {BREAK_EVEN:.1%} break-even.")
    if unpriced:
        print(f"({unpriced} more Underdog lines have no sportsbook prices yet -- books often post "
              "props the day of the game.)")
    print()
    if not good:
        return
    for i, x in enumerate(good[:top], 1):
        flag = "  LIVE" if x["live"] else ""
        print(f"{i:>2}. {x['player']} / {x['stat_name']} / {x['side']} / {x['line']:g} / {x['p']:.1%}"
              f"   {x['game']}  ({x['books']} books, {x['same_line']} at this line){flag}")
    pair = first_two(good[:top])
    if pair:
        a, b = pair
        print(f"\nFirst two picks in different games: #{a + 1} and #{b + 1} "
              f"(both hit {good[a]['p'] * good[b]['p']:.1%} of the time).")
    print("\nHit % = sportsbooks' median vig-free chance, adjusted where their line differs from "
          "Underdog's (\"0 at this line\" = all adjusted, less certain). Lines move, and Streaks may "
          "not offer every line or both sides: check the app before picking.")


# ---- Compare candidates by prize-adjusted value -------------------------
# Underdog changes the prize with each pick's odds, so the best pick is not
# the likeliest one: it's the one with the highest hit % x m, where m = the
# prize after taking the pick / the prize before. A streak's average worth
# is its current prize times the product of those scores.

STAT_ALIASES = {
    "pts": "points", "reb": "rebounds", "rebs": "rebounds", "ast": "assists", "asts": "assists",
    "stl": "steals", "blk": "blocks", "3pm": "threePointersMade", "threes": "threePointersMade",
    "3pt": "threePointersMade", "pra": "points+rebounds+assists", "pr": "points+rebounds",
    "pa": "points+assists", "ra": "rebounds+assists", "stocks": "blocks+steals",
}


def _norm(text: str) -> str:
    import unicodedata
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c for c in plain.lower() if c.isalnum())


def parse_candidate(spec: str) -> dict[str, Any]:
    """'Player / stat / higher|lower / line / prize before / prize after [/ hit %]'"""
    parts = [p.strip() for p in spec.split("/")]
    if len(parts) not in (6, 7):
        raise ValueError(f'expected "Player / stat / higher|lower / line / prize before / prize after '
                         f'[/ hit %]", got "{spec}"')
    side = parts[2].capitalize()
    if side not in ("Higher", "Lower"):
        raise ValueError(f'side must be higher or lower in "{spec}"')

    def money(s: str) -> float:
        return float(s.replace("$", "").replace(",", ""))

    before, after = money(parts[4]), money(parts[5])
    if before <= 0 or after <= 0:
        raise ValueError(f'prizes must be positive in "{spec}"')
    p = None
    if len(parts) == 7:
        p = float(parts[6].rstrip("%"))
        p = p / 100 if p > 1 else p
    return {"player": parts[0], "stat": parts[1], "side": side, "line": float(parts[3]),
            "m": after / before, "p": p}


def find_market(events: list[dict[str, Any]], player: str, stat: str) -> tuple[dict, dict, dict, str]:
    """(event, over, under, full player name) for a player's full-game over/under on a stat."""
    want_stat = _norm(STAT_ALIASES.get(_norm(stat), stat))
    want_player = _norm(player)
    found = []
    for event in events:
        odds = event.get("odds") or {}
        players = event.get("players") or {}
        for over in odds.values():
            if over.get("betTypeID") != "ou" or over.get("sideID") != "over" or not over.get("playerID"):
                continue
            if over.get("periodID", "game") != "game":
                continue
            stat_id = over.get("statID", "")
            if want_stat not in (_norm(stat_id), _norm(stat_name(stat_id))):
                continue
            name = (players.get(over["playerID"]) or {}).get("name") or over["playerID"]
            if want_player == _norm(name) or want_player in _norm(name):
                under = odds.get(over.get("opposingOddID", ""))
                if under:
                    found.append((event, over, under, name))
    exact = [f for f in found if _norm(f[3]) == want_player]
    found = exact or found
    names = sorted({f[3] for f in found})
    if not found:
        raise LookupError(f"no {stat} over/under for {player} in the data")
    if len(names) > 1:
        raise LookupError(f"{player} matches {', '.join(names)}: use the full name")
    return found[0]


def score_candidate(cand: dict[str, Any], events: list[dict[str, Any]] | None, now: datetime,
                    min_books: int = 2) -> dict[str, Any]:
    """Fill in hit % (from the sportsbooks, unless given), game and score."""
    out = dict(cand, game="", game_id=cand["player"], books=0, same_line=0, live=False, note="",
               own_p=cand["p"] is not None)
    if events is not None:
        try:
            event, over, under, name = find_market(events, cand["player"], cand["stat"])
            status = event.get("status") or {}
            out.update(player=name, stat=stat_name(over.get("statID", "")), game=game_label(event),
                       game_id=event.get("eventID", ""), live=bool(status.get("started") or status.get("live")))
            used = chances_at_line(over, under, cand["line"], now)
            out.update(books=len(used), same_line=sum(1 for _, line, _ in used if line == cand["line"]))
            if cand["p"] is None:
                if len(used) < min_books:
                    out["note"] = f"only {len(used)} sportsbook(s) price this; add a hit % yourself"
                else:
                    p_higher = statistics.median(p for _, _, p in used)
                    out["p"] = p_higher if cand["side"] == "Higher" else 1 - p_higher
        except LookupError as e:
            out["note"] = str(e) if cand["p"] is None else ""
    if out["p"] is not None:
        out["score"] = out["p"] * out["m"]
    return out


def two_streaks(best: dict[str, Any], second: dict[str, Any]) -> str:
    """Same pick on both streaks, or the best two picks split across them."""
    same_ev, split_ev = best["score"], (best["score"] + second["score"]) / 2
    same_alive = best["p"]
    split_alive = 1 - (1 - best["p"]) * (1 - second["p"])
    cost = 1 - split_ev / same_ev
    linked = best["game_id"] == second["game_id"]
    lines = [
        "Two streaks:",
        f"  Same pick (#1 on both): at least one streak survives {same_alive:.1%}; "
        f"average worth x{same_ev:.3f}.",
        f"  Split (#1 on one, #2 on the other): at least one survives {split_alive:.1%}"
        f"{' (same game, so really a bit less)' if linked else ''}; average worth x{split_ev:.3f}.",
    ]
    if cost <= 0.05:
        lines.append(f"  -> SPLIT: it costs {cost:.1%} of average worth and cuts the chance of losing both "
                     f"from {1 - same_alive:.1%} to {1 - split_alive:.1%}.")
    else:
        lines.append(f"  -> SAME PICK: #2 is {cost:.1%} worse on average, too much to give up for safety "
                     "(your call if you'd rather not risk both on one pick).")
    return "\n".join(lines)


def show_compare(league: str, specs: list[str], min_books: int) -> None:
    now = datetime.now(timezone.utc)
    cands = [parse_candidate(s) for s in specs]
    events = fetch_events(league)  # also gives each pick's game, even with your own hit %
    scored = [score_candidate(c, events, now, min_books) for c in cands]
    ranked = sorted((x for x in scored if "score" in x), key=lambda x: x["score"], reverse=True)
    print("Ranked by score = hit % x prize change (higher is better):\n")
    for i, x in enumerate(ranked, 1):
        where = f"   {x['game']}" if x["game"] else ""
        books = ("  (your hit %)" if x["own_p"] else f"  ({x['books']} books, {x['same_line']} at this line)")
        flag = "  LIVE" if x["live"] else ""
        print(f"{i:>2}. {x['player']} / {x['stat']} / {x['side']} / {x['line']:g}   "
              f"hit {x['p']:.1%} x {x['m']:.3f} = {x['score']:.3f}{where}{books}{flag}")
    for x in scored:
        if "score" not in x:
            print(f"  could not score {x['player']} / {x['stat']}: {x['note']}")
    if len(ranked) >= 2:
        print("\n" + two_streaks(ranked[0], ranked[1]))
    print(f"\nFor scale: a pick at the {BREAK_EVEN:.1%} break-even with no prize change scores {BREAK_EVEN:.3f}. "
          "Prize change = the app's prize after adding the pick / before.")


# ---- Pick log ----------------------------------------------------------

LOG_PATH = Path("state/streak_picks.json")


def load_log(path: Path = LOG_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text()) if path.exists() else []


def save_log(picks: list[dict[str, Any]], path: Path = LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(picks, indent=2))
    os.replace(tmp, path)


def log_summary(picks: list[dict[str, Any]]) -> str:
    graded = [x for x in picks if x.get("result") in ("hit", "miss")]
    if not graded:
        return f"No graded picks yet (break-even is {BREAK_EVEN:.1%})."
    n = len(graded)
    hits = sum(1 for x in graded if x["result"] == "hit")
    rate = hits / n
    # Wilson 95% range for the true hit rate
    z = 1.96
    centre = (rate + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    lo, hi = centre - half, centre + half
    verdict = ("clearly above" if lo > BREAK_EVEN else "clearly below" if hi < BREAK_EVEN
               else "too few picks to tell vs")
    text = (f"{hits}/{n} hit = {rate:.1%} (95% range {lo:.1%}-{hi:.1%}): {verdict} the {BREAK_EVEN:.1%} break-even.")
    predicted = [x["p"] for x in graded if x.get("p")]
    if predicted:
        text += f"\nThe finder expected {statistics.mean(predicted):.1%} on the {len(predicted)} picks it scored."
    return text


def run_log(args: argparse.Namespace) -> None:
    picks = load_log()
    if args.action == "add":
        side = args.side.capitalize()
        if side not in ("Higher", "Lower"):
            raise SystemExit("side must be higher or lower")
        pick = {"id": max((x["id"] for x in picks), default=0) + 1,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "player": args.player, "stat": args.stat,
                "side": side, "line": args.line, "p": args.p, "game": args.game, "result": None}
        picks.append(pick)
        save_log(picks)
        print(f"Logged #{pick['id']}: {pick['player']} / {pick['stat']} / {side} / {pick['line']:g}")
        return
    if args.action == "result":
        for x in picks:
            if x["id"] == args.id:
                x["result"] = args.result
                save_log(picks)
                print(f"#{x['id']} {x['player']} {x['stat']} {x['side']} {x['line']:g}: {args.result}")
                break
        else:
            raise SystemExit(f"no pick #{args.id}")
    for x in picks[-args.last:]:
        p = f"{x['p']:.1%}" if x.get("p") else "-"
        print(f"#{x['id']:<3} {x['date']}  {x['player']} / {x['stat']} / {x['side']} / {x['line']:g}"
              f"  [{p}]  {x.get('game') or ''}  -> {x.get('result') or 'open'}")
    print(log_summary(picks))


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
    pk = sub.add_parser("picks", help="Underdog lines scored against the sportsbooks")
    pk.add_argument("league", nargs="?", default="NBA", type=str.upper)
    pk.add_argument("--top", type=int, default=20)
    pk.add_argument("--live", action="store_true", help="include games already under way")
    pk.add_argument("--min-books", type=int, default=2)
    cp = sub.add_parser("compare", help="rank candidate picks by hit %% x prize change")
    cp.add_argument("league", type=str.upper)
    cp.add_argument("candidates", nargs="+",
                    help='"Player / stat / higher|lower / line / prize before / prize after [/ hit %%]"')
    cp.add_argument("--min-books", type=int, default=2)
    lg = sub.add_parser("log", help="record picks and results; real hit rate vs 53.4%%")
    lg.set_defaults(action="show")
    lsub = lg.add_subparsers(dest="action")
    la = lsub.add_parser("add", help='e.g. log add "Jalen Brunson" points higher 25.5 --p 0.58')
    la.add_argument("player")
    la.add_argument("stat")
    la.add_argument("side", help="higher or lower")
    la.add_argument("line", type=float)
    la.add_argument("--p", type=float, help="the finder's hit %% as a fraction, e.g. 0.58")
    la.add_argument("--game", default="")
    lr = lsub.add_parser("result", help="e.g. log result 3 hit")
    lr.add_argument("id", type=int)
    lr.add_argument("result", choices=["hit", "miss", "push"])
    lsub.add_parser("show")
    lg.add_argument("--last", type=int, default=20)
    args = parser.parse_args()

    if args.cmd == "picks":
        show_picks(args.league, args.top, args.live, args.min_books)
    elif args.cmd == "compare":
        try:
            show_compare(args.league, args.candidates, args.min_books)
        except ValueError as e:
            raise SystemExit(str(e))
    elif args.cmd == "log":
        run_log(args)
    elif args.cmd == "odds":
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
