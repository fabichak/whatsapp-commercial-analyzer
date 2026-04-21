"""Tests for Stage 3 stub — see TECH_PLAN.md §M1-T4."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from src.schemas import ObjectionType, ScriptStep
from src.script_index import load_script

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_YAML = REPO_ROOT / "data" / "script.yaml"


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
