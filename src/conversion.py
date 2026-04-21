"""Stage 6: conversion detection.

M1-T5 stub `run()` still ships zero-conversion rows. M2-S6-T1 adds
`truncate_for_llm` — the pure windowing helper Stage 6's Haiku call
will feed. See TECH_PLAN.md §M2-S6-T1.
"""

from __future__ import annotations

import logging
import time

import tiktoken

from src.context import Context
from src.schemas import Conversation, ConversationConversion, Message

log = logging.getLogger(__name__)

FIRST_WINDOW = 15
LAST_WINDOW = 15
OBJECTION_RADIUS = 10
DEFAULT_MAX_TOKENS = 3000
_ENCODER_NAME = "cl100k_base"
_encoder: tiktoken.Encoding | None = None


def _enc() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding(_ENCODER_NAME)
    return _encoder


def count_tokens(s: str) -> int:
    return len(_enc().encode(s))


def _render_msg(m: Message) -> str:
    role = "spa" if m.from_me else "cli"
    return f"[{m.msg_id}] {role}: {m.text}"


def _merge_windows(
    windows: list[tuple[int, int]], n: int
) -> list[tuple[int, int]]:
    clipped = [(max(0, a), min(n, b)) for a, b in windows if a < n and b > 0]
    clipped = [w for w in clipped if w[0] < w[1]]
    if not clipped:
        return []
    clipped.sort()
    merged: list[tuple[int, int]] = [clipped[0]]
    for a, b in clipped[1:]:
        la, lb = merged[-1]
        if a <= lb:
            merged[-1] = (la, max(lb, b))
        else:
            merged.append((a, b))
    return merged


def truncate_for_llm(
    convo: Conversation,
    objection_indices: list[int] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Build ≤`max_tokens` transcript slice for Stage 6 Haiku.

    Windows included: first `FIRST_WINDOW` msgs, ±`OBJECTION_RADIUS` around
    each objection index, last `LAST_WINDOW` msgs. Overlapping windows merge;
    gaps render as `[... K mensagens ...]`. Pure function — no I/O.

    Indices refer to positions in `convo.messages` (not msg_id).
    """
    msgs = convo.messages
    n = len(msgs)
    if n == 0:
        return ""

    windows: list[tuple[int, int]] = [(0, FIRST_WINDOW), (n - LAST_WINDOW, n)]
    for idx in objection_indices or []:
        windows.append((idx - OBJECTION_RADIUS, idx + OBJECTION_RADIUS + 1))
    merged = _merge_windows(windows, n)

    parts: list[str] = []
    prev_end = 0
    for i, (a, b) in enumerate(merged):
        if i == 0 and a > 0:
            parts.append(f"[... {a} mensagens ...]")
        elif i > 0:
            gap = a - prev_end
            if gap > 0:
                parts.append(f"[... {gap} mensagens ...]")
        for j in range(a, b):
            parts.append(_render_msg(msgs[j]))
        prev_end = b
    if prev_end < n:
        parts.append(f"[... {n - prev_end} mensagens ...]")

    out = "\n".join(parts)
    if count_tokens(out) <= max_tokens:
        return out

    # Over budget: shrink objection windows symmetrically until it fits or
    # we've collapsed them entirely. First/last windows are non-negotiable.
    radius = OBJECTION_RADIUS
    while radius > 0 and count_tokens(out) > max_tokens:
        radius -= 1
        windows = [(0, FIRST_WINDOW), (n - LAST_WINDOW, n)]
        for idx in objection_indices or []:
            windows.append((idx - radius, idx + radius + 1))
        merged = _merge_windows(windows, n)
        parts = []
        prev_end = 0
        for i, (a, b) in enumerate(merged):
            if i == 0 and a > 0:
                parts.append(f"[... {a} mensagens ...]")
            elif i > 0:
                gap = a - prev_end
                if gap > 0:
                    parts.append(f"[... {gap} mensagens ...]")
            for j in range(a, b):
                parts.append(_render_msg(msgs[j]))
            prev_end = b
        if prev_end < n:
            parts.append(f"[... {n - prev_end} mensagens ...]")
        out = "\n".join(parts)
    return out


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
