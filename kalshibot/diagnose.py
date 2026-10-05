"""Show what the bot sees for one sport right now, game by game.

Usage: python -m kalshibot.diagnose nfl           (or soccer / mlb / cfb / nhl)
       python -m kalshibot.diagnose kalshi
       python -m kalshibot.diagnose markets nhl   (or any sport above)
       python -m kalshibot.diagnose order         (test order that won't fill)
       python -m kalshibot.diagnose account       (orders/fills/positions on this key)

Runs the exact same query and parsing as the live bot, then prints each
event with the reason it is or isn't counted as a live game. "kalshi"
instead checks which Kalshi API address accepts your credentials.
"markets" shows the Kalshi markets for a sport and which one the bot would
buy for each of today's games.
"""
from __future__ import annotations

import json
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

from .executor import yes_ask_cents
from .kalshi_client import KalshiClient
from .main import build_kalshi_client
from .matching import SERIES_BY_SPORT, MarketMatcher, event_date, game_date
from .odds_providers.sportsgameodds import LEAGUE_IDS, SportsGameOddsProvider

KALSHI_CANDIDATE_URLS = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
]


def _try(label: str, fn) -> str:
    try:
        fn()
        return f"{label} OK"
    except requests.HTTPError as e:
        return f"{label} HTTP {e.response.status_code} {e.response.text[:200]}"
    except Exception as e:
        return f"{label} {type(e).__name__}"


def check_kalshi() -> None:
    creds = build_kalshi_client(demo=False)._creds
    print("Credentials loaded:", "yes" if creds else "NO -- check .env")
    for url in KALSHI_CANDIDATE_URLS:
        client = KalshiClient(creds, base_url=url, timeout=15)
        markets = _try("markets", lambda: client.list_markets(limit=1))
        balance = _try("balance", client.get_balance) if creds else "balance skipped"
        print(f"{url}\n    {markets} | {balance}")
    print(
        "\nUse the address where BOTH say OK: add it to .env as\n"
        "KALSHI_BASE_URL=<that address>"
    )


SERIES_KEYWORDS = {
    "nhl": ["nhl", "hockey"],
    "nfl": ["nfl", "profootball"],
    "mlb": ["mlb", "baseball"],
    "cfb": ["ncaaf", "collegefootball"],
    "soccer": ["soccer", "premier", "laliga", "bundesliga", "seriea", "ligue"],
}

MARKET_FIELDS = ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "no_sub_title", "close_time")


def check_markets(sport: str) -> None:
    kalshi = build_kalshi_client(demo=False)
    matcher = MarketMatcher(kalshi)
    series = SERIES_BY_SPORT.get(sport)
    markets = kalshi.iter_markets(series_ticker=series, status="open")
    print(f"Kalshi series {series}: {len(markets)} open markets\n")

    if not markets:
        print("That series has no open markets -- it may be the wrong ticker. Kalshi sports series:")
        try:
            all_series = kalshi.list_series(category="Sports").get("series", [])
        except Exception as e:
            print(f"  (couldn't list series: {e})")
            all_series = []
        keywords = SERIES_KEYWORDS.get(sport, [sport])
        for s in all_series:
            text = (s.get("ticker", "") + " " + s.get("title", "")).lower().replace(" ", "")
            if any(k in text for k in keywords):
                print(f"  {s.get('ticker')}: {s.get('title')}")
        return

    print("Sample markets (to check how Kalshi names teams):")
    for m in markets[:4]:
        print(json.dumps({f: m.get(f) for f in MARKET_FIELDS}, indent=2))
    print("\nPrice fields on the first market (the bot buys at the YES ask):")
    print(json.dumps({k: v for k, v in markets[0].items() if "yes" in k or "no_" in k or "price" in k}, indent=2))
    print(f"  -> bot reads YES ask as {yes_ask_cents(markets[0])}¢")

    provider = SportsGameOddsProvider()
    print("\nWhat the bot would buy for each of today's games:")
    for event in provider.fetch_events(sport):
        snap = provider._parse_event(sport, event) if event.get("type") == "match" else None
        if snap is None or snap.is_final:
            continue
        game = f"{snap.away_team} @ {snap.home_team} on {game_date(snap)} (fav {snap.favorite_team})"
        events = matcher.candidate_events(snap)
        resolved = matcher.resolve(snap)
        choice = f"BUY YES {resolved.ticker}" if resolved else "SKIP"
        found = ", ".join(f"{e} [{event_date(e)}]" for e in sorted(events)) or "none"
        print(f"  {game}\n      Kalshi events for these teams: {found}\n      -> {choice}")


def check_order() -> None:
    """Send one real order Kalshi will not fill: buy 1 YES at 1¢, cancelled
    at once unless someone sells at 1¢. Proves the key may trade and the
    order format is accepted, without spending money (worst case 1¢)."""
    kalshi = build_kalshi_client(demo=False)
    print("Balance:", kalshi.get_balance())
    market = next(
        (m for m in kalshi.iter_markets(max_pages=1, series_ticker=SERIES_BY_SPORT["nhl"], status="open")
         if (yes_ask_cents(m) or 0) > 1),
        None,
    )
    if market is None:
        print("No open NHL market with a YES ask above 1¢ to test against.")
        return
    print(f"Test order: buy 1 YES on {market['ticker']} at 1¢ (ask is {yes_ask_cents(market)}¢), immediate-or-cancel")
    try:
        response = kalshi.create_order(ticker=market["ticker"], count=1, price_dollars="0.0100")
    except requests.HTTPError as e:
        print(f"REJECTED: HTTP {e.response.status_code} {e.response.text[:500]}")
        return
    print("ACCEPTED -- live orders will work. Kalshi's reply:")
    print(json.dumps(response, indent=2))


ACCOUNT_SECTIONS = [
    # (endpoint, key holding the list in Kalshi's reply)
    ("fills", "fills"),
    ("orders", "orders"),
    ("positions", "market_positions"),
    ("settlements", "settlements"),
]


def check_account() -> None:
    """What the account behind THIS API key holds, straight from Kalshi --
    to compare against what the Kalshi app shows when you're logged in."""
    kalshi = build_kalshi_client(demo=False)
    print("API key:", kalshi._creds.api_key_id if kalshi._creds else "NONE")
    print("Balance:", kalshi.get_balance())
    for kind, list_key in ACCOUNT_SECTIONS:
        try:
            reply = kalshi.get_portfolio(kind, limit=10)
        except requests.HTTPError as e:
            print(f"\n{kind}: HTTP {e.response.status_code} {e.response.text[:200]}")
            continue
        items = reply.get(list_key)
        if items is None:  # unexpected shape: show what came back
            print(f"\n{kind}: keys {sorted(reply)}")
            items = next((v for v in reply.values() if isinstance(v, list)), [])
        print(f"\n{kind} (latest {len(items)}):")
        for item in items:
            print("  " + json.dumps(item))


def main() -> None:
    sport = sys.argv[1] if len(sys.argv) > 1 else "nfl"
    if sport == "kalshi":
        check_kalshi()
        return
    if sport == "account":
        check_account()
        return
    if sport == "order":
        check_order()
        return
    if sport == "markets":
        target = sys.argv[2] if len(sys.argv) > 2 else "nhl"
        if target not in SERIES_BY_SPORT:
            print(f"Unknown sport {target!r}; pick one of: {', '.join(SERIES_BY_SPORT)}")
            return
        check_markets(target)
        return
    if sport not in LEAGUE_IDS:
        print(f"Unknown sport {sport!r}; pick one of: {', '.join(LEAGUE_IDS)}")
        return

    provider = SportsGameOddsProvider()
    events = provider.fetch_events(sport)
    print(f"{sport}: {len(events)} events returned (leagues {LEAGUE_IDS[sport]})\n")

    counts: dict[str, int] = {}
    sample_live_status = None
    sample_live_odds = None
    for event in events:
        status = event.get("status", {})
        teams = event.get("teams", {})
        away = teams.get("away", {}).get("names", {}).get("long", "?")
        home = teams.get("home", {}).get("names", {}).get("long", "?")

        snap = provider._parse_event(sport, event) if event.get("type") == "match" else None
        if event.get("type") != "match":
            verdict = f"skipped (type={event.get('type')})"
        elif snap is None:
            verdict = "DROPPED (no moneyline or unparseable)"
        elif snap.is_live:
            verdict = "COUNTED AS LIVE"
            sample_live_status = sample_live_status or status
            sample_live_odds = sample_live_odds or {
                k: v for k, v in event.get("odds", {}).items()
                if v.get("statID") == "points" and v.get("periodID") == "game" and v.get("betTypeID") == "ml"
            }
        elif snap.is_final:
            verdict = "finished"
        else:
            verdict = "upcoming (pregame line cached each cycle)"

        counts[verdict] = counts.get(verdict, 0) + 1
        line = f"{verdict:42} {away} @ {home}"
        if snap is not None:
            pregame = snap.pregame_favorite_odds
            line += (
                f" | fav {snap.favorite_team} pregame {pregame if pregame is not None else 'NONE'}"
                f" live {snap.live_favorite_odds:+d} | score {snap.away_score}-{snap.home_score}"
                f" period {snap.period} clock {snap.seconds_remaining}s"
            )
        print(line)

    print("\nSummary:")
    for verdict, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3}  {verdict}")

    # Raw status of one live game, to check how period and clock come
    # through for this sport (the NHL clock format is not yet verified).
    if sample_live_status is not None:
        print("\nRaw status of one live game:")
        print(json.dumps(sample_live_status, indent=2))
        # Shows whether the moneyline actually moves in-game (check the
        # per-bookmaker lastUpdatedAt times against the clock).
        print("\nRaw moneyline odds of that game:")
        print(json.dumps(sample_live_odds, indent=2))


if __name__ == "__main__":
    main()
