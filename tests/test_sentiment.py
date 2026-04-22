"""Tests for Stage 5 — M2-S5-T1 template sentiment scoring."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.schemas import TemplateSentiment
from src.sentiment import (
    BATCH_SIZE,
    SentimentBatchResult,
    _pack_batches,
    run as stage5_run,
    score_templates,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_YAML = REPO_ROOT / "input" / "script.yaml"
SCRIPT_MD = REPO_ROOT / "input" / "script-comercial.md"
PROMPTS_DIR = REPO_ROOT / "prompts"


class FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    def complete(self, *, model, messages, system, max_tokens, response_format):
        self.calls.append({
            "model": model,
            "messages": messages,
            "system": system,
            "max_tokens": max_tokens,
            "response_format": response_format,
        })
        if not self.results:
            raise RuntimeError("FakeClient exhausted")
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    def get_usage_report(self):
        return {"max": {"calls": 0}, "api": {"cost_usd": 0.0, "calls": 0}, "fallback_events": []}


@dataclass
class MiniCtx:
    db_path: Path
    script_path: Path
    data_dir: Path
    output_dir: Path
    prompts_dir: Path
    client: object
    script_yaml_path: Path | None = None
    input_dir: Path | None = None
    input_hash: str | None = "test"
    force: bool = False
    restart: bool = False
    chat_limit: int | None = None
    phones_filter: object | None = None
    phones_hash: str | None = None
    llm_mode: str = "api"
    budget_usd: float = 1.0
    dry_run: bool = False


def _prep_ctx(tmp_path, client) -> MiniCtx:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    shutil.copyfile(SCRIPT_YAML, input_dir / "script.yaml")
    return MiniCtx(
        db_path=tmp_path / "msgstore.db",
        script_path=SCRIPT_MD,
        data_dir=data_dir,
        output_dir=tmp_path / "out",
        prompts_dir=PROMPTS_DIR,
        input_dir=input_dir,
        script_yaml_path=input_dir / "script.yaml",
        client=client,
    )


def _mk_tmpl(tid: int, text: str = "x", count: int = 1) -> dict:
    return {
        "template_id": tid,
        "canonical_text": text,
        "instance_count": count,
        "example_msg_ids": [tid * 10],
        "first_seen_ts": 1000,
        "last_seen_ts": 2000,
    }


def _write_templates(data_dir: Path, templates: list[dict]) -> None:
    (data_dir / "spa_templates.json").write_text(
        json.dumps(templates, ensure_ascii=False), encoding="utf-8"
    )


def _ts(tid: int, w=3, c=3, s=3, pol="neu", crit="ok") -> TemplateSentiment:
    return TemplateSentiment(
        template_id=tid, warmth=w, clarity=c, script_adherence=s,
        polarity=pol, critique=crit,
    )


# ---------------- tests ----------------


def test_scores_in_range_all_templates(tmp_path):
    tmpls = [_mk_tmpl(i, f"txt {i}") for i in range(3)]
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, tmpls)
    client = FakeClient([
        SentimentBatchResult(items=[
            _ts(0, w=5, c=4, s=4, pol="pos", crit="acolhedor"),
            _ts(1, w=1, c=4, s=2, pol="neu", crit="frio"),
            _ts(2, w=3, c=3, s=3, pol="neu", crit="ok"),
        ]),
    ])
    ctx.client = client
    out = score_templates(ctx)
    assert len(out) == 3
    for ts in out:
        assert 1 <= ts.warmth <= 5
        assert 1 <= ts.clarity <= 5
        assert 1 <= ts.script_adherence <= 5
        assert ts.polarity in {"pos", "neu", "neg"}


def test_uses_haiku_and_structured_output(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "oi")])
    client = FakeClient([SentimentBatchResult(items=[_ts(0)])])
    ctx.client = client
    score_templates(ctx)
    call = client.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["response_format"] is SentimentBatchResult
    assert "BATCH" in call["messages"][0]["content"]


def test_batch_packing_10_per_call(tmp_path):
    items = [{"template_id": i, "text": "x"} for i in range(25)]
    batches = _pack_batches(items, BATCH_SIZE)
    assert [len(b) for b in batches] == [10, 10, 5]


def test_multi_batch_calls(tmp_path):
    tmpls = [_mk_tmpl(i) for i in range(23)]
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, tmpls)
    client = FakeClient([
        SentimentBatchResult(items=[_ts(i) for i in range(10)]),
        SentimentBatchResult(items=[_ts(i) for i in range(10, 20)]),
        SentimentBatchResult(items=[_ts(i) for i in range(20, 23)]),
    ])
    ctx.client = client
    out = score_templates(ctx)
    assert len(out) == 23
    assert len(client.calls) == 3


def test_critique_portuguese_preserved(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "oi")])
    crit = "Tom frio; sugerir abertura acolhedora com emoção."
    client = FakeClient([SentimentBatchResult(items=[_ts(0, crit=crit)])])
    ctx.client = client
    stage5_run(ctx)
    raw = json.loads((ctx.data_dir / "template_sentiment.json").read_text(encoding="utf-8"))
    assert raw[0]["critique"] == crit
    assert "ç" in raw[0]["critique"] or "ã" in raw[0]["critique"]


def test_invalid_warmth_raises(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0)])
    bad = TemplateSentiment.model_construct(
        template_id=0, warmth=9, clarity=3, script_adherence=3,
        polarity="neu", critique="x",
    )
    client = FakeClient([SentimentBatchResult.model_construct(items=[bad])])
    ctx.client = client
    with pytest.raises(ValueError, match="invalid warmth"):
        score_templates(ctx)


def test_run_writes_output_file(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "oi"), _mk_tmpl(1, "tchau")])
    client = FakeClient([SentimentBatchResult(items=[_ts(0), _ts(1)])])
    ctx.client = client
    result = stage5_run(ctx)
    out_path = ctx.data_dir / "template_sentiment.json"
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert {d["template_id"] for d in data} == {0, 1}
    assert result["stage"] == 5


def test_run_skips_when_real_output_exists(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0)])
    # non-stub cached content
    (ctx.data_dir / "template_sentiment.json").write_text(
        json.dumps([{
            "template_id": 0, "warmth": 5, "clarity": 5,
            "script_adherence": 5, "polarity": "pos", "critique": "cached",
        }]),
        encoding="utf-8",
    )
    client = FakeClient([])
    ctx.client = client
    stage5_run(ctx)
    assert client.calls == []


def test_run_regenerates_when_stub(tmp_path):
    """Stub from M1 (all '(não avaliado)') triggers regen even without --force."""
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0)])
    (ctx.data_dir / "template_sentiment.json").write_text(
        json.dumps([{
            "template_id": 0, "warmth": 3, "clarity": 3,
            "script_adherence": 3, "polarity": "neu", "critique": "(não avaliado)",
        }]),
        encoding="utf-8",
    )
    client = FakeClient([SentimentBatchResult(items=[_ts(0, crit="real")])])
    ctx.client = client
    stage5_run(ctx)
    assert len(client.calls) == 1
    data = json.loads((ctx.data_dir / "template_sentiment.json").read_text(encoding="utf-8"))
    assert data[0]["critique"] == "real"


def test_run_force_reruns(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0)])
    (ctx.data_dir / "template_sentiment.json").write_text(
        json.dumps([{
            "template_id": 0, "warmth": 5, "clarity": 5,
            "script_adherence": 5, "polarity": "pos", "critique": "cached",
        }]),
        encoding="utf-8",
    )
    client = FakeClient([SentimentBatchResult(items=[_ts(0, crit="fresh")])])
    ctx.client = client
    ctx.force = True
    stage5_run(ctx)
    data = json.loads((ctx.data_dir / "template_sentiment.json").read_text(encoding="utf-8"))
    assert data[0]["critique"] == "fresh"
