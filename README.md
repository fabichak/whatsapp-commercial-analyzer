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
