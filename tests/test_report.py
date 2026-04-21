"""Offline tests for Stage 8 report module.

Covers prompt-payload build, CSV writing, and end-to-end `run(ctx)` with
a fake LLM client. Real Sonnet call is exercised by
`scripts/verify_stage8.py`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import report as report_mod
from src.context import Context


def _seed_data(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "spa_templates.json").write_text(json.dumps([
        {
            "template_id": 0, "canonical_text": "bom dia! tudo bem?",
            "instance_count": 12, "example_msg_ids": [1, 2], "first_seen_ts": 1,
            "last_seen_ts": 99,
        },
        {
            "template_id": 1, "canonical_text": "segue valor: R$ 420",
            "instance_count": 5, "example_msg_ids": [10], "first_seen_ts": 2,
            "last_seen_ts": 98,
        },
    ], ensure_ascii=False))
    (data_dir / "template_sentiment.json").write_text(json.dumps([
        {"template_id": 0, "warmth": 5, "clarity": 4, "script_adherence": 4,
         "polarity": "pos", "critique": "caloroso"},
        {"template_id": 1, "warmth": 2, "clarity": 3, "script_adherence": 3,
         "polarity": "neg", "critique": "frio"},
    ], ensure_ascii=False))
    with (data_dir / "conversions.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "chat_id": 1, "phone": "5511999999999", "conversion_score": 0,
            "conversion_evidence": "(stub)", "first_objection_idx": None,
            "first_objection_type": None, "resolution_idx": None,
            "winning_reply_excerpt": None, "final_outcome": "ambiguous",
        }) + "\n")
    (data_dir / "turnarounds.json").write_text("[]")
    (data_dir / "lost_deals.json").write_text("[]")
    (data_dir / "aggregations.json").write_text(
        json.dumps({"per_step": {}, "off_script_clusters": []})
    )


def _make_ctx(tmp_path: Path, client) -> Context:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "stage8_report.md").write_text("system prompt stub")
    return Context(
        db_path=tmp_path / "msgstore.db",
        script_path=tmp_path / "script-comercial.md",
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        prompts_dir=prompts_dir,
        client=client,
    )


class _FakeClient:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []
        self._cost = 0.0

    def complete(self, model, messages, system="", max_tokens=1024, response_format=None):
        self.calls.append({"model": model, "messages": messages, "system": system})
        self._cost += 0.02
        return self.text

    def get_usage_report(self):
        return {"api": {"cost_usd": self._cost}, "max": {}, "fallback_events": []}


def _full_report_text() -> str:
    return "\n\n".join(report_mod.SECTION_HEADERS) + "\n\nCorpo: ação, ção, é ô.\n"


def test_run_writes_report_and_csvs(tmp_path):
    _seed_data(tmp_path / "data")
    client = _FakeClient(_full_report_text())
    ctx = _make_ctx(tmp_path, client)

    result = report_mod.run(ctx)

    report_path = ctx.output_dir / "report.md"
    assert report_path.exists()
    text = report_path.read_text(encoding="utf-8")
    for h in report_mod.SECTION_HEADERS:
        assert h in text

    for name, header in report_mod.CSV_FILES.items():
        p = ctx.output_dir / name
        assert p.exists(), f"missing {name}"
        with p.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == header

    assert result["stage"] == 8
    assert result["llm_usd_api"] > 0
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "claude-sonnet-4-6"


def test_csv_utf8_roundtrip(tmp_path):
    _seed_data(tmp_path / "data")
    client = _FakeClient(_full_report_text())
    ctx = _make_ctx(tmp_path, client)
    report_mod.run(ctx)
    raw = (ctx.output_dir / "spa_templates_scored.csv").read_bytes().decode("utf-8")
    assert "caloroso" in raw
    assert "frio" in raw


def test_missing_prompt_file_raises(tmp_path):
    _seed_data(tmp_path / "data")
    client = _FakeClient("x")
    ctx = _make_ctx(tmp_path, client)
    (ctx.prompts_dir / "stage8_report.md").unlink()
    with pytest.raises(FileNotFoundError):
        report_mod.run(ctx)


def test_top_templates_sorted(tmp_path):
    _seed_data(tmp_path / "data")
    client = _FakeClient(_full_report_text())
    ctx = _make_ctx(tmp_path, client)
    payload = report_mod._build_prompt_payload(ctx)
    pos_ids = [t["template_id"] for t in payload["top_positive_templates"]]
    neg_ids = [t["template_id"] for t in payload["top_negative_templates"]]
    assert pos_ids[0] == 0  # positive polarity × warmth=5 × count=12
    assert neg_ids[0] == 1  # negative polarity × warmth=2 × count=5
