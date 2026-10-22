"""Show what the bot sees for one sport right now, game by game.

Usage: python -m kalshibot.diagnose nfl           (or soccer / mlb / cfb / nhl)
       python -m kalshibot.diagnose kalshi
       python -m kalshibot.diagnose markets nhl   (or any sport above; soccer-1h = first-half markets)
       python -m kalshibot.diagnose order         (test order that won't fill)
       python -m kalshibot.diagnose account       (orders/fills/positions on this key)
       python -m kalshibot.diagnose leagues       (every SportsGameOdds league on your plan)
       python -m kalshibot.diagnose notify        (test phone notifications)
       python -m kalshibot.diagnose site          (build and publish the results page now)
       python -m kalshibot.diagnose paper         (paper rules' record -- tracked, never ordered)
       python -m kalshibot.diagnose record        (the bot's wins and losses; add "import" for past bets,
                                                  or "reset" to start over at 0-0 -- stop the bot first)
       python -m kalshibot.diagnose transfer 3 1  (move $1 from exchange 0 to 3)
       python -m kalshibot.diagnose props         (Kalshi NHL player-prop markets the prop rule would use)

Runs the exact same query and parsing as the live bot, then prints each
event with the reason it is or isn't counted as a live game. "kalshi"
instead checks which Kalshi API address accepts your credentials.
"markets" shows the Kalshi markets for a sport and which one the bot would
buy for each of today's games.
"""
from __future__ import annotations

import json
import secrets
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

from . import notify
from .executor import order_cost, yes_ask_cents
from .kalshi_client import KalshiClient
from .ledger import BetLedger, record_line
from .risk import RiskConfig
from .main import build_kalshi_client
from .matching import SERIES_BY_SPORT, MarketMatcher, event_date, game_date
from .odds_providers.sportsgameodds import (
    API_BASE as SGO_API_BASE, DEFAULT_MONEYLINES, LEAGUE_IDS, MONEYLINES, SportsGameOddsProvider,
)

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
    "nba": ["nba", "basketball"],
    "nfl": ["nfl", "profootball"],
    "mlb": ["mlb", "baseball"],
    "cfb": ["ncaaf", "collegefootball"],
    "soccer": ["soccer", "premier", "laliga", "bundesliga", "seriea", "ligue"],
    "tennis": ["tennis", "atp", "wta"],
}

MARKET_FIELDS = ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "no_sub_title", "close_time")


def check_markets(sport: str, market: str = "game") -> None:
    kalshi = build_kalshi_client(demo=False)
    matcher = MarketMatcher(kalshi)
    markets = []
    if market == "first_half":
        print("First-half markets only.")
    for series in matcher.series_for(sport, market):
        found = kalshi.iter_markets(series_ticker=series, status="open")
        print(f"Kalshi series {series}: {len(found)} open markets")
        markets.extend(found)
    print()

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
    print(json.dumps({k: v for k, v in markets[0].items() if "yes" in k or "no_" in k or "price" in k or "exchange" in k}, indent=2))
    print(f"  -> bot reads YES ask as {yes_ask_cents(markets[0])}¢")

    provider = SportsGameOddsProvider()
    print("\nWhat the bot would buy for each of today's games:")
    for event in provider.fetch_events(sport):
        snap = provider._parse_event(sport, event) if event.get("type") == "match" else None
        if snap is None or snap.is_final:
            continue
        game = f"{snap.away_team} @ {snap.home_team} on {game_date(snap)} (fav {snap.favorite_team})"
        events = matcher.candidate_events(snap, market)
        resolved = matcher.resolve(snap, market)
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
        (m for m in kalshi.iter_markets(max_pages=1, series_ticker=SERIES_BY_SPORT["nhl"][0], status="open")
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


def check_transfer(destination: int, amount: str) -> None:
    """Move `amount` DOLLARS from exchange 0 to another exchange and show
    the balance on each before and after."""
    kalshi = build_kalshi_client(demo=False)

    def show(label: str) -> None:
        parts = kalshi.get_balance().get("balance_breakdown", [])
        print(label, ", ".join(f"exchange {p.get('exchange_index')}: ${p.get('balance')}" for p in parts))

    show("Before:")
    try:
        print("Kalshi's reply:", kalshi.transfer_between_exchanges(float(amount), 0, destination))
    except requests.HTTPError as e:
        print(f"REJECTED: HTTP {e.response.status_code} {e.response.text[:500]}")
        return
    show("After: ")


def check_leagues() -> None:
    """Every league the SportsGameOdds key can see -- to find out whether
    the plan includes tennis, and under which leagueIDs."""
    provider = SportsGameOddsProvider()
    resp = requests.get(f"{SGO_API_BASE}/leagues", headers=provider._headers(), timeout=30)
    resp.raise_for_status()
    leagues = resp.json().get("data", [])
    print(f"{len(leagues)} leagues on this SportsGameOdds key:")
    for league in sorted(leagues, key=lambda l: (str(l.get("sportID")), str(l.get("leagueID")))):
        sport = str(league.get("sportID"))
        flag = "   <-- TENNIS" if "TENNIS" in sport.upper() else ""
        print(f"  {sport:12} {str(league.get('leagueID')):20} {league.get('name') or league.get('long') or ''}{flag}")
    if not any("TENNIS" in str(l.get("sportID", "")).upper() for l in leagues):
        print("\nNo tennis leagues on this key.")


SPORT_BY_SERIES = {series: sport for sport, many in SERIES_BY_SPORT.items() for series in many}


def check_notify() -> None:
    """Send a test notification, or suggest a topic if none is set."""
    if notify.topic() is None:
        print("No NTFY_TOPIC in .env yet. Add this line to .env (a random name only you know):")
        print(f"  NTFY_TOPIC=kalshibot-{secrets.token_hex(8)}")
        print("then subscribe to that same topic in the ntfy app and run this again.")
        return
    ok = notify.send("KalshiBot test", "Notifications are working.", tags=["tada"])
    print("Sent -- check your phone." if ok else "Sending failed -- see the warning above.")


def import_bot_orders(kalshi: KalshiClient, ledger: BetLedger) -> None:
    """Add the bot's past filled orders to the ledger. Bot orders are limit
    orders with a client_order_id; Kalshi app orders are market orders
    with a blank one. 1-cent orders are the `diagnose order` test."""
    orders = kalshi.get_portfolio("orders", limit=200).get("orders", [])
    added = 0
    for o in orders:
        filled = float(o.get("fill_count_fp") or o.get("fill_count") or 0)
        if (o.get("type") != "limit" or not o.get("client_order_id") or filled <= 0
                or float(o.get("yes_price_dollars") or 0) <= 0.01 or ledger.has(o["order_id"])):
            continue
        ticker = o["ticker"]
        title = kalshi.get_market(ticker).get("market", {}).get("title", ticker)
        cost, fee = order_cost(o)
        ledger.record(order_id=o["order_id"], ticker=ticker, team=title.removesuffix(" wins"),
                      sport=SPORT_BY_SERIES.get(ticker.split("-")[0], "?"), rule="(imported)",
                      contracts=filled, cost=cost or 0.0, fee=fee, placed_at=o.get("created_time"))
        added += 1
    results = {s.get("ticker"): s.get("market_result")
               for s in kalshi.get_portfolio("settlements", limit=200).get("settlements", [])}
    for bet in ledger.open_bets():
        if bet["ticker"] in results:
            ledger.settle(bet["order_id"], str(results[bet["ticker"]]))
    print(f"Imported {added} past bot bet(s).\n")


def reset_record() -> None:
    ledger = BetLedger()
    backup = ledger.reset()
    print(f"Record reset to 0-0. Old record saved as {backup}.")
    if ledger.open_bets():
        print(f"Kept {len(ledger.open_bets())} open bet(s); they'll count once they settle.")
    check_site()


def check_record(do_import: bool) -> None:
    ledger = BetLedger()
    if do_import:
        import_bot_orders(build_kalshi_client(demo=False), ledger)
    bets = ledger.all_bets()
    if not bets:
        print("No bot bets recorded yet. (python -m kalshibot.diagnose record import adds past ones.)")
        return
    for b in bets:
        pnl = "" if b["pnl"] is None else f"{b['pnl']:+.2f}"
        print(f"  {b['placed_at'][:16]}  {b['status'].upper():5} {pnl:>8}  {b['team']} "
              f"({b['sport']}, {b['contracts']:g} @ ${b['cost']:.2f})")
    print("\n" + record_line(ledger.summary()))


def check_paper() -> None:
    """The paper rules' record (config/paper_rules.yaml): tracked, never ordered."""
    from .paper import PaperBook
    book = PaperBook()
    bets = book.all_bets()
    if not bets:
        print("No paper bets yet (config/paper_rules.yaml).")
        return
    for b in bets:
        pnl = "" if b["pnl"] is None else f"{b['pnl']:+.2f}"
        print(f"  {b['placed_at'][:16]}  {b['status'].upper():5} {pnl:>8}  {b['label']} at {b['price_cents']}c "
              f"({b['game']}, inning {b['inning']}, {b['rule']})")
    print("
Paper record (no money): " + book.record_line())


def check_props() -> None:
    """What the NHL player-prop rule finds: Kalshi's NHL series, sample
    prop markets as Kalshi sends them, and each game's matched players."""
    from .props import STAT_LABELS, PropWatcher, load_prop_rules
    kalshi = build_kalshi_client(demo=False)
    watcher = PropWatcher(kalshi, load_prop_rules())
    if not watcher.rules:
        print("No enabled player-prop rule in config/rules.yaml.")
        return
    print("Every Kalshi series starting KXNHL:")
    for s in kalshi.list_series(category="Sports").get("series", []):
        if str(s.get("ticker", "")).upper().startswith("KXNHL"):
            print(f"  {s.get('ticker'):28} {s.get('title')}")
    series = watcher.series()
    print(f"\nUsed for props: {series or 'NONE -- tell Claude which series above are player points / shots'}")
    for stat, ticker in series:
        sample = kalshi.list_markets(series_ticker=ticker, status="open", limit=3).get("markets", [])
        print(f"\n{ticker} ({stat}) -- {len(sample)} sample open markets:")
        for m in sample:
            print("  " + json.dumps({k: m.get(k) for k in (
                "ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "floor_strike",
                "strike_type", "yes_bid_dollars", "yes_ask_dollars", "exchange_index")}))
    markets = watcher.fetch()
    print(f"\n{len(markets)} prop markets read as <player> <line>+ "
          f"(e.g. {', '.join(f'{m.player} {m.line}+ {m.stat}' for m in markets[:3]) or 'none'})")

    provider = SportsGameOddsProvider()
    live = provider.list_live_games("nhl")
    games = provider.list_upcoming_games("nhl") + live
    print(f"\nNHL games now: {len(live)} live, {len(games) - len(live)} upcoming")
    for g in games:
        ladders = watcher.for_game(markets, g)
        print(f"\n{g.away_team} @ {g.home_team} ({'LIVE' if g in live else 'upcoming'}, {game_date(g)}): "
              f"{len(g.player_names)} players on SportsGameOdds' roster, {len(ladders)} with Kalshi lines")
        for (stat, player), ladder in sorted(ladders.items())[:6]:
            lines = ", ".join(f"{n}+ {yes_ask_cents(m.market) or '-'}¢" for n, m in sorted(ladder.items()))
            print(f"  {player} {STAT_LABELS[stat]}: {lines}")
        for row in watcher.describe_watch(markets, g):
            print(f"  WATCHING {row}")
    print("\nPuck-drop prices are saved while the bot runs before a game starts; "
          "this check doesn't save any.")


def check_site() -> None:
    """Build the results page now and publish it if RESULTS_REPO and
    GITHUB_TOKEN are set -- to test publishing without waiting for the bot."""
    from .publish import SitePublisher
    from .report import build_html, load_rule_texts
    publisher = SitePublisher()
    html = build_html(BetLedger().all_bets(), load_rule_texts(), [], mode="live",
                      stake_usd=RiskConfig.load().stake_usd)
    uploaded = publisher.update(html, force=True)
    print("Wrote site/index.html (open it in a browser to preview).")
    if not publisher.enabled:
        print("Not published: add RESULTS_REPO and GITHUB_TOKEN to .env.")
    elif uploaded:
        print(f"Uploaded -- live in a minute or two at {publisher.url}")
    else:
        print("NOT uploaded -- see the warning above. A 403 means the GitHub token can't write to "
              f"{publisher.url}'s repository: give it Contents: Read and write on that repository.")


def main() -> None:
    sport = sys.argv[1] if len(sys.argv) > 1 else "nfl"
    if sport == "kalshi":
        check_kalshi()
        return
    if sport == "site":
        check_site()
        return
    if sport == "notify":
        check_notify()
        return
    if sport == "record":
        if len(sys.argv) > 2 and sys.argv[2] == "reset":
            reset_record()
            return
        check_record(do_import=len(sys.argv) > 2 and sys.argv[2] == "import")
        return
    if sport == "leagues":
        check_leagues()
        return
    if sport == "account":
        check_account()
        return
    if sport == "transfer":
        if len(sys.argv) != 4:
            print("Usage: python -m kalshibot.diagnose transfer <to exchange> <dollars>")
            return
        check_transfer(int(sys.argv[2]), sys.argv[3])
        return
    if sport == "order":
        check_order()
        return
    if sport == "props":
        check_props()
        return
    if sport == "paper":
        check_paper()
        return
    if sport == "markets":
        target = sys.argv[2] if len(sys.argv) > 2 else "nhl"
        if target == "soccer-1h":
            check_markets("soccer", "first_half")
            return
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
            # Which moneyline-style odds this event does carry, to see what
            # the game-winner line is called for this sport.
            ml = sorted(k for k, v in event.get("odds", {}).items() if "ml" in str(v.get("betTypeID")))
            print(f"    dropped event's moneyline oddIDs: {ml[:8] or 'none'}; team keys: {sorted(teams)}")
        elif snap.is_live:
            verdict = "COUNTED AS LIVE"
            sample_live_status = sample_live_status or status
            lines = MONEYLINES.get(sport, DEFAULT_MONEYLINES)
            sample_live_odds = sample_live_odds or {
                k: v for k, v in event.get("odds", {}).items()
                if v.get("statID") == "points" and (v.get("betTypeID"), v.get("periodID")) in lines
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
            if snap.first_half_favorite_odds is not None:
                line += f" | 1st-half fav odds {snap.first_half_favorite_odds:+d}"
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
