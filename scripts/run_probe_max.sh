#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
LOG=scripts/probe_max.log
: > "$LOG"
# 90s timeout per python run — prevent forever-hang
timeout 300 uv run python -m scripts.probe_max 2>&1 | tee "$LOG"
echo "exit=${PIPESTATUS[0]}"
echo "log: $LOG"
