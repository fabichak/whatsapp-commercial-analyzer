# whatsapp-commercial-analyzer

Pipeline that analyzes ~28k spa-sales WhatsApp messages from `msgstore.db` and
produces a PT-BR Markdown report (`output/report.md`) plus CSV artifacts.

See `PLAN.md` (architecture spec) and `TECH_PLAN.md` (task breakdown).

## Auth setup

One of:

- **Max subscription** — run `claude login` (via `claude-agent-sdk`). Flat-rate.
- **Paid API** — copy `.env.example` to `.env`, set `ANTHROPIC_API_KEY=...`.
- **Both** — hybrid mode: Max primary, API fallback on quota exhaust.

## Quickstart

```bash
uv sync
uv run pytest -q
uv run python -m scripts.run_pipeline --chat-limit 5
```

## LLM mode flag

```bash
uv run python -m scripts.run_pipeline --chat-limit 5 --llm-mode hybrid
# --llm-mode {max,api,hybrid}   default: hybrid if both creds, else auto
```

## Testing status (milestone M1 complete)

Stages 1–7 are stubbed (no LLM). Stage 8 calls Sonnet 4.6 live and writes
`output/report.md` + 5 CSVs.

### Offline — pure-logic tests (no API, ~1s)

```bash
uv run pytest -q                        # 51 tests, all green
uv run pytest tests/test_load.py -q     # Stage 1 (DB → conversations.jsonl)
uv run pytest tests/test_dedupe.py -q   # Stage 2 (rapidfuzz templates)
uv run pytest tests/test_script_index.py -q  # Stage 3 (script.yaml parse)
uv run pytest tests/test_llm.py -q      # dual-client dispatcher, budget, retry
uv run pytest tests/test_pipeline.py -q # orchestrator sentinels + CLI
uv run pytest tests/test_report.py -q   # Stage 8 structure w/ fake LLM
```

### Smoke — per-stage, real data, no API

```bash
uv run python scripts/verify_stage1.py   # loads full msgstore.db, prints chat stats
uv run python scripts/verify_stage2.py   # dedupes spa messages, prints top 20 templates
```

### Smoke — Stage 8 live Sonnet call (~$0.06, <$0.30 cap)

Requires `.env` with `ANTHROPIC_API_KEY` **or** `claude login` OAuth session.

```bash
uv run python scripts/verify_stage8.py
# asserts: 7 PT-BR H2 headers present, 5 CSVs valid, UTF-8 clean, cost under cap
```

### End-to-end — M1 acceptance

```bash
uv run python -m scripts.run_pipeline --chat-limit 5 --budget-usd 1.00
# runs stages 1-8, writes output/report.md, <5 min, <$0.50
cat output/report.md
```

Re-run a single stage (sentinels gate the rest):

```bash
uv run python -m scripts.run_pipeline --stage 8 --budget-usd 1.00
uv run python -m scripts.run_pipeline --stage 8 --force --budget-usd 1.00
```

### Phone-list mode (bypass 20-msg threshold)

```bash
printf "5511962719203\n5511987654321\n" > /tmp/phones.txt
uv run python -m scripts.run_pipeline --phones-file /tmp/phones.txt --budget-usd 1.00
# mutually exclusive with --chat-limit
```

## Artifacts produced

- `data/conversations.jsonl` — long chats (≥20 msgs; all if phones filter)
- `data/conversations_short.jsonl` — short chats (unless phones filter active)
- `data/spa_templates.json`, `data/spa_message_template_map.json`
- `data/script.yaml` (committed), `data/script_extensions.yaml` (M2, gitignored)
- `data/labeled_messages.jsonl`, `data/template_sentiment.json`
- `data/conversions.jsonl`, `data/turnarounds.json`, `data/lost_deals.json`
- `data/aggregations.json`
- `data/stage{1..8}.done` — sentinels w/ chat_limit + phones_hash
- `output/report.md` — PT-BR report, 7 sections
- `output/{turnarounds,lost_deals,per_step,spa_templates_scored,off_script_clusters}.csv`
