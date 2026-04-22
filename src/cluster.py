"""Stage 7: off-script embedding + HDBSCAN clustering.

See TECH_PLAN.md §M2-S7-T1.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np

from src.context import Context
from src.schemas import Aggregation, OffScriptCluster, PerStepAgg

log = logging.getLogger(__name__)

EMBED_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MIN_CLUSTER_SIZE = 3
UNKNOWN_STEP = "unknown"
TOP_N_INTENTS = 5
TOP_N_OBJECTIONS = 5
EXAMPLE_IDS_PER_CLUSTER = 5

_MODEL = None


def _get_model():
    """Lazy-load + cache the embedding model."""
    global _MODEL
    if _MODEL is None:
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(EMBED_MODEL_NAME, device="cpu")
    return _MODEL


def embed_texts(texts: list[str]) -> np.ndarray:
    """Deterministic CPU embedding of texts → (N, D) float32 array."""
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    model = _get_model()
    vecs = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=False,
    )
    return np.asarray(vecs, dtype=np.float32)


def cluster_embeddings(vectors: np.ndarray) -> np.ndarray:
    """Run HDBSCAN with cosine metric, min_cluster_size=MIN_CLUSTER_SIZE.

    Returns label array; -1 = noise.
    """
    if len(vectors) < MIN_CLUSTER_SIZE:
        return np.full(len(vectors), -1, dtype=int)
    import hdbscan
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=MIN_CLUSTER_SIZE,
        min_samples=1,
        metric="cosine",
        algorithm="generic",
    )
    return clusterer.fit_predict(vectors.astype(np.float64))


def select_medoid_index(vectors: np.ndarray) -> int:
    """Index of member with min sum of cosine distances to other members."""
    n = len(vectors)
    if n == 1:
        return 0
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    unit = vectors / norms
    sim = unit @ unit.T
    dist = 1.0 - sim
    return int(np.argmin(dist.sum(axis=1)))


def _assign_parent_step(
    chat_messages_sorted: list[dict],
) -> dict[int, str]:
    """For each customer msg_id, last preceding spa step_id (or UNKNOWN)."""
    out: dict[int, str] = {}
    last_spa_step: str | None = None
    for m in chat_messages_sorted:
        if m["from_me"]:
            sid = m.get("step_id")
            if sid:
                last_spa_step = sid
        else:
            out[m["msg_id"]] = last_spa_step if last_spa_step else UNKNOWN_STEP
    return out


def _build_clusters_for_group(
    texts: list[str],
    msg_ids: list[int],
) -> tuple[list[OffScriptCluster], int]:
    """Return (clusters, noise_count) for one step bucket."""
    if not texts:
        return [], 0
    vecs = embed_texts(texts)
    labels = cluster_embeddings(vecs)
    by_label: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        by_label[int(lab)].append(i)
    noise_count = len(by_label.get(-1, []))
    clusters: list[OffScriptCluster] = []
    for lab, idxs in sorted(by_label.items()):
        if lab == -1:
            continue
        sub_vecs = vecs[idxs]
        medoid_local = select_medoid_index(sub_vecs)
        medoid_global = idxs[medoid_local]
        clusters.append(OffScriptCluster(
            step_id="",  # filled by caller
            medoid_text=texts[medoid_global],
            size=len(idxs),
            example_msg_ids=[msg_ids[i] for i in idxs[:EXAMPLE_IDS_PER_CLUSTER]],
        ))
    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters, noise_count


def build_aggregations(
    labeled: list[dict],
    conversations: list[dict],
) -> Aggregation:
    """Core Stage 7 logic: produce Aggregation from labeled + conversations."""
    # Index texts by (chat_id, msg_id)
    text_map: dict[tuple[int, int], tuple[str, int]] = {}
    chat_messages: dict[int, list[dict]] = {}
    for conv in conversations:
        cid = conv["chat_id"]
        msgs = conv["messages"]
        chat_messages[cid] = sorted(msgs, key=lambda m: (m["ts_ms"], m["msg_id"]))
        for m in msgs:
            text_map[(cid, m["msg_id"])] = (m["text"], m["ts_ms"])

    # Enrich labeled msgs with step_id from spa + parent-step for customers.
    label_index: dict[tuple[int, int], dict] = {
        (lm["chat_id"], lm["msg_id"]): lm for lm in labeled
    }
    parent_step_by_msg: dict[tuple[int, int], str] = {}
    for cid, msgs_sorted in chat_messages.items():
        enriched = []
        for m in msgs_sorted:
            key = (cid, m["msg_id"])
            lm = label_index.get(key)
            if lm is None:
                continue
            enriched.append({
                "msg_id": m["msg_id"],
                "from_me": lm["from_me"],
                "step_id": lm.get("step_id"),
            })
        parents = _assign_parent_step(enriched)
        for mid, step in parents.items():
            parent_step_by_msg[(cid, mid)] = step

    # Per-step counts + intents/objections (from ALL customer msgs).
    on_counts: Counter = Counter()
    off_counts: Counter = Counter()
    intents_by_step: dict[str, Counter] = defaultdict(Counter)
    objections_by_step: dict[str, Counter] = defaultdict(Counter)

    # Off-script customer msgs per step bucket.
    off_texts: dict[str, list[str]] = defaultdict(list)
    off_ids: dict[str, list[int]] = defaultdict(list)

    for lm in labeled:
        if lm["from_me"]:
            continue
        key = (lm["chat_id"], lm["msg_id"])
        parent = parent_step_by_msg.get(key, UNKNOWN_STEP)
        ctx_lbl = lm["step_context"]
        if ctx_lbl == "on_script":
            on_counts[parent] += 1
        elif ctx_lbl == "off_script":
            off_counts[parent] += 1
            tm = text_map.get(key)
            if tm is not None:
                txt = tm[0].strip()
                if txt:
                    off_texts[parent].append(txt)
                    off_ids[parent].append(lm["msg_id"])
        if lm.get("intent"):
            intents_by_step[parent][lm["intent"]] += 1
        if lm.get("objection_type"):
            objections_by_step[parent][lm["objection_type"]] += 1

    # Cluster each step bucket (except unknown → global off_script_clusters).
    per_step: dict[str, PerStepAgg] = {}
    global_clusters: list[OffScriptCluster] = []
    all_steps = set(on_counts) | set(off_counts) | set(intents_by_step) | set(objections_by_step) | set(off_texts)
    all_steps.discard(UNKNOWN_STEP)

    for step in sorted(all_steps):
        clusters, _noise = _build_clusters_for_group(
            off_texts.get(step, []),
            off_ids.get(step, []),
        )
        clusters = [c.model_copy(update={"step_id": step}) for c in clusters]
        per_step[step] = PerStepAgg(
            step_id=step,
            on_script_count=on_counts.get(step, 0),
            off_script_count=off_counts.get(step, 0),
            top_intents=intents_by_step.get(step, Counter()).most_common(TOP_N_INTENTS),
            top_clusters=clusters,
            top_objections=objections_by_step.get(step, Counter()).most_common(TOP_N_OBJECTIONS),
        )

    # Unknown bucket → global clusters.
    u_clusters, _u_noise = _build_clusters_for_group(
        off_texts.get(UNKNOWN_STEP, []),
        off_ids.get(UNKNOWN_STEP, []),
    )
    global_clusters = [c.model_copy(update={"step_id": UNKNOWN_STEP}) for c in u_clusters]

    return Aggregation(per_step=per_step, off_script_clusters=global_clusters)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def run(ctx: Context) -> dict:
    t0 = time.time()
    labeled_path = ctx.data_dir / "labeled_messages.jsonl"
    conv_path = ctx.data_dir / "conversations.jsonl"
    if not labeled_path.exists():
        raise FileNotFoundError(f"missing stage 4 output: {labeled_path}")
    if not conv_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {conv_path}")

    labeled = _load_jsonl(labeled_path)
    conversations = _load_jsonl(conv_path)

    agg = build_aggregations(labeled, conversations)

    out_path = ctx.data_dir / "aggregations.json"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(agg.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    n_global = len(agg.off_script_clusters)
    n_steps = len(agg.per_step)
    log.info("stage7: %d steps, %d global clusters → %s", n_steps, n_global, out_path)
    return {
        "stage": 7,
        "outputs": [out_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
