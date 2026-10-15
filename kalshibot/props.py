"""NHL player-prop rule: when a player is -200 or stronger to reach a line
at puck drop, bet the next LOWER line once Kalshi prices it at -140 or
longer.

Example: Connor McDavid "3+ shots on goal" is 70¢ (-233) at puck drop. His
"2+ shots" line is the alternative one step down. If, during the game,
"2+ shots" falls to 58¢ (-140) or less, buy YES on "2+ shots".

Everything is priced on Kalshi, where the bet is placed:
  - "the line he's -200 to hit" = the HIGHEST line on his Kalshi ladder
    whose price at puck drop was 67¢ (-200) or more. Kalshi lists each
    line as its own market ("2+", "3+", ...).
  - the bet = the line one below it, bought at Kalshi's YES ask.
  - a player whose -200 line is already the lowest one (e.g. "1+ points")
    has no lower line, so he is skipped.

How Kalshi's prop markets are found (NOT yet checked against live data --
`python -m kalshibot.diagnose props` shows what is found):
  - series: Kalshi Sports series whose ticker starts with KXNHL and whose
    name mentions the stat (points / shots), not team, period or total ones
  - market: one per player and line, named like "Connor McDavid: 2+" --
    the player name and line are read from yes_sub_title/title
  - game: the market's event date must equal the game's date AND the
    player must be on SportsGameOdds' roster for that game, so a market is
    never tied to the wrong game. Anything unclear is skipped.

The price at puck drop is the last price seen while the game was still
upcoming (the bot checks every few minutes), saved to state/ so a restart
keeps it. Started mid-game with nothing saved: that game's players are
skipped.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .executor import american_odds, yes_ask_cents
from .kalshi_client import KalshiClient
from .matching import _normalize, event_date, game_date
from .odds_providers.base import GameSnapshot

logger = logging.getLogger("kalshibot.props")

# Words in a Kalshi series' ticker/title that pick out each stat's
# player-prop series, and words that rule a series out.
STAT_SERIES: dict[str, re.Pattern] = {
    "points": re.compile(r"point|pts"),
    "shots": re.compile(r"shot|sog"),
}
SERIES_SKIP = re.compile(r"team|total|spread|period|1st|first|power|blocked|game$|wins|goalie|save")


@dataclass
class PropRule:
    id: str
    sport: str
    stats: list[str]
    pregame_max_odds: int          # the line he's -X to hit at puck drop, e.g. -200
    band: tuple[int, int]          # buy the lower line at this Kalshi ask, cents
    max_bets_per_game: int
    enabled: bool = True

    @property
    def pregame_min_cents(self) -> int:
        """Kalshi price equal to pregame_max_odds (-200 -> 67¢)."""
        o = -self.pregame_max_odds
        return -(-100 * o // (o + 100))   # ceil

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PropRule":
        return cls(
            id=d["id"], sport=d["sport"], stats=list(d.get("stats", [])),
            pregame_max_odds=int(d["pregame"]["max_odds"]),
            band=tuple(d["trigger"]["kalshi_cents"]),
            max_bets_per_game=int(d.get("max_bets_per_game", 1)),
            enabled=bool(d.get("enabled", True)),
        )


def load_prop_rules(path: str | Path = "config/rules.yaml") -> list[PropRule]:
    raw = yaml.safe_load(Path(path).read_text())
    return [PropRule.from_dict(r) for r in raw.get("rules", []) if r.get("kind") == "player_prop"]


@dataclass(frozen=True)
class PropMarket:
    stat: str
    player: str        # as Kalshi writes it
    line: int          # N in "N+"
    market: dict


_PLUS = re.compile(r"(\d+)\s*\+")
_OR_MORE = re.compile(r"(\d+)\s+or\s+more", re.IGNORECASE)
_OVER = re.compile(r"\bover\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_line(text: str) -> int | None:
    """"2+" / "2 or more" -> 2; "over 1.5" -> 2."""
    for pattern in (_PLUS, _OR_MORE):
        m = pattern.search(text)
        if m:
            return int(m.group(1))
    m = _OVER.search(text)
    return int(float(m.group(1))) + 1 if m else None


def parse_player(text: str) -> str | None:
    """"Connor McDavid: 2+" -> "Connor McDavid"; "Connor McDavid 2+ shots" too."""
    name = text.split(":")[0] if ":" in text else re.split(r"\s(?:over\s)?\d", text, maxsplit=1)[0]
    name = name.strip(" -")
    return name if len(name.split()) >= 2 else None


def parse_market(stat: str, market: dict[str, Any]) -> PropMarket | None:
    for field in ("yes_sub_title", "title", "subtitle"):
        text = str(market.get(field) or "")
        line, player = parse_line(text), parse_player(text)
        if line is not None and player:
            return PropMarket(stat, player, line, market)
    return None


def mid_cents(market: dict[str, Any]) -> int | None:
    """Fair price now: halfway between bid and ask, else the last trade."""
    def cents(key: str) -> float | None:
        raw = market.get(f"{key}_dollars")
        try:
            return float(raw) * 100 if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None
    bid, ask = cents("yes_bid"), cents("yes_ask")
    if bid and ask and 0 < bid <= ask < 100:
        return round((bid + ask) / 2)
    last = cents("last_price")
    return round(last) if last and 1 <= last <= 99 else None


class PropPregameStore:
    """Each prop market's price at puck drop, by ticker (state/prop_pregame.json)."""

    def __init__(self, path: str | Path = "state/prop_pregame.json"):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, int] = json.loads(self._path.read_text()) if self._path.exists() else {}

    def get(self, ticker: str) -> int | None:
        return self._data.get(ticker)

    def set_many(self, prices: dict[str, int]) -> None:
        changed = {t: p for t, p in prices.items() if self._data.get(t) != p}
        if not changed:
            return
        with self._lock:
            self._data.update(changed)
            self._path.write_text(json.dumps(self._data, indent=1, sort_keys=True))


@dataclass
class PropBet:
    rule: PropRule
    snapshot: GameSnapshot
    player: str
    stat: str
    main_line: int
    main_pregame_cents: int
    bet: PropMarket
    ask: int

    @property
    def label(self) -> str:
        return f"{self.player} {self.bet.line}+ {STAT_LABELS[self.stat]}"

    @property
    def key(self) -> str:
        """Bet-state id: one bet per player, stat and game."""
        return f"{self.snapshot.game_id}|{self.stat}|{_normalize(self.player)}"


STAT_LABELS = {"points": "points", "shots": "shots on goal"}


class PropWatcher:
    def __init__(self, kalshi: KalshiClient, rules: list[PropRule], store: PropPregameStore | None = None):
        self._kalshi = kalshi
        self.rules = [r for r in rules if r.enabled]
        self._store = store or PropPregameStore()
        self._series: list[tuple[str, str]] | None = None   # (stat, series ticker)
        self._warned: set[str] = set()

    def stats(self) -> set[str]:
        return {s for r in self.rules for s in r.stats}

    def series(self) -> list[tuple[str, str]]:
        """(stat, series ticker) for each Kalshi NHL player-prop series. Looked
        up once per run; logged so it can be checked."""
        if self._series is None:
            found = []
            for s in self._kalshi.list_series(category="Sports").get("series", []):
                ticker = str(s.get("ticker") or "")
                text = (ticker + " " + str(s.get("title") or "")).lower()
                if not ticker.upper().startswith("KXNHL") or SERIES_SKIP.search(text):
                    continue
                for stat, pattern in STAT_SERIES.items():
                    if stat in self.stats() and pattern.search(text):
                        found.append((stat, ticker))
                        break
            logger.info("Kalshi NHL prop series: %s", ", ".join(f"{t} ({s})" for s, t in found) or "none found")
            self._series = found
        return self._series

    def fetch(self) -> list[PropMarket]:
        out = []
        for stat, series in self.series():
            try:
                for m in self._kalshi.iter_markets(series_ticker=series, status="open"):
                    parsed = parse_market(stat, m)
                    if parsed is not None:
                        out.append(parsed)
            except Exception as e:
                logger.warning("Couldn't read Kalshi prop series %s: %s", series, e)
        return out

    @staticmethod
    def for_game(markets: list[PropMarket], snapshot: GameSnapshot) -> dict[tuple[str, str], dict[int, PropMarket]]:
        """This game's ladders: {(stat, player): {line: market}}. Only
        players on the game's roster, only markets dated the game's day."""
        roster = {_normalize(p) for p in snapshot.player_names}
        day = game_date(snapshot)
        ladders: dict[tuple[str, str], dict[int, list[PropMarket]]] = defaultdict(lambda: defaultdict(list))
        for pm in markets:
            if _normalize(pm.player) in roster and day is not None \
                    and event_date(str(pm.market.get("event_ticker") or "")) == day:
                ladders[(pm.stat, pm.player)][pm.line].append(pm)
        # A line listed twice is ambiguous -- drop it rather than guess.
        return {k: {line: ms[0] for line, ms in lines.items() if len(ms) == 1} for k, lines in ladders.items()}

    def record_pregame(self, markets: list[PropMarket], upcoming: list[GameSnapshot]) -> None:
        prices = {}
        for snap in upcoming:
            for ladder in self.for_game(markets, snap).values():
                for pm in ladder.values():
                    price = mid_cents(pm.market)
                    if price is not None:
                        prices[pm.market["ticker"]] = price
        self._store.set_many(prices)

    def candidates(self, markets: list[PropMarket], snapshot: GameSnapshot) -> list[PropBet]:
        """Every player whose lower line is in a rule's price range now."""
        out = []
        ladders = self.for_game(markets, snapshot)
        for rule in self.rules:
            if rule.sport != snapshot.sport:
                continue
            for (stat, player), ladder in ladders.items():
                if stat not in rule.stats:
                    continue
                pregame = {line: self._store.get(pm.market["ticker"]) for line, pm in ladder.items()}
                if all(p is None for p in pregame.values()):
                    warn = f"{snapshot.game_id}|{stat}|{player}"
                    if warn not in self._warned:
                        self._warned.add(warn)
                        logger.info("No puck-drop prices saved for %s %s (bot started mid-game?) -- skipped",
                                    player, STAT_LABELS[stat])
                    continue
                strong = [line for line, p in pregame.items() if p is not None and p >= rule.pregame_min_cents]
                if not strong:
                    continue
                main = max(strong)
                lower = ladder.get(main - 1)
                if lower is None:
                    continue   # already the lowest line (e.g. 1+ points)
                ask = yes_ask_cents(lower.market)
                lo, hi = rule.band
                if ask is not None and lo <= ask <= hi:
                    out.append(PropBet(rule, snapshot, player, stat, main, pregame[main], lower, ask))
        return out

    def describe_watch(self, markets: list[PropMarket], snapshot: GameSnapshot) -> list[str]:
        """Lines for the log/diagnose: each watched player's -200 line and the
        lower line's price now."""
        rows = []
        for rule in self.rules:
            for (stat, player), ladder in sorted(self.for_game(markets, snapshot).items()):
                if stat not in rule.stats:
                    continue
                pregame = {line: self._store.get(pm.market["ticker"]) for line, pm in ladder.items()}
                strong = [line for line, p in pregame.items() if p is not None and p >= rule.pregame_min_cents]
                if strong and max(strong) - 1 in ladder:
                    main = max(strong)
                    ask = yes_ask_cents(ladder[main - 1].market)
                    rows.append(f"{player} {main}+ {STAT_LABELS[stat]} was {pregame[main]}¢ "
                                f"({american_odds(pregame[main]):+d}); {main - 1}+ now "
                                + (f"{ask}¢ ({american_odds(ask):+d})" if ask else "no ask"))
        return rows
