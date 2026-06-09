#!/bin/bash
# Auto-commit + push the MIST Console private repo so the conversation history
# (data/) and any local edits stay synced without manual commits. No-ops when the
# tree is clean. Driven by the launchd agent com.exobrain.mist-console-autocommit
# (every 30 min; launchd never runs two copies of the same job concurrently).
cd "/Users/alexhedtke/Documents/mist-console" || exit 0

# Nothing to commit -> done.
[ -z "$(git status --porcelain)" ] && exit 0

git add -A
git commit -q -m "Auto-sync: $(date '+%Y-%m-%d %H:%M')" || exit 0
git push -q origin main || exit 0
