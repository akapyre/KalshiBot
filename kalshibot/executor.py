"""Turns a BetDecision (+ resolved Kalshi ticker/side) into either a logged
dry-run line or a real order. Two independent gates before any live order:

  1. `dry_run=True` (constructor arg, set from CLI flag / config)
  2. TRADING_ENABLED env var (risk.yaml's trading_enabled_env_var) must be
     the literal string "true"

Both must clear for a live order to go out. Either one blocking it just logs
what *would* have happened, same as dry-run.
"""
from __future__ import annotations

import logging
import os

from .kalshi_client import KalshiClient
from .risk import RiskManager
from .rules_engine import BetDecision
from .state import BetStateStore

logger = logging.getLogger("kalshibot.executor")


class Executor:
    def __init__(
        self,
        kalshi_client: KalshiClient,
        risk_manager: RiskManager,
        state_store: BetStateStore,
        dry_run: bool,
    ):
        self._kalshi = kalshi_client
        self._risk = risk_manager
        self._state = state_store
        self._dry_run = dry_run

    def _trading_enabled(self) -> bool:
        env_var = self._risk.config.trading_enabled_env_var
        return os.environ.get(env_var, "false").strip().lower() == "true"

    def execute(
        self,
        decision: BetDecision,
        *,
        ticker: str,
        side: str,            # "yes" or "no"
        yes_price_cents: int,
        open_position_count: int,
        realized_pnl_today_usd: float,
    ) -> None:
        allowed, reason = self._risk.allow(
            open_position_count=open_position_count,
            realized_pnl_today_usd=realized_pnl_today_usd,
        )
        if not allowed:
            logger.warning(
                "SKIPPED %s on %s (%s): risk gate blocked -- %s",
                decision.rule_id, decision.game_id, decision.team, reason,
            )
            return

        stake = self._risk.stake_usd()
        price = yes_price_cents if side == "yes" else 100 - yes_price_cents
        count = max(1, round((stake * 100) / price)) if price > 0 else 0

        live = self._dry_run is False and self._trading_enabled()
        prefix = "LIVE ORDER" if live else "DRY-RUN"
        logger.info(
            "%s: rule=%s game=%s team=%s live_odds=%s ticker=%s side=%s "
            "price=%d¢ count=%d stake=$%.2f",
            prefix, decision.rule_id, decision.game_id, decision.team,
            decision.live_odds, ticker, side, price, count, stake,
        )

        if live:
            self._kalshi.create_order(
                ticker=ticker,
                side=side,
                action="buy",
                count=count,
                order_type="market",
            )

        # Record the rule as fired regardless of live/dry-run so the bet-state
        # machine (single-bet-per-game, NFL re-entry) behaves identically in
        # both modes -- dry-run should mirror live decisioning exactly.
        self._state.record(decision.game_id, decision.rule_id)
