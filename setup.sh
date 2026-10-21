#!/bin/bash
# One-time setup for TMrex, the trademark clearance tool (macOS). Run from Terminal:  bash setup.sh
#  1. Saves your USPTO API key to .env (free from the USPTO Open Data Portal, data.uspto.gov).
#  2. Gets the register database, data/uspto.db, one of two ways:
#     - If a TrademarkBench register exists (default ~/Desktop/TrademarkBench/local/uspto_full.db, or set
#       BENCH_DB), clones it. The clone is an APFS copy-on-write copy: instant, no extra disk space until the
#       copies diverge, and the original is never changed.
#     - Otherwise, tells you how to build it from USPTO bulk data with sync/build_db.py (several hours).
#  3. Schedules sync/sync_daily.py to run twice a day with launchd.
# Safe to rerun: each step is skipped if it's already done, except the schedule, which is reinstalled.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BENCH_DB="${BENCH_DB:-$HOME/Desktop/TrademarkBench/local/uspto_full.db}"
BENCH_ENV="$(dirname "$(dirname "$BENCH_DB")")/.env"
LABEL="com.tmrex.uspto-sync"
OLD_LABEL="com.trademarkclearance.uspto-sync"  # its name before the rename to TMrex
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "$(uname)" != "Darwin" ]; then
  echo "setup.sh is for macOS. Elsewhere: put USPTO_API_KEY=... in .env, run sync/build_db.py,"
  echo "and schedule sync/sync_daily.py daily (for example with cron)."
  exit 1
fi
case "$ROOT" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "This folder is under Desktop, Documents or Downloads, where macOS blocks scheduled jobs."
    echo "Move it to your home folder (for example ~/tmrex) and rerun."
    exit 1;;
esac

mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/reports"

# 1. API key
if [ -e "$ROOT/.env" ] && grep -q '^USPTO_API_KEY=' "$ROOT/.env"; then
  echo ".env already has a USPTO API key; leaving it as is"
elif [ -e "$BENCH_ENV" ] && grep -q '^USPTO_API_KEY=' "$BENCH_ENV"; then
  grep '^USPTO_API_KEY=' "$BENCH_ENV" > "$ROOT/.env"
  chmod 600 "$ROOT/.env"
  echo "Copied the USPTO API key from TrademarkBench's .env"
else
  echo "Paste your USPTO Open Data Portal API key (it won't be shown), then press Return:"
  read -rs KEY
  [ -n "$KEY" ] || { echo "No key entered"; exit 1; }
  printf 'USPTO_API_KEY=%s\n' "$KEY" > "$ROOT/.env"
  chmod 600 "$ROOT/.env"
  echo "Saved the key to .env"
fi

# 2. Database
NEED_BUILD=0
if [ -e "$ROOT/data/uspto.db" ]; then
  echo "data/uspto.db already exists; leaving it as is"
elif [ -e "$BENCH_DB" ]; then
  echo "Cloning $BENCH_DB ..."
  cp -c "$BENCH_DB" "$ROOT/data/uspto.db"
else
  NEED_BUILD=1
fi

# 3. Schedule
launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__ROOT__#$ROOT#g" "$ROOT/sync/uspto-sync.plist.template" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Scheduled the sync for 6:30 a.m. and 6:30 p.m. (log in logs/sync.log)"

echo
if [ "$NEED_BUILD" = 1 ]; then
  echo "Next: build the register database from USPTO bulk data (several hours; needs about 100 GB free):"
  echo "  caffeinate -i python3 -I \"$ROOT/sync/build_db.py\""
  echo "If it's interrupted, run it again and it picks up where it stopped."
else
  echo "Next: catch up on files published since the database was last updated:"
  echo "  python3 -I \"$ROOT/sync/sync_daily.py\""
fi
