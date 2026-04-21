#!/usr/bin/env bash
# Time bundled claude CLI with real stage4 prompt size (no Python).
set -u
cd "$(dirname "$0")/.."
CLI=.venv/lib/python3.11/site-packages/claude_agent_sdk/_bundled/claude
LOG=scripts/probe_cli.log
: > "$LOG"

echo "=== version ===" | tee -a "$LOG"
"$CLI" --version 2>&1 | tee -a "$LOG"

# Small prompt
echo "=== A small haiku -p ===" | tee -a "$LOG"
T=$(date +%s)
timeout 60 "$CLI" -p "say ok" --model claude-haiku-4-5 --output-format json --max-turns 1 > /tmp/cli_a.json 2>>"$LOG"
RC=$?
echo "rc=$RC dt=$(( $(date +%s) - T ))s bytes=$(wc -c < /tmp/cli_a.json 2>/dev/null)" | tee -a "$LOG"

# Medium prompt (~4k chars)
BIG=$(python3 -c "print('Classifique este texto em 1 palavra: ' + 'blabla ' * 500)")
echo "=== B haiku -p medium (len=${#BIG}) ===" | tee -a "$LOG"
T=$(date +%s)
timeout 180 "$CLI" -p "$BIG" --model claude-haiku-4-5 --output-format json --max-turns 1 > /tmp/cli_b.json 2>>"$LOG"
RC=$?
echo "rc=$RC dt=$(( $(date +%s) - T ))s bytes=$(wc -c < /tmp/cli_b.json 2>/dev/null)" | tee -a "$LOG"

# Medium + system prompt
SYS="Voce eh classificador. Retorne JSON {\"tag\":\"x\"}."
echo "=== C haiku -p + system ===" | tee -a "$LOG"
T=$(date +%s)
timeout 180 "$CLI" -p "$BIG" --system-prompt "$SYS" --model claude-haiku-4-5 --output-format json --max-turns 1 > /tmp/cli_c.json 2>>"$LOG"
RC=$?
echo "rc=$RC dt=$(( $(date +%s) - T ))s bytes=$(wc -c < /tmp/cli_c.json 2>/dev/null)" | tee -a "$LOG"

echo "log: $LOG"
