#!/bin/bash

sleep 100m
# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
RETRY_INTERVAL=600   # 10 minutes in seconds
MAX_RETRIES=120      # give up after 2 hours of retrying
CLAUDE_BASE_PROMPT="Speak like a caveman. Using Context7, sequential-thinking. Read @TECH_PLAN.md and then work on TASK and verify it according to spec. Do not read PROGRESS.md."

# ─────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m' # No Colour

# ─────────────────────────────────────────────
# Run claude with rate-limit retry logic
# Usage: run_claude <TASK_ID> <COMMIT_MSG>
# ─────────────────────────────────────────────
run_claude() {
  local task_id="$1"
  local commit_msg="$2"
  local prompt="${CLAUDE_BASE_PROMPT/TASK/$task_id}"
  local attempt=0
  local was_hung=0
  local tmp_output
  tmp_output="log.tmp"

  echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${CYAN}▶ Starting task: ${task_id}${NC}"
  echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

  while true; do
    attempt=$((attempt + 1))

    if [ "$attempt" -gt "$MAX_RETRIES" ]; then
      echo -e "${RED}✖ Max retries ($MAX_RETRIES) reached for $task_id. Aborting.${NC}"
      rm -f "$tmp_output"
      return 1
    fi

    if [ "$attempt" -gt 1 ]; then
      echo -e "${YELLOW}↻ Retry attempt $attempt / $MAX_RETRIES for $task_id ...${NC}"
    fi

    # Executa o Claude em background e redireciona os logs
    claude --dangerously-skip-permissions --verbose -p "$prompt" > "$tmp_output" 2>&1 &
    local claude_pid=$!

    # INOVAÇÃO: Lança um tail em background para espelhar o log na tela em tempo real
    tail -f "$tmp_output" &
    local tail_pid=$!

    # Heartbeat + idle detection:
    local IDLE_TIMEOUT=180
    local last_size=0
    local idle_for=0

    echo -e "${CYAN}  [Monitoring logs in real time. Timeout set to ${IDLE_TIMEOUT}s]${NC}"
    
    while kill -0 "$claude_pid" 2>/dev/null; do
      sleep 10

      local cur_size
      cur_size=$(wc -c < "$tmp_output" 2>/dev/null || echo 0)

      if [ "$cur_size" -eq "$last_size" ]; then
        idle_for=$((idle_for + 10))
        if [ "$idle_for" -ge "$IDLE_TIMEOUT" ]; then
          echo -e "\n${YELLOW}[idle ${IDLE_TIMEOUT}s — killing hung process]${NC}"
          kill "$claude_pid" 2>/dev/null
          sleep 2
          kill -9 "$claude_pid" 2>/dev/null
          was_hung=1
          break
        fi
      else
        idle_for=0
        last_size="$cur_size"
      fi
    done

    # Mata o 'tail' que estava espelhando o log na tela
    kill "$tail_pid" 2>/dev/null

    wait "$claude_pid" 2>/dev/null
    local exit_code=$?
    # SIGTERM exit code (143) means we killed a hung process
    [ "$exit_code" -eq 143 ] && exit_code=0

    # ── Hung process: verify completion before continuing ─────────
    if [ "$was_hung" -eq 1 ]; then
      was_hung=0
      echo -e "\n${YELLOW}⚠ Process was killed (hung). Asking Claude to verify $task_id...${NC}"

      local verify_output
      verify_output=$(mktemp)
      local verify_prompt="Confirm that $task_id was completed successfully. Check the last git commit and the codebase to verify the task is done to completion according to TECH_PLAN.md spec. Reply with only DONE or INCOMPLETE, absolutely nothing else."

      claude --dangerously-skip-permissions -p "$verify_prompt" > "$verify_output" 2>&1 &
      local verify_pid=$!
      
      # Tail para o verify também
      tail -f "$verify_output" &
      local verify_tail_pid=$!

      while kill -0 "$verify_pid" 2>/dev/null; do
        sleep 5
      done
      
      kill "$verify_tail_pid" 2>/dev/null
      wait "$verify_pid" 2>/dev/null

      if grep -qiE "^DONE" "$verify_output"; then
        echo -e "\n${GREEN}✔ Verified: $task_id is complete.${NC}"
        rm -f "$verify_output"
        exit_code=0   # fall through to git commit below
      else
        echo -e "\n${YELLOW}↻ Not complete. Retrying $task_id from scratch...${NC}"
        rm -f "$verify_output"
        continue      # retry the main task loop
      fi
    fi

    # ── Detect rate-limit in output ───────────────────────────────
    if grep -qiE \
      "rate.?limit|quota.?exceeded|usage.?limit|too many requests|limit reached|try again|hit your limit" \
      "$tmp_output"; then

      echo -e "\n${YELLOW}⏳ Rate limit detected for $task_id.${NC}"
      echo -e "${YELLOW}   Waiting ${RETRY_INTERVAL}s ($(( RETRY_INTERVAL / 60 )) min) before retry...${NC}"

      for ((i=RETRY_INTERVAL; i>0; i-=30)); do
        echo -e "${YELLOW}   ... ${i}s remaining${NC}"
        sleep 30
      done

      continue  # retry same task
    fi

    # ── Success ───────────────────────────────────────────────────
    if [ "$exit_code" -eq 0 ]; then
      echo -e "\n${GREEN}✔ Claude finished $task_id successfully.${NC}"
      rm -f "$tmp_output"

      echo -e "${CYAN}  → git add .${NC}"
      git add . || { echo -e "${RED}✖ git add failed${NC}"; return 1; }

      echo -e "${CYAN}  → git commit -m \"$commit_msg\"${NC}"
      git commit -m "$commit_msg" || { echo -e "${RED}✖ git commit failed${NC}"; return 1; }

      echo -e "${GREEN}✔ Committed: $commit_msg${NC}"
      return 0
    fi

    # ── Non-rate-limit error ───────────────────────────────────────
    echo -e "${RED}✖ Claude exited with code $exit_code for $task_id.${NC}"
    echo -e "${RED}  Not a rate-limit error — stopping script.${NC}"
    rm -f "$tmp_output"
    return 1
  done
}

# ─────────────────────────────────────────────
# Task queue
# ─────────────────────────────────────────────
run_claude "M2-S4-T2"  "M2-S4-T2"  || exit 1
run_claude "M2-S5-T1"  "M2-S5-T1"  || exit 1
run_claude "M2-S6-T1"  "M2-S6-T1"  || exit 1
run_claude "M3-T1"     "M3-T1"     || exit 1
run_claude "M2-S6-T2"  "M2-S6-T2"  || exit 1
run_claude "M2-S6-T3"  "M2-S6-T3"  || exit 1
run_claude "M2-S7-T1"  "M2-S7-T1"  || exit 1
run_claude "M2-S8-T1"  "M2-S8-T1"  || exit 1
run_claude "M3-T2"     "M3-T2"     || exit 1
run_claude "M3-T3"     "M3-T3"     || exit 1
run_claude "M3-T4"     "M3-T4"     || exit 1

# Você pode adicionar as próximas tasks faltantes aqui embaixo
# run_claude "M3-T5"     "M3-T5"     || exit 1

echo -e "\n${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✔ All tasks completed successfully!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"