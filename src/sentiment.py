"""Stage 5 stub: neutral sentiment for every spa template, zero LLM calls.

See TECH_PLAN.md §M1-T5. Real impl in M2-S5-T1.
"""

from __future__ import annotations

import json
import logging
import time

from src.context import Context
from src.schemas import SpaTemplate, TemplateSentiment

log = logging.getLogger(__name__)


def run(ctx: Context) -> dict:
    t0 = time.time()
    templates_path = ctx.data_dir / "spa_templates.json"
    if not templates_path.exists():
        raise FileNotFoundError(f"missing stage 2 output: {templates_path}")

    raw = json.loads(templates_path.read_text(encoding="utf-8"))
    templates = [SpaTemplate.model_validate(t) for t in raw]

    out = [
        TemplateSentiment(
            template_id=t.template_id,
            warmth=3,
            clarity=3,
            script_adherence=3,
            polarity="neu",
            critique="(não avaliado)",
        )
        for t in templates
    ]

    out_path = ctx.data_dir / "template_sentiment.json"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([s.model_dump() for s in out], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("stage5 stub: %d template sentiments → %s", len(out), out_path)
    return {
        "stage": 5,
        "outputs": [out_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
