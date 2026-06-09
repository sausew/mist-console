#!/bin/bash
# run-routine.sh <routine-dir-name>
# Runs a Claude Code routine from ~/.claude/scheduled-tasks/<dir>/SKILL.md.
# Invoked by a launchd job that the MIST Console generates when a routine is
# scheduled + enabled. Mirrors how the desktop app runs a routine: feed the
# SKILL.md body to `claude` headless in the harness cwd (so CLAUDE.md + the
# MIST persona auto-load).
set -e
DIR="$1"
[ -n "$DIR" ] || { echo "usage: run-routine.sh <routine-dir>"; exit 2; }
SK="$HOME/.claude/scheduled-tasks/$DIR/SKILL.md"
[ -f "$SK" ] || { echo "no SKILL.md for routine '$DIR'"; exit 0; }

HARNESS="/Users/alexhedtke/Documents/Exobrain harness"
CLAUDE="$HOME/.npm-global/bin/claude"
export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Strip the YAML frontmatter (everything up to and including the 2nd '---'),
# pass the remaining body as the prompt.
PROMPT="$(awk 'BEGIN{fm=0} /^---[[:space:]]*$/{fm++; next} fm>=2{print}' "$SK")"
[ -n "$PROMPT" ] || PROMPT="$(cat "$SK")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] running routine: $DIR"
cd "$HARNESS"
exec "$CLAUDE" -p --dangerously-skip-permissions "$PROMPT"
