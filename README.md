# KalshiBot

Automated wager bot for Kalshi (CFTC-regulated event-contract exchange). Polls
live sports odds/game-state, evaluates your fixed rule set, and places (or, in
dry-run, logs) a flat-stake order when a rule triggers.

**This places real money with no daily loss cap and no max-concurrent-position
limit configured. Read "Before you flip it live" below before you do.**

## How it's wired together

```
odds provider (live odds + score/period)
        |
        v
  RulesEngine  --(BetDecision)-->  MarketMatcher  --(ticker+side)-->  Executor
        ^                                                                |
        |                                                                v
  BetStateStore <----------------------------------------- records fired rule
```

- `config/rules.yaml` -- your rule set, translated to exact numeric
  comparisons. **Read the comments at the top of this file** -- several of
  your rules had a direction/threshold that could be read two ways in plain
  English, and I picked one reading for each. They're flagged inline.
- `config/risk.yaml` -- $15 flat stake; `daily_loss_cap_usd` and
  `max_concurrent_positions` are both `null` (disabled) per your instructions,
  wired up so you can set them later without touching code.
- `kalshibot/kalshi_client.py` -- Kalshi's RSA-PSS signed REST API.
- `kalshibot/odds_providers/sportsgameodds.py` -- live odds + game-state
  adapter for sportsgameodds.com. **Not verified against a live API key**
  (I don't have one) -- see the warning at the top of that file before
  trusting it past dry-run. (TheRundown's RapidAPI listing was tried
  first and dropped -- its odds values are masked placeholders regardless
  of plan tier.)
- `kalshibot/matching.py` -- maps a game to a Kalshi market ticker + side.
  Also unverified against live data; it logs and skips rather than guessing
  when it can't find exactly one confident match.
- `kalshibot/rules_engine.py` -- pure logic, no I/O, fully covered by
  `tests/test_rules_engine.py`.
- `kalshibot/executor.py` -- dry-run vs live gate + risk checks + order
  placement.
- `kalshibot/state.py` -- persists which rules have fired per game to
  `state/bets.json`, so a restart never causes a duplicate wager.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values below
```

1. **Kalshi API key**: Profile Settings -> Create New API Key at
   kalshi.com/account/profile. Save the private key PEM it gives you (shown
   once) to a file, point `KALSHI_PRIVATE_KEY_PATH` at it, and set
   `KALSHI_API_KEY_ID` to the key ID.
2. **Odds/game-state provider**: sign up at sportsgameodds.com (free tier
   works to start, real -- not masked -- odds, just delayed vs. their paid
   tiers), set `SPORTSGAMEODDS_API_KEY`. Swap providers entirely by
   implementing `OddsProvider` -- see `kalshibot/odds_providers/base.py`.
3. Fund your Kalshi account with however much you want the bot risking.

## Running

```bash
# Dry run (default): logs every trigger and would-be wager, places nothing.
python3 -m kalshibot.main

# Against Kalshi's demo/sandbox environment instead of production:
python3 -m kalshibot.main --demo

# Live orders -- requires BOTH --live AND TRADING_ENABLED=true in the environment:
TRADING_ENABLED=true python3 -m kalshibot.main --live
```

Run tests any time with `python3 -m pytest tests/ -v`.

## Before you flip it live

1. **Confirm the rule interpretations.** Read the top of `config/rules.yaml`.
   The soccer "falls below +200" line and the general "or below/or higher"
   phrasing throughout your rules are genuinely ambiguous in plain English --
   I picked the reading that seemed most consistent with the rest of the rule
   set, but you should check it against a few dry-run log lines before
   trusting it with money.
2. **Verify the two unverified integrations** (`sportsgameodds.py` field
   names and `LEAGUE_IDS`, `matching.py` ticker/title parsing) against real
   API responses -- I built these from public docs, not a live account, so
   the exact JSON shape is my best guess, not a tested fact.
3. **Watch dry-run through a handful of live games first.** It runs the exact
   same rules engine and state machine as live mode -- it just doesn't submit
   the order -- so what you see in the logs is what would have happened.
4. **No daily loss cap / no position limit means no automatic circuit
   breaker.** If the odds/game-state feed misbehaves (stale data, a
   mismatched market from `matching.py`) the bot will keep firing every poll
   cycle up to whatever `max_bets_per_game` allows per game, with no ceiling
   across games or across a bad day. The real emergency stop is killing the
   process (Ctrl+C) -- `TRADING_ENABLED` is only read at each order attempt
   from the *running process's* environment, so changing it in another shell
   won't affect an already-running bot; you'd need to stop and restart it
   with the new value.
5. Kalshi sports event contracts are legal, CFTC-regulated products, but
   confirm you're comfortable with the tax/reporting implications of
   automated trading before running this unattended.
