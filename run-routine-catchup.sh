#!/bin/bash
# run-routine-catchup.sh <routine-dir> <HH:MM>
# Wrapper around run-routine.sh for routines that SHOULD catch up on wake if the
# machine was asleep at their scheduled time (e.g. the morning briefing: a late
# briefing beats no briefing). Runs at most once per calendar day, and only the
# latest slot, so a multi-day sleep produces exactly ONE run, not a backlog.
#
# launchd fires a missed StartCalendarInterval job once when the machine wakes.
# This wrapper then decides whether that fire should actually run:
#   - skip if it already ran today (per-day marker)      -> no double runs
#   - skip if we are before today's scheduled time       -> no overnight/dark-wake runs
#   - otherwise run, and stamp the marker with today
set -e
DIR="$1"
SCHED="$2"                 # e.g. "08:00"
[ -n "$DIR" ] && [ -n "$SCHED" ] || { echo "usage: run-routine-catchup.sh <dir> <HH:MM>"; exit 2; }

today=$(date +%Y-%m-%d)
now_epoch=$(date +%s)
sched_epoch=$(date -j -f "%Y-%m-%d %H:%M" "$today $SCHED" +%s)

marker_dir="$HOME/.mist/routine-markers"
marker="$marker_dir/$DIR.lastrun"
mkdir -p "$marker_dir"

# Already ran today? Don't run again — "only the latest one".
if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$today" ]; then
	echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP $DIR — already ran today ($today)."
	exit 0
fi

# Before today's scheduled time (e.g. a 3am dark-wake fire)? Wait for the real slot.
if [ "$now_epoch" -lt "$sched_epoch" ]; then
	echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP $DIR — before scheduled $SCHED today; not an on-wake catch-up."
	exit 0
fi

# Run (possibly a late catch-up). Stamp the marker BEFORE exec so a second fire
# the same day can't double-run.
diff_min=$(( (now_epoch - sched_epoch) / 60 ))
echo "[$(date '+%Y-%m-%d %H:%M:%S')] RUN $DIR — scheduled $SCHED, Δ ${diff_min}m (catch-up ok)."
echo "$today" > "$marker"
exec /bin/bash "$HOME/Documents/mist-console/run-routine.sh" "$DIR"
