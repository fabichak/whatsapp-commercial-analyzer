"""Stage 4 stub: pass-through labeling, zero LLM calls.

See TECH_PLAN.md §M1-T5. Real implementation lands in M2-S4-T1/T2.
Emits one LabeledMessage per input message with all LLM-derived fields null.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from src.context import Context
from src.schemas import Conversation, LabeledMessage

log = logging.getLogger(__name__)


def run(ctx: Context) -> dict:
    t0 = time.time()
    convos_path = ctx.data_dir / "conversations.jsonl"
    if not convos_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {convos_path}")

    out_path = ctx.data_dir / "labeled_messages.jsonl"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    with convos_path.open("r", encoding="utf-8") as fin, out_path.open(
        "w", encoding="utf-8"
    ) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            convo = Conversation.model_validate_json(line)
            for m in convo.messages:
                lm = LabeledMessage(
                    msg_id=m.msg_id,
                    chat_id=convo.chat_id,
                    from_me=m.from_me,
                    step_id=None,
                    step_context="unknown",
                    intent=None,
                    objection_type=None,
                    sentiment=None,
                    matches_script=None,
                    deviation_note=None,
                )
                fout.write(lm.model_dump_json() + "\n")
                n += 1

    log.info("stage4 stub: %d labeled messages → %s", n, out_path)
    return {
        "stage": 4,
        "outputs": [out_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
