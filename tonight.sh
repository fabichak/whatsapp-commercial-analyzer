#!/bin/bash
# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
RETRY_INTERVAL=600    # 10 minutes between rate-limit retries
MAX_RETRIES=120       # give up after 2 hours of retrying
HARD_TIMEOUT=3600     # 2-hour absolute wall-clock kill per task (seconds)
IDLE_TIMEOUT=300      # kill if no new output for 5 minutes (stream stall)

CLAUDE_BASE_PROMPT="Speak like a caveman. Using Context7, sequential-thinking. Read @TECH_PLAN.md and then work on TASK and verify it according to spec, using the claude max plan. Do not read PROGRESS.md."

# ─────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

# ─────────────────────────────────────────────
# Deterministic completion check via git
# No Claude spawn — check if a commit for this task already exists
# ─────────────────────────────────────────────
task_already_committed() {
  local task_id="$1"
  git log --oneline | grep -qF "$task_id"
}

# ─────────────────────────────────────────────
# Run claude with stream-json + result-event detection
# This is the KEY fix: we watch for {"type":"result"} in the stream,
# which is emitted BEFORE Claude tries to exit (and hangs).
# We kill Claude ourselves as soon as we see it — no waiting for
# process exit, which is the source of all hangs.
#
# Usage: run_claude <TASK_ID> <COMMIT_MSG>
# ─────────────────────────────────────────────
run_claude() {
  local task_id="$1"
  local commit_msg="$2"
  local prompt="${CLAUDE_BASE_PROMPT/TASK/$task_id}"
  local attempt=0
  local tmp_output
  tmp_output="/tmp/claude_${task_id//\//_}.log"

  echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${CYAN}▶ Starting task: ${task_id}${NC}"
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

  # Fast-path: already committed from a previous (interrupted) run
  if task_already_committed "$task_id"; then
    echo -e "${GREEN}✔ $task_id already in git log — skipping.${NC}"
    return 0
  fi

  while true; do
    attempt=$((attempt + 1))
    rm -f "$tmp_output"

    if [ "$attempt" -gt "$MAX_RETRIES" ]; then
      echo -e "${RED}✖ Max retries ($MAX_RETRIES) reached for $task_id. Aborting.${NC}"
      return 1
    fi

    [ "$attempt" -gt 1 ] && echo -e "${YELLOW}↻ Retry $attempt / $MAX_RETRIES for $task_id ...${NC}"

    # ── Launch Claude in background (stream-json mode) ────────────
    # --output-format stream-json emits newline-delimited JSON events.
    # We redirect stderr to the same log so rate-limit messages are captured.
    claude \
      --dangerously-skip-permissions \
      --output-format stream-json \
	  --verbose \
      -p "$prompt" \
      > "$tmp_output" 2>&1 &
    local claude_pid=$!

    # Mirror output in real time
    tail -f "$tmp_output" &
    local tail_pid=$!

    # ── Monitor loop ──────────────────────────────────────────────
    local start_time
    start_time=$(date +%s)
    local last_size=0
    local idle_for=0
    local done_signal=0
    local kill_reason=""

    while kill -0 "$claude_pid" 2>/dev/null; do
      sleep 10

      # ── DETERMINISTIC COMPLETION CHECK ───────────────────────────
      # stream-json emits {"type":"result",...} when Claude finishes.
      # This fires BEFORE the process hangs on stdout close.
      # Kill as soon as we see it — don't wait for process to exit.
      if grep -q '"type":"result"' "$tmp_output" 2>/dev/null; then
        done_signal=1
        kill_reason="result-event"
        break
      fi

      local cur_size
      cur_size=$(wc -c < "$tmp_output" 2>/dev/null || echo 0)

      # ── Hard wall-clock timeout ───────────────────────────────────
      local elapsed=$(( $(date +%s) - start_time ))
      if [ "$elapsed" -ge "$HARD_TIMEOUT" ]; then
        kill_reason="hard-timeout"
        break
      fi

      # ── Idle/stall detection ──────────────────────────────────────
      if [ "$cur_size" -eq "$last_size" ]; then
        idle_for=$((idle_for + 10))
        if [ "$idle_for" -ge "$IDLE_TIMEOUT" ]; then
          kill_reason="idle-stall"
          break
        fi
      else
        idle_for=0
        last_size=$cur_size
      fi
    done

    # Kill Claude (it may have already exited, that's fine)
    kill "$claude_pid" 2>/dev/null
    sleep 1
    kill -9 "$claude_pid" 2>/dev/null 2>&1
    kill "$tail_pid" 2>/dev/null
    wait "$claude_pid" 2>/dev/null

    # ── Evaluate outcome ──────────────────────────────────────────

    # Rate-limit check (same as before, works for both success and stall)
    if grep -qiE \
      "rate.?limit|quota.?exceeded|usage.?limit|too many requests|limit reached|try again|hit your limit" \
      "$tmp_output"; then
      echo -e "\n${YELLOW}⏳ Rate limit detected. Waiting ${RETRY_INTERVAL}s...${NC}"
      for ((i=RETRY_INTERVAL; i>0; i-=30)); do
        echo -e "${YELLOW}   ... ${i}s remaining${NC}"
        sleep 30
      done
      continue
    fi

    if [ "$done_signal" -eq 1 ]; then
      # ── Saw result event: check if it was success or error ────────
      if grep -q '"subtype":"success"' "$tmp_output" 2>/dev/null; then
        echo -e "\n${GREEN}✔ Claude signalled success for $task_id (via stream-json result event).${NC}"
      else
        # Could be {"subtype":"error"} — but still check git below
        echo -e "\n${YELLOW}⚠ Claude emitted result event but subtype is not 'success'. Checking git anyway...${NC}"
      fi

      # ── PRIMARY VERIFICATION: git, not another Claude ─────────────
      # If Claude actually wrote and committed the work, we're done.
      # This is deterministic and cannot hang.
      if task_already_committed "$task_id"; then
        echo -e "${GREEN}✔ Confirmed in git log: $task_id is committed.${NC}"
        rm -f "$tmp_output"
        return 0
      fi

      # Result event seen but no commit yet: Claude finished reasoning
      # but might not have committed. Try git add + commit ourselves.
      echo -e "${CYAN}  → git add .${NC}"
      git add . || { echo -e "${RED}✖ git add failed${NC}"; return 1; }
      echo -e "${CYAN}  → git commit -m \"$commit_msg\"${NC}"
      if git commit -m "$commit_msg"; then
        echo -e "${GREEN}✔ Committed: $commit_msg${NC}"
        rm -f "$tmp_output"
        return 0
      else
        echo -e "${YELLOW}⚠ git commit returned non-zero (nothing to commit?). Checking git log...${NC}"
        if task_already_committed "$task_id"; then
          echo -e "${GREEN}✔ Already committed — treating as done.${NC}"
          rm -f "$tmp_output"
          return 0
        fi
        echo -e "${YELLOW}↻ No commit found. Retrying task...${NC}"
        continue
      fi
    fi

    # ── No result event: killed for stall or hard timeout ─────────
    echo -e "\n${YELLOW}⚠ Killed due to: ${kill_reason}. Checking git before retrying...${NC}"

    # Even a hung Claude might have committed before stalling
    if task_already_committed "$task_id"; then
      echo -e "${GREEN}✔ Found $task_id in git log despite hang — task was complete!${NC}"
      rm -f "$tmp_output"
      return 0
    fi

    echo -e "${YELLOW}↻ Task not in git log. Retrying from scratch...${NC}"
    # Brief cooldown before retry to let WSL2 TCP state settle
    sleep 15
    continue
  done
}

# ─────────────────────────────────────────────
# Task queue
# ─────────────────────────────────────────────
run_claude "M2-S6-T2"  "M2-S6-T2"  || exit 1
run_claude "M2-S6-T3"  "M2-S6-T3"  || exit 1
run_claude "M2-S7-T1"  "M2-S7-T1"  || exit 1
run_claude "M2-S8-T1"  "M2-S8-T1"  || exit 1
run_claude "M3-T2"     "M3-T2"     || exit 1
run_claude "M3-T3"     "M3-T3"     || exit 1
run_claude "M3-T4"     "M3-T4"     || exit 1

# run_claude "M3-T5"     "M3-T5"     || exit 1

echo -e "\n${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✔ All tasks completed successfully!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"