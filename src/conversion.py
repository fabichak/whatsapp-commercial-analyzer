"""Stage 6: conversion detection.

M2-S6-T1 added `truncate_for_llm` — pure windowing helper.
M2-S6-T2 adds per-chat Haiku call → `ConversationConversion`. One call
per chat (~387), resume-safe via append-jsonl. See TECH_PLAN.md
§M2-S6-T1 / §M2-S6-T2.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal, Optional

import tiktoken
from pydantic import BaseModel
from tqdm import tqdm

from src.context import Context
from src.schemas import Conversation, ConversationConversion, Message, ObjectionId

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


DETECT_MODEL = "claude-haiku-4-5"
CONVERSION_PROMPT_RELPATH = "stage6_conversion.md"
LABELED_RELPATH = "labeled_messages.jsonl"
CONVERSATIONS_RELPATH = "conversations.jsonl"
CONVERSIONS_RELPATH = "conversions.jsonl"
DETECT_MAX_TOKENS = 1024

VALID_OUTCOMES = {"booked", "lost", "ambiguous"}
VALID_OBJECTION_IDS = {
    "price", "location", "time_slot", "competitor",
    "hesitation_vou_pensar", "delegated_talk_to_someone",
    "delayed_response_te_falo", "trust_boundary_male", "other",
}


class ConversionDetection(BaseModel):
    """LLM return schema (msg_ids, not indices — resolved post-call)."""
    conversion_score: int
    conversion_evidence: str
    first_objection_msg_id: Optional[int] = None
    first_objection_type: Optional[ObjectionId] = None
    resolution_msg_id: Optional[int] = None
    winning_reply_excerpt: Optional[str] = None
    final_outcome: Literal["booked", "lost", "ambiguous"]


def _read_prompt(ctx: Context) -> str:
    p = ctx.prompts_dir / CONVERSION_PROMPT_RELPATH
    if not p.exists():
        raise FileNotFoundError(f"missing prompt: {p}")
    return p.read_text(encoding="utf-8")


def _collect_objection_indices(labeled_path: Path) -> dict[int, dict[int, str]]:
    """Returns {chat_id: {msg_id: objection_type}} for customer msgs with
    non-null objection_type. msg_id → transcript-position resolved later.
    """
    out: dict[int, dict[int, str]] = {}
    if not labeled_path.exists():
        return out
    with labeled_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            obj = rec.get("objection_type")
            if obj is None or rec.get("from_me"):
                continue
            cid = rec["chat_id"]
            mid = rec["msg_id"]
            out.setdefault(cid, {})[mid] = obj
    return out


def _msgid_to_idx(convo: Conversation) -> dict[int, int]:
    return {m.msg_id: i for i, m in enumerate(convo.messages)}


def _load_existing(path: Path) -> dict[int, ConversationConversion]:
    if not path.exists():
        return {}
    out: dict[int, ConversationConversion] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                cc = ConversationConversion.model_validate_json(line)
            except Exception:
                continue
            if cc.conversion_evidence == "(stub)":
                continue
            out[cc.chat_id] = cc
    return out


def _flush_jsonl(path: Path, convos_order: list[int], have: dict[int, ConversationConversion]) -> None:
    """Atomic rewrite preserving input-file chat order."""
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with tmp.open("w", encoding="utf-8") as f:
        for cid in convos_order:
            cc = have.get(cid)
            if cc is not None:
                f.write(cc.model_dump_json() + "\n")
    os.replace(tmp, path)


def _build_user_msg(transcript: str) -> str:
    return (
        "CONVERSA (cada linha: `[MSG_ID] role: texto`):\n"
        + transcript
        + "\n\nClassifique esta conversa conforme instruído. Responda com "
        "um único objeto JSON."
    )


def _validate(d: ConversionDetection) -> None:
    if not (0 <= d.conversion_score <= 3):
        raise ValueError(f"conversion_score {d.conversion_score} out of [0,3]")
    if d.final_outcome not in VALID_OUTCOMES:
        raise ValueError(f"invalid final_outcome {d.final_outcome!r}")
    if d.first_objection_type is not None and d.first_objection_type not in VALID_OBJECTION_IDS:
        raise ValueError(f"invalid first_objection_type {d.first_objection_type!r}")
    if d.first_objection_type is None and d.first_objection_msg_id is not None:
        # tolerate: blank objection_type but present msg_id → downgrade
        d.first_objection_msg_id = None


def _to_conversion(
    convo: Conversation, d: ConversionDetection
) -> ConversationConversion:
    m2i = _msgid_to_idx(convo)
    obj_idx = m2i.get(d.first_objection_msg_id) if d.first_objection_msg_id is not None else None
    res_idx = m2i.get(d.resolution_msg_id) if d.resolution_msg_id is not None else None
    excerpt = d.winning_reply_excerpt
    if excerpt is None and res_idx is not None:
        excerpt = convo.messages[res_idx].text[:200]
    if excerpt is not None:
        excerpt = excerpt[:200]
    return ConversationConversion(
        chat_id=convo.chat_id,
        phone=convo.phone,
        conversion_score=d.conversion_score,
        conversion_evidence=d.conversion_evidence,
        first_objection_idx=obj_idx,
        first_objection_type=d.first_objection_type,
        resolution_idx=res_idx,
        winning_reply_excerpt=excerpt,
        final_outcome=d.final_outcome,
    )


def detect_conversions(ctx: Context) -> list[ConversationConversion]:
    """Per-chat Haiku call → ConversationConversion list.

    Env: STAGE6_CONCURRENCY (default 8).
    """
    if ctx.client is None:
        raise RuntimeError("detect_conversions requires ctx.client")

    convos_path = ctx.data_dir / CONVERSATIONS_RELPATH
    if not convos_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {convos_path}")

    labeled_path = ctx.data_dir / LABELED_RELPATH
    objection_map = _collect_objection_indices(labeled_path)

    out_path = ctx.data_dir / CONVERSIONS_RELPATH
    if getattr(ctx, "force", False) or getattr(ctx, "restart", False):
        done: dict[int, ConversationConversion] = {}
    else:
        done = _load_existing(out_path)
    if done:
        log.info("stage6: resuming with %d conversions already on disk", len(done))

    convos: list[Conversation] = []
    with convos_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            convos.append(Conversation.model_validate_json(line))

    order = [c.chat_id for c in convos]
    pending = [c for c in convos if c.chat_id not in done]
    log.info(
        "stage6: %d chats total, %d pending, %d already done",
        len(convos), len(pending), len(done),
    )

    system = _read_prompt(ctx)
    write_lock = threading.Lock()

    def _flush() -> None:
        with write_lock:
            _flush_jsonl(out_path, order, done)

    def _run_one(convo: Conversation) -> ConversationConversion:
        obj_map = objection_map.get(convo.chat_id, {})
        m2i = _msgid_to_idx(convo)
        obj_indices = sorted(
            idx for mid, _ in obj_map.items()
            if (idx := m2i.get(mid)) is not None
        )
        transcript = truncate_for_llm(convo, objection_indices=obj_indices)
        result = ctx.client.complete(
            model=DETECT_MODEL,
            messages=[{"role": "user", "content": _build_user_msg(transcript)}],
            system=system,
            max_tokens=DETECT_MAX_TOKENS,
            response_format=ConversionDetection,
        )
        if not isinstance(result, ConversionDetection):
            raise TypeError(f"expected ConversionDetection, got {type(result).__name__}")
        _validate(result)
        return _to_conversion(convo, result)

    workers = max(1, int(os.environ.get("STAGE6_CONCURRENCY", "8")))

    if workers == 1:
        for convo in tqdm(pending, desc="stage6: conversions", disable=None):
            cc = _run_one(convo)
            done[convo.chat_id] = cc
            _flush()
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_run_one, c): c.chat_id for c in pending}
            for fut in tqdm(as_completed(futs), total=len(futs), desc="stage6: conversions", disable=None):
                cc = fut.result()
                done[cc.chat_id] = cc
                _flush()

    _flush()
    ordered = [done[cid] for cid in order if cid in done]
    log.info("stage6: %d conversions → %s", len(ordered), out_path)
    return ordered


def run(ctx: Context) -> dict:
    t0 = time.time()
    conversions_path = ctx.data_dir / CONVERSIONS_RELPATH
    turnarounds_path = ctx.data_dir / "turnarounds.json"
    lost_path = ctx.data_dir / "lost_deals.json"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)

    detect_conversions(ctx)

    # M2-S6-T3 populates these; stub until then.
    if not turnarounds_path.exists():
        turnarounds_path.write_text("[]", encoding="utf-8")
    if not lost_path.exists():
        lost_path.write_text("[]", encoding="utf-8")

    api_cost = 0.0
    if ctx.client is not None:
        api_cost = float(ctx.client.get_usage_report().get("api", {}).get("cost_usd", 0.0))

    return {
        "stage": 6,
        "outputs": [conversions_path, turnarounds_path, lost_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": api_cost,
        "elapsed_s": time.time() - t0,
    }
