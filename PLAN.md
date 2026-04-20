# Spa WhatsApp Conversation Analyzer — Spec (v2)

## Context

Puris SPA sells massage and day-spa packages primarily via WhatsApp in PT-BR. The dataset is an unencrypted Android `msgstore.db` (SQLite 3) at `./msgstore.db` containing **28,113 plain-text messages** across **1,993 chats** over 7 months (2025-09-13 → 2026-04-20). **387 chats have >20 messages** and are real sales conversations.

The commercial script is documented in `./script-comercial.md` — it defines 7 numbered steps (Saudação → Qualificação → Validação → Apresentação → Personalização → Adicionais → Fechamento) plus Follow-Ups and negotiation rules. The script is **authoritative but incomplete**: day-spa pitch flow isn't written, some standardized objection answers aren't there, and agents are explicitly allowed to paraphrase. The pipeline treats the script as ground truth for step labels and deviation detection, but must also discover implicit patterns from the data.

**Goals:**
1. **Per-step report** of the most common customer answers, with the top off-script Q&As per step → drives script expansion and standardized responses.
2. **Positive vs negative tone analysis** of what the spa says, propagated by template so every copy-pasted phrase is scored once.
3. **Turnaround analysis (new):** find conversations where the customer raised an objection (price, location, timing, competitor, "vou pensar", "falar com alguém", "te falo depois", trust/boundary for male clients) and the spa still closed the sale. Contrast with lost-deal cases with the same objection. Output includes the customer's phone number and the winning argument.
4. Do all of this for **~$5–10** in LLM spend.

**Outputs:** PT-BR Markdown report + JSON/CSV artifacts. LLM stack: Claude Haiku 4.5 (bulk) + Sonnet 4.6 (synthesis).

---

## DB facts (confirmed)

- Text filter: `message.message_type = 0 AND text_data IS NOT NULL` → 28,113 rows.
- Sender: `message.from_me` (1 = spa, 0 = customer). SPA ~13,250, CUSTOMER ~14,863.
- Join: `message → chat (chat_row_id = chat._id) → jid (chat.jid_row_id = jid._id)`.
- Customer phone: `jid.user` (bare number, e.g. `5511962719203`) / `jid.raw_string` (`...@s.whatsapp.net`).
- Timestamps: Unix ms in `message.timestamp`.

---

## Pipeline design — 8 stages

Python CLI orchestrated by `scripts/run_pipeline.py`, each stage writes to `data/` so they're resumable. `--stage N` re-runs just one stage; `--chat-limit N` for cheap dry runs.

### Stage 1 — Load & normalize (no LLM)
- **File:** `src/load.py`
- Read `msgstore.db`, join to chat+jid, order by timestamp. Produce `data/conversations.jsonl` — one JSON per chat: `{chat_id, phone, messages: [{msg_id, ts_ms, from_me, text}]}`.
- Keep chats with ≥20 messages (~387). Also emit `data/conversations_short.jsonl` (shorter chats) for aggregate counts but no LLM analysis.
- Clean: strip URLs, collapse whitespace. Keep originals verbatim for report citations.

### Stage 2 — Spa message deduplication (no LLM)
- **File:** `src/dedupe.py`
- Use `rapidfuzz` token-set ratio on accent-normalized text to cluster ~13k spa messages into **canonical templates** (~300–500 expected). Threshold tunable (~88 token_set_ratio).
- Output `data/spa_templates.json`: `[{template_id, canonical_text, instance_count, example_msg_ids, first_seen_ts, last_seen_ts}]`.
- Enables 20–30× LLM cost reduction in Stages 4–5.

### Stage 3 — Script ingestion & expansion (Sonnet 4.6, ~1 call)
- **File:** `src/script_index.py`
- Parse `script-comercial.md` into a structured `data/script.yaml`:
  - Steps 1–7 with canonical copy, expected customer responses, transitions.
  - Negotiation rules (discount only if asked, 5% Mon–Thu day-spa, etc.) as policy flags.
  - Services the spa pushes (Massagem, Mini-day spa, Day-spa) with price grid and add-ons.
  - Objection taxonomy (pre-seeded): `price`, `location`, `time_slot`, `competitor`, `hesitation_vou_pensar`, `delegated_talk_to_someone`, `delayed_response_te_falo`, `trust_boundary_male`, `other`.
- One Sonnet call to **expand** the script: propose a draft Day-Spa pitch flow (missing from script), propose standardized replies for common objections, flag script internal inconsistencies. Output merged into `data/script.yaml` under an `inferred_extensions` section for user review.
- Cost: ~$0.50.

### Stage 4 — Step labeling & objection tagging (Haiku 4.5, batched)
- **File:** `src/label.py`
- Input: ordered messages from Stage 1, script from Stage 3.
- Spa messages: look up template_id (Stage 2) → if first-time template, one Haiku call to assign `{step_id, matches_script: bool, deviation_note}`. Propagate to all instances.
- Customer messages: batch 30 per call → `{msg_id, step_context, intent, objection_type|null, sentiment}`. `objection_type` ∈ the taxonomy above.
- Output `data/labeled_messages.jsonl`.
- Cost: ~$2–3.

### Stage 5 — Tone/quality scoring on spa templates (Haiku 4.5)
- **File:** `src/sentiment.py`
- Score each unique template (~500): `warmth (1–5)`, `clarity (1–5)`, `script_adherence (1–5)`, `polarity (pos/neu/neg)`, `critique` (one line in PT-BR).
- Propagate to instances by template_id.
- Output `data/template_sentiment.json`.
- Cost: ~$0.50.

### Stage 6 — Conversion & turnaround detection (Haiku + Sonnet)
- **File:** `src/conversion.py`
- **6a. Per-conversation conversion score (Haiku, 1 call per chat, ~387 calls):**
  Feed the full message list (truncated intelligently to ≤3k tokens using step boundaries). Model returns:
  `{conversion_score: 0–3, conversion_evidence: "...", first_objection_idx, first_objection_type, resolution_idx|null, winning_reply_excerpt|null, final_outcome: 'booked'|'lost'|'ambiguous'}`.
  Conversion_score 0 = no booking signal, 3 = explicit confirmation ("pode agendar sim", "já fiz o pix", "confirmado para dia X").
  Cost: ~$1.50.

- **6b. Turnaround & lost-deal extraction (no LLM):**
  Turnaround candidate = conversation where `first_objection_type != null` AND `conversion_score ≥ 2` AND `resolution_idx > first_objection_idx`.
  Lost-deal counter-example = same objection type with `conversion_score ≤ 1`.
  Pair turnarounds with 1–2 same-objection lost deals for the report.

- Output `data/conversions.jsonl` + `data/turnarounds.json` (with phone numbers) + `data/lost_deals.json`.

### Stage 7 — Off-script clustering & aggregation (embeddings, no chat LLM)
- **File:** `src/cluster.py`
- For customer messages with `step_context=off_script` or unrecognized intent: embed with `paraphrase-multilingual-MiniLM-L12-v2` (local, free) → HDBSCAN clusters per step.
- Pick medoid as canonical example; count cluster size.
- Aggregate: per step → on-script vs off-script counts, top intents, top off-script clusters, top objection types.
- Output `data/aggregations.json`.

### Stage 8 — Report generation (Sonnet 4.6)
- **File:** `src/report.py`
- Feed Sonnet the compact aggregates + the top 20 turnarounds + the paired lost-deals.
- Produces:
  - `output/report.md` (PT-BR) with sections:
    1. **Resumo executivo** — volumes, taxa de conversão estimada, top 10 insights.
    2. **Análise por etapa do script** — para cada etapa: mensagens mais comuns do cliente, top 5 perguntas/respostas fora do roteiro, sugestão de resposta padronizada.
    3. **O que dizemos que funciona (top 10 templates positivos)** — texto, score, exemplo.
    4. **O que dizemos que pode melhorar (top 10 templates negativos)** — texto, crítica, sugestão.
    5. **Viradas de jogo (top 20 turnarounds)** — para cada: `telefone`, data, tipo de objeção, mensagem do cliente, resposta vencedora do spa, confirmação. Inclui par comparativo "negócio perdido" quando disponível.
    6. **Padrões de argumentação vencedora** — síntese por tipo de objeção (preço, localização, tempo, "vou pensar", "falar com alguém", "te falo depois", comparação com concorrente).
    7. **Lacunas no script** — day-spa pitch, respostas a objeções sem cobertura.
  - `output/turnarounds.csv` — telefone, data, tipo_objecao, mensagem_cliente, resposta_vencedora, confirmacao.
  - `output/lost_deals.csv` — mesma estrutura.
  - `output/per_step.csv`, `output/spa_templates_scored.csv`, `output/off_script_clusters.csv`.
- Cost: ~$2.

---

## File layout

```
/home/martin/aicommerce-analyser/
├── msgstore.db                   # input (exists)
├── script-comercial.md           # input (exists)
├── PLAN.md                       # this file
├── pyproject.toml                # deps: anthropic, rapidfuzz, pyyaml, hdbscan, sentence-transformers
├── .env                          # ANTHROPIC_API_KEY (gitignored)
├── .gitignore                    # ignores .env, data/, output/, *.db
├── scripts/run_pipeline.py       # orchestrator (see "Orchestrator" section below)
├── src/
│   ├── load.py                   # Stage 1
│   ├── dedupe.py                 # Stage 2
│   ├── script_index.py           # Stage 3
│   ├── label.py                  # Stage 4
│   ├── sentiment.py              # Stage 5
│   ├── conversion.py             # Stage 6 (turnaround + lost-deal)
│   ├── cluster.py                # Stage 7
│   ├── report.py                 # Stage 8
│   └── llm.py                    # Claude client + retry + token accounting
├── data/                         # intermediate artifacts (gitignored)
└── output/                       # final deliverables
    ├── report.md
    ├── turnarounds.csv
    ├── lost_deals.csv
    ├── per_step.csv
    ├── spa_templates_scored.csv
    └── off_script_clusters.csv
```

## Orchestrator (`scripts/run_pipeline.py`)

A thin, stateless driver — all real work lives in `src/*.py`. Each stage module exposes a single `run(ctx: Context) -> StageResult` function; the orchestrator just chains them, handles CLI flags, enforces budget, and prints progress.

**CLI:**
```
python -m scripts.run_pipeline [--stage N | --from N | --to N]
                               [--chat-limit N] [--budget-usd X]
                               [--force] [--dry-run]
```

**Responsibilities (and only these):**
1. **Parse args** → build a `Context` dataclass: `db_path`, `script_path`, `data_dir`, `output_dir`, `chat_limit`, `budget_usd`, `force`, Claude client.
2. **Load `.env`** (Anthropic API key) and sanity-check it exists (unless `--dry-run`).
3. **Resolve which stages to run:**
   - Default: run stages 1→8 in order, **skip any stage whose sentinel `data/stageN.done` exists** (resumable).
   - `--stage N`: run only stage N (ignores sentinel).
   - `--from N --to M`: run inclusive range.
   - `--force`: ignore sentinels.
4. **For each stage, in order:**
   - Check that prior stage's outputs exist (fail loudly with a clear message if not).
   - Call `src.<stage_module>.run(ctx)`.
   - On success: write `data/stageN.done` with timestamp + stage version hash.
   - On exception: print a readable error with which stage failed and how to resume (`--stage N`).
5. **Token/budget accounting:** `src/llm.py` exposes a global counter. After each stage, print running total tokens + estimated USD. Abort before starting a stage if projected cost would exceed `--budget-usd`.
6. **Print a final summary:** stages run, elapsed, total cost, output paths.

**What it does NOT do:** no DB queries, no LLM calls, no data transformation. If you find business logic creeping in, move it to the relevant `src/<stage>.py`.

## Token / cost controls

- **Dedup first** — ~13k spa messages → ~500 templates before any LLM call.
- **Template propagation** — spa-side labels/sentiment run once per template.
- **Haiku for bulk, Sonnet for synthesis** — ~12× cheaper on heavy stages.
- **Conversation truncation** — Stage 6 truncates long chats by keeping the first 20 messages around each objection + the ending 15 messages.
- **Resumable stages** — each writes a `data/<stage>.done` sentinel; `--stage N` re-runs only N.
- **Budget guard** — `src/llm.py` accumulates cost; orchestrator aborts if `--budget-usd` exceeded.
- **Projected total: $6–8.**

## Verification

1. **Load sanity:** `wc -l data/conversations.jsonl` ≈ 387; spot-check 2 chats for phone + message count.
2. **Dedup sanity:** top 20 templates by `instance_count` — should include obvious canned messages ("Olá! Bom dia 😊 Tudo bem?...", price quotes).
3. **Script ingestion:** open `data/script.yaml` — confirm 7 steps with correct names, and that `inferred_extensions.day_spa_pitch` looks plausible.
4. **Label spot-check:** random 10 labeled conversations printed with step ids — ≥80% should feel right. Review flagged `matches_script=false` templates to catch systematic errors.
5. **Conversion calibration:** pick 10 chats where you personally know the outcome; compare to `conversion_score`. Adjust prompt if >2 mismatches.
6. **Turnaround sanity:** read the top 5 turnarounds manually — each should show a clear objection → spa reply → booking sequence. Discard false positives and tune detector.
7. **Dry run:** `run_pipeline.py --chat-limit 20 --budget-usd 1.00` runs all 8 stages end-to-end for ~$0.50 before the full run.
8. **Final review:** open `output/report.md` → verify 5 turnarounds include real phone numbers matching known bookings, and that PT-BR reads naturally.

---

## Test plan

Framework: **`pytest`**. Tests live under `tests/` mirroring `src/`. Each test file named `test_<module>.py`. Run with `pytest -q` (fast subset) or `pytest -m integration` (slow / needs API key).

**Shared fixtures (`tests/conftest.py`):**
- `tiny_db` — builds a temp SQLite file in-memory with ~3 fake chats (one long, one short, one converted) populating `message`, `chat`, `jid` with the exact schema of `msgstore.db`. Used by Stages 1, 6.
- `sample_conversations` — hand-crafted JSONL with 5 conversations including: a textbook script-follow case, a price-objection turnaround, a location-objection lost deal, a "vou pensar" hesitation, a noisy mixed-language chat.
- `fake_llm` — monkey-patches `src.llm.complete` to return canned JSON per-prompt-hash. Makes Stages 3–6 deterministic and offline.
- `real_llm` (marker `@pytest.mark.integration`) — uses real Anthropic API, capped at tiny inputs (≤5 calls per run).

### Stage 1 — `tests/test_load.py`
- **Unit**
  - `test_load_filters_message_type_0` — inject rows with types 0/1/7 into `tiny_db`; only type 0 survives.
  - `test_load_drops_null_text` — messages with `text_data IS NULL` are excluded.
  - `test_load_orders_by_timestamp` — messages within a chat are monotonically ordered by `ts_ms`.
  - `test_load_threshold_min_messages` — chat with 19 messages excluded; chat with 20 included (parametrize boundary).
  - `test_load_strips_urls_and_whitespace` — input with `https://...` and `\n\n  ` → cleaned.
  - `test_load_preserves_original_text` — cleaned copy used for dedup, but original kept in the JSONL for citation.
  - `test_load_extracts_phone` — `jid.user="5511..."` ends up in the per-conversation `phone` field.
- **Integration**
  - `test_load_against_real_db` (marker `slow`) — run against `msgstore.db`, assert ~387 chats produced, no exceptions.

### Stage 2 — `tests/test_dedupe.py`
- **Unit**
  - `test_exact_duplicates_collapse` — two identical strings → one template with `instance_count=2`.
  - `test_fuzzy_near_duplicates_collapse` — "Olá bom dia 😊" vs "Ola, bom dia!" → one template (accent/punct normalization).
  - `test_different_messages_stay_separate` — greeting vs price quote never merged.
  - `test_threshold_boundary` — parametrize rapidfuzz score at 87 (stay split) vs 89 (merge) around threshold 88.
  - `test_template_metadata` — `first_seen_ts ≤ last_seen_ts`, `example_msg_ids` non-empty.
  - `test_only_from_me_1` — customer messages never enter the template set.
- **Integration**
  - `test_dedupe_top_templates_are_plausible` — against real data, top 5 templates include a greeting and a price message (checked by keyword: "bom dia", "R$").

### Stage 3 — `tests/test_script_index.py`
- **Unit (no LLM)**
  - `test_parse_script_extracts_7_steps` — parses `script-comercial.md`, yields steps keyed by id (1, 2, 3, 3.5, 5, 6, 7).
  - `test_parse_price_grid` — extracts the price table (Massagens R$200, Mini-day spa R$420, etc.) as structured rows.
  - `test_parse_negotiation_rules` — "nunca ofereça desconto sem a pessoa pedir" lands in policy flags.
  - `test_objection_taxonomy_preseeded` — the 9 canonical objection types are present in the YAML.
- **Integration (`@pytest.mark.integration`)**
  - `test_expansion_produces_day_spa_pitch` — real Sonnet call on a 2-step subset, asserts `inferred_extensions.day_spa_pitch` exists and mentions "escalda-pés" or "banho de imersão".

### Stage 4 — `tests/test_label.py`
- **Unit (with `fake_llm`)**
  - `test_spa_template_labeled_once` — two instances of the same template → only 1 LLM call, both labels identical.
  - `test_customer_batching_respects_size` — 65 customer messages → exactly 3 Haiku calls (batches of 30, 30, 5).
  - `test_label_schema_validated` — malformed LLM response (missing `step_id`) raises a parseable error; retry logic kicks in.
  - `test_off_script_flagged` — customer asking "tem estacionamento?" (not in script) → `step_context=off_script`.
  - `test_objection_type_recognized` — "achei caro" → `objection_type=price`; "muito longe" → `location`; "vou pensar" → `hesitation_vou_pensar`.
- **Integration (`@pytest.mark.integration`)**
  - `test_real_haiku_on_10_messages` — one real call, assert output parses as valid JSON with all required keys.

### Stage 5 — `tests/test_sentiment.py`
- **Unit (with `fake_llm`)**
  - `test_scores_in_range` — every template gets `warmth`, `clarity`, `assertiveness` in [1, 5] and `polarity` ∈ {pos, neu, neg}.
  - `test_propagation_by_template_id` — 200-instance template scored once → all 200 instances carry the same scores.
  - `test_critique_is_portuguese` — fake LLM returns PT-BR critiques; assertion on character set / common PT words.
- **Integration (`@pytest.mark.integration`)**
  - `test_real_sentiment_polarity` — 3 templates: known-warm, known-cold, known-neutral → polarity matches intuition.

### Stage 6 — `tests/test_conversion.py`
- **Unit (with `fake_llm`)**
  - `test_conversion_score_parsed` — fake response with `conversion_score=3` → returned structure has score=3, outcome='booked'.
  - `test_turnaround_detected` — conversation with first_objection=price at idx 4, conversion_score=3 → appears in `turnarounds.json`.
  - `test_no_turnaround_when_no_objection` — converted chat with no objection → not in turnarounds.
  - `test_lost_deal_detected` — same structure but conversion_score=0 → appears in `lost_deals.json`.
  - `test_phone_number_attached` — each turnaround entry has the original `jid.user` as phone.
  - `test_lost_deal_pairing` — each turnaround with objection_type=X has ≤2 paired lost deals of the same type.
  - `test_truncation_of_long_chat` — chat with 200 messages → input to LLM is ≤3k tokens and still includes first objection + ending.
  - `test_all_objection_types_covered` — for each of the 9 taxonomy types, a synthetic chat gets classified correctly (parametrized).
- **Integration (`@pytest.mark.integration`)**
  - `test_real_haiku_on_one_conversation` — one real Haiku call on a known-converted chat, assert `conversion_score ≥ 2`.

### Stage 7 — `tests/test_cluster.py`
- **Unit (local embeddings, no API)**
  - `test_embeddings_deterministic` — same input twice → identical vectors (fix seed).
  - `test_clustering_groups_paraphrases` — 3 variants of "quanto custa?" + 3 variants of "onde fica?" → 2 clusters.
  - `test_medoid_selection` — cluster medoid is the message with minimum summed distance to others.
  - `test_empty_input_handled` — no off-script messages → empty aggregation, no crash.
  - `test_aggregation_counts_match_inputs` — sum of cluster sizes + noise = total off-script messages.

### Stage 8 — `tests/test_report.py`
- **Unit (with `fake_llm`)**
  - `test_report_sections_present` — output `.md` contains all 7 required section headers in PT-BR.
  - `test_turnarounds_csv_schema` — CSV has columns: `telefone, data, tipo_objecao, mensagem_cliente, resposta_vencedora, confirmacao`.
  - `test_top20_cap` — if input has 50 turnarounds, report lists exactly 20 (highest-quality).
  - `test_phone_numbers_in_md` — turnaround section contains at least one phone-like string `5511...`.
  - `test_csv_utf8_roundtrip` — accented PT-BR (ção, é) round-trips through CSV without mojibake.
- **Integration (`@pytest.mark.integration`)**
  - `test_real_sonnet_small_report` — 3 turnarounds + 2 lost deals → real Sonnet call produces valid Markdown that parses without errors.

### `src/llm.py` — `tests/test_llm.py`
- `test_retry_on_rate_limit` — mock 429, 429, 200 → third call succeeds, 2 retries logged.
- `test_token_accounting` — after 3 canned responses, total input/output tokens = sum of per-call counts.
- `test_budget_abort` — setting `budget_usd=0.001` before a 5000-token call → raises `BudgetExceeded`.
- `test_structured_output_schema` — asking for JSON with a schema; response parsed into the dataclass; malformed response raises `SchemaError`.

### Orchestrator — `tests/test_pipeline.py`
- `test_stage_sentinels_written` — after Stage 1 succeeds, `data/stage1.done` exists.
- `test_skip_completed_stages` — sentinel present → stage function not called (mock asserts `call_count==0`).
- `test_force_flag_reruns` — `--force` ignores sentinels.
- `test_missing_prior_output_fails_loudly` — skip Stage 1, try Stage 2 → error mentions "Stage 1 output missing".
- `test_chat_limit_propagates` — `--chat-limit 20` → only 20 conversations in Stage 1 JSONL.
- `test_budget_abort_stops_pipeline` — Stage 2 exceeds budget → Stage 3 never starts.
- **Integration (`@pytest.mark.integration`)**
  - `test_end_to_end_on_5_chats` — `--chat-limit 5` with real API, completes under $0.20, produces all expected files in `output/`.

### Test running

- `pytest -q` — fast units only (all with `fake_llm`), should run in <30 s, no API key needed.
- `pytest -m integration` — paid tests, run manually before big milestones.
- `pytest --cov=src --cov-report=term-missing` — aim for ≥85% on pure logic modules (`load`, `dedupe`, `cluster`, `script_index`).
- Pre-commit hook: `pytest -q` must pass before commit.

