#!/usr/bin/env bash
# Tiny stage 4 MAX smoke — 3 templates, verbose logging.
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
export CLAUDE_MAX_ONESHOT=1
export CLAUDE_MAX_TIMEOUT_S=120
LOG=scripts/stage4_mini.log
: > "$LOG"
echo "=== start $(date -Is) ===" | tee -a "$LOG"
uv run python -m scripts.verify_stage4_mini 2>&1 | tee -a "$LOG"
echo "=== exit=${PIPESTATUS[0]} $(date -Is) ===" | tee -a "$LOG"
echo "log: $LOG"
