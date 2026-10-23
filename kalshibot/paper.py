"""Paper rules (config/paper_rules.yaml): bets the bot tracks but never places.

Nothing here can place an order -- this module has no executor and only
reads Kalshi. Each paper bet is saved to state/paper_bets.json at the live
YES ask, then settled from the market's result, so a rule can build a live
record before it is trusted with money.

mlb_underdog_runline: the pregame underdog's win chance (sportsbooks' live
odds) is up 15+ points from pregame by the 5th inning -> YES on its lowest
"wins by over N.5 runs" line priced 10-29c. One per game.

ev_moneyline (kind: ev_moneyline): +EV line shopping with Kalshi as the
book. A team's Kalshi YES ask, plus Kalshi's fee, vs its de-vigged sharp
win chance (snapshot.fair_win_chance: Pinnacle, else the consensus). A
paper bet when the expected return is at least min_ev; one per team per
game per phase (pregame / live). Every comparison is also appended to
state/ev_observations.jsonl, to study the gaps later.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .executor import american_odds, yes_ask_cents
from .kalshi_client import KalshiClient
from .matching import MarketMatcher
from .odds_providers.base import GameSnapshot

logger = logging.getLogger(__name__)

# A game whose Kalshi event can't be found is retried after this long.
RESOLVE_RETRY_S = 30 * 60
_RUNS_RE = re.compile(r"over\s+([\d.]+)", re.I)


def win_chance(american: int) -> float:
    """American odds -> implied win chance, 0-1 (vig not removed)."""
    return 100 / (american + 100) if american > 0 else -american / (-american + 100)


def _cents(dollars: Any) -> int | None:
    try:
        return round(float(dollars) * 100)
    except (TypeError, ValueError):
        return None


def kalshi_fee(contracts: int, price_cents: int) -> float:
    """Kalshi's taker fee: 7% x contracts x P x (1-P), rounded up to the cent."""
    p = price_cents / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100


@dataclass(frozen=True)
class RunlineRule:
    id: str
    sport: str
    summary: str
    dog_gain_points: float
    max_inning: int
    kalshi_band: tuple[int, int]
    min_runs_over: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunlineRule":
        lo, hi = d["kalshi_cents"]
        return cls(id=d["id"], sport=d.get("sport", "mlb"), summary=" ".join(str(d.get("summary", "")).split()),
                   dog_gain_points=float(d.get("dog_gain_points", 15)), max_inning=int(d.get("max_inning", 5)),
                   kalshi_band=(int(lo), int(hi)), min_runs_over=float(d.get("min_runs_over", 2.5)))


@dataclass(frozen=True)
class EvRule:
    id: str
    sports: tuple[str, ...]
    summary: str
    min_ev: float                  # expected return per $1 staked, after fees (0.03 = 3%)
    kalshi_band: tuple[int, int]
    pregame_hours: float           # watch upcoming games starting within this many hours

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvRule":
        lo, hi = d.get("kalshi_cents", [10, 90])
        return cls(id=d["id"], sports=tuple(d["sports"]), summary=" ".join(str(d.get("summary", "")).split()),
                   min_ev=float(d.get("min_ev", 0.03)), kalshi_band=(int(lo), int(hi)),
                   pregame_hours=float(d.get("pregame_hours", 3)))


def expected_return(fair: float, ask_cents: int) -> float:
    """Expected profit per $1 staked buying YES at ask_cents when it wins with
    chance `fair`, after Kalshi's taker fee."""
    price = ask_cents / 100
    fee = 0.07 * price * (1 - price)
    return (fair - price - fee) / price


def load_paper_rules(path: str | Path = "config/paper_rules.yaml") -> tuple[list[RunlineRule | EvRule], float]:
    p = Path(path)
    if not p.exists():
        return [], 25.0
    raw = yaml.safe_load(p.read_text()) or {}
    rules: list[RunlineRule | EvRule] = []
    for r in raw.get("rules", []):
        if r.get("enabled", True) is False:
            continue
        rules.append(EvRule.from_dict(r) if r.get("kind") == "ev_moneyline" else RunlineRule.from_dict(r))
    return rules, float(raw.get("stake_usd", 25))


class PaperBook:
    """state/paper_bets.json: a list of paper bets, open until their market settles."""

    def __init__(self, path: str | Path = "state/paper_bets.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._bets: list[dict[str, Any]] = json.loads(self._path.read_text()) if self._path.exists() else []

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._bets, indent=2))
        os.replace(tmp, self._path)

    def all_bets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(b) for b in self._bets]

    def has(self, game_id: str, rule_id: str) -> bool:
        with self._lock:
            return any(b["game_id"] == game_id and b["rule"] == rule_id for b in self._bets)

    def count(self, game_prefix: str, rule_id: str) -> int:
        with self._lock:
            return sum(b["game_id"].startswith(game_prefix) and b["rule"] == rule_id for b in self._bets)

    def add(self, bet: dict[str, Any]) -> None:
        with self._lock:
            self._bets.append(bet)
            self._save()

    def settle(self, kalshi: KalshiClient) -> None:
        """Mark open paper bets won/lost from their market's result."""
        with self._lock:
            open_bets = [b for b in self._bets if b["status"] == "open"]
        changed = False
        for bet in open_bets:
            try:
                market = kalshi.get_market(bet["ticker"]).get("market", {})
            except Exception as e:
                logger.warning("Couldn't check paper bet %s: %s", bet["ticker"], e)
                continue
            result = market.get("result")
            if result not in ("yes", "no"):
                continue
            with self._lock:
                bet = next(b for b in self._bets if b["ticker"] == bet["ticker"] and b["placed_at"] == bet["placed_at"])
                bet["status"] = "won" if result == "yes" else "lost"
                bet["pnl"] = round((bet["contracts"] if result == "yes" else 0) - bet["cost"] - bet["fee"], 2)
                changed = True
            logger.info("PAPER %s: %s %s (%+.2f USD) -- paper record %s", bet["status"].upper(), bet["rule"],
                        bet["label"], bet["pnl"], self.record_line())
        if changed:
            with self._lock:
                self._save()

    def record_line(self) -> str:
        settled = [b for b in self._bets if b["status"] in ("won", "lost")]
        wins = sum(b["status"] == "won" for b in settled)
        net = sum(b["pnl"] for b in settled)
        return f"{wins}-{len(settled) - wins}, {net:+.2f} USD, {len(self._bets) - len(settled)} open"


def runline_markets(kalshi: KalshiClient, game_event_ticker: str, team_code: str) -> list[tuple[float, dict[str, Any]]]:
    """Open "<team> wins by over N.5 runs" markets for this game: [(N.5, market)], lowest first."""
    series, suffix = game_event_ticker.split("-", 1)
    event = series.replace("GAME", "SPREAD") + "-" + suffix
    out = []
    for m in kalshi.iter_markets(event_ticker=event, status="open"):
        seg = str(m.get("ticker", "")).rsplit("-", 1)[-1]
        found = _RUNS_RE.search(str(m.get("yes_sub_title") or ""))
        if found and re.fullmatch(re.escape(team_code) + r"\d+", seg):
            out.append((float(found.group(1)), m))
    return sorted(out, key=lambda x: x[0])


class PaperTrader:
    def __init__(self, kalshi: KalshiClient, matcher: MarketMatcher, rules: list[RunlineRule | EvRule],
                 stake_usd: float, book: PaperBook | None = None,
                 observations: str | Path = "state/ev_observations.jsonl"):
        self._kalshi = kalshi
        self._matcher = matcher
        self.rules = rules
        self._stake = stake_usd
        self.book = book or PaperBook()
        self._events: dict[str, tuple[dict[str, Any] | None, float]] = {}
        self._observations = Path(observations)

    def _event(self, snapshot: GameSnapshot) -> dict[str, Any] | None:
        hit = self._events.get(snapshot.game_id)
        if hit is not None and (hit[0] is not None or time.time() - hit[1] < RESOLVE_RETRY_S):
            return hit[0]
        event = self._matcher.resolve_event(snapshot)
        self._events[snapshot.game_id] = (event, time.time())
        return event

    def _paper_bet(self, rule_id: str, key: str, snapshot: GameSnapshot, market: dict[str, Any], label: str,
                   ask: int, **extra: Any) -> dict[str, Any]:
        contracts = int(self._stake * 100 // ask)
        bet = dict(rule=rule_id, game_id=key, game=f"{snapshot.away_team} @ {snapshot.home_team}",
                   ticker=market["ticker"], label=label, price_cents=ask, contracts=contracts,
                   cost=round(contracts * ask / 100, 2), fee=kalshi_fee(contracts, ask), inning=snapshot.period,
                   score=f"{snapshot.away_score}-{snapshot.home_score}",
                   placed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), status="open", pnl=None, **extra)
        self.book.add(bet)
        return bet

    def check_ev(self, rule: EvRule, snapshot: GameSnapshot) -> None:
        if snapshot.sport not in rule.sports or not snapshot.fair_win_chance or snapshot.is_final:
            return
        if not snapshot.started:
            try:
                start = datetime.fromisoformat(snapshot.start_time_utc.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return
            if start - time.time() > rule.pregame_hours * 3600:
                return
        phase = "live" if snapshot.started else "pregame"
        event = self._event(snapshot)
        if event is None:
            return
        lo, hi = rule.kalshi_band
        for side in ("home", "away"):
            market = self._kalshi.get_market(str(event[side]["ticker"])).get("market", {})
            ask = yes_ask_cents(market)
            if ask is None:
                continue
            fair = snapshot.fair_win_chance[side]
            ev = expected_return(fair, ask)
            team = snapshot.home_team if side == "home" else snapshot.away_team
            self._observe(dict(ts=int(time.time()), rule=rule.id, sport=snapshot.sport, game_id=snapshot.game_id,
                               game=f"{snapshot.away_team} @ {snapshot.home_team}", phase=phase, side=side, team=team,
                               ticker=market.get("ticker"), ask=ask, bid=_cents(market.get("yes_bid_dollars")),
                               fair=round(fair, 4), source=snapshot.fair_source, ev=round(ev, 4),
                               period=snapshot.period, score=f"{snapshot.away_score}-{snapshot.home_score}"))
            key = f"{snapshot.game_id}|{side}|{phase}"
            if ev < rule.min_ev or not lo <= ask <= hi or self.book.has(key, rule.id):
                continue
            bet = self._paper_bet(rule.id, key, snapshot, market, f"{team} wins ({phase})", ask,
                                  fair_cents=round(100 * fair, 1), ev=round(ev, 4), source=snapshot.fair_source)
            logger.info("PAPER BET (no order placed): %s -- %s, %s at Kalshi %d¢ (%+d); fair %.1f%% from %s, "
                        "expected return %+.1f%%", rule.id, bet["game"], bet["label"], ask, american_odds(ask),
                        100 * fair, snapshot.fair_source, 100 * ev)

    def _observe(self, row: dict[str, Any]) -> None:
        try:
            self._observations.parent.mkdir(parents=True, exist_ok=True)
            with self._observations.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            logger.warning("Couldn't save EV observation: %s", e)

    def check(self, snapshot: GameSnapshot) -> None:
        for rule in self.rules:
            if isinstance(rule, EvRule):
                self.check_ev(rule, snapshot)
                continue
            if snapshot.sport != rule.sport or self.book.has(snapshot.game_id, rule.id):
                continue
            if snapshot.period is None or snapshot.period > rule.max_inning or snapshot.pregame_favorite_odds is None:
                continue
            gain = 100 * (win_chance(snapshot.pregame_favorite_odds) - win_chance(snapshot.live_favorite_odds))
            if gain < rule.dog_gain_points:
                continue
            event = self._event(snapshot)
            if event is None:
                continue
            dog_side = "away" if snapshot.favorite_team == snapshot.home_team else "home"
            dog = event[dog_side]
            dog_code = str(dog["ticker"]).rsplit("-", 1)[-1]
            lo, hi = rule.kalshi_band
            for runs, m in runline_markets(self._kalshi, str(dog["event_ticker"]), dog_code):
                ask = yes_ask_cents(m)
                if runs < rule.min_runs_over or ask is None or not lo <= ask <= hi:
                    continue
                team = snapshot.away_team if dog_side == "away" else snapshot.home_team
                label = f"{team} wins by over {runs:g} runs"
                bet = self._paper_bet(rule.id, snapshot.game_id, snapshot, m, label, ask, dog_gain_points=round(gain, 1))
                logger.info("PAPER BET (no order placed): %s -- %s, %s at Kalshi %d¢ (%+d); underdog's win chance "
                            "up %.0f points, inning %s, score %s", rule.id, bet["game"], label, ask,
                            american_odds(ask), gain, snapshot.period, bet["score"])
                break

    def run(self, live_games: list[GameSnapshot], upcoming: list[GameSnapshot] = ()) -> None:
        for snapshot in list(live_games) + list(upcoming):
            try:
                self.check(snapshot)
            except Exception:
                logger.exception("Paper rule check failed for %s @ %s", snapshot.away_team, snapshot.home_team)
        self.book.settle(self._kalshi)
