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
from typing import Any

from . import notify
from .kalshi_client import KalshiClient
from .ledger import BetLedger, record_line
from .risk import RiskManager
from .rules_engine import BetDecision
from .state import BetStateStore

logger = logging.getLogger("kalshibot.executor")


def yes_ask_cents(market: dict[str, Any]) -> int | None:
    """Cheapest price someone will sell YES at right now, in cents.

    Kalshi quotes prices as dollar strings ("yes_ask_dollars": "0.5600") and,
    on older responses, as integer cents ("yes_ask": 56). None when there is
    no seller -- the caller must not substitute a guess.
    """
    raw = market.get("yes_ask_dollars")
    if raw not in (None, ""):
        try:
            cents = round(float(raw) * 100)
        except (TypeError, ValueError):
            cents = None
    else:
        cents = market.get("yes_ask")
    if not isinstance(cents, int) or not 1 <= cents <= 99:
        return None
    return cents


def american_odds(price_cents: int) -> int:
    """Kalshi price -> equivalent American odds, for readable logs."""
    p = price_cents / 100
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def order_cost(order: dict[str, Any]) -> tuple[float | None, float | None]:
    """(cost, fees) in dollars from a Kalshi order record, if present."""
    order = order.get("order", order)

    def total(*keys: str) -> float | None:
        vals = [order.get(k) for k in keys if order.get(k) not in (None, "")]
        try:
            return sum(float(v) for v in vals) if vals else None
        except (TypeError, ValueError):
            return None

    return (total("taker_fill_cost_dollars", "maker_fill_cost_dollars"),
            total("taker_fees_dollars", "maker_fees_dollars"))


def _filled_count(response: dict[str, Any]) -> float | None:
    """Contracts filled, wherever this response version puts it (top level
    or under "order", as "fill_count" or "fill_count_fp"). None if absent."""
    for source in (response, response.get("order") or {}):
        for key in ("fill_count_fp", "fill_count"):
            if source.get(key) not in (None, ""):
                try:
                    return float(source[key])
                except (TypeError, ValueError):
                    return None
    return None


class Executor:
    def __init__(
        self,
        kalshi_client: KalshiClient,
        risk_manager: RiskManager,
        state_store: BetStateStore,
        dry_run: bool,
        ledger: BetLedger | None = None,
    ):
        self._kalshi = kalshi_client
        self._risk = risk_manager
        self._state = state_store
        self._dry_run = dry_run
        self._ledger = ledger

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
        exchange_index: int | None = None,
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
        # Whole contracts that fit inside the stake -- never spend more than it.
        count = int((stake * 100) // price) if price > 0 else 0
        if count < 1:
            logger.warning("SKIPPED %s: %d¢ price leaves no contracts for $%.2f", ticker, price, stake)
            return

        live = self._dry_run is False and self._trading_enabled()
        prefix = "SENDING LIVE ORDER" if live else "DRY-RUN"
        logger.info(
            "%s: rule=%s game=%s team=%s ticker=%s side=%s price=%d¢ (%+d) "
            "count=%d cost=$%.2f",
            prefix, decision.rule_id, decision.game_id, decision.team, ticker, side,
            price, american_odds(price), count, count * price / 100,
        )

        if live:
            # Limit at the current ask, filled immediately or not at all, so
            # a thin book can never fill us at a worse price than we logged.
            response = self._kalshi.create_order(
                ticker=ticker,
                count=count,
                price_dollars=f"{yes_price_cents / 100:.4f}",
                side="bid",   # buy YES -- the bot never buys NO
                exchange_index=exchange_index,
                time_in_force="immediate_or_cancel",
            )
            filled = _filled_count(response)
            if filled == 0:
                # Price moved away before we got there. Not recorded, so the
                # rule is checked again next cycle.
                logger.warning("LIVE ORDER on %s did not fill -- will retry next cycle", ticker)
                return
            logger.info("LIVE ORDER filled: %s contracts on %s", "?" if filled is None else filled, ticker)
            self._log_bet(decision, ticker, response, filled if filled is not None else count, price)

        # Record the rule as fired regardless of live/dry-run so the bet-state
        # machine (single-bet-per-game, NFL re-entry) behaves identically in
        # both modes -- dry-run should mirror live decisioning exactly.
        self._state.record(decision.game_id, decision.rule_id)

    def _log_bet(self, decision: BetDecision, ticker: str, response: dict[str, Any],
                 contracts: float, price_cents: int) -> None:
        """Add a filled bet to the win/loss ledger and tell the phone. Never
        lets a bookkeeping problem undo or repeat the bet itself."""
        try:
            order_id = response.get("order_id") or response.get("order", {}).get("order_id") or ""
            cost, fee = None, None
            if order_id:
                try:
                    cost, fee = order_cost(self._kalshi.get_order(order_id))
                except Exception as e:
                    logger.warning("Couldn't read the order's exact cost (%s); estimating it", e)
            if cost is None:
                cost = contracts * price_cents / 100
            summary = None
            if self._ledger is not None and order_id:
                self._ledger.record(order_id=order_id, ticker=ticker, team=decision.team,
                                    sport=decision.sport, rule=decision.rule_id,
                                    contracts=contracts, cost=cost, fee=fee)
                summary = self._ledger.summary()
            notify.send(
                f"Bet placed: {decision.team}",
                f"{contracts:g} contracts at {price_cents}¢ ({american_odds(price_cents):+d}) = ${cost:.2f}"
                f" · {decision.sport.upper()}" + (f"\n{record_line(summary)}" if summary else ""),
                tags=["moneybag"],
            )
        except Exception:
            logger.exception("Bet on %s filled but couldn't be added to the ledger", ticker)
