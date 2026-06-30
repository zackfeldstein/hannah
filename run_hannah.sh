#!/usr/bin/env bash
#
# Hannah cron runner.
#
# Runs one observation cycle and appends all output to logs/cron.log so the run
# can be analyzed later. Designed for cron, which starts with a minimal
# environment, so it sets PATH explicitly and uses absolute interpreters.
#
# Install (every 5 minutes):
#   crontab -e
#   */5 * * * * /ssd/repos/z-web/time-poc/run_hannah.sh
#
set -uo pipefail

# Project root = the directory this script lives in.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Put the llama.cpp binaries on PATH (cron's PATH is otherwise just /usr/bin:/bin).
export PATH="$HOME/src/llama.cpp/build/bin:/usr/local/bin:/usr/bin:/bin"

# Use the system Python (stdlib-only script; avoids depending on any venv).
PYTHON="/usr/bin/python3"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
CRON_LOG="$LOG_DIR/cron.log"
LOCK_FILE="$LOG_DIR/hannah.lock"

# Avoid overlapping runs if one cycle ever runs longer than the cron interval.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date '+%F %T') [skip] previous run still active" >>"$CRON_LOG"
    exit 0
fi

{
    echo "===================== $(date '+%F %T') run start ====================="
    "$PYTHON" hannah.py
    echo "--------------------- $(date '+%F %T') run end (exit $?) ---------------------"
    echo
} >>"$CRON_LOG" 2>&1
