"""Polling loop: pull live games -> evaluate rules -> match to a Kalshi
market -> execute (dry-run or live, per --dry-run / TRADING_ENABLED)."""
from __future__ import annotations

import argparse
import logging
import os
import time

from .executor import Executor
from .kalshi_client import DEFAULT_BASE_URL, DEMO_BASE_URL, KalshiClient, KalshiCredentials
from .matching import MarketMatcher
from .odds_providers.base import Sport
from .odds_providers.therundown import TheRundownProvider
from .risk import RiskConfig, RiskManager
from .rules_engine import RulesEngine, load_rules
from .state import BetStateStore

SPORTS: list[Sport] = ["soccer", "tennis", "nfl"]

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
    base_url = DEMO_BASE_URL if demo else DEFAULT_BASE_URL
    return KalshiClient(creds, base_url=base_url)


def get_open_position_count(kalshi: KalshiClient) -> int:
    try:
        positions = kalshi.get_positions()
        return len(positions.get("market_positions", []))
    except Exception:
        return 0


def run(poll_interval_s: int, dry_run: bool, demo: bool) -> None:
    kalshi = build_kalshi_client(demo)
    odds_provider = TheRundownProvider()
    matcher = MarketMatcher(kalshi)
    rules, stake_usd = load_rules()
    state_store = BetStateStore()
    risk = RiskManager(RiskConfig.load())
    engine = RulesEngine(rules, stake_usd, state_store)
    executor = Executor(kalshi, risk, state_store, dry_run=dry_run)

    logger.info(
        "KalshiBot starting: dry_run=%s demo_env=%s stake=$%.2f loss_cap=%s max_positions=%s",
        dry_run, demo, stake_usd, risk.config.daily_loss_cap_usd, risk.config.max_concurrent_positions,
    )

    while True:
        for sport in SPORTS:
            try:
                snapshots = odds_provider.list_live_games(sport)
            except Exception:
                logger.exception("Failed to fetch live games for sport=%s", sport)
                continue

            for snapshot in snapshots:
                decisions = engine.evaluate(snapshot)
                for decision in decisions:
                    resolved = matcher.resolve(snapshot)
                    if resolved is None:
                        continue  # already logged by matcher; never guess a ticker

                    market = kalshi.get_market(resolved.ticker)
                    yes_price = market.get("market", {}).get("yes_bid", 50)

                    executor.execute(
                        decision,
                        ticker=resolved.ticker,
                        side=resolved.side,
                        yes_price_cents=yes_price,
                        open_position_count=get_open_position_count(kalshi),
                        realized_pnl_today_usd=0.0,  # TODO: wire up settlement P&L once daily_loss_cap is enabled
                    )

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
