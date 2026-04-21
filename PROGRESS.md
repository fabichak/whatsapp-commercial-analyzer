# Progress tracker

Check off each task as it is completed. Keep this file in sync with the task breakdown in `/home/martin/.claude/plans/you-are-a-technical-flickering-squid.md`.

**Legend:** `[ ]` = not started · `[~]` = in progress · `[x]` = done · `[!]` = blocked

## Status at a glance

| Status | ID | Title | Effort | Prereqs | LLM cost |
|---|---|---|---|---|---|
| [x] | M0-T1 | Bootstrap project (uv, pyproject, gitignore, .env) | 1–2h | — | $0 |
| [x] | M0-T2 | `src/llm.py` — client, retry, budget, accounting | 3–4h | M0-T1 | $0 |
| [x] | M0-T3 | `src/schemas.py` + `src/context.py` | 2h | M0-T1 | $0 |
| [ ] | M1-T1 | Orchestrator skeleton `scripts/run_pipeline.py` | 3h | M0-T3 | $0 |
| [ ] | M1-T2 | Stage 1: load DB → conversations.jsonl | 4h | M1-T1 | $0 |
| [ ] | M1-T3 | Stage 2: rapidfuzz dedupe | 3h | M1-T2 | $0 |
| [ ] | M1-T4 | Stage 3: hand-curated `script.yaml` | 3h | M0-T3 | $0 |
| [ ] | M1-T5 | Stages 4–7 stubs (pass-through) | 2h | M1-T2, M1-T4 | $0 |
| [ ] | M1-T6 | Stage 8 minimal hollow report | 3h | M1-T5 | $0.30 |
| — | **M1 COMPLETE** | **End-to-end on 5 chats, <$0.50** | | | |
| [ ] | M2-S3-T1 | Stage 3 LLM script expansion | 4h | M1-T4 | $0.50 |
| [ ] | M2-S4-T1 | Stage 4 spa-template labeling | 4h | M1-T3 | $1.00 |
| [ ] | M2-S4-T2 | Stage 4 customer batching & tagging | 4h | M2-S4-T1 | $1.50 |
| [ ] | M2-S5-T1 | Stage 5 template sentiment | 3h | M1-T3 | $0.40 |
| [ ] | M2-S6-T1 | Stage 6 truncation utility | 2h | M1-T2, M2-S4-T2 | $0 |
| [ ] | M3-T1 | Ground-truth collection helper (20 chats) | 3h | M1-T2 | $0 |
| [ ] | M2-S6-T2 | Stage 6 conversion detection | 5h | M2-S6-T1, M2-S4-T2, M3-T1 | $1.80 |
| [ ] | M2-S6-T3 | Stage 6 turnaround extraction (pure) | 3h | M2-S6-T2 | $0 |
| [ ] | M2-S7-T1 | Stage 7 embedding + HDBSCAN | 4h | M2-S4-T2 | $0 |
| [ ] | M2-S8-T1 | Stage 8 full report | 5h | M2-S7-T1, M2-S6-T3, M2-S5-T1 | $1.50 |
| [ ] | M3-T2 | Prompt-tuning loop for Stage 6 (≥16/20) | 2–4h | M2-S6-T2 | $0.50 |
| [ ] | M3-T3 | Full-corpus run | 1h | all M2 | $6–8 |
| [ ] | M3-T4 | Human review + script v2 draft | 3h | M3-T3 | $0.30 |

**Total effort:** ~70–85 dev-hours (~2 working weeks).
**LLM spend projection:** $8–10 including calibration.
**First stakeholder-readable artifact:** end of M1 (hollow `output/report.md`).

## Verification log

Record the output of each task's verification command here, one short line per task. Paste failures verbatim so we can diagnose later.

| Task | Date | Verification result |
|---|---|---|
| M0-T1 | — | — |
| M0-T2 | — | — |
| M0-T3 | — | — |
| M1-T1 | — | — |
| M1-T2 | — | — |
| M1-T3 | — | — |
| M1-T4 | — | — |
| M1-T5 | — | — |
| M1-T6 | — | — |
| M2-S3-T1 | — | — |
| M2-S4-T1 | — | — |
| M2-S4-T2 | — | — |
| M2-S5-T1 | — | — |
| M2-S6-T1 | — | — |
| M3-T1 | — | — |
| M2-S6-T2 | — | — |
| M2-S6-T3 | — | — |
| M2-S7-T1 | — | — |
| M2-S8-T1 | — | — |
| M3-T2 | — | — |
| M3-T3 | — | — |
| M3-T4 | — | — |
