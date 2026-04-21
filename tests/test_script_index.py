"""Tests for Stage 3 — see TECH_PLAN.md §M1-T4 (base) + §M2-S3-T1 (expansion)."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.schemas import ObjectionType, ScriptStep
from src.script_index import (
    DaySpaPitch,
    DaySpaPitchStep,
    Inconsistency,
    ObjectionReply,
    ScriptExtensions,
    TAXONOMY_IDS,
    load_merged,
    load_script,
)
from src.script_index import run as stage3_run

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_YAML = REPO_ROOT / "data" / "script.yaml"
SCRIPT_MD = REPO_ROOT / "script-comercial.md"
PROMPTS_DIR = REPO_ROOT / "prompts"


def test_script_yaml_loads():
    doc = load_script(SCRIPT_YAML)
    assert isinstance(doc["steps"][0], ScriptStep)
    assert isinstance(doc["objection_taxonomy"][0], ObjectionType)


def test_parse_script_extracts_9_steps():
    doc = load_script(SCRIPT_YAML)
    ids = {s.id for s in doc["steps"]}
    assert ids == {"1", "2", "3", "3.5", "5", "6", "7", "fup1", "fup2"}


def test_objection_taxonomy_preseeded():
    doc = load_script(SCRIPT_YAML)
    assert len(doc["objection_taxonomy"]) == 9
    ids = {o.id for o in doc["objection_taxonomy"]}
    assert "hesitation_vou_pensar" in ids
    assert "trust_boundary_male" in ids


def test_promocoes_dates_parsed():
    raw = yaml.safe_load(SCRIPT_YAML.read_text(encoding="utf-8"))
    promo = raw["promocoes"]["dia_das_maes"]
    assert isinstance(promo["valid_from"], date)
    assert isinstance(promo["valid_until"], date)
    assert isinstance(promo["usage_deadline"], date)
    assert promo["valid_from"] < promo["valid_until"] < promo["usage_deadline"]


def test_missing_step_raises(tmp_path):
    bad = tmp_path / "script.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "steps": [
                    {
                        "id": "1",
                        "name": "x",
                        "canonical_texts": [],
                        "expected_customer_intents": [],
                        "transitions_to": [],
                    }
                ],
                "objection_taxonomy": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing step ids"):
        load_script(bad)


# ---------------- M2-S3-T1: expansion ----------------


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def complete(self, *, model, messages, system, max_tokens, response_format):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "system": system,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

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
    force: bool = False
    chat_limit: int | None = None
    phones_filter: object | None = None
    phones_hash: str | None = None
    llm_mode: str = "api"
    budget_usd: float = 1.0
    dry_run: bool = False


def _valid_extensions() -> ScriptExtensions:
    return ScriptExtensions(
        day_spa_pitch=DaySpaPitch(
            intro="Convite para a experiência.",
            steps=[
                DaySpaPitchStep(order=1, name="Escalda-pés", phrase="Inicie com escalda-pés aromático."),
                DaySpaPitchStep(order=2, name="Banho de imersão", phrase="Banho de imersão morno."),
                DaySpaPitchStep(order=3, name="Massagem", phrase="Massagem relaxante com pedras quentes."),
            ],
            closing="Vamos reservar seu horário?",
        ),
        objection_replies=[
            ObjectionReply(objection_id=tid, reply_template=f"resposta {tid}", rationale="x")
            for tid in TAXONOMY_IDS
        ],
        inconsistencies=[Inconsistency(location="Promoções", description="Preço base diverge")],
    )


def _prep_tmp(tmp_path: Path) -> MiniCtx:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shutil.copyfile(SCRIPT_YAML, data_dir / "script.yaml")
    return MiniCtx(
        db_path=tmp_path / "msgstore.db",
        script_path=SCRIPT_MD,
        data_dir=data_dir,
        output_dir=tmp_path / "out",
        prompts_dir=PROMPTS_DIR,
        client=None,
    )


def test_expansion_writes_extensions_file(tmp_path):
    ctx = _prep_tmp(tmp_path)
    ctx.client = FakeClient(_valid_extensions())
    stage3_run(ctx)
    ext_path = ctx.data_dir / "script_extensions.yaml"
    assert ext_path.exists()
    data = yaml.safe_load(ext_path.read_text(encoding="utf-8"))
    assert "day_spa_pitch" in data
    assert "objection_replies" in data
    assert "inconsistencies" in data
    assert len(data["objection_replies"]) == 9


def test_expansion_does_not_mutate_script_yaml(tmp_path):
    ctx = _prep_tmp(tmp_path)
    ctx.client = FakeClient(_valid_extensions())
    path = ctx.data_dir / "script.yaml"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    stage3_run(ctx)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after
    # repo copy untouched too
    assert SCRIPT_YAML.read_bytes() == SCRIPT_YAML.read_bytes()


def test_expansion_skipped_when_extensions_exist(tmp_path):
    ctx = _prep_tmp(tmp_path)
    (ctx.data_dir / "script_extensions.yaml").write_text("preexisting: true\n", encoding="utf-8")
    fake = FakeClient(_valid_extensions())
    ctx.client = fake
    stage3_run(ctx)
    assert fake.calls == []


def test_expansion_force_regenerates(tmp_path):
    ctx = _prep_tmp(tmp_path)
    (ctx.data_dir / "script_extensions.yaml").write_text("preexisting: true\n", encoding="utf-8")
    ctx.force = True
    fake = FakeClient(_valid_extensions())
    ctx.client = fake
    stage3_run(ctx)
    assert len(fake.calls) == 1
    # got overwritten
    data = yaml.safe_load((ctx.data_dir / "script_extensions.yaml").read_text(encoding="utf-8"))
    assert "day_spa_pitch" in data


def test_load_merged_returns_combined_dict(tmp_path):
    ctx = _prep_tmp(tmp_path)
    ctx.client = FakeClient(_valid_extensions())
    stage3_run(ctx)
    merged = load_merged(ctx)
    assert "steps" in merged
    assert "extensions" in merged
    assert "day_spa_pitch" in merged["extensions"]
    assert len(merged["extensions"]["objection_replies"]) == 9


def test_expansion_rejects_missing_objection_id(tmp_path):
    ctx = _prep_tmp(tmp_path)
    bad = _valid_extensions()
    bad.objection_replies = bad.objection_replies[:8]  # drop one
    ctx.client = FakeClient(bad)
    with pytest.raises(ValueError, match="objection_replies"):
        stage3_run(ctx)


def test_expansion_call_uses_sonnet_and_structured_output(tmp_path):
    ctx = _prep_tmp(tmp_path)
    fake = FakeClient(_valid_extensions())
    ctx.client = fake
    stage3_run(ctx)
    assert fake.calls[0]["model"] == "claude-sonnet-4-6"
    assert fake.calls[0]["response_format"] is ScriptExtensions
    assert fake.calls[0]["max_tokens"] == 8000
