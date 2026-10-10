#!/usr/bin/env bash
# Entry point for cron / manual runs. flock prevents two runs overlapping.
cd "$(dirname "$0")" || exit 1
export DISPLAY="${DISPLAY:-:0}"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/bin:/usr/local/bin:$PATH"
mkdir -p logs data
source .venv/bin/activate
exec flock -n data/run.lock python main.py "$@" >> logs/cron.log 2>&1
