"""Stage 7 stub: empty aggregation artifact, no embedding/clustering yet.

See TECH_PLAN.md §M1-T5. Real impl in M2-S7-T1.
"""

from __future__ import annotations

import json
import logging
import time

from src.context import Context

log = logging.getLogger(__name__)


def run(ctx: Context) -> dict:
    t0 = time.time()
    labeled_path = ctx.data_dir / "labeled_messages.jsonl"
    if not labeled_path.exists():
        raise FileNotFoundError(f"missing stage 4 output: {labeled_path}")

    out_path = ctx.data_dir / "aggregations.json"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"per_step": {}, "off_script_clusters": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("stage7 stub: empty aggregations → %s", out_path)
    return {
        "stage": 7,
        "outputs": [out_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
