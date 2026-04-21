"""Stage 6 stub: zero-conversion ambiguous output for every chat, no LLM.

See TECH_PLAN.md §M1-T5. Real impl in M2-S6-T1/T2/T3.
"""

from __future__ import annotations

import json
import logging
import time

from src.context import Context
from src.schemas import Conversation, ConversationConversion

log = logging.getLogger(__name__)


def run(ctx: Context) -> dict:
    t0 = time.time()
    convos_path = ctx.data_dir / "conversations.jsonl"
    if not convos_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {convos_path}")

    conversions_path = ctx.data_dir / "conversions.jsonl"
    turnarounds_path = ctx.data_dir / "turnarounds.json"
    lost_path = ctx.data_dir / "lost_deals.json"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    with convos_path.open("r", encoding="utf-8") as fin, conversions_path.open(
        "w", encoding="utf-8"
    ) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            convo = Conversation.model_validate_json(line)
            cc = ConversationConversion(
                chat_id=convo.chat_id,
                phone=convo.phone,
                conversion_score=0,
                conversion_evidence="(stub)",
                first_objection_idx=None,
                first_objection_type=None,
                resolution_idx=None,
                winning_reply_excerpt=None,
                final_outcome="ambiguous",
            )
            fout.write(cc.model_dump_json() + "\n")
            n += 1

    turnarounds_path.write_text("[]", encoding="utf-8")
    lost_path.write_text("[]", encoding="utf-8")

    log.info("stage6 stub: %d conversions → %s", n, conversions_path)
    return {
        "stage": 6,
        "outputs": [conversions_path, turnarounds_path, lost_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
