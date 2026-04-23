# whatsapp-commercial-analyzer

Pipeline that analyzes WhatsApp sales conversations from a `msgstore.db` export
and produces a PT-BR Markdown report (`output/report.md`) plus CSV artifacts.
Built for the Puris Spa 1C (first-contact) sales script, but generalizable.

Eight stages, each gated by a sentinel file. LLM calls routed through a
dual-client (Claude Max subscription and/or Anthropic paid API) with cache,
budget enforcement, and retry.

See `PLAN.md` (architecture) and `TECH_PLAN.md` (task breakdown).

---

## 1. Setup

### 1.1 Install

Requires Python >= 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

### 1.2 Auth (pick one or both)

- **Claude Max subscription** — run `claude login` once. OAuth session file is
  read by `claude-agent-sdk`. Flat-rate; no per-call cost. Use
  `--llm-mode max` or `hybrid`.
- **Paid Anthropic API** — copy `.env.example` to `.env` and set
  `ANTHROPIC_API_KEY=sk-ant-...`. Use `--llm-mode api` or `hybrid`.
- **Both (recommended)** — hybrid mode uses Max primary, falls back to API on
  quota exhaust.

### 1.3 Required input files

Place these under `input/` (paths overridable — see CLI args):

| File | Purpose |
|------|---------|
| `input/msgstore.db` | WhatsApp SQLite export (chat, message, jid tables). Primary data source. |
| `input/script-comercial.md` | Human-authored sales-script narrative (Markdown). Used as context for Stage 3 LLM expansion. |
| `input/script.yaml` | Structured, hand-curated version of the script — list of `steps` with `id`, `name`, `canonical_texts`, `expected_customer_intents`, `transitions_to`. Source of truth for step classification. |

### 1.4 Environment variables (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | (empty) | Required for `--llm-mode api` or hybrid fallback. |
| `LLM_BUDGET_USD` | `10` | Default API-path budget cap (overridden by `--budget-usd`). Max path not gated. |
| `CLAUDE_MAX_ONESHOT` | `1` | Use bundled `claude` CLI one-shot (`-p`) instead of SDK stream-json. Required on WSL2 (stream-json stdin hangs). |
| `CLAUDE_MAX_TIMEOUT_S` | `180` | Per-call CLI timeout, seconds. |
| `CLAUDE_MAX_KILL_OTHERS` | `0` | Kill stray `claude` procs before each spawn. WILL kill parent Claude Code session — only for batch runs. `run_pipeline.py` sets this to `1` automatically. |
| `STAGE4_CONCURRENCY` | `5` | Parallel LLM calls in Stage 4. |
| `STAGE4_TEMPLATE_BATCH_SIZE` | `10` | Templates bundled per LLM call (1 = spec-literal). |
| `STAGE4_VERIFY_TEMPLATE_LIMIT` | `50` | Cap for `verify_stage4_max.py` (0 = all). |

---

## 2. Prepare (run once per new WhatsApp number)

```bash
uv run python -m scripts.prepare
```

Does, in order:

1. **Generate `input/script.yaml`** — if missing, LLM-drafts it from
   `input/script-comercial.md`. Output validated against
   `src.script_index.load_script` (required step ids + 9 objection ids).
   **Review the file** before proceeding — it is business truth.
2. **Run Stage 1** — if `data/conversations.jsonl` missing, loads
   `input/msgstore.db` to produce it.
3. **Launch `scripts/label_ground_truth.py`** — interactive labeler, 20
   stratified chats, writes `data/ground_truth_outcomes.csv`.

Flags:

| Flag | Effect |
|------|--------|
| `--force-script` | Regenerate `script.yaml` even if it exists. |
| `--skip-script` | Skip step 1. |
| `--skip-ground-truth` | Skip steps 2 + 3. |
| `--llm-mode`, `--budget-usd`, `--input-dir`, `--data-dir`, `--output-dir`, `--prompts-dir` | Same semantics as `run_pipeline`. |

**`run_pipeline` refuses to start** if `input/script.yaml` or
`data/ground_truth_outcomes.csv` is missing — run `prepare` first.

---

## 3. Run

```bash
# full pipeline, all stages
uv run python -m scripts.run_pipeline --budget-usd 5.00

# smoke run — first 5 chats only
uv run python -m scripts.run_pipeline --chat-limit 5 --budget-usd 1.00

# single stage (prereqs must already exist)
uv run python -m scripts.run_pipeline --stage 8

# resume — re-run from stage N onward
uv run python -m scripts.run_pipeline --from 4

# force rebuild — clear sentinels + LLM cache, keep ground_truth_outcomes.csv
uv run python -m scripts.run_pipeline --restart
```

Sentinels (`data/stage{1..8}.done`) record `chat_limit`, `phones_hash`,
`input_hash`, `llm_mode`, `git_sha`. Stage is skipped if sentinel matches
current context. Input change (new `msgstore.db`, edited `script.yaml`) auto-
invalidates downstream sentinels.

### Phone-list mode

Bypasses the 20-msg long-chat threshold — forces inclusion of specific chats.

```bash
printf "5511962719203\n5511987654321\n" > /tmp/phones.txt
uv run python -m scripts.run_pipeline --phones-file /tmp/phones.txt
```

Mutually exclusive with `--chat-limit`. When active, `conversations_short.jsonl`
is not written.

### Tests

```bash
uv run pytest -q                         # all offline tests (~1s, no API)
uv run python scripts/verify_stage1.py   # load msgstore.db, print chat stats
uv run python scripts/verify_stage2.py   # dedupe, print top templates
uv run python scripts/verify_stage8.py   # live Sonnet call, ~$0.06
```

---

## 3. CLI arguments

All accepted by `scripts.run_pipeline`. Defined in `src/context.py`
(`Context.from_args`) plus the stage-selector peek in
`scripts/run_pipeline.py`.

### Stage selection (mutually exclusive; defaults to stages 1–8)

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--stage N` | int | — | Run exactly one stage. Prereq files from prior stages must already exist. |
| `--from N` | int | `1` | Run stages N..8. |
| `--to N` | int | `8` | Run stages `from`..N (combine with `--from`). |

### Data-scoping filters (mutually exclusive)

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--chat-limit N` | int | unset | Process only the first N long chats (post-filter). Smoke / dev mode. Invalidates sentinels with different limit. |
| `--phones-file PATH` | path | unset | Text file of E.164-digits phones, one per line (10–15 digits, `#` comments allowed). Forces those chats in regardless of msg count. Empty file → error. |

### LLM routing & cost

| Flag | Choices / type | Default | Description |
|------|----------------|---------|-------------|
| `--llm-mode` | `max` \| `api` \| `hybrid` | `hybrid` | `max` = Claude subscription via agent SDK. `api` = paid Anthropic API. `hybrid` = Max first, API fallback on quota/429. |
| `--budget-usd` | float | `10.0` | Hard cap on cumulative API-path spend. Raising `BudgetExceeded` aborts the stage. Max path (flat subscription) is not counted. |

### Execution control

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--restart` | flag | off | Wipe `data/` (except `ground_truth_outcomes.csv`) and `data/llm_cache/`, then run from scratch. Inputs under `input/` untouched. |
| `--force` | flag | off | Deprecated alias for `--restart`. |
| `--dry-run` | flag | off | Parse config, build Context, skip execution. (Stages may opt in further.) |

### Path overrides

| Flag | Default | Description |
|------|---------|-------------|
| `--input-dir` | `./input` | Base dir. `--db-path`, `--script-path`, `--script-yaml` resolve under here unless set explicitly. |
| `--db-path` | `<input-dir>/msgstore.db` | WhatsApp SQLite file. |
| `--script-path` | `<input-dir>/script-comercial.md` | Markdown script narrative. |
| `--script-yaml` | `<input-dir>/script.yaml` | Structured script YAML. |
| `--data-dir` | `./data` | Derived artifacts, sentinels, LLM cache. Created if missing. |
| `--output-dir` | `./output` | Final report + CSVs. Created if missing. |
| `--prompts-dir` | `./prompts` | Per-stage prompt templates (`stage{3..8}_*.md`). |

---

## 4. Input files — in detail

### `input/msgstore.db`
WhatsApp Android SQLite export. Stage 1 (`src/load.py`) reads `message`, `chat`,
`jid` tables, filters to individual (non-group) chats, splits by 20-msg
threshold into long/short JSONL. **To replace:** drop a new `msgstore.db` in
`input/`. Its SHA is folded into `input_hash`, auto-invalidating sentinels.

### `input/script-comercial.md`
Free-text Markdown description of the sales process — promotions, step-by-step
playbook, tone guidelines. **To edit:** just edit. Stage 3 re-expands the
script; downstream stages re-label. Included in `input_hash`.

### `input/script.yaml`
Hand-curated structured script — authoritative step list. Each step:

```yaml
- id: "1"
  name: "Saudação e acolhimento"
  canonical_texts: [ "Olá!..." ]         # reference phrasings (fuzzy-match anchor)
  expected_customer_intents: [ "greet_back", "give_name", ... ]
  transitions_to: ["2"]
```

**To edit:** add/modify steps; keep `id` stable across runs to preserve
labeling continuity. LLM-generated extensions (synonym templates, variants)
live in `data/script_extensions.yaml` (gitignored, regenerated by Stage 3).

### `data/ground_truth_outcomes.csv` (optional, hand-labeled)
Schema: `chat_id,phone,outcome,notes`. `outcome` ∈ {booked, lost, ambiguous}.
Used by Stage 6 validation harness (`scripts/verify_stage6.py`) — checks that
LLM-detected conversion matches human judgment on a 20-chat sample
(accept threshold: ≥16/20, ambiguous excluded). Preserved across `--restart`.
Not required for the pipeline to run. See §7 below for how to create it.

---

## 7. Authoring `script.yaml` for a new WhatsApp number

**There is no generator tool.** `script.yaml` is hand-authored — it is the
authoritative taxonomy for step classification across the pipeline.
`src/script_index.py` (Stage 3) *expands* it via LLM into
`data/script_extensions.yaml` (synonym variants, objection replies, pitch
restructuring) but never writes to `script.yaml` itself.

### Workflow for a fresh deployment

1. Drop new inputs in `input/`:
   - `input/msgstore.db` — WhatsApp export for the target number.
   - `input/script-comercial.md` — free-form Markdown of the sales process
     (promotions, tone guide, step-by-step). Whatever the business uses
     internally; LLM reads this as context in Stage 3.

2. Hand-write `input/script.yaml`. Required top-level keys:

   ```yaml
   steps:
     - id: "1"                              # stable string id, referenced everywhere
       name: "<etapa em PT-BR>"             # human label
       canonical_texts:                     # reference phrasings the agent uses
         - "<literal message template>"
       expected_customer_intents:           # free-form intent tags you care about
         - "greet_back"
         - "ask_price"
       transitions_to: ["2", "3"]           # downstream step ids

   objection_taxonomy:                      # closed set of objection ids
     - id: "price"
     - id: "time_slot"
     - id: "location"
     # ...

   # optional, business-specific blocks (services, price_grid, promotions...)
   ```

   Look at `input/script.yaml` in this repo as a worked example (Puris Spa,
   9 steps, 9-id objection taxonomy).

3. Authoring tips:
   - **Step ids are stable keys.** Keep them short (`"1"`, `"3.5"`, `"fup1"`).
     Changing an id invalidates downstream labels.
   - **`canonical_texts`** anchor RapidFuzz matching — include the literal
     messages the agent actually sends. Multiple variants per step are fine.
   - **`expected_customer_intents`** are free-form; the LLM is told to pick
     from this list when labeling customer messages at that step.
   - **`objection_taxonomy`** must be a closed set — the Stage 3 prompt
     demands exactly one reply per id. Start with the 9 generic ids shown
     in the sample and trim/add per business.
   - Any change to `script.yaml` bumps `input_hash`, auto-invalidating all
     sentinels on the next run.

4. Run Stage 3 first to validate shape + seed `script_extensions.yaml`:

   ```bash
   uv run python -m scripts.run_pipeline --stage 3
   ```

   Errors here (schema mismatch, duplicate step ids, missing objection ids)
   point directly at `script.yaml`. Fix and re-run.

### Why hand-authored, not generated
The script is business truth. Generating it from the Markdown risks
hallucinated steps and silent drift in intent labels across runs. Stage 3
extensions are bounded ("do not invent services not in the script") for the
same reason.

---

## 8. Ground truth: `data/ground_truth_outcomes.csv`

### Role
Validation harness for Stage 6 (conversion detection). The LLM assigns each
chat a `conversion_score` 0–3 with evidence; ground truth lets you measure
how often that matches human judgment. Not an input to the pipeline — the
pipeline runs without it. Use it to gate whether Stage 6 is trustworthy
before shipping a report.

### When to create
- After Stage 1 runs for the first time on a new `msgstore.db` (need
  `data/conversations.jsonl`).
- Before trusting Stage 6 output on a new deployment.
- Re-do if `msgstore.db` changes materially (new chats, new outcomes) —
  `chat_id` is stable only within a single export.

### How to create
Use the interactive labeler:

```bash
uv run python scripts/label_ground_truth.py
```

Behavior:
- Loads `data/conversations.jsonl`, stratifies by message count into
  short / medium / long tertiles, samples 6 / 8 / 6 = **20 chats**
  (seed=42, deterministic).
- Prints each chat as a timestamped ME/THEM transcript.
- Prompts: `[b]ooked / [l]ost / [a]mbiguous / [s]kip / [q]uit`, then asks
  for free-text notes.
- Appends to `data/ground_truth_outcomes.csv` after each entry — resumable.
  Re-running skips already-labeled `chat_id`s.

Outcome definitions:
- `booked` — clear confirmation (date/time agreed, payment sent, etc.).
- `lost` — clear refusal, ghosted after objection, explicit no.
- `ambiguous` — interest shown but no resolution. Excluded from accuracy
  denominator.

### How to validate Stage 6 against it

```bash
uv run python scripts/verify_stage6.py
# passes if ≥16/20 non-ambiguous chats match
```

### Tuning for a different number
- Change `SHORT_N`, `MED_N`, `LONG_N` in `scripts/label_ground_truth.py`
  to rebalance the sample. Default 20 is enough for a 48%-conversion,
  300-chat base; scale up for noisier data.
- `SEED = 42` — bump if you want a different sample of the same tertiles.
- For high-volume numbers, consider labeling more and stratifying by
  other axes (objection type, week).

---

## 5. Output files — in detail

### Under `data/` (intermediate, re-used across runs)

| File | Producer | Contents |
|------|----------|----------|
| `conversations.jsonl` | Stage 1 | One long chat per line (≥20 msgs, or all phones-file chats). Fields: `chat_id`, `phone`, `messages[{msg_id, ts_ms, from_me, text, text_raw}]`. |
| `conversations_short.jsonl` | Stage 1 | Chats below threshold (skipped when `--phones-file` set). |
| `spa_templates.json` | Stage 2 | Deduped Spa (agent) message templates. `[{template_id, canonical_text, instance_count, ...}]`. RapidFuzz-clustered. |
| `spa_message_template_map.json` | Stage 2 | `msg_id → template_id` lookup. |
| `script_extensions.yaml` | Stage 3 | LLM-generated script expansions (synonym variants per step). Gitignored. |
| `labeled_messages.jsonl` | Stage 4 | Per-message labels: `msg_id, chat_id, from_me, step_id, step_context, intent, objection_type, sentiment, matches_script, deviation_note`. |
| `spa_template_labels.json` | Stage 4 | Per-template step/intent assignment. |
| `customer_labels.json` | Stage 4 | Per-customer-message intent cache. |
| `template_sentiment.json` | Stage 5 | Per-template tone scores: `warmth` (1–5), `clarity` (1–5), `script_adherence` (1–5), `polarity` ∈ {pos, neu, neg}, `critique`. |
| `conversions.jsonl` | Stage 6 | Per-chat outcome: `conversion_score` (0–3), `conversion_evidence`, `first_objection_idx`, `first_objection_type`, `resolution`. |
| `turnarounds.json` | Stage 6 | Chats where agent overcame an objection to book. |
| `lost_deals.json` | Stage 6 | Chats lost after clear intent signal. |
| `aggregations.json` | Stage 7 | Rolled-up stats per step: `on_script_count`, `off_script_count`, `top_intents`, `top_objections`, `top_clusters` (HDBSCAN medoids). |
| `stage{1..8}.done` | orchestrator | Sentinel JSON: `ts`, `git_sha`, `module_version`, `chat_limit`, `phones_hash`, `input_hash`, `llm_mode`, `outputs[]`. |
| `llm_cache/` | `src/llm.py` | Keyed prompt-response cache. Keyed on prompt hash + `input_hash`. Wiped by `--restart`. |

### Under `output/` (final deliverables)

| File | Contents |
|------|----------|
| `report.md` | PT-BR Markdown report, 7 `##` H2 sections: resumo executivo, funil por etapa, objeções dominantes, turnarounds, negócios perdidos, templates avaliados, recomendações. |
| `turnarounds.csv` | `telefone, data, tipo_objecao, mensagem_cliente, resposta_vencedora, confirmacao`. |
| `lost_deals.csv` | Same schema as turnarounds, last-message confirmation blank. |
| `per_step.csv` | `step_id, on_script_count, off_script_count, top_intents, top_objections, top_clusters` (JSON-encoded list cells). |
| `spa_templates_scored.csv` | `template_id, canonical_text, instance_count, warmth, clarity, script_adherence, polarity, critique`. |
| `off_script_clusters.csv` | `step_id, medoid_text, size, example_msg_ids` — HDBSCAN clusters of off-script customer messages. |

---

## 6. Pipeline stages

| # | Module | Uses LLM | Input | Output |
|---|--------|----------|-------|--------|
| 1 | `src.load` | no | `msgstore.db` | `conversations{,_short}.jsonl` |
| 2 | `src.dedupe` | no | `conversations.jsonl` | `spa_templates.json`, `spa_message_template_map.json` |
| 3 | `src.script_index` | yes | `script-comercial.md`, `script.yaml` | `script_extensions.yaml` |
| 4 | `src.label` | yes | conversations + templates + script | `labeled_messages.jsonl`, `spa_template_labels.json`, `customer_labels.json` |
| 5 | `src.sentiment` | yes | `spa_templates.json` | `template_sentiment.json` |
| 6 | `src.conversion` | yes | conversations + labels | `conversions.jsonl`, `turnarounds.json`, `lost_deals.json` |
| 7 | `src.cluster` | no | `labeled_messages.jsonl` | `aggregations.json` |
| 8 | `src.report` | yes | all of the above | `output/report.md` + 5 CSVs |
