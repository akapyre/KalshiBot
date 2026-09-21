"""Evaluates config/rules.yaml against live GameSnapshots and produces
BetDecisions. Pure logic, no I/O -- keeps this testable without live APIs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .odds_providers.base import GameSnapshot
from .state import BetStateStore


@dataclass
class Rule:
    id: str
    sport: str
    pregame_min: int | None
    pregame_max: int | None
    trigger_min: int | None
    trigger_max: int | None
    trigger_exclusive_min: bool
    trigger_quarter: int | None
    odds_beyond_invalidate: int | None
    trailing_goal_invalidate: bool
    requires_prior_rule: str | None
    max_bets_per_game: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        pregame = d.get("pregame", {})
        trigger = d.get("trigger", {})
        invalidate = d.get("invalidate_if", {}) or {}
        return cls(
            id=d["id"],
            sport=d["sport"],
            pregame_min=pregame.get("min_odds"),
            pregame_max=pregame.get("max_odds"),
            trigger_min=trigger.get("min_odds"),
            trigger_max=trigger.get("max_odds"),
            trigger_exclusive_min=bool(trigger.get("exclusive_min", False)),
            trigger_quarter=trigger.get("quarter"),
            odds_beyond_invalidate=invalidate.get("odds_beyond"),
            trailing_goal_invalidate=bool(invalidate.get("trailing_by_goal_after_halftime", False)),
            requires_prior_rule=d.get("requires_prior_rule"),
            max_bets_per_game=int(d.get("max_bets_per_game", 1)),
        )


@dataclass
class BetDecision:
    rule_id: str
    game_id: str
    sport: str
    team: str
    live_odds: int
    stake_usd: float


def load_rules(path: str | Path = "config/rules.yaml") -> tuple[list[Rule], float]:
    raw = yaml.safe_load(Path(path).read_text())
    rules = [Rule.from_dict(r) for r in raw["rules"]]
    return rules, float(raw.get("stake_usd", 15))


def _pregame_ok(rule: Rule, pregame_odds: int) -> bool:
    if rule.pregame_min is not None and pregame_odds < rule.pregame_min:
        return False
    if rule.pregame_max is not None and pregame_odds > rule.pregame_max:
        return False
    return True


def _trigger_ok(rule: Rule, snapshot: GameSnapshot) -> bool:
    odds = snapshot.live_favorite_odds
    if rule.trigger_min is not None:
        if rule.trigger_exclusive_min:
            if not odds > rule.trigger_min:
                return False
        elif odds < rule.trigger_min:
            return False
    if rule.trigger_max is not None and odds > rule.trigger_max:
        return False
    if rule.trigger_quarter is not None and snapshot.period != rule.trigger_quarter:
        return False
    return True


def _favorite_trailing_by_goal_after_half(snapshot: GameSnapshot) -> bool:
    if not snapshot.past_halftime:
        return False
    if snapshot.home_score is None or snapshot.away_score is None:
        return False
    favorite_is_home = snapshot.favorite_team == snapshot.home_team
    favorite_score = snapshot.home_score if favorite_is_home else snapshot.away_score
    opponent_score = snapshot.away_score if favorite_is_home else snapshot.home_score
    return opponent_score - favorite_score >= 1


def _invalidated(rule: Rule, snapshot: GameSnapshot) -> bool:
    if rule.odds_beyond_invalidate is not None and snapshot.live_favorite_odds > rule.odds_beyond_invalidate:
        return True
    if rule.trailing_goal_invalidate and _favorite_trailing_by_goal_after_half(snapshot):
        return True
    return False


class RulesEngine:
    def __init__(
        self,
        rules: list[Rule],
        stake_usd: float,
        state_store: BetStateStore,
    ):
        self._rules = rules
        self._stake_usd = stake_usd
        self._state = state_store

    def evaluate(self, snapshot: GameSnapshot) -> list[BetDecision]:
        decisions: list[BetDecision] = []
        for rule in self._rules:
            if rule.sport != snapshot.sport:
                continue
            if snapshot.pregame_favorite_odds is None:
                continue
            if not _pregame_ok(rule, snapshot.pregame_favorite_odds):
                continue

            fired = self._state.fired_rules(snapshot.game_id)
            if rule.id in fired:
                continue  # this exact rule already fired for this game
            if len(fired) >= rule.max_bets_per_game:
                continue  # already at (or past) this rule's total-bets ceiling
            if rule.requires_prior_rule and rule.requires_prior_rule not in fired:
                continue  # e.g. NFL Q4 re-entry needs nfl_favorite_fade to have fired first

            if _invalidated(rule, snapshot):
                continue
            if not _trigger_ok(rule, snapshot):
                continue

            decisions.append(
                BetDecision(
                    rule_id=rule.id,
                    game_id=snapshot.game_id,
                    sport=snapshot.sport,
                    team=snapshot.favorite_team,
                    live_odds=snapshot.live_favorite_odds,
                    stake_usd=self._stake_usd,
                )
            )
        return decisions
