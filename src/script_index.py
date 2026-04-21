"""Stage 3: script indexing (M1 stub).

See TECH_PLAN.md §M1-T4.

M1 behavior: if data/script.yaml exists and ctx.force is False, no-op. LLM-driven
expansion (M2-S3-T1) will write data/script_extensions.yaml; both files are
loaded and merged by load_merged() for downstream stages.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from src.context import Context
from src.schemas import ObjectionType, ScriptStep

log = logging.getLogger(__name__)

SCRIPT_YAML_RELPATH = "script.yaml"
SCRIPT_EXTENSIONS_RELPATH = "script_extensions.yaml"


def _script_path(ctx: Context) -> Path:
    return ctx.data_dir / SCRIPT_YAML_RELPATH


def _extensions_path(ctx: Context) -> Path:
    return ctx.data_dir / SCRIPT_EXTENSIONS_RELPATH


def load_script(path: Path) -> dict[str, Any]:
    """Load + validate data/script.yaml. Raises on schema mismatch."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a YAML mapping at top level")

    steps_raw = raw.get("steps") or []
    steps = [ScriptStep(**s) for s in steps_raw]
    step_ids = {s.id for s in steps}
    expected = {"1", "2", "3", "3.5", "5", "6", "7", "fup1", "fup2"}
    missing = expected - step_ids
    if missing:
        raise ValueError(f"script.yaml missing step ids: {sorted(missing)}")

    tax_raw = raw.get("objection_taxonomy") or []
    taxonomy = [ObjectionType(**o) for o in tax_raw]
    tax_ids = {o.id for o in taxonomy}
    required_tax = {
        "price", "location", "time_slot", "competitor",
        "hesitation_vou_pensar", "delegated_talk_to_someone",
        "delayed_response_te_falo", "trust_boundary_male", "other",
    }
    tax_missing = required_tax - tax_ids
    if tax_missing:
        raise ValueError(f"objection_taxonomy missing ids: {sorted(tax_missing)}")

    return {
        "steps": steps,
        "services": raw.get("services") or [],
        "price_grid": raw.get("price_grid") or [],
        "additionals": raw.get("additionals") or [],
        "negotiation_rules": raw.get("negotiation_rules") or {},
        "objection_taxonomy": taxonomy,
        "promocoes": raw.get("promocoes") or {},
    }


def load_merged(ctx: Context) -> dict[str, Any]:
    """Load data/script.yaml + (optional) data/script_extensions.yaml merged."""
    base = load_script(_script_path(ctx))
    ext_path = _extensions_path(ctx)
    if ext_path.exists():
        ext = yaml.safe_load(ext_path.read_text(encoding="utf-8")) or {}
        base["extensions"] = ext
    else:
        base["extensions"] = {}
    return base


def run(ctx: Context) -> dict:
    t0 = time.time()
    path = _script_path(ctx)
    if not path.exists():
        raise FileNotFoundError(
            f"missing hand-curated script: {path}. "
            "Commit data/script.yaml per TECH_PLAN.md §M1-T4."
        )

    if ctx.force:
        log.info("stage3: --force set, but M1 stub is hand-curated; not regenerating.")

    # Validate by loading — raises on schema error.
    doc = load_script(path)
    log.info(
        "stage3: %d steps, %d objection types, script.yaml OK",
        len(doc["steps"]),
        len(doc["objection_taxonomy"]),
    )

    return {
        "stage": 3,
        "outputs": [path],
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
