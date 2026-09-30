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
    trigger_quarter: int | None      # exact-match period (e.g. NFL "in the 4th quarter")
    trigger_period_min: int | None   # minimum period, inclusive (e.g. MLB "past the 6th inning")
    trigger_scores: frozenset[tuple[int, int]] | None  # allowed (favorite, underdog) scores
    odds_beyond_invalidate: int | None
    trailing_goal_invalidate: bool
    late_period: int | None          # final regulation period (e.g. NHL 3rd)...
    late_minutes: float | None       # ...and how many minutes left counts as "too late"
    requires_prior_rule: str | None
    max_bets_per_game: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        pregame = d.get("pregame", {})
        trigger = d.get("trigger", {})
        invalidate = d.get("invalidate_if", {}) or {}
        scores = trigger.get("favorite_trailing_scores")
        late = invalidate.get("late_in_game") or {}
        return cls(
            id=d["id"],
            sport=d["sport"],
            pregame_min=pregame.get("min_odds"),
            pregame_max=pregame.get("max_odds"),
            trigger_min=trigger.get("min_odds"),
            trigger_max=trigger.get("max_odds"),
            trigger_exclusive_min=bool(trigger.get("exclusive_min", False)),
            trigger_quarter=trigger.get("quarter"),
            trigger_period_min=trigger.get("period_min"),
            trigger_scores=frozenset(tuple(s) for s in scores) if scores else None,
            odds_beyond_invalidate=invalidate.get("odds_beyond"),
            trailing_goal_invalidate=bool(invalidate.get("trailing_by_goal_after_halftime", False)),
            late_period=late.get("period"),
            late_minutes=late.get("minutes_remaining"),
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
    if rule.trigger_period_min is not None:
        if snapshot.period is None or snapshot.period < rule.trigger_period_min:
            return False
    if rule.trigger_scores is not None:
        if _favorite_and_underdog_score(snapshot) not in rule.trigger_scores:
            return False
    return True


def _favorite_and_underdog_score(snapshot: GameSnapshot) -> tuple[int, int] | None:
    if snapshot.home_score is None or snapshot.away_score is None:
        return None
    if snapshot.favorite_team == snapshot.home_team:
        return snapshot.home_score, snapshot.away_score
    return snapshot.away_score, snapshot.home_score


def _favorite_trailing_by_goal_after_half(snapshot: GameSnapshot) -> bool:
    if not snapshot.past_halftime:
        return False
    scores = _favorite_and_underdog_score(snapshot)
    return scores is not None and scores[1] - scores[0] >= 1


def _in_late_game(rule: Rule, snapshot: GameSnapshot) -> bool:
    # Unknown period (overtime reports as "ot", which has no number) or an
    # unreadable clock in the final period both count as "too late" --
    # skipping a bet is the safe failure here.
    if snapshot.period is None or snapshot.period > rule.late_period:
        return True
    if snapshot.period < rule.late_period:
        return False
    return snapshot.seconds_remaining is None or snapshot.seconds_remaining <= rule.late_minutes * 60


def _invalidated(rule: Rule, snapshot: GameSnapshot) -> bool:
    if rule.odds_beyond_invalidate is not None and snapshot.live_favorite_odds > rule.odds_beyond_invalidate:
        return True
    if rule.trailing_goal_invalidate and _favorite_trailing_by_goal_after_half(snapshot):
        return True
    if rule.late_period is not None and _in_late_game(rule, snapshot):
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
