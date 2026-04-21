#!/usr/bin/env bash
# Run M2-S3-T1 + M2-S4-T1 + M2-S4-T2 smoke tests via MAX (OAuth) only.
# Output tee'd to scripts/verify_max.log for Claude to read back.
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
export CLAUDE_MAX_ONESHOT=1
export CLAUDE_MAX_TIMEOUT_S=180
export CLAUDE_MAX_KILL_OTHERS="${CLAUDE_MAX_KILL_OTHERS:-1}"
export STAGE4_CONCURRENCY="${STAGE4_CONCURRENCY:-5}"
export STAGE4_TEMPLATE_BATCH_SIZE="${STAGE4_TEMPLATE_BATCH_SIZE:-10}"
export STAGE4_VERIFY_TEMPLATE_LIMIT="${STAGE4_VERIFY_TEMPLATE_LIMIT:-50}"
LOG=scripts/verify_max.log
: > "$LOG"

run() {
  echo "=== $* ===" | tee -a "$LOG"
  "$@" 2>&1 | tee -a "$LOG"
  echo "--- exit=${PIPESTATUS[0]} ---" | tee -a "$LOG"
}

run env -u STAGE4_TEMPLATE_BATCH_SIZE -u STAGE4_CONCURRENCY -u STAGE4_VERIFY_TEMPLATE_LIMIT -u CLAUDE_MAX_ONESHOT -u CLAUDE_MAX_KILL_OTHERS uv run pytest tests/test_script_index.py tests/test_label.py -q
run uv run python -m scripts.verify_stage3_max
run uv run python -m scripts.verify_stage4_max

echo "DONE — log at $LOG"
