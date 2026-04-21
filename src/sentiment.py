"""Stage 5: template sentiment scoring via Haiku.

Batches 10 SpaTemplate canonical texts per call; emits one
`TemplateSentiment` per template. See TECH_PLAN.md §M2-S5-T1.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

from pydantic import BaseModel
from tqdm import tqdm

from src.context import Context
from src.schemas import SpaTemplate, TemplateSentiment

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
PROMPT_RELPATH = "stage5_sentiment.md"
TEMPLATES_RELPATH = "spa_templates.json"
OUTPUT_RELPATH = "template_sentiment.json"

BATCH_SIZE = 10
BATCH_MAX_TOKENS = 2048
TEXT_CHAR_CAP = 1200  # trim very long templates to keep prompt tight

VALID_POLARITY = {"pos", "neu", "neg"}


class SentimentBatchResult(BaseModel):
    items: list[TemplateSentiment]


def _read_prompt(ctx: Context) -> str:
    p = ctx.prompts_dir / PROMPT_RELPATH
    if not p.exists():
        raise FileNotFoundError(f"missing prompt: {p}")
    return p.read_text(encoding="utf-8")


def _load_templates(ctx: Context) -> list[SpaTemplate]:
    p = ctx.data_dir / TEMPLATES_RELPATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [SpaTemplate.model_validate(t) for t in raw]


def _pack_batches(items: list[dict], size: int = BATCH_SIZE) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _build_user_msg(batch: list[dict]) -> str:
    return (
        "BATCH (JSON):\n"
        + json.dumps({"items": batch}, ensure_ascii=False)
        + "\n\nAvalie cada item do BATCH. Responda com um objeto "
        "`items` contendo um `TemplateSentiment` por entrada, na "
        "mesma ordem e com o mesmo `template_id`."
    )


def _validate(ts: TemplateSentiment) -> None:
    for field in ("warmth", "clarity", "script_adherence"):
        v = getattr(ts, field)
        if not (1 <= v <= 5):
            raise ValueError(f"invalid {field}={v!r} for template {ts.template_id}")
    if ts.polarity not in VALID_POLARITY:
        raise ValueError(f"invalid polarity {ts.polarity!r}")


def score_templates(ctx: Context) -> list[TemplateSentiment]:
    if ctx.client is None:
        raise RuntimeError("score_templates requires ctx.client")

    templates = _load_templates(ctx)
    if not templates:
        return []

    system = _read_prompt(ctx)
    items = [
        {"template_id": t.template_id, "text": t.canonical_text[:TEXT_CHAR_CAP]}
        for t in templates
    ]
    batches = _pack_batches(items)

    out: dict[int, TemplateSentiment] = {}
    for batch in tqdm(batches, desc="stage5: sentiment batches", disable=None):
        user_msg = _build_user_msg(batch)
        result = ctx.client.complete(
            model=MODEL,
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            max_tokens=BATCH_MAX_TOKENS,
            response_format=SentimentBatchResult,
        )
        if not isinstance(result, SentimentBatchResult):
            raise TypeError(f"expected SentimentBatchResult, got {type(result).__name__}")
        expected = {it["template_id"] for it in batch}
        for ts in result.items:
            _validate(ts)
            if ts.template_id not in expected:
                log.warning("stage5: unknown template_id %d in response", ts.template_id)
                continue
            out[ts.template_id] = ts
        missing = expected - {ts.template_id for ts in result.items}
        if missing:
            log.warning("stage5: batch missing %d templates: %s", len(missing), sorted(missing)[:5])

    # Preserve template order from spa_templates.json
    ordered = [out[t.template_id] for t in templates if t.template_id in out]
    return ordered


def run(ctx: Context) -> dict:
    t0 = time.time()

    out_path = ctx.data_dir / OUTPUT_RELPATH
    ctx.data_dir.mkdir(parents=True, exist_ok=True)

    if out_path.exists() and not ctx.force:
        raw = json.loads(out_path.read_text(encoding="utf-8"))
        # If stub-filled (all "(não avaliado)"), re-score.
        is_stub = bool(raw) and all(
            r.get("critique") == "(não avaliado)" for r in raw
        )
        if not is_stub:
            log.info("stage5: %s exists, skipping (use --force)", out_path)
            return {
                "stage": 5,
                "outputs": [out_path],
                "llm_usd_max": 0.0,
                "llm_usd_api": 0.0,
                "elapsed_s": time.time() - t0,
            }
        log.info("stage5: stub output detected, regenerating")

    scored = score_templates(ctx)
    out_path.write_text(
        json.dumps([s.model_dump() for s in scored], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("stage5: %d template sentiments → %s", len(scored), out_path)

    api_cost = 0.0
    if ctx.client is not None:
        api_cost = float(ctx.client.get_usage_report().get("api", {}).get("cost_usd", 0.0))

    return {
        "stage": 5,
        "outputs": [out_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": api_cost,
        "elapsed_s": time.time() - t0,
    }
