# Task breakdown for the Spa WhatsApp Analyzer pipeline

## Context

`PLAN.md`  specifies the architecture of an 8-stage pipeline that analyzes 28k spa-sales WhatsApp messages to produce a PT-BR Markdown report plus CSV artifacts. The spec is solid but written at a design level — a developer reading it today cannot start pulling tickets. This plan breaks `PLAN.md` into **dev-ticket-grain tasks** (~2–6h each) with inputs, outputs, acceptance criteria, and a concrete verification command per task.

The repo is **greenfield**: only `PLAN.md`, `script-comercial.md`, and `msgstore.db` exist. No `src/`, no `pyproject.toml`, no tests yet.

### Goals of this breakdown
1. Make every stage **pickable**: a developer can start any task without re-reading the whole spec.
2. Front-load **business value**: a stakeholder-visible `output/report.md` exists by end of day 1 (even if hollow), so the loop "tune prompts → regenerate → read report" starts immediately.
3. Keep tests **cheap and honest**: pytest for pure logic, smoke scripts for everything LLM-dependent.
4. Stay under the **$10 LLM budget** by gating full-corpus runs behind a passing 5-chat dry run.

### Decisions recorded from user
- **Test style:** pragmatic mix — pytest for pure logic (`load`, `dedupe`, `cluster`, `script_index`, `llm`), executable smoke scripts (`scripts/verify_stageN.py`) for LLM stages.
- **Build order:** vertical slice first (M1: thin E2E with stubbed LLM stages), then deepen stage by stage (M2).
- **Ground truth:** user will hand over a CSV of ~10 chat_ids with known outcomes (booked / lost / ambiguous) for Stage 6 calibration.
- **Task grain:** dev-ticket (~2–6h), with file paths, function signatures, verification commands.

### Corrections to `PLAN.md`
These are noted so the developer doesn't get stuck:

1. **Path typo in file layout** — `PLAN.md` §"File layout" writes `/home/martin/aicommerce-analyser/`; the real working directory is `/home/martin/whatsapp-commercial-analyzer/`. Use the latter.
2. **Day-Spa pitch is NOT missing** — `script-comercial.md` has a "Day Spa" section (2h and 3h variants, Essência Puris / Conexão / Pausa Entre Amigas / Imersão Puris / Ritual de Conexão / Celebração) with a price block. Stage 3 should **re-structure** this existing content into `script.yaml`, not invent it. Expansion via LLM should still propose missing pieces (transition phrasing, upsell hooks).
3. **Step IDs are non-contiguous:** `1, 2, 3, 3.5, 5, 6, 7` — there is no step 4. Parser must not assume monotonic integer IDs. Store ids as strings.
4. **Sentiment rubric mismatch:** `PLAN.md` §Stage 5 text lists `warmth, clarity, script_adherence, polarity, critique`; the Stage 5 test block lists `warmth, clarity, assertiveness`. **Canonical:** `warmth (1–5), clarity (1–5), script_adherence (1–5), polarity (pos/neu/neg), critique (PT-BR string)`.
5. **Promoções section exists:** `script-comercial.md` has a "PROMOÇÕES DE DIA DAS MÃES" block with time-bound offers (sales 16/04–17/05, valid until 31/06). Stage 3 must capture these with `valid_from` / `valid_until` dates so Stage 4 can flag whether an agent quoted promo pricing in or out of window.

---

## Revision v2 — design decisions (2026-04-20)

Applied after review pass. Supersedes any earlier inline statement that conflicts.

1. **Template → message map:** `SpaTemplate.example_msg_ids` stays (examples only). Full mapping lives in a new sidecar file `data/spa_message_template_map.json` — `{msg_id: template_id}`. Stage 2 writes it; Stage 4 reads it to propagate template labels.
2. **Stage 6 prereq fix:** `M2-S6-T1` (truncation) and `M2-S6-T2` (conversion detection) depend on **`M2-S4-T2`** (customer batching) — objection indices come from Stage 4 labels, not from Stage 6 itself. PROGRESS + dependency table updated.
3. **Spa-side `step_context` derived:** `LabeledMessage.step_context` for spa messages is **derived** from `matches_script`: `true → "on_script"`, `false → "off_script"`. No extra LLM field. `transition`/`unknown` reserved for customer-side ambiguity.
4. **Stage 4 customer batching — cross-chat, 30 per call:** Batches pack 30 customer messages drawn across chats to max fill. Each message in prompt carries its own `chat_id` + `step_context_hint` (last 3 spa msgs from the same chat). Trades prompt-build complexity for fewer calls. Per-chat batching rejected (wasted slots on short chats).
5. **FUPs are first-class steps:** `script.yaml` has **9 step entries** — `"1","2","3","3.5","5","6","7","fup1","fup2"`. Stage 4 may assign `step_id ∈ {fup1,fup2}` to spa messages resembling follow-up templates (silence bumps, "ainda com interesse?" etc.). `ScriptStep.id` already a string — no schema change.
6. **Script file split:**
   - `data/script.yaml` — committed, hand-curated source of truth (M1-T4).
   - `data/script_extensions.yaml` — gitignored, LLM-generated via M2-S3-T1. Stage 3 writes **here**, not into `script.yaml`. Stages 4/8 load both and merge in memory.
7. **Turnaround ranking (Stage 8):** `score = conversion_score × clarity`, where `clarity` comes from `TemplateSentiment.clarity` of the template containing `winning_reply_msg_id`. Requires reverse lookup via the Decision-1 sidecar map. Length-proxy heuristic dropped.
8. **Prompt caching — tracked, not enforced:** `src/llm.py` accumulates `cache_read_input_tokens` + `cache_creation_input_tokens` and reports them in `get_usage_report()`. **No** `cache_control` breakpoints added to prompts. If budget runs tight, revisit.
9. **Ground truth — 20 chats:** M3-T1 labels **20** (stratified: 6 short, 8 medium, 6 long). M3-T2 ship threshold = **≥16/20** match.
10. **`final_outcome=booked` is text-inferred only:** No CRM/booking-DB cross-check. Accept text false-positives ("vou agendar sim" that never materialized). Documented limitation, not a bug.
11. **DB schema confirmed (2026-04-20):** `message(_id, chat_row_id, from_me, message_type, text_data, timestamp, sender_jid_row_id, sort_id, key_id, ...)` · `chat(_id, jid_row_id, group_type, ...)` · `jid(_id, user, raw_string, ...)`. Counts: 34,249 total msgs; **28,113** with `message_type=0 AND text_data IS NOT NULL`; 2,013 chats; 18,495 jids. Stage 1 must assert/filter `chat.group_type=0` (no group chats in analysis).
12. **Model IDs — aliased (undated):** Use `claude-sonnet-4-6` and `claude-haiku-4-5` everywhere. Drop the dated suffix `-20251001`. Price table in `src/llm.py` keyed on the aliased names.
13. **Dedupe O(N²) accepted:** `rapidfuzz.process.cdist` on ~13k spa messages → 169M pairs, ~1–2 GB RAM. Feasible on dev box. No blocking step needed unless runtime >5 min.
14. **Aggregation schema added:** New Pydantic models `PerStepAgg`, `OffScriptCluster`, `Aggregation`. See updated Shared schemas section.

---

## Revision v3 — design decisions (2026-04-20)

Applied after user requested Max-subscription support + phone-list testing mode. Supersedes conflicting earlier statements.

1. **LLM auth — dual-client with fallback.** `src/llm.py` holds two long-lived clients: `MaxClient` (via `claude-agent-sdk`, OAuth session from `claude login`) and `ApiClient` (raw `anthropic.Anthropic(api_key=...)`). Single `complete()` dispatcher tries Max path first; on Agent SDK rate-limit / quota-exhausted exception, flips `max_exhausted=True` with reset timestamp and routes subsequent calls to `ApiClient`. Budget guard (`BudgetExceeded`) applies **only to API path** — Max calls are flat-rate, accounted as `cost_usd=0`. Accumulator splits into `{max, api, fallback_events}` for reporting. Supersedes v2 Decision 12's implicit single-path assumption.
2. **LLM mode CLI flag.** `--llm-mode {max,api,hybrid}` selects dispatcher behavior. Default = `hybrid` when both creds present, else whichever is available. `api` alone = skip Max entirely (bypasses OAuth). `max` alone = no fallback; raises on quota exhaust.
3. **Config precedence (M0-T2 init):**
   - Both `ANTHROPIC_OAUTH_TOKEN` (or `claude login` session file) + `ANTHROPIC_API_KEY` present → Max primary, API fallback.
   - Only OAuth → Max-only mode.
   - Only API key → API-only mode.
   - Neither → `ConfigError` at client init.
4. **Phone-list testing mode.** New CLI flag `--phones-file <path>`: text file with one bare phone per line (e.g. `5511962719203`, no `+`, no spaces). Stage 1 filters by `jid.user IN phones_set` and **bypasses the 20-message threshold** — every matched chat goes to `conversations.jsonl` regardless of length (user intent: full chat of those numbers, even if short). Mutually exclusive with `--chat-limit`.
5. **Sentinel cache-busting for filter modes.** `data/stageN.done` sentinels gain a `phones_hash: sha256(sorted(phones_set))` field (or `null` if no phones filter). Orchestrator re-runs stage if hash differs from sentinel, even without `--force`. Prevents contamination when swapping between full run, `--chat-limit`, and `--phones-file` modes.

---

## Revision v4 — resume + input folder (2026-04-22)

Applied after Stage 4 credit-exhaust incident. Every LLM-bearing stage must tolerate mid-run crash without re-paying for completed calls. Supersedes conflicting earlier statements.

1. **`input/` folder is the single source of pipeline inputs.** Contents: `input/msgstore.db`, `input/script-comercial.md`, `input/script.yaml`. `data/` holds ONLY derived artifacts (stage outputs, sentinels, extension script, LLM cache). Repo root is no longer a dumping ground for inputs. `.gitignore` tracks `input/script.yaml` + `input/script-comercial.md`, ignores `input/msgstore.db`. Updates: `Context` gains `input_dir` + `script_yaml_path` fields; `--input-dir` / `--db-path` / `--script-path` / `--script-yaml` CLI flags default off `input_dir`. Stage 3 (`src/script_index.py`) and Stage 4 (`src/label.py`) read `script.yaml` from `ctx.script_yaml_path`. **Stage 3 sentinel `outputs` must NOT list `script.yaml`** — the orchestrator deletes listed outputs on cache-bust, and deleting an input is destruction.
2. **Resume-by-default; `--restart` opts out.** Orchestrator skips stages whose sentinel matches on `chat_limit`, `phones_hash`, AND new `input_hash` (see §4). `--force` remains as a deprecated alias for `--restart`. On `--restart`, `scripts.run_pipeline.purge_state` wipes `data/` (except `ground_truth_outcomes.csv`) and `data/llm_cache/`, then every stage re-runs fresh.
3. **LLM-call-level disk cache — THE resume mechanism.** `src/llm.py::ClaudeClient.complete()` computes `sha256(model, messages, system, response_format_name, response_format_schema, input_hash)` and short-circuits on cache hit. Hits: zero API cost, counted in `usage["cache"]` bucket. Misses write atomically (`os.replace` of `<key>.json`) after the API returns a validated result. Cache wired via `ClaudeClient.set_cache(dir, input_hash)`, called from `Context.from_args` after `ClaudeClient` init. Cache lives in `data/llm_cache/`. **This is what makes stage 4 credit-exhaust recoverable:** after a crash, re-running sweeps through the same batches and every completed LLM call returns from disk instantly — API budget only pays for the tail of unfinished batches.
4. **`input_hash` field on sentinels — auto cache-bust on input edit.** `Context.input_hash = sha256(sha256(msgstore.db) | sha256(script-comercial.md) | sha256(script.yaml))[:16]`, computed in `Context.__post_init__`. Sentinels record this hash; `sentinel_valid` rejects mismatches. When sentinel is invalid the orchestrator clears the stage's listed outputs (not the sentinel-owned inputs) before re-running. Editing anything under `input/` auto-triggers full re-run — no `--restart` needed.
5. **Per-stage artifact must persist incrementally.** Every stage that issues multiple LLM calls MUST write its result artifact after every LLM response, not only at stage end. Pattern: `threading.Lock` + atomic temp+`os.replace`. Already applied to `src/label.py::label_spa_templates`, `src/label.py::label_customer_messages`, `src/sentiment.py::score_templates`. Each also **loads existing artifact at start** and skips items already labeled. Combined with §3, a re-run skips fully labeled items entirely (no LLM call), and cache-hits the rest (no $). Stage 4 `run()` drops its prior "file exists → skip" gates since outer sentinel + incremental loading handle resume.
6. **Convention for new LLM-bearing stages (Stages 6/7/8, future 9+).** Every new stage meeting the following criteria MUST follow §5: (a) it issues more than one LLM call, OR (b) its single LLM call costs > $0.25. Test: inject `Ctrl-C` mid-stage; second invocation must complete with `usage["cache"]["calls"] > 0` and `usage["api"]["cost_usd"]` near zero. Stage 8 single-shot report generation is exempt from (a) but bound by (b) — it's already cached via §3 by virtue of routing through `ctx.client.complete()`.
7. **Partial-artifact file format stays final-format.** Partial writes use the stage's final JSON/JSONL schema (not a separate "progress" format). Advantage: no post-run consolidation step; a crash leaves a valid-but-short artifact that downstream stages will refuse for prereq checks until the stage completes and writes the sentinel. Disadvantage (accepted): another stage that naively consumes the partial artifact without checking the sentinel would see partial data. The `STAGE_PREREQS` + `sentinel_valid` gate already prevents that.

---

## Build strategy: vertical slice, then deepen

```
M0  Scaffolding        (1 day)   →  repo compiles, tests run, CLI prints help
M1  Vertical slice     (1 day)   →  --chat-limit 5 produces a full (hollow) report.md for <$0.50
M2  Deepening passes   (5 days)  →  each stage upgraded from stub to real; slice is regression oracle
M3  Full-run + report  (1 day)   →  --chat-limit 0 on 387 chats, human review, script v2 draft
```

Every M2 deepening task must keep the M1 slice passing. "`scripts/run_pipeline.py --chat-limit 5` exits 0 and regenerates `output/report.md`" is the standing green-bar.

---

## File layout (confirmed)

```
/home/martin/whatsapp-commercial-analyzer/
├── msgstore.db                      # input (exists, 26 MB)
├── script-comercial.md              # input (exists, 20 KB)
├── PLAN.md                          # spec (exists)
├── pyproject.toml                   # NEW — managed with uv
├── uv.lock
├── .env                             # NEW, gitignored; ANTHROPIC_API_KEY=...
├── .gitignore                       # NEW; ignores .env, data/, output/, __pycache__
├── scripts/
│   ├── run_pipeline.py              # orchestrator
│   ├── verify_stage1.py             # smoke scripts per stage (executable)
│   ├── verify_stage2.py
│   ├── ...
│   └── label_ground_truth.py        # helper: user labels 20 sample chats
├── src/
│   ├── __init__.py
│   ├── context.py                   # shared Context dataclass
│   ├── llm.py                       # Anthropic client + retry + budget + token accounting
│   ├── schemas.py                   # Pydantic models for every JSON artifact
│   ├── load_1.py                      # Stage 1
│   ├── dedupe_2.py                    # Stage 2
│   ├── script_index_3.py              # Stage 3
│   ├── label_4.py                     # Stage 4
│   ├── sentiment_5.py                 # Stage 5
│   ├── conversion_6.py                # Stage 6
│   ├── cluster_7.py                   # Stage 7
│   └── report_8.py                    # Stage 8
├── prompts/                         # all LLM prompts as .md files (versioned)
│   ├── stage3_expand_script.md
│   ├── stage4_spa_template.md
│   ├── stage4_customer_batch.md
│   ├── stage5_sentiment.md
│   ├── stage6_conversion.md
│   └── stage8_report.md
├── data/                            # intermediate artifacts (gitignored)
│   └── ground_truth_outcomes.csv    # user-provided: chat_id,outcome,notes
├── output/                          # final deliverables (gitignored)
└── tests/
    ├── conftest.py                  # fixtures: tiny_db, sample_conversations
    ├── fixtures/
    │   ├── tiny.db                  # 3-chat SQLite built by tools/build_tiny_db.py
    │   └── sample_conversations.jsonl
    ├── test_load.py
    ├── test_dedupe.py
    ├── test_script_index.py
    ├── test_cluster.py
    ├── test_llm.py
    └── test_pipeline.py             # orchestrator sentinels, --force, budget abort
```

Pytest-less LLM stages (4, 5, 6, 8) are verified by `scripts/verify_stageN.py` that runs the stage on `tests/fixtures/sample_conversations.jsonl` against the real API (≤5 calls, <$0.10) and asserts on output files.

---

## Shared schemas (`src/schemas.py`)

Every artifact has a Pydantic model. Defining them once unblocks parallel work on consumer stages. **Build this FIRST** (task `M0-T3`).

```python
class Message(BaseModel):
    msg_id: int
    ts_ms: int
    from_me: bool
    text: str              # cleaned
    text_raw: str          # original, for citations

class Conversation(BaseModel):
    chat_id: int
    phone: str             # bare number, e.g. "5511962719203"
    messages: list[Message]

class SpaTemplate(BaseModel):
    template_id: int
    canonical_text: str
    instance_count: int
    example_msg_ids: list[int]  # examples only; full map in data/spa_message_template_map.json
    first_seen_ts: int
    last_seen_ts: int

# Stage 2 also writes data/spa_message_template_map.json — {str(msg_id): template_id}.
# Stage 4 reads it to propagate template labels to every instance.

class ScriptStep(BaseModel):
    id: str                # "1","2","3","3.5","5","6","7","fup1","fup2" (9 steps incl. FUPs)
    name: str
    canonical_texts: list[str]
    expected_customer_intents: list[str]
    transitions_to: list[str]

class ObjectionType(BaseModel):
    id: Literal["price","location","time_slot","competitor",
                "hesitation_vou_pensar","delegated_talk_to_someone",
                "delayed_response_te_falo","trust_boundary_male","other"]
    name_pt: str
    triggers: list[str]

class LabeledMessage(BaseModel):
    msg_id: int
    chat_id: int
    from_me: bool
    step_id: str | None
    # For spa messages, step_context is DERIVED: matches_script=true -> "on_script",
    # matches_script=false -> "off_script". "transition"/"unknown" only for customer side.
    step_context: Literal["on_script","off_script","transition","unknown"]
    intent: str | None                    # customer only
    objection_type: str | None            # customer only, nullable
    sentiment: Literal["pos","neu","neg"] | None  # customer only
    matches_script: bool | None           # spa only
    deviation_note: str | None            # spa only

class TemplateSentiment(BaseModel):
    template_id: int
    warmth: int           # 1..5
    clarity: int          # 1..5
    script_adherence: int # 1..5
    polarity: Literal["pos","neu","neg"]
    critique: str         # PT-BR

class ConversationConversion(BaseModel):
    chat_id: int
    phone: str
    conversion_score: int                 # 0..3
    conversion_evidence: str
    first_objection_idx: int | None
    first_objection_type: str | None
    resolution_idx: int | None
    winning_reply_excerpt: str | None
    final_outcome: Literal["booked","lost","ambiguous"]

class Turnaround(BaseModel):
    chat_id: int
    phone: str
    date: str                             # YYYY-MM-DD of first_objection
    objection_type: str
    customer_message: str
    winning_reply: str
    winning_reply_msg_id: int             # for template lookup (clarity score in Stage 8 ranking)
    confirmation: str
    paired_lost_deals: list[int]          # up to 2 chat_ids

# Stage 7 aggregates

class OffScriptCluster(BaseModel):
    step_id: str                          # parent step (last preceding spa step before off-script msg)
    medoid_text: str
    size: int
    example_msg_ids: list[int]

class PerStepAgg(BaseModel):
    step_id: str
    on_script_count: int
    off_script_count: int
    top_intents: list[tuple[str, int]]    # [(intent, count), ...]
    top_clusters: list[OffScriptCluster]
    top_objections: list[tuple[str, int]] # [(objection_type, count), ...]

class Aggregation(BaseModel):
    per_step: dict[str, PerStepAgg]       # keyed by step_id
    off_script_clusters: list[OffScriptCluster]  # global noise bucket (no parent step)
```

---

## Milestone M0 — Scaffolding

### M0-T1 · Bootstrap project (uv, pyproject, gitignore, .env)
**Files:** `pyproject.toml`, `uv.lock`, `.gitignore`, `.env.example`, `README.md` (minimal)
**Effort:** 1–2h
**What to build:**
- `uv init --python 3.11` and add deps: `anthropic>=0.40`, `claude-agent-sdk`, `rapidfuzz`, `pyyaml`, `pydantic>=2`, `python-dotenv`, `sentence-transformers`, `hdbscan`, `pandas`, `tqdm`; dev: `pytest`, `pytest-cov`, `ruff`.
- `.gitignore` includes: `.env`, `data/`, `output/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.venv/`, `*.db-journal`.
- `.env.example` with `ANTHROPIC_API_KEY=` and comment noting that `claude login` (Agent SDK OAuth for Max subscription) is an alternative or complement — see M0-T2.
- Top-level `README.md` with `uv sync && uv run pytest -q && uv run python -m scripts.run_pipeline --chat-limit 5` quickstart. Note auth setup: either run `claude login` (Max) or set `ANTHROPIC_API_KEY` (paid API), or both for hybrid mode.

**Verification:**
```bash
uv sync && uv run python -c "import anthropic, rapidfuzz, hdbscan, sentence_transformers; print('ok')"
```

### M0-T2 · `src/llm.py` — dual-client dispatcher (Max + API fallback), retry, budget, token accounting
**Files:** `src/llm.py`, `tests/test_llm.py`
**Effort:** 5–6h
**What to build:**
- Two adapter classes behind a single dispatcher:
  - `MaxClient` — wraps `claude-agent-sdk`. Auth via OAuth session (from `claude login`). No per-token cost.
  - `ApiClient` — wraps `anthropic.Anthropic(api_key=...)`. Reads `ANTHROPIC_API_KEY` via `python-dotenv`. Per-token cost applies.
  - Both expose the same internal `_complete(model, messages, system, max_tokens, response_format) -> (BaseModel|str, UsageDelta)` signature.
- Singleton `ClaudeClient` dispatcher:
  - Init reads `llm_mode` (`max` | `api` | `hybrid`) from `ctx` (CLI flag, default `hybrid`). Resolves available creds per Revision v3 Decision 3. Raises `ConfigError` if required creds missing for chosen mode.
  - `complete(model, messages, system, max_tokens, response_format) -> BaseModel | str`:
    - `hybrid` mode: try `MaxClient._complete` first. On `RateLimitError` / quota-exhausted from Agent SDK → set `max_exhausted=True` with `reset_ts` (parsed from exception if present, else now + 1h), route this call to `ApiClient._complete`. Further calls go straight to API until `time.time() > reset_ts` (then retry Max).
    - `max` mode: no fallback; rate-limit propagates up.
    - `api` mode: skip Max entirely.
  - Retry on `RateLimitError`, `APIConnectionError`, `APITimeoutError` — exponential backoff, 5 attempts max per path. Hybrid-mode Max rate-limit after first retry triggers fallback (don't burn all 5 attempts on Max).
  - Token accounting: module-level `Accumulator` with split buckets:
    ```python
    {
      "max": {"calls": int, "input_tokens": int, "output_tokens": int,
              "cache_read_input_tokens": int, "cache_creation_input_tokens": int},
      "api": {"calls": int, "input_tokens": int, "output_tokens": int,
              "cache_read_input_tokens": int, "cache_creation_input_tokens": int,
              "cost_usd": float},
      "fallback_events": [{"ts": float, "reason": str, "model": str}, ...]
    }
    ```
    Max-path `cost_usd` omitted (flat subscription, reported as `$0`).
  - Cost estimation (API path only): hardcoded price table keyed on aliased model IDs `claude-haiku-4-5` and `claude-sonnet-4-6`. Raises `BudgetExceeded` if `api.cost_usd + projected_call_cost > budget_usd` (env `LLM_BUDGET_USD`, default $10). Max-mode runs have no budget guard.
  - Structured output (both paths): if `response_format` is a Pydantic model, use tool-use pattern — describe schema as an Anthropic `tool`, force `tool_choice`, parse result. Raises `SchemaError` on parse failure after retries. Both SDKs support identical tool-use payloads.
  - Prompt caching: **tracked, not enforced** per v2 Decision 8. No `cache_control` breakpoints. Agent SDK applies caching automatically; `cache_read_input_tokens` / `cache_creation_input_tokens` land in the `max` bucket.
- Expose `get_usage_report() -> dict` (full Accumulator) and `reset_usage()`.

**Tests (`tests/test_llm.py`, pytest with `monkeypatch` — all offline):**
- `test_retry_on_rate_limit_api_path` — API mode, mock 429, 429, 200 → third succeeds, 2 retries logged under `api`.
- `test_hybrid_fallback_on_max_rate_limit` — Max mock raises rate-limit → API mock called once → result returned; `fallback_events` has 1 entry; subsequent calls in same run go straight to API.
- `test_hybrid_resume_max_after_reset` — after `reset_ts` passes, next call attempts Max again.
- `test_max_mode_no_fallback_propagates` — `llm_mode=max`, Max raises rate-limit → exception propagates, API mock never called.
- `test_api_mode_skips_max` — `llm_mode=api`, Max mock never initialized/called.
- `test_token_accounting_split_buckets` — 2 Max calls + 1 API call with known token counts → `max` and `api` buckets match.
- `test_budget_guard_api_path_only` — 1000 Max calls (free) + API call that would exceed → `BudgetExceeded` raised on API attempt only; Max calls never trigger guard.
- `test_budget_abort_pre_call` — `budget_usd=0.001`, API mode, 5k-token call → `BudgetExceeded` before request sent.
- `test_structured_output_schema_both_paths` — `response_format=Foo` with malformed tool response on Max → fallback to API → API also malformed → `SchemaError` raised.
- `test_config_error_no_creds` — neither OAuth session nor API key present → `ConfigError` at init.

**Acceptance:** `uv run pytest tests/test_llm.py -q` green; runs offline (no API calls, no OAuth check).

### M0-T3 · `src/schemas.py` + `src/context.py`
**Files:** `src/schemas.py`, `src/context.py`
**Effort:** 2h
**What to build:**
- All Pydantic models from the "Shared schemas" section above.
- `Context` dataclass: `db_path: Path`, `script_path: Path`, `data_dir: Path`, `output_dir: Path`, `prompts_dir: Path`, `chat_limit: int | None`, `phones_filter: frozenset[str] | None`, `phones_hash: str | None` (sha256 of sorted phones, or `None`), `llm_mode: Literal["max","api","hybrid"]`, `budget_usd: float`, `force: bool`, `dry_run: bool`, `client: ClaudeClient`. `Context.from_args(argv)` classmethod for the orchestrator handles CLI parsing, phone-file loading, and mutual-exclusion enforcement between `--chat-limit` and `--phones-file`.

**Verification:**
```bash
uv run python -c "from src.schemas import Conversation, LabeledMessage, Turnaround; print([m.model_json_schema() for m in (Conversation, LabeledMessage, Turnaround)])"
```
Should print three JSON schemas without error.

---

## Milestone M1 — Vertical slice (thin E2E, all stubs)

Goal: `uv run python -m scripts.run_pipeline --chat-limit 5 --budget-usd 1.00` completes end-to-end in ≤5 minutes, costs <$0.50, and writes a valid (mostly empty) `output/report.md` with all 7 sections present.

### M1-T1 · Orchestrator skeleton — `scripts/run_pipeline.py`
**Files:** `scripts/run_pipeline.py`, `tests/test_pipeline.py`
**Effort:** 3h
**What to build:**
- CLI flags:
  - `--stage N`, `--from N --to M`
  - `--chat-limit N` (mutually exclusive with `--phones-file`)
  - `--phones-file PATH` (mutually exclusive with `--chat-limit`) — text file, one bare phone per line, blank lines and `#`-comments ignored
  - `--llm-mode {max,api,hybrid}` (default: `hybrid` if both creds available, else auto-detect)
  - `--budget-usd X` (API path only)
  - `--force`, `--dry-run`
- Loads phones: `Context.from_args` reads the file, strips/validates each line against `^\d{10,15}$`, stores as `frozenset[str]`, computes `phones_hash = sha256(",".join(sorted(phones))).hexdigest()[:16]`.
- Builds `Context`, imports `src.{load,dedupe,script_index,label,sentiment,conversion,cluster,report}` — each exposes `run(ctx: Context) -> StageResult` where `StageResult = {stage: int, outputs: list[Path], llm_usd_max: float, llm_usd_api: float, elapsed_s: float}`.
- Resume logic: checks `data/stageN.done` sentinels unless `--force`. Sentinel content: `{"ts": ..., "git_sha": ..., "module_version": "...", "chat_limit": int|null, "phones_hash": str|null, "llm_mode": "..."}`. Stage re-runs if `phones_hash` or `chat_limit` in sentinel differs from current `ctx` — prevents contamination across filter modes.
- Fails loudly if a stage's prerequisite file (previous stage's output) is missing: `"Stage N requires data/conversations.jsonl from Stage 1. Run: python -m scripts.run_pipeline --stage 1"`.
- After each stage, prints `[stage N] elapsed=12.3s max=(120 calls, 45k in / 12k out) api=$0.04 total_api=$0.21 budget=$1.00`.
- Final summary: stages run, total time, `get_usage_report()` output (both buckets + fallback events), list of output files.

**Tests (pytest):**
- `test_stage_sentinels_written` — run stage 1 on tiny_db → `data/stage1.done` exists with valid JSON including `phones_hash` + `chat_limit`.
- `test_skip_completed_stages` — sentinel present with matching hash → stage `run` mock not called.
- `test_force_flag_reruns` — `--force` calls mock despite sentinel.
- `test_sentinel_invalidated_by_phones_hash_change` — sentinel has `phones_hash=A`, ctx has `phones_hash=B` → stage re-runs without `--force`.
- `test_sentinel_invalidated_by_chat_limit_change` — sentinel has `chat_limit=5`, ctx has `chat_limit=null` → stage re-runs.
- `test_missing_prior_output_fails_loudly` — `--stage 2` without stage 1 output → `SystemExit` with error mentioning "Stage 1".
- `test_chat_limit_propagates` — mock stage 1 captures `ctx.chat_limit=5`.
- `test_phones_file_loaded` — temp file with 3 valid + 1 invalid + 1 comment line → `ctx.phones_filter` has 3 entries; `phones_hash` deterministic across runs.
- `test_chat_limit_phones_file_mutex` — both flags set → `SystemExit` with error mentioning "mutually exclusive".
- `test_llm_mode_propagates` — `--llm-mode=api` → `ctx.llm_mode=="api"` → `ClaudeClient` init skips Max.
- `test_budget_abort_stops_pipeline` — mock stage 2 raises `BudgetExceeded` → stage 3 mock not called.

**Acceptance:** `uv run python -m scripts.run_pipeline --help` prints usage. Tests green.

### M1-T2 · Stage 1 stub → real (DB → conversations.jsonl)
**Files:** `src/load.py`, `scripts/verify_stage1.py`, `tests/test_load.py`, `tools/build_tiny_db.py`
**Effort:** 4h
**What to build:**
- `run(ctx)`:
  1. Connect to `ctx.db_path` (read-only).
  2. Base SQL: `SELECT m.*, c.jid_row_id, c.group_type, j.user, j.raw_string FROM message m JOIN chat c ON m.chat_row_id=c._id JOIN jid j ON c.jid_row_id=j._id WHERE m.message_type=0 AND m.text_data IS NOT NULL AND c.group_type=0 ORDER BY c._id, m.timestamp`. (`group_type=0` → 1-to-1 chats only; no group chats.)
  3. **Phone filter (testing mode):** if `ctx.phones_filter` is not None, append `AND j.user IN (?, ?, ...)` with parameter binding (no string interpolation — SQL injection guard). When filter active, **bypass the 20-message threshold** entirely — every matched chat (short or long) goes to `data/conversations.jsonl`; `conversations_short.jsonl` is not written.
  4. Group by `chat_row_id`; for each group, produce `Conversation` with cleaned text (strip URLs via `re.sub(r"https?://\S+", "", t)`, collapse whitespace, preserve `text_raw`).
  5. Normal mode (no phone filter): split ≥20 messages → `data/conversations.jsonl`; <20 → `data/conversations_short.jsonl`.
  6. If `ctx.chat_limit` set (and no phone filter — mutex enforced at CLI layer), truncate the long list to first N (by chat_id asc, for determinism).
  7. If `ctx.phones_filter` set but some phones were not found in DB, log a warning listing missing phones; do not fail.
- `tools/build_tiny_db.py` creates `tests/fixtures/tiny.db` with exact `msgstore.db` schema (run `sqlite3 msgstore.db ".schema message chat jid"` during build to verify schema parity) and inserts 3 chats: one 25-msg converting chat, one 22-msg lost chat, one 5-msg stub to test threshold.

**Pytest (`tests/test_load.py`, offline):**
- `test_load_filters_message_type_0` — inject type 0/1/7 → only type 0 emerges.
- `test_load_drops_null_text` — `text_data IS NULL` excluded.
- `test_load_orders_by_timestamp` — per chat, `ts_ms` monotonic.
- `test_load_threshold_min_messages` — parametrize 19/20 boundary; 19-msg chat in short file.
- `test_load_strips_urls_and_whitespace` — `"hi https://x.com\n\n hi"` → `"hi  hi"` (or single-space). `text_raw` preserves original.
- `test_load_extracts_phone` — `jid.user="5511962719203"` → `Conversation.phone="5511962719203"`.
- `test_chat_limit` — 3 chats, `chat_limit=2` → jsonl has 2 entries.
- `test_phones_filter_keeps_only_matched` — tiny_db has 3 chats with phones A/B/C; `phones_filter={A,C}` → jsonl has 2 entries, chat B excluded.
- `test_phones_filter_bypasses_min_messages` — 5-msg stub chat whose phone is in filter → lands in `conversations.jsonl` (not short file), short file not written.
- `test_phones_filter_warns_on_missing` — filter contains phone not in DB → warning logged, run succeeds.
- `test_phones_filter_sql_injection_safe` — phone string `"1' OR 1=1 --"` → parameter-bound, no rows match, no SQL error.

**Smoke (`scripts/verify_stage1.py`):**
- Runs `load.run(ctx)` against the real `msgstore.db` with no chat_limit.
- Asserts: `~387 ± 10` long conversations, all have phones starting `55`, all messages chronological.
- Prints the top 5 busiest chats by message count for eyeball check.

**Acceptance:**
```bash
uv run pytest tests/test_load.py -q                          # green
uv run python scripts/verify_stage1.py                       # prints "387 chats, N messages, OK"
uv run python -m scripts.run_pipeline --stage 1 --chat-limit 5  # writes data/conversations.jsonl with 5 entries

# Testing mode — phone list
echo -e "5511962719203\n5511987654321" > /tmp/phones.txt
uv run python -m scripts.run_pipeline --stage 1 --phones-file /tmp/phones.txt
# Writes data/conversations.jsonl with matched chats only (any length)
```

### M1-T3 · Stage 2 stub → real (dedupe spa messages)
**Files:** `src/dedupe.py`, `scripts/verify_stage2.py`, `tests/test_dedupe.py`
**Effort:** 3h
**What to build:**
- `run(ctx)`: read `data/conversations.jsonl`, extract all `from_me=True` messages, accent-normalize via `unicodedata.normalize('NFKD', t).encode('ascii','ignore').decode().lower()`, cluster with `rapidfuzz.process.cdist` token-set ratio at threshold 88 (union-find merge).
- Output `data/spa_templates.json` matching `list[SpaTemplate]`.
- **Also output** `data/spa_message_template_map.json` — `{str(msg_id): template_id}` covering every spa message (not just examples). Stage 4 uses this to propagate template labels.
- Pick canonical text = instance with longest original text in cluster.

**Pytest (offline):**
- `test_exact_duplicates_collapse` — two identical → one template, count=2.
- `test_fuzzy_near_duplicates_collapse` — "Olá bom dia 😊" + "Ola, bom dia!" → one template.
- `test_different_messages_stay_separate` — greeting vs price → 2 templates.
- `test_threshold_boundary` — parametrize scores 87/89 around 88.
- `test_template_metadata` — `first_seen_ts ≤ last_seen_ts`, `example_msg_ids` non-empty and unique.
- `test_only_from_me_1` — customer messages never templated.

**Smoke (`scripts/verify_stage2.py`):**
- Runs against full Stage 1 output.
- Asserts: 300 ≤ template count ≤ 600 (sanity window).
- Prints top 20 templates by `instance_count`; human-reviewable.
- Asserts top 20 contains at least one message with "bom dia" AND one with "R$" (obvious-class check).

**Acceptance:**
```bash
uv run pytest tests/test_dedupe.py -q
uv run python scripts/verify_stage2.py   # prints top 20 templates, all counts make sense
```

### M1-T4 · Stage 3 stub (hand-curated script.yaml, no LLM yet)
**Files:** `src/script_index.py` (stub mode), `data/script.yaml` (committed — it's curated content)
**Effort:** 3h
**What to build (stub for M1):**
- `run(ctx)`: if `data/script.yaml` exists and `ctx.force=False`, no-op (return existing). This lets M1 work without LLM cost.
- Commit a hand-written `data/script.yaml` covering:
  - 9 steps (ids `1, 2, 3, 3.5, 5, 6, 7, fup1, fup2`) with `name`, `canonical_texts` (copy-pasted from script-comercial.md — FUP1/FUP2 blocks too), `expected_customer_intents` (3–5 per step guessed by developer).
  - `services`: Massagem Relaxante / Drenagem / Miofascial / Desportiva / Redutora / Facial / Shiatsu / Mini-day-spa / Day-spa with prices from the script.
  - `price_grid`: flattened from the Preços table (rows: service, persons, price, payment_rules).
  - `additionals`: 8 add-ons with prices.
  - `negotiation_rules`: 3 rules encoded as policy flags (`no_unsolicited_discount`, `mon_wed_5pct_dayspa`, `no_price_before_interest`).
  - `objection_taxonomy`: 9 canonical types (see `ObjectionType` schema) with PT-BR trigger words.
  - `promocoes`: dia-das-maes block with `valid_from: 2026-04-16`, `valid_until: 2026-05-17`, usage deadline `2026-06-30`.
  - **No** `inferred_extensions:` field — LLM output lives in a separate `data/script_extensions.yaml` (gitignored), not here. Stages 4/8 load both and merge in memory.

**Pytest (offline, script parsing only — actual LLM expansion is M2):**
- `test_script_yaml_loads` — loads, validates against Pydantic.
- `test_parse_script_extracts_7_steps` — all step ids present.
- `test_objection_taxonomy_preseeded` — 9 types.
- `test_promocoes_dates_parsed` — `valid_from` is a `date` not a string.

**Acceptance:**
```bash
uv run pytest tests/test_script_index.py -q
```

### M1-T5 · Stages 4–7 stubs (pass-through)
**Files:** `src/label.py`, `src/sentiment.py`, `src/conversion.py`, `src/cluster.py`
**Effort:** 2h total
**What to build:**
- **`label.py` stub:** reads `conversations.jsonl`; for every message emit `LabeledMessage(step_id=None, step_context="unknown", intent=None, objection_type=None, sentiment=None)`. Writes `data/labeled_messages.jsonl`. Zero LLM calls.
- **`sentiment.py` stub:** reads `spa_templates.json`; emit `TemplateSentiment(warmth=3, clarity=3, script_adherence=3, polarity="neu", critique="(não avaliado)")` for each. Writes `data/template_sentiment.json`.
- **`conversion.py` stub:** reads `conversations.jsonl`; for each emit `ConversationConversion(conversion_score=0, final_outcome="ambiguous", first_objection_idx=None, ...)`. Writes `data/conversions.jsonl`, empty `data/turnarounds.json=[]`, `data/lost_deals.json=[]`.
- **`cluster.py` stub:** reads labeled messages; writes empty `data/aggregations.json={"per_step":{}, "off_script_clusters":[]}`.

**Acceptance:** pipeline runs through stages 1–7 with `--chat-limit 5` in <10s, zero LLM cost.

### M1-T6 · Stage 8 minimal — Sonnet writes a hollow report
**Files:** `src/report.py`, `prompts/stage8_report.md`, `scripts/verify_stage8.py`
**Effort:** 3h
**What to build:**
- `run(ctx)`: load all aggregates. Pass to Sonnet 4.6 with a prompt that instructs: "Produce a PT-BR Markdown report with exactly these 7 sections even if some are empty; show '(sem dados)' where appropriate". Stage 8 always runs (no stub). On M1 with empty inputs, report will be skeletal but well-structured.
- Also emit empty/near-empty CSVs: `turnarounds.csv`, `lost_deals.csv`, `per_step.csv`, `spa_templates_scored.csv`, `off_script_clusters.csv`.

**Smoke (`scripts/verify_stage8.py`):**
- Runs Stage 8 on the M1 hollow inputs.
- Asserts: `output/report.md` contains each of the 7 section headers from `PLAN.md` §Stage 8.
- Asserts: CSV headers present, UTF-8 BOM optional but accented chars round-trip.
- Cost assertion: single Sonnet call <$0.30.

**Acceptance (M1 complete):**
```bash
uv run python -m scripts.run_pipeline --chat-limit 5 --budget-usd 1.00
# Runs all 8 stages in <5 min, costs <$0.50, produces output/report.md with all 7 sections.
open output/report.md   # stakeholder can read the skeleton
```

---

## Milestone M2 — Deepening passes

Each M2 task replaces a stubbed stage with the real implementation. After each task, M1 acceptance must still pass.

### Stage 3 deepening

#### M2-S3-T1 · LLM script expansion (Sonnet, 1 call)
**Files:** `src/script_index.py`, `prompts/stage3_expand_script.md`
**Effort:** 4h
**What to build:**
- Augment `script_index.run(ctx)`: if `data/script_extensions.yaml` missing or `ctx.force`, call Sonnet with the full `script-comercial.md` + the committed `data/script.yaml`, asking for:
  - A tightened Day-Spa pitch flow (re-structures what's already there).
  - Standardized reply templates for each of the 9 objection types.
  - Flag internal inconsistencies (e.g., Massagem Especial price "R$285 por R$255" in promoções vs base R$200 — flag or explain).
- **Write output to** `data/script_extensions.yaml` (gitignored, top-level keys: `day_spa_pitch`, `objection_replies`, `inconsistencies`). **Never** modify `data/script.yaml`.
- Downstream: Stages 4/8 load both files and merge via `script_index.load_merged(ctx)`.
- Budget guard: hard cap 8k output tokens.

**Smoke (`scripts/verify_stage3.py`, real API):**
- Runs the expansion.
- Asserts: `script_extensions.yaml` exists; `day_spa_pitch.steps` has ≥3 items and mentions "escalda-pés" or "banho de imersão".
- Asserts: `objection_replies` has entries for all 9 taxonomy ids.
- Asserts: `data/script.yaml` byte-identical before/after (not modified).
- Asserts: cost <$0.60.

**Pytest (offline, with `fake_llm` for the `complete()` call):**
- `test_expansion_writes_extensions_file` — mock returns canned JSON → `data/script_extensions.yaml` has expected top-level keys.
- `test_expansion_does_not_mutate_script_yaml` — checksum before/after matches.
- `test_expansion_skipped_when_extensions_exist` — pre-existing `script_extensions.yaml` + `ctx.force=False` → `complete()` not called.
- `test_load_merged_returns_combined_dict` — both files present → merged dict carries hand-curated keys AND extension keys under `extensions.*` (or agreed namespace).

### Stage 4 deepening

#### M2-S4-T1 · Spa-template step labeling (Haiku, once per template)
**Files:** `src/label.py`, `prompts/stage4_spa_template.md`
**Effort:** 4h
**What to build:**
- For each `SpaTemplate`, call Haiku with the canonical text + the script steps summary (incl. `fup1`/`fup2`) → returns `{step_id, matches_script, deviation_note}`. Propagate to every instance of that template via `data/spa_message_template_map.json` (Stage 2 output).
- Write `data/spa_template_labels.json` (indexed by template_id) as an intermediate; `label.py` consumer joins it onto messages.
- Derive `step_context` per spa message: `matches_script=true → "on_script"`, `false → "off_script"`.
- ~500 templates × ~500 tokens in × ~100 tokens out ≈ $1.00.

**Pytest (offline, with `fake_llm`):**
- `test_spa_template_labeled_once` — 2 instances of same template → 1 `complete()` call, both labels identical.
- `test_label_schema_validated` — malformed response → retry via `llm.py` structured-output path.

**Smoke:** part of `scripts/verify_stage4.py` (shared with customer batching task).

#### M2-S4-T2 · Customer message batching & tagging (Haiku, cross-chat batches of 30)
**Files:** `src/label.py`, `prompts/stage4_customer_batch.md`
**Effort:** 4h
**What to build:**
- **Cross-chat batching**: pack 30 customer messages per Haiku call, drawn from **any** chats (fill every batch to max). Final leftover batch may be <30.
- Each message in the prompt carries its own envelope: `{msg_id, chat_id, text, step_context_hint: [last_3_spa_msgs_in_that_chat]}`. Model must not confuse messages across chats → prompt states "each item is independent; use only its own `step_context_hint`".
- Prompt prefix (shared across all 500 calls): script step summaries + 9-type objection triggers. Same static content every call → candidate for future prompt-caching (tracked, not enforced per Decision 8).
- Returns per-message `{msg_id, step_context, intent, objection_type|null, sentiment}`.
- ~15k customer messages / 30 = 500 calls × ~1500 tokens in × ~500 tokens out ≈ $1.50.

**Pytest (offline):**
- `test_customer_batching_packs_cross_chat` — 3 chats of 10/20/5 msgs → batches of 30, 5 (not 10, 20, 5).
- `test_batch_envelope_has_chat_id` — every prompt item includes chat_id + per-msg step_context_hint.
- `test_off_script_flagged` — fake_llm returns `step_context=off_script` for a message like "tem estacionamento?" → persists.
- `test_objection_type_recognized` — parametrize: "achei caro"→price, "muito longe"→location, "vou pensar"→hesitation_vou_pensar. Uses real Haiku on a batch of 9 (one per type) — actually this is the integration test below.

**Smoke (`scripts/verify_stage4.py`, real API, small):**
- Runs Stage 4 on `--chat-limit 5` output (~500 spa msgs, ~500 customer msgs). Cost ≤$0.15.
- Asserts: every `LabeledMessage` validates. ≥80% of spa messages have a non-null `step_id`. ≥50% of customer messages have a non-null `intent`. At least one message of each of the 3 most-common objection types (`price`, `hesitation_vou_pensar`, `time_slot`) appears.
- Prints 10 random labeled messages for manual eyeballing.

### Stage 5 deepening

#### M2-S5-T1 · Template sentiment scoring
**Files:** `src/sentiment.py`, `prompts/stage5_sentiment.md`
**Effort:** 3h
**What to build:**
- For each `SpaTemplate`, one Haiku call → `TemplateSentiment`. Batch 10 templates per call to cut overhead (50 calls × ~2000 tokens ≈ $0.40).
- Rubric in PT-BR system prompt, with few-shot: a known-warm ("fico muito feliz em te receber 💛"), a known-cold ("segue valor: R$420"), a known-critical ("você não pode levar isso").

**Pytest (offline, `fake_llm`):**
- `test_scores_in_range` — every template: warmth, clarity, script_adherence ∈ [1,5]; polarity ∈ {pos,neu,neg}.
- `test_propagation_by_template_id` — no-op (propagation is a DB join at report time, not in sentiment module).
- `test_critique_is_portuguese` — fake response includes "ç" / "ã" / common PT words → saved as-is.

**Smoke (`scripts/verify_stage5.py`, real API):**
- Picks top 6 templates by instance_count from full corpus (or Stage 1 chat_limit=5 output), scores them.
- Human-readable print; asserts rubric fields present and polarity distributed (not all "neu").
- Cost <$0.05.

### Stage 6 deepening — THE HIGH-VALUE STAGE

#### M2-S6-T1 · Conversation truncation utility
**Files:** `src/conversion.py` (function `truncate_for_llm`)
**Effort:** 2h
**What to build:**
- Pure function: given a conversation and its labeled objection indices, produce a ≤3000-token string containing: first 15 msgs + ±10 around each `objection_idx` + last 15 msgs. De-dup overlapping windows. Mark elisions with `[... N mensagens ...]`.

**Pytest (offline):**
- `test_truncation_of_long_chat` — 200-msg chat with objection at idx=80 → output tokens ≤3k via `tiktoken` proxy count. Must include msg 0–14, 70–90, 185–199.
- `test_truncation_short_chat_passthrough` — 30-msg chat → returned verbatim.
- `test_truncation_no_objections` — only first+last windows included.

#### M2-S6-T2 · Conversion detection (Haiku, 1 call per chat)
**Files:** `src/conversion.py`, `prompts/stage6_conversion.md`
**Effort:** 5h
**Prereqs:** M2-S6-T1 (truncation), **M2-S4-T2** (customer labels — objection indices feed the truncation window), M3-T1 (ground truth for few-shot).
**What to build:**
- Load `data/labeled_messages.jsonl` from Stage 4; collect `(chat_id, msg_idx, objection_type)` triples for messages with non-null `objection_type`. Pass these as `objection_indices` to `truncate_for_llm`.
- For each of ~387 chats: truncate, send to Haiku with structured-output schema for `ConversationConversion`. Prompt explicitly enumerates the 9 objection types and includes 2 positive + 2 negative few-shot examples from the user's ground-truth set (hand-picked chats M3-T1).
- Cost: 387 × ~2500 in × ~300 out ≈ $1.80.
- Write `data/conversions.jsonl`.

**Pytest (offline, `fake_llm`):**
- `test_conversion_score_parsed` — fake response with `conversion_score=3` → structure ok.
- `test_all_objection_types_covered` — parametrize 9 synthetic chats (one per type), fake_llm classifies each correctly → structure ok.
- `test_phone_number_attached` — `jid.user` propagated into `ConversationConversion.phone`.

**Smoke (`scripts/verify_stage6.py`, real API, small):**
- Runs on 20 ground-truth chats only.
- Asserts: ≥16/20 match the user's `outcome` label in `data/ground_truth_outcomes.csv` (booked vs lost). If fewer: exits non-zero with a prompt-tuning suggestion.
- Cost <$0.20.

#### M2-S6-T3 · Turnaround & lost-deal extraction (pure logic, no LLM)
**Files:** `src/conversion.py` (function `extract_turnarounds`), `tests/test_conversion.py`
**Effort:** 3h
**What to build:**
- `extract_turnarounds(conversions, conversations) -> tuple[list[Turnaround], list[LostDeal]]`:
  - **Turnaround:** `first_objection_type != null` AND `conversion_score ≥ 2` AND `resolution_idx > first_objection_idx` AND `final_outcome="booked"`.
  - **Lost deal:** `first_objection_type != null` AND `conversion_score ≤ 1` AND `final_outcome="lost"`.
  - Pair each turnaround with 1–2 lost deals of same objection type (prefer earliest-dated lost deals for diversity).
  - Populate `Turnaround.customer_message` from the message at `first_objection_idx`, `winning_reply` from the spa message at `resolution_idx`, `confirmation` from the first `from_me=False` message after `resolution_idx` containing booking-positive phrases (or empty if none).

**Pytest (offline):**
- `test_turnaround_detected` — synthetic conversion meeting all 4 conditions → in list.
- `test_no_turnaround_when_no_objection` — null first_objection → excluded.
- `test_lost_deal_detected` — score 0 + objection → in lost list.
- `test_lost_deal_pairing` — 5 turnarounds of type=price + 3 lost deals of type=price → each turnaround gets 1–2 paired.
- `test_phone_number_attached` — each Turnaround has `jid.user`.

**Acceptance:**
```bash
uv run pytest tests/test_conversion.py -q   # all green, offline
uv run python scripts/verify_stage6.py      # ≥16/20 ground-truth matches
```

### Stage 7 deepening

#### M2-S7-T1 · Off-script embedding + HDBSCAN
**Files:** `src/cluster.py`, `tests/test_cluster.py`
**Effort:** 4h
**What to build:**
- Load `labeled_messages.jsonl`. Filter `from_me=False AND step_context="off_script"`.
- `step_id` for customer off-script messages = **the last preceding spa message's step_id** in the same chat (parent step context). Assign during load; messages with no preceding spa step → `step_id="unknown"` and routed to `Aggregation.off_script_clusters` (global bucket).
- Group by `step_id`. Within each group:
  - Embed with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (loaded once, cached). CPU is fine.
  - HDBSCAN `min_cluster_size=3, metric='cosine'`.
  - Medoid = message with min sum of cosine distances to cluster members.
- Aggregations: per step → `{on_script_count, off_script_count, top_intents: [...], top_clusters: [{medoid_text, size, example_msg_ids}], top_objections: [...]}`.
- Write `data/aggregations.json`.

**Pytest (offline, no API):**
- `test_embeddings_deterministic` — fixed seed, 2 runs → identical vectors within 1e-6.
- `test_clustering_groups_paraphrases` — ["quanto custa?", "qual o valor?", "qual preço?", "onde fica?", "qual endereço?", "fica aonde?"] → 2 clusters of 3.
- `test_medoid_selection` — synthetic 5-point cluster → medoid is the geometrically central member.
- `test_empty_input_handled` — empty filter → `aggregations.json={"per_step":{},"off_script_clusters":[]}`, no crash.
- `test_aggregation_counts_match_inputs` — sum(cluster sizes) + noise_count == total off-script input.

**Smoke:** subsumed by M1 slice (Stage 7 always runs on real data after M2-S4 is in).

### Stage 8 deepening

#### M2-S8-T1 · Full report generation with real data
**Files:** `src/report.py`, `prompts/stage8_report.md`, `tests/test_report.py`
**Effort:** 5h
**What to build:**
- Input packet to Sonnet:
  - Per-step aggregates (Stage 7).
  - Top 10 positive + top 10 negative templates (by polarity × instance_count × warmth).
  - Top 20 turnarounds ranked by `score = conversion_score × clarity`. `clarity` = `TemplateSentiment.clarity` of the template containing `winning_reply_msg_id` (lookup via `data/spa_message_template_map.json` → `data/template_sentiment.json`). If `winning_reply_msg_id` has no template mapping (rare — very-long or never-repeated replies), fall back to `clarity=3` (neutral).
  - Paired lost deals.
  - Script extensions loaded from `data/script_extensions.yaml` (for §7 "Lacunas no script").
- Prompt structure: system sets style (PT-BR, direct, no filler); user message provides the structured JSON blobs with schemas inline.
- Total context: ≤40k tokens in, ≤6k out → Sonnet call ~$1.50.
- Post-processing: write the 5 CSVs directly from Python (not the LLM). `turnarounds.csv` columns: `telefone, data, tipo_objecao, mensagem_cliente, resposta_vencedora, confirmacao`. Use `pandas.to_csv(encoding="utf-8", index=False)`.

**Pytest (offline, `fake_llm`):**
- `test_report_sections_present` — all 7 PT-BR headers in output.
- `test_turnarounds_csv_schema` — exact column names.
- `test_top20_cap` — 50 inputs → 20 rows in the report §5.
- `test_phone_numbers_in_md` — report body contains ≥1 `/55\d{10,11}/` phone.
- `test_csv_utf8_roundtrip` — read back, "ção", "é", "ô" survive.

**Smoke (`scripts/verify_stage8.py` — upgraded from M1-T6):**
- Runs on full-pipeline output with real Sonnet.
- Asserts all 7 sections non-empty (each has at least 100 chars of body text).
- Manual eyeball: open `output/report.md` — does it read like a human wrote it?

---

## Milestone M3 — Full run, calibration, human review

### M3-T1 · Ground-truth collection helper
**Files:** `scripts/label_ground_truth.py`, `data/ground_truth_outcomes.csv`
**Effort:** 3h
**What to build:**
- Interactive CLI: loads `data/conversations.jsonl`, picks **20** chats stratified by length (6 short, 8 medium, 6 long). Prints each chat formatted, prompts user: `[b]ooked / [l]ost / [a]mbiguous / [s]kip / notes:`.
- Writes `data/ground_truth_outcomes.csv`: `chat_id, phone, outcome, notes`.
- **User action required before M2-S6 finishes:** user runs this script, labels 20 chats. Without it, Stage 6 smoke test can't run.

**Acceptance:** User hands over CSV with 20 rows.

### M3-T2 · Prompt-tuning loop for Stage 6
**Files:** `prompts/stage6_conversion.md` (iterated), observations in `data/calibration_log.md`
**Effort:** 2–4h
**What to do:**
- Run `scripts/verify_stage6.py` against the 20 ground-truth chats.
- If ≥16/20 match: ship.
- If <16/20: inspect mismatches in `data/conversions.jsonl`, adjust prompt (clarify "ambiguous", add negative example for the missed class), re-run. Cap: 3 iterations; if still <16 after 3, escalate to Sonnet for Stage 6 at ~5× cost.

**Verification:** final iteration ≥16/20, notes in `data/calibration_log.md`.

### M3-T3 · Full-corpus run
**Effort:** 1h (mostly wall clock)
**What to do:**
```bash
uv run python -m scripts.run_pipeline --budget-usd 10.00 --force
```
- Monitor cost print-outs per stage.
- Hard abort criterion: if Stage 4 exceeds $4 (double estimate), stop and investigate.
- Total wall time: expect 30–60 min (Stage 4 customer batching + Stage 6 per-chat calls dominate).

**Verification:**
- `wc -l data/conversations.jsonl` ≈ 387.
- `jq length data/turnarounds.json` ≥ 20 (ideally 30–60).
- `output/report.md` exists and is ≥30 KB.

### M3-T4 · Human review + script v2 draft
**Files:** `output/script_v2_proposal.md` (new Sonnet output)
**Effort:** 3h (mostly reading)
**What to do:**
- Read top 5 turnarounds manually; cross-check against `winning_reply` in raw messages. Flag false positives in `data/review_notes.md`.
- If >1 false positive in top 5: iterate on `extract_turnarounds` scoring (M2-S6-T3) and re-rank; no re-LLMing needed.
- One-off Sonnet call: input = `script-comercial.md` + `output/report.md` §7 "Lacunas" + §5 top turnaround arguments → output = `output/script_v2_proposal.md` with concrete paste-ready additions to the script (one standardized response per objection type, plus a cleaner Day-Spa pitch).

**Business acceptance:**
- Stakeholder reads `output/report.md` and `output/script_v2_proposal.md`.
- Stakeholder identifies ≥3 insights they didn't already know.
- Stakeholder can point to ≥1 standardized response they will adopt into the next version of the script.
- The 5 sampled turnarounds include real phone numbers matching known bookings (user verifies against booking records).

---

## Cross-cutting concerns

### Test running

```bash
# Fast feedback — runs on every save, no API
uv run pytest -q                    # all pure-logic tests (~5s)

# Per-stage smoke — runs when touching a stage, costs pennies
uv run python scripts/verify_stage1.py   # no API
uv run python scripts/verify_stage2.py   # no API
uv run python scripts/verify_stage3.py   # ~$0.05
uv run python scripts/verify_stage4.py   # ~$0.15
uv run python scripts/verify_stage5.py   # ~$0.05
uv run python scripts/verify_stage6.py   # ~$0.10
uv run python scripts/verify_stage8.py   # ~$0.30

# End-to-end — runs before commit on stage changes
uv run python -m scripts.run_pipeline --chat-limit 5 --budget-usd 1.00   # ~$0.50

# The real deal — runs once per milestone or after prompt changes
uv run python -m scripts.run_pipeline --budget-usd 10.00 --force         # ~$6-8
```

### M2-OPS-T1 · Safe `CLAUDE_MAX_KILL_OTHERS` — spare process tree
**Files:** `src/llm.py` (`MaxClient._kill_stray_claude`, new `_protected_pids`), `scripts/run_verify_max.sh`, `scripts/run_pipeline.py` (doc only)
**Effort:** 1–2h
**Context / why:**
`MaxClient._kill_stray_claude` (opt-in via `CLAUDE_MAX_KILL_OTHERS=1`) originally spared only `os.getpid()`. Two breakages surfaced during M2-S4 verify runs:
1. **Sibling-worker SIGKILL race.** With `STAGE4_CONCURRENCY>1`, threads in the same process call `_kill_stray_claude` before spawning their own CLI child. `pgrep -f claude` finds a sibling's in-flight CLI, kills it → subprocess returns `rc=-9` with empty stderr → `RuntimeError: claude CLI failed rc=-9`.
2. **Parent Claude Code session killed.** If the pipeline runs inside a Claude Code interactive session, the parent `claude` process matches the pattern and gets SIGKILL'd, terminating the user's session.

**What to build:**
- New helper `MaxClient._protected_pids() -> set[int]` that collects:
  - `os.getpid()` (self).
  - All ancestors via `/proc/<pid>/status` `PPid:` walk (stop at `1` or seen).
  - All descendants of every protected pid via BFS over `pgrep -P <parent>`.
- `_kill_stray_claude` skips any pid in the protected set. All other `claude_agent_sdk/_bundled/claude` and `^claude($| )` matches are still `kill -9`'d.
- Lock (`threading.Lock`) serializes concurrent callers.
- Runs of the pipeline / verify scripts must configure `CLAUDE_MAX_KILL_OTHERS=1` explicitly — orchestrator does **not** set it by default. `scripts/run_verify_max.sh` keeps the existing export so batch runs sweep leftover CLI procs from prior crashed runs.
- Document in `scripts/run_pipeline.py --help` that `CLAUDE_MAX_KILL_OTHERS=1` is safe to set even when running inside a parent Claude Code session, because the ancestor chain is protected.

**Pytest (offline, `tests/test_llm.py`):**
- `test_protected_pids_includes_self` — set contains `os.getpid()`.
- `test_protected_pids_includes_ancestors` — monkeypatch `/proc/<pid>/status` reader → walk collects synthetic parent chain, stops at pid 1.
- `test_protected_pids_includes_descendants` — monkeypatch `pgrep -P` output → BFS collects 2-level children.
- `test_kill_stray_spares_protected` — monkeypatch `pgrep -f` to return `{protected_pid, stray_pid}`; monkeypatch `os.kill` to record invocations → only `stray_pid` killed.
- `test_kill_stray_lock_serializes` — two threads call concurrently → `os.kill` call sequence linearized.

**Smoke:** re-run `bash scripts/run_verify_max.sh` with `STAGE4_CONCURRENCY=5` and verify no `rc=-9` errors across ≥16 Haiku batch calls; the parent Claude Code session (if any) must survive.

**Acceptance:**
```bash
uv run pytest tests/test_llm.py::test_kill_stray_spares_protected -q
CLAUDE_MAX_KILL_OTHERS=1 STAGE4_CONCURRENCY=5 bash scripts/run_verify_max.sh
# Stage 4 exit=0; no rc=-9 errors; this Claude Code session still alive.
```

### Risks & open items

| Risk | Mitigation |
|---|---|
| Haiku mis-classifies objection types in PT-BR slang (e.g. "tô ruim de grana" for price) | M2-S6 smoke test over 20 ground-truth chats catches systematic errors before the full run. |
| HDBSCAN returns mostly noise (cluster membership ≤20%) | `test_aggregation_counts_match_inputs` surfaces noise ratio; if high, drop `min_cluster_size` to 2 or try `UMAP + HDBSCAN`. |
| `rapidfuzz` threshold 88 either over-merges ("bom dia" + "boa tarde") or under-merges | M1-T3 smoke prints top 20 templates; eyeball for 10 minutes before committing threshold. Parameterize as `ctx.dedupe_threshold`. |
| Phone numbers in report raise privacy concern | Leave them plaintext in JSON/CSV (they're the primary key for business verification); the final `output/report.md` should keep them — this is an internal tool for the spa owner, not a public deliverable. If this changes, add a `--anonymize` flag that hashes all phones before Stage 8. |
| Budget blown on Stage 4 customer batching | Orchestrator aborts before starting Stage 4 if projected cost (500 calls × measured-avg-cost-per-call from a 5-call sample) exceeds remaining budget. |
| Script `script-comercial.md` has inconsistent Markdown (empty `#` headers, emoji-only lines) | Stage 3 parser is not regex — it passes the raw file to Sonnet and gets back structured JSON. Brittleness goes away. |

### What's deliberately out of scope

- Media messages (`message_type != 0`). ~5% of conversations reference voice notes or images; labeling these needs a different pipeline.
- Multi-turn memory of a specific customer across chat_ids. Phone uniqueness gets us partway but we don't de-duplicate customers.
- Real-time / incremental runs. Everything re-runs from `msgstore.db`; no watermark logic.
- A web UI. `output/report.md` opened in any Markdown viewer is the UI.

---

## Task summary (scan this)

| ID | Title | Effort | Prereqs | LLM cost |
|---|---|---|---|---|
| M0-T1 | Bootstrap project (uv, pyproject, gitignore, .env) | 1–2h | — | $0 |
| M0-T2 | `src/llm.py` — dual-client (Max+API), fallback, retry, budget | 5–6h | M0-T1 | $0 |
| M0-T3 | `src/schemas.py` + `src/context.py` | 2h | M0-T1 | $0 |
| M1-T1 | Orchestrator skeleton `scripts/run_pipeline.py` | 3h | M0-T3 | $0 |
| M1-T2 | Stage 1: load DB → conversations.jsonl | 4h | M1-T1 | $0 |
| M1-T3 | Stage 2: rapidfuzz dedupe | 3h | M1-T2 | $0 |
| M1-T4 | Stage 3: hand-curated `script.yaml` | 3h | M0-T3 | $0 |
| M1-T5 | Stages 4–7 stubs (pass-through) | 2h | M1-T2, M1-T4 | $0 |
| M1-T6 | Stage 8 minimal hollow report | 3h | M1-T5 | $0.30 |
| **M1 COMPLETE** | **End-to-end on 5 chats, <$0.50** | | | |
| M2-S3-T1 | Stage 3 LLM script expansion | 4h | M1-T4 | $0.50 |
| M2-S4-T1 | Stage 4 spa-template labeling | 4h | M1-T3 | $1.00 |
| M2-S4-T2 | Stage 4 customer batching & tagging | 4h | M2-S4-T1 | $1.50 |
| M2-S5-T1 | Stage 5 template sentiment | 3h | M1-T3 | $0.40 |
| M2-S6-T1 | Stage 6 truncation utility | 2h | M1-T2, M2-S4-T2 | $0 |
| M3-T1 | Ground-truth collection helper (20 chats) | 3h | M1-T2 | $0 |
| M2-S6-T2 | Stage 6 conversion detection | 5h | M2-S6-T1, M2-S4-T2, M3-T1 | $1.80 |
| M2-S6-T3 | Stage 6 turnaround extraction (pure) | 3h | M2-S6-T2 | $0 |
| M2-S7-T1 | Stage 7 embedding + HDBSCAN | 4h | M2-S4-T2 | $0 |
| M2-S8-T1 | Stage 8 full report | 5h | M2-S7-T1, M2-S6-T3, M2-S5-T1 | $1.50 |
| M3-T2 | Prompt-tuning loop for Stage 6 (≥16/20) | 2–4h | M2-S6-T2 | $0.50 |
| M3-T3 | Full-corpus run | 1h | all M2 | $6–8 |
| M3-T4 | Human review + script v2 draft | 3h | M3-T3 | $0.30 |
| M2-OPS-T1 | Safe `CLAUDE_MAX_KILL_OTHERS` — spare ancestor/descendant process tree | 1–2h | M0-T2 | $0 |

**Total effort:** ~72–87 dev-hours (~2 working weeks for one developer). (+2h for dual-client LLM auth.)
**Total LLM spend projection:** $8–10 including calibration iterations.
**First stakeholder-readable artifact:** end of day 1 (M1 complete).
