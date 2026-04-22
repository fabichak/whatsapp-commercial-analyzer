"""Tests for Stage 7 — M2-S7-T1 off-script embedding + HDBSCAN."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from src.cluster import (
    build_aggregations,
    cluster_embeddings,
    embed_texts,
    run as stage7_run,
    select_medoid_index,
)


@dataclass
class MiniCtx:
    data_dir: Path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
        encoding="utf-8",
    )


def test_embeddings_deterministic():
    texts = ["quanto custa?", "qual o valor?", "onde fica?"]
    v1 = embed_texts(texts)
    v2 = embed_texts(texts)
    assert v1.shape == v2.shape
    assert np.max(np.abs(v1 - v2)) < 1e-6


def test_clustering_groups_paraphrases():
    texts = [
        "quanto custa?", "qual o valor?", "qual preço?",
        "onde fica?", "qual endereço?", "fica aonde?",
    ]
    vecs = embed_texts(texts)
    labels = cluster_embeddings(vecs)
    price_labels = {labels[0], labels[1], labels[2]}
    loc_labels = {labels[3], labels[4], labels[5]}
    assert -1 not in price_labels
    assert -1 not in loc_labels
    assert len(price_labels) == 1
    assert len(loc_labels) == 1
    assert price_labels != loc_labels


def test_medoid_selection():
    # 5 points on 2D; central point at origin, others at unit offsets.
    vecs = np.array([
        [1.0, 0.0],
        [0.0, 1.0],
        [-1.0, 0.0],
        [0.0, -1.0],
        [0.1, 0.1],  # near-origin → most central by cosine? all outer are orthogonal pairs
    ], dtype=np.float32)
    # For this geometry the central point minimises sum of cosine dists.
    # Verify it picks the near-origin-ish point (index 4), which has non-orthogonal
    # relationships to all others.
    idx = select_medoid_index(vecs)
    assert idx == 4


def test_empty_input_handled(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_jsonl(data_dir / "labeled_messages.jsonl", [])
    _write_jsonl(data_dir / "conversations.jsonl", [])
    ctx = MiniCtx(data_dir=data_dir)
    stage7_run(ctx)
    out = json.loads((data_dir / "aggregations.json").read_text(encoding="utf-8"))
    assert out == {"per_step": {}, "off_script_clusters": []}


def test_aggregation_counts_match_inputs():
    # Build a synthetic chat: spa msg (step=1), then 6 off-script customer msgs
    # (3 price paraphrases, 3 location paraphrases), plus 2 on-script, 1 unknown.
    conv = {
        "chat_id": 1,
        "phone": "5511999999999",
        "messages": [
            {"msg_id": 100, "ts_ms": 1000, "from_me": True,  "text": "Oi!",      "text_raw": "Oi!"},
            {"msg_id": 101, "ts_ms": 1001, "from_me": False, "text": "quanto custa?",  "text_raw": ""},
            {"msg_id": 102, "ts_ms": 1002, "from_me": False, "text": "qual o valor?",  "text_raw": ""},
            {"msg_id": 103, "ts_ms": 1003, "from_me": False, "text": "qual preço?",    "text_raw": ""},
            {"msg_id": 104, "ts_ms": 1004, "from_me": False, "text": "onde fica?",     "text_raw": ""},
            {"msg_id": 105, "ts_ms": 1005, "from_me": False, "text": "qual endereço?", "text_raw": ""},
            {"msg_id": 106, "ts_ms": 1006, "from_me": False, "text": "fica aonde?",    "text_raw": ""},
            {"msg_id": 107, "ts_ms": 1007, "from_me": False, "text": "ok obrigado",    "text_raw": ""},  # on_script
            {"msg_id": 108, "ts_ms": 1008, "from_me": False, "text": "aleatório xyz",  "text_raw": ""},  # off_script solo
        ],
    }
    labeled = [
        {"msg_id": 100, "chat_id": 1, "from_me": True,  "step_id": "1", "step_context": "on_script"},
        {"msg_id": 101, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "off_script"},
        {"msg_id": 102, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "off_script"},
        {"msg_id": 103, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "off_script"},
        {"msg_id": 104, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "off_script"},
        {"msg_id": 105, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "off_script"},
        {"msg_id": 106, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "off_script"},
        {"msg_id": 107, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "on_script"},
        {"msg_id": 108, "chat_id": 1, "from_me": False, "step_id": None, "step_context": "off_script"},
    ]
    agg = build_aggregations(labeled, [conv])
    total_offscript = sum(1 for lm in labeled if not lm["from_me"] and lm["step_context"] == "off_script")

    # All off-script customer msgs parented at step "1".
    step1 = agg.per_step["1"]
    cluster_sizes = sum(c.size for c in step1.top_clusters)
    # noise = off_script_count - sum of cluster sizes
    noise = step1.off_script_count - cluster_sizes
    assert cluster_sizes + noise == step1.off_script_count == total_offscript
    assert step1.on_script_count == 1
    # Expect 2 clusters (price + location), each size 3.
    assert len(step1.top_clusters) == 2
    assert {c.size for c in step1.top_clusters} == {3}
    # No unknown bucket since spa msg precedes all customer msgs.
    assert agg.off_script_clusters == []
