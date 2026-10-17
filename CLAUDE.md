# KalshiBot -- notes for Claude

Automated Kalshi sports bot that bets with REAL MONEY. Flat $25 per bet
(`config/risk.yaml`). Rules live in `config/rules.yaml`; each rule has a
plain-English `summary`. Start with README.md.

## Running it
- `./start.sh` = pull, install, run live (`python -m kalshibot.main --interval 300 --live`).
  `./start.sh --dry` logs bets without placing them.
- Run the bot on ONE computer at a time (Windows PC or MacBook), or bets
  can be placed twice. Before starting it, make sure it isn't already
  running in another window.
- Ask the owner before: starting/restarting the live bot, moving money
  (`diagnose transfer`), resetting the record, or changing stake/rules.
- Logs: `logs/kalshibot.log`. Bet record: `state/ledger.json`.
- Results site: https://akapyre.github.io/kalshi-bot-results/
  (`python -m kalshibot.diagnose site` republishes it now).

## Useful checks
`python -m kalshibot.diagnose <sport|markets <sport>|props|account|record|site|notify|leagues>`
`python -m kalshibot.backtest <sport> --days 400`, `python -m kalshibot.sweep <sport>`
Tests: `python -m pytest -q tests` (the repo's skool_bot/ tests are separate).

## Kalshi facts learned the hard way
- Orders: V2 `POST /portfolio/events/orders` (V1 returns 410); needs
  `self_trade_prevention_type` and the market's `exchange_index`.
- Money is split by exchange: tennis and MLB markets are on exchange 3,
  everything else on 0. "insufficient_balance" on a tennis/MLB bet means
  exchange 3 needs funds: `python -m kalshibot.diagnose transfer 3 <dollars>`.
  (Automatic top-up has NOT been approved by the owner.)
- NHL player props: KXNHLPTS / KXNHLAST / KXNHLGOAL, markets like
  "Cale Makar: 1+". No shots-on-goal props on Kalshi. Books are thin.

## Never
- Commit or print `.env`, the `.pem` key, or tokens.
- Rebase or force-push branch `claude/fervent-ramanujan-4dmrfn`: it's
  shared with the owner's Skool bot (`skool_bot/`). Pull/merge first.
