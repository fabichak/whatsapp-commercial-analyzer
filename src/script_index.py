"""Stage 3: script indexing + LLM expansion.

See TECH_PLAN.md §M1-T4 (hand-curated base) and §M2-S3-T1 (LLM expansion).

`data/script.yaml` is the hand-curated source of truth (committed).
`data/script_extensions.yaml` is LLM-generated (gitignored). This module
never mutates `script.yaml`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from src.context import Context
from src.schemas import ObjectionType, ScriptStep

log = logging.getLogger(__name__)

SCRIPT_YAML_RELPATH = "script.yaml"
SCRIPT_EXTENSIONS_RELPATH = "script_extensions.yaml"
EXPAND_PROMPT_RELPATH = "stage3_expand_script.md"
EXPAND_MODEL = "claude-sonnet-4-6"
EXPAND_MAX_TOKENS = 8000

TAXONOMY_IDS = (
    "price", "location", "time_slot", "competitor",
    "hesitation_vou_pensar", "delegated_talk_to_someone",
    "delayed_response_te_falo", "trust_boundary_male", "other",
)


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
    required_tax = set(TAXONOMY_IDS)
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


# ---------------- M2-S3-T1: LLM expansion ----------------


class DaySpaPitchStep(BaseModel):
    order: int
    name: str
    phrase: str


class DaySpaPitch(BaseModel):
    intro: str
    steps: list[DaySpaPitchStep] = Field(min_length=3)
    closing: str


class ObjectionReply(BaseModel):
    objection_id: str
    reply_template: str
    rationale: str


class Inconsistency(BaseModel):
    location: str
    description: str


class ScriptExtensions(BaseModel):
    day_spa_pitch: DaySpaPitch
    objection_replies: list[ObjectionReply]
    inconsistencies: list[Inconsistency]


def _read_prompt(ctx: Context) -> str:
    p = ctx.prompts_dir / EXPAND_PROMPT_RELPATH
    if not p.exists():
        raise FileNotFoundError(f"missing prompt file: {p}")
    return p.read_text(encoding="utf-8")


def _build_user_msg(script_md: str, script_yaml_text: str) -> str:
    return (
        "## script-comercial.md\n\n```markdown\n"
        + script_md
        + "\n```\n\n## script.yaml\n\n```yaml\n"
        + script_yaml_text
        + "\n```\n\nProduza o objeto de extensões conforme instruído."
    )


def _validate_extensions(ext: ScriptExtensions) -> None:
    seen = {r.objection_id for r in ext.objection_replies}
    missing = set(TAXONOMY_IDS) - seen
    unknown = seen - set(TAXONOMY_IDS)
    if missing:
        raise ValueError(f"objection_replies missing ids: {sorted(missing)}")
    if unknown:
        raise ValueError(f"objection_replies has unknown ids: {sorted(unknown)}")
    if len(ext.objection_replies) != 9:
        raise ValueError(
            f"expected 9 objection_replies, got {len(ext.objection_replies)}"
        )


def expand_script(ctx: Context) -> ScriptExtensions:
    """Call LLM to produce script extensions. Raises on invalid output."""
    if ctx.client is None:
        raise RuntimeError("expand_script requires ctx.client (ClaudeClient)")

    system = _read_prompt(ctx)
    script_md_path = ctx.script_path
    if not script_md_path.exists():
        raise FileNotFoundError(f"missing {script_md_path}")
    script_md = script_md_path.read_text(encoding="utf-8")
    script_yaml_text = _script_path(ctx).read_text(encoding="utf-8")

    user_msg = _build_user_msg(script_md, script_yaml_text)

    result = ctx.client.complete(
        model=EXPAND_MODEL,
        messages=[{"role": "user", "content": user_msg}],
        system=system,
        max_tokens=EXPAND_MAX_TOKENS,
        response_format=ScriptExtensions,
    )
    if not isinstance(result, ScriptExtensions):
        raise TypeError(f"expected ScriptExtensions, got {type(result).__name__}")

    _validate_extensions(result)
    return result


def write_extensions(ctx: Context, ext: ScriptExtensions) -> Path:
    path = _extensions_path(ctx)
    data = ext.model_dump(mode="json")
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def run(ctx: Context) -> dict:
    t0 = time.time()
    path = _script_path(ctx)
    if not path.exists():
        raise FileNotFoundError(
            f"missing hand-curated script: {path}. "
            "Commit data/script.yaml per TECH_PLAN.md §M1-T4."
        )

    # Validate the hand-curated base (raises on schema error).
    doc = load_script(path)
    log.info(
        "stage3: %d steps, %d objection types, script.yaml OK",
        len(doc["steps"]),
        len(doc["objection_taxonomy"]),
    )

    ext_path = _extensions_path(ctx)
    skipped = ext_path.exists() and not ctx.force
    if skipped:
        log.info("stage3: script_extensions.yaml exists, skipping LLM expansion (use --force)")
    else:
        log.info("stage3: expanding script via LLM (%s)", EXPAND_MODEL)
        ext = expand_script(ctx)
        written = write_extensions(ctx, ext)
        log.info(
            "stage3: wrote %s (%d objection_replies, %d pitch steps, %d inconsistencies)",
            written,
            len(ext.objection_replies),
            len(ext.day_spa_pitch.steps),
            len(ext.inconsistencies),
        )

    outputs = [path]
    if ext_path.exists():
        outputs.append(ext_path)

    api_cost = 0.0
    if ctx.client is not None:
        api_cost = float(ctx.client.get_usage_report().get("api", {}).get("cost_usd", 0.0))

    return {
        "stage": 3,
        "outputs": outputs,
        "llm_usd_max": 0.0,
        "llm_usd_api": api_cost,
        "elapsed_s": time.time() - t0,
    }
