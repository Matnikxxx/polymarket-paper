#!/bin/bash
# one strategy cycle: settle finished bets, then act on the current hour.
cd "$(dirname "$0")" || exit 1
echo "===== cycle $(date -u +%FT%TZ) =====" >> cycle.log
/opt/homebrew/bin/python3 paper_trader.py resolve >> cycle.log 2>&1
/opt/homebrew/bin/python3 paper_trader.py tick    >> cycle.log 2>&1
