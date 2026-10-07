"""Polling loop: pull live games -> evaluate rules -> match to a Kalshi
market -> execute (dry-run or live, per --dry-run / TRADING_ENABLED)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()

from . import notify
from .executor import Executor, american_odds, yes_ask_cents
from .ledger import BetLedger, record_line
from .kalshi_client import DEFAULT_BASE_URL, DEMO_BASE_URL, KalshiClient, KalshiCredentials
from .matching import MarketMatcher
from .odds_providers.base import Sport
from .odds_providers.sportsgameodds import SportsGameOddsProvider
from .risk import RiskConfig, RiskManager
from .rules_engine import RulesEngine, load_rules
from .state import BetStateStore

# Tennis (ATP/WTA) came with the upgraded SportsGameOdds plan; its events
# and Kalshi's KXATPMATCH/KXWTAMATCH markets were checked 2026-10-07.
SPORTS: list[Sport] = ["soccer", "nfl", "mlb", "cfb", "nhl", "tennis"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kalshibot.main")


def build_kalshi_client(demo: bool) -> KalshiClient:
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    creds = None
    if key_id and private_key_path:
        creds = KalshiCredentials(
            api_key_id=key_id,
            private_key_pem=open(private_key_path, "rb").read(),
        )
    else:
        logger.warning(
            "KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set -- running with "
            "public market-data access only, no order placement is possible"
        )
    base_url = DEMO_BASE_URL if demo else os.environ.get("KALSHI_BASE_URL", DEFAULT_BASE_URL)
    return KalshiClient(creds, base_url=base_url)


def get_open_position_count(kalshi: KalshiClient) -> int:
    try:
        positions = kalshi.get_positions()
        return len(positions.get("market_positions", []))
    except Exception:
        return 0


def settle_finished_bets(kalshi: KalshiClient, ledger: BetLedger) -> None:
    """Mark the bot's open bets won/lost once Kalshi settles their market,
    and send the result with the running record to the phone."""
    open_bets = ledger.open_bets()
    if not open_bets:
        return
    try:
        settlements = kalshi.get_portfolio("settlements", limit=200).get("settlements", [])
    except Exception as e:
        logger.warning("Couldn't check settlements this cycle: %s", e)
        return
    results = {s.get("ticker"): s.get("market_result") for s in settlements}
    for bet in open_bets:
        if bet["ticker"] not in results:
            continue
        settled = ledger.settle(bet["order_id"], str(results[bet["ticker"]]))
        if settled is None:
            continue
        line = record_line(ledger.summary())
        logger.info("BET %s: %s %s (%+.2f USD) -- %s", settled["status"].upper(),
                    settled["team"], settled["ticker"], settled["pnl"], line)
        if settled["status"] == "won":
            notify.send(f"WON: {settled['team']} +${settled['pnl']:.2f}", line, tags=["white_check_mark"])
        elif settled["status"] == "lost":
            notify.send(f"LOST: {settled['team']} -${-settled['pnl']:.2f}", line, tags=["x"])
        else:
            notify.send(f"Void/refunded: {settled['team']}", line, tags=["leftwards_arrow_with_hook"])


def _kalshi_error_code(body: str) -> str:
    try:
        return json.loads(body).get("error", {}).get("code") or "error"
    except (ValueError, AttributeError):
        return "error"


def run(poll_interval_s: int, dry_run: bool, demo: bool) -> None:
    kalshi = build_kalshi_client(demo)
    odds_provider = SportsGameOddsProvider()
    matcher = MarketMatcher(kalshi)
    rules, stake_usd = load_rules()
    state_store = BetStateStore()
    risk = RiskManager(RiskConfig.load())
    engine = RulesEngine(rules, stake_usd, state_store)
    ledger = BetLedger()
    executor = Executor(kalshi, risk, state_store, dry_run=dry_run, ledger=ledger)
    # (game, rule, error code) already sent to the phone, so a failure that
    # repeats every cycle (e.g. insufficient balance) notifies only once.
    notified_failures: set[tuple[str, str, str]] = set()

    logger.info(
        "KalshiBot starting: dry_run=%s demo_env=%s stake=$%.2f loss_cap=%s max_positions=%s",
        dry_run, demo, stake_usd, risk.config.daily_loss_cap_usd, risk.config.max_concurrent_positions,
    )
    if not dry_run:
        if executor._trading_enabled():
            logger.info("LIVE MODE: real orders WILL be placed")
        else:
            logger.warning(
                "--live was passed but %s is not true in .env -- orders will only be logged",
                risk.config.trading_enabled_env_var,
            )
        try:
            logger.info("Kalshi balance: %s", kalshi.get_balance())
        except Exception as e:
            logger.warning("Could not read Kalshi balance: %s", e)
        mode = "LIVE" if executor._trading_enabled() else "logging only"
        if notify.send(f"KalshiBot started ({mode})", record_line(ledger.summary()), tags=["robot"]):
            logger.info("Phone notifications on (ntfy topic set)")

    while True:
        live_counts: dict[str, int] = {}
        for sport in SPORTS:
            try:
                snapshots = odds_provider.list_live_games(sport)
            except Exception:
                logger.exception("Failed to fetch live games for sport=%s", sport)
                time.sleep(2)  # back off before the next sport's request on a rate-limited plan
                continue
            time.sleep(1)  # space out per-sport requests so we don't burst a free-tier rate limit
            live_counts[sport] = len(snapshots)

            for snapshot in snapshots:
                decisions = engine.evaluate(snapshot)
                for decision in decisions:
                    # Logged before any Kalshi call, so a trigger is visible
                    # even if finding/pricing the Kalshi market fails.
                    logger.info(
                        "RULE TRIGGERED: %s -- %s @ %s, betting %s at live %+d (pregame %+d)",
                        decision.rule_id, snapshot.away_team, snapshot.home_team,
                        decision.team, snapshot.live_favorite_odds, snapshot.pregame_favorite_odds,
                    )
                    try:
                        resolved = matcher.resolve(snapshot)
                        if resolved is None:
                            continue  # already logged by matcher; never guess a ticker

                        market = kalshi.get_market(resolved.ticker).get("market", {})
                        ask = yes_ask_cents(market)
                        if ask is None:
                            # Never price off a made-up number; not recorded, so it retries.
                            logger.warning(
                                "No YES ask on %s right now (fields: %s) -- will retry next cycle",
                                resolved.ticker, sorted(k for k in market if "yes" in k or "price" in k),
                            )
                            continue

                        problem = engine.kalshi_price_problem(decision.rule_id, ask)
                        if problem:
                            logger.warning(
                                "SKIPPED %s on %s: Kalshi price %d¢ (%+d) is %s -- will re-check next cycle",
                                decision.rule_id, decision.team, ask, american_odds(ask), problem,
                            )
                            continue

                        executor.execute(
                            decision,
                            ticker=resolved.ticker,
                            side=resolved.side,
                            yes_price_cents=ask,
                            exchange_index=market.get("exchange_index"),
                            open_position_count=get_open_position_count(kalshi),
                            realized_pnl_today_usd=0.0,  # TODO: wire up settlement P&L once daily_loss_cap is enabled
                        )
                    except Exception as e:
                        # Not recorded as fired, so it retries next cycle.
                        # Kalshi explains rejections (e.g. insufficient
                        # balance) in the response body, so show it.
                        body = getattr(getattr(e, "response", None), "text", "") or ""
                        logger.exception(
                            "Kalshi step failed for %s (%s) -- will retry next cycle %s",
                            decision.rule_id, decision.team, body[:300],
                        )
                        key = (decision.game_id, decision.rule_id, _kalshi_error_code(body))
                        if not dry_run and key not in notified_failures:
                            notified_failures.add(key)
                            notify.send(
                                f"Bet failed: {decision.team}",
                                f"{decision.rule_id}: {key[2]} -- the bot will keep retrying"
                                + (" (add funds to that market's exchange)" if key[2] == "insufficient_balance" else ""),
                                tags=["warning"], priority=4,
                            )

        settle_finished_bets(kalshi, ledger)
        logger.info("Poll cycle done: %s", live_counts or "no sports queried successfully")
        time.sleep(poll_interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="KalshiBot")
    parser.add_argument("--interval", type=int, default=30, help="poll interval in seconds")
    parser.add_argument(
        "--live",
        action="store_true",
        help="allow real orders (also requires TRADING_ENABLED=true env var)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use Kalshi's demo/sandbox environment instead of production",
    )
    args = parser.parse_args()
    run(poll_interval_s=args.interval, dry_run=not args.live, demo=args.demo)


if __name__ == "__main__":
    main()
