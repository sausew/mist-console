#!/bin/bash
# run-routine-ontime.sh <routine-dir> <HH:MM> [grace-minutes]
# Wrapper around run-routine.sh that ONLY runs the routine if it is firing
# within a grace window of its scheduled time. launchd will fire a missed
# StartCalendarInterval job once the machine wakes from sleep; for time-sensitive
# routines (morning briefing, evening wind-down) a stale catch-up run is worse
# than no run, so we skip it. If the laptop was awake on schedule the delta is
# ~0 and the routine runs normally.
set -e
DIR="$1"
SCHED="$2"                 # e.g. "08:00"
GRACE="${3:-15}"           # minutes late still considered on-time
[ -n "$DIR" ] && [ -n "$SCHED" ] || { echo "usage: run-routine-ontime.sh <dir> <HH:MM> [grace-min]"; exit 2; }

now_epoch=$(date +%s)
sched_epoch=$(date -j -f "%Y-%m-%d %H:%M" "$(date +%Y-%m-%d) $SCHED" +%s)
diff_min=$(( (now_epoch - sched_epoch) / 60 ))

# Allow a little negative jitter (clock/launchd firing a few seconds early) and
# up to GRACE minutes late. Anything beyond that is a missed slot -> skip.
if [ "$diff_min" -lt -5 ] || [ "$diff_min" -gt "$GRACE" ]; then
	echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP $DIR — missed scheduled $SCHED (Δ ${diff_min}m, grace ${GRACE}m). No catch-up run."
	exit 0
fi

exec /bin/bash "$HOME/Documents/mist-console/run-routine.sh" "$DIR"
