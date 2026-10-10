#!/usr/bin/env bash
# Start KalshiBot with one command, on a Mac (Terminal) or Windows (Git Bash):
#   ./start.sh          real bets (needs TRADING_ENABLED=true in .env)
#   ./start.sh --dry    log only, no real bets
# It pulls the latest code, activates the virtual environment, installs any
# new requirements, and on a Mac keeps the computer awake while it runs.
set -e
cd "$(dirname "$0")"

git pull --ff-only origin claude/fervent-ramanujan-4dmrfn || echo "(Couldn't update -- starting with the code already here.)"

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate          # Mac
else
  source .venv/Scripts/activate      # Windows Git Bash
fi
pip install -q -r requirements.txt

ARGS="--interval 300 --live"
[ "$1" = "--dry" ] && ARGS="--interval 300"

if command -v caffeinate >/dev/null 2>&1; then
  # macOS: stop idle sleep while the bot runs (closing the lid still sleeps).
  exec caffeinate -i python -m kalshibot.main $ARGS
else
  exec python -m kalshibot.main $ARGS
fi
