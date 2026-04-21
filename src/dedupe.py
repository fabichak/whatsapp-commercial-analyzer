"""Stage 2: dedupe spa messages → spa_templates.json + spa_message_template_map.json.

See TECH_PLAN.md §M1-T3 and §"Shared schemas".

Algorithm:
  1. Collect every from_me=True message across data/conversations.jsonl.
  2. Normalize (NFKD accent strip + lowercase) for similarity.
  3. rapidfuzz.process.cdist(token_set_ratio) → NxN score matrix (uint8).
  4. Union-find merge at threshold >= 88.
  5. Canonical text = cluster member with longest original (cleaned) text.
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from rapidfuzz import fuzz, process

from src.context import Context
from src.schemas import SpaTemplate

log = logging.getLogger(__name__)

DEDUPE_THRESHOLD = 88


def _normalize(t: str) -> str:
    nfkd = unicodedata.normalize("NFKD", t)
    return nfkd.encode("ascii", "ignore").decode("ascii").lower().strip()


@dataclass
class _SpaMsg:
    msg_id: int
    ts_ms: int
    text: str  # cleaned (post-load)
    norm: str  # normalized for similarity


def _load_spa_messages(convos_path: Path) -> list[_SpaMsg]:
    out: list[_SpaMsg] = []
    with convos_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            for m in obj["messages"]:
                if not m["from_me"]:
                    continue
                text = m["text"] or ""
                norm = _normalize(text)
                if not norm:
                    continue
                out.append(
                    _SpaMsg(
                        msg_id=int(m["msg_id"]),
                        ts_ms=int(m["ts_ms"] or 0),
                        text=text,
                        norm=norm,
                    )
                )
    return out


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]:
            self.r[ra] += 1


def _cluster(messages: list[_SpaMsg], threshold: int) -> list[list[int]]:
    n = len(messages)
    if n == 0:
        return []
    norms = [m.norm for m in messages]
    # uint8 score matrix; dtype keeps peak memory ~N^2 bytes (~169MB for 13k).
    scores = process.cdist(
        norms, norms, scorer=fuzz.token_set_ratio, dtype=np.uint8, workers=-1
    )
    uf = _UnionFind(n)
    # upper triangle only (i<j) to halve work
    for i in range(n):
        row = scores[i]
        # argwhere is vectorized; threshold filter first
        hits = np.nonzero(row[i + 1 :] >= threshold)[0]
        for j_off in hits:
            uf.union(i, i + 1 + int(j_off))

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    return list(groups.values())


def _build_template(
    template_id: int, messages: list[_SpaMsg], idxs: list[int]
) -> SpaTemplate:
    members = [messages[i] for i in idxs]
    # Canonical = longest original (cleaned) text
    canonical = max(members, key=lambda m: len(m.text)).text
    timestamps = [m.ts_ms for m in members]
    return SpaTemplate(
        template_id=template_id,
        canonical_text=canonical,
        instance_count=len(members),
        example_msg_ids=[m.msg_id for m in members[:5]],
        first_seen_ts=min(timestamps),
        last_seen_ts=max(timestamps),
    )


def run(ctx: Context) -> dict:
    t0 = time.time()
    convos_path = ctx.data_dir / "conversations.jsonl"
    if not convos_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {convos_path}")

    messages = _load_spa_messages(convos_path)
    log.info("stage2: %d spa messages", len(messages))

    clusters = _cluster(messages, DEDUPE_THRESHOLD)
    # Deterministic ordering: largest cluster first, ties by smallest msg_id.
    clusters.sort(
        key=lambda idxs: (-len(idxs), min(messages[i].msg_id for i in idxs))
    )

    templates: list[SpaTemplate] = []
    msg_to_template: dict[str, int] = {}
    for tid, idxs in enumerate(clusters):
        tpl = _build_template(tid, messages, idxs)
        templates.append(tpl)
        for i in idxs:
            msg_to_template[str(messages[i].msg_id)] = tid

    templates_path = ctx.data_dir / "spa_templates.json"
    map_path = ctx.data_dir / "spa_message_template_map.json"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    templates_path.write_text(
        json.dumps([t.model_dump() for t in templates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    map_path.write_text(
        json.dumps(msg_to_template, ensure_ascii=False), encoding="utf-8"
    )
    log.info(
        "stage2: %d templates → %s (map: %d msgs)",
        len(templates),
        templates_path,
        len(msg_to_template),
    )

    return {
        "stage": 2,
        "outputs": [templates_path, map_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
