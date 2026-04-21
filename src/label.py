"""Stage 4: labeling.

M2-S4-T1 (this module): spa-template step labeling via Haiku. One LLM
call per unique SpaTemplate; labels propagate to every instance via
`data/spa_message_template_map.json`.

Customer-side labels (M2-S4-T2) stay stubbed until that task lands.

See TECH_PLAN.md §M2-S4-T1.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel
from tqdm import tqdm

from src.context import Context
from src.schemas import Conversation, LabeledMessage, SpaTemplate

log = logging.getLogger(__name__)

LABEL_MODEL = "claude-haiku-4-5"
LABEL_MAX_TOKENS = 256
SPA_PROMPT_RELPATH = "stage4_spa_template.md"

TEMPLATES_RELPATH = "spa_templates.json"
TEMPLATE_MAP_RELPATH = "spa_message_template_map.json"
SPA_LABELS_RELPATH = "spa_template_labels.json"
LABELED_RELPATH = "labeled_messages.jsonl"
SCRIPT_YAML_RELPATH = "script.yaml"

VALID_STEP_IDS = {"1", "2", "3", "3.5", "5", "6", "7", "fup1", "fup2"}


class SpaTemplateLabel(BaseModel):
    step_id: str
    matches_script: bool
    deviation_note: Optional[str] = None


# ---------------- helpers ----------------


def _read_prompt(ctx: Context) -> str:
    p = ctx.prompts_dir / SPA_PROMPT_RELPATH
    if not p.exists():
        raise FileNotFoundError(f"missing prompt: {p}")
    return p.read_text(encoding="utf-8")


def _load_templates(ctx: Context) -> list[SpaTemplate]:
    p = ctx.data_dir / TEMPLATES_RELPATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [SpaTemplate.model_validate(t) for t in raw]


def _load_template_map(ctx: Context) -> dict[int, int]:
    p = ctx.data_dir / TEMPLATE_MAP_RELPATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {int(k): int(v) for k, v in raw.items()}


def _steps_summary(ctx: Context) -> str:
    """Compact PT-BR summary of the 9 script steps for the prompt."""
    path = ctx.data_dir / SCRIPT_YAML_RELPATH
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    lines: list[str] = []
    for s in doc.get("steps", []):
        sid = s["id"]
        name = s["name"]
        sample = (s.get("canonical_texts") or [""])[0]
        # truncate long canonical text so prompt stays tight
        sample = sample[:220] + ("…" if len(sample) > 220 else "")
        lines.append(f'- id="{sid}" — {name}\n    exemplo: "{sample}"')
    return "\n".join(lines)


def _build_user_msg(steps_summary: str, template_text: str) -> str:
    return (
        "SCRIPT_STEPS:\n"
        + steps_summary
        + "\n\nTEMPLATE_TEXT:\n"
        + template_text
        + "\n\nClassifique o TEMPLATE_TEXT conforme instruído."
    )


def _label_one_template(
    ctx: Context, system: str, steps_summary: str, text: str
) -> SpaTemplateLabel:
    user_msg = _build_user_msg(steps_summary, text)
    result = ctx.client.complete(
        model=LABEL_MODEL,
        messages=[{"role": "user", "content": user_msg}],
        system=system,
        max_tokens=LABEL_MAX_TOKENS,
        response_format=SpaTemplateLabel,
    )
    if not isinstance(result, SpaTemplateLabel):
        raise TypeError(f"expected SpaTemplateLabel, got {type(result).__name__}")
    if result.step_id not in VALID_STEP_IDS:
        raise ValueError(
            f"invalid step_id {result.step_id!r}; expected one of {sorted(VALID_STEP_IDS)}"
        )
    return result


# ---------------- public API ----------------


def label_spa_templates(ctx: Context) -> dict[int, SpaTemplateLabel]:
    """One Haiku call per template. Writes data/spa_template_labels.json."""
    if ctx.client is None:
        raise RuntimeError("label_spa_templates requires ctx.client")

    templates = _load_templates(ctx)
    system = _read_prompt(ctx)
    steps_summary = _steps_summary(ctx)

    labels: dict[int, SpaTemplateLabel] = {}
    for tmpl in tqdm(templates, desc="stage4: spa templates", disable=None):
        label = _label_one_template(ctx, system, steps_summary, tmpl.canonical_text)
        labels[tmpl.template_id] = label

    out = ctx.data_dir / SPA_LABELS_RELPATH
    out.write_text(
        json.dumps(
            {str(tid): lb.model_dump() for tid, lb in labels.items()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("stage4: wrote %d template labels → %s", len(labels), out)
    return labels


def _load_spa_labels(ctx: Context) -> dict[int, SpaTemplateLabel]:
    p = ctx.data_dir / SPA_LABELS_RELPATH
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {int(k): SpaTemplateLabel.model_validate(v) for k, v in raw.items()}


def _derive_step_context(matches_script: Optional[bool]) -> Literal["on_script", "off_script", "unknown"]:
    if matches_script is True:
        return "on_script"
    if matches_script is False:
        return "off_script"
    return "unknown"


def run(ctx: Context) -> dict:
    t0 = time.time()
    convos_path = ctx.data_dir / "conversations.jsonl"
    if not convos_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {convos_path}")

    spa_labels_path = ctx.data_dir / SPA_LABELS_RELPATH
    if not spa_labels_path.exists() or ctx.force:
        label_spa_templates(ctx)
    else:
        log.info("stage4: spa_template_labels.json exists, skipping (use --force)")

    template_map = _load_template_map(ctx)
    spa_labels = _load_spa_labels(ctx)

    out_path = ctx.data_dir / LABELED_RELPATH
    ctx.data_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    n_spa_labeled = 0
    with convos_path.open("r", encoding="utf-8") as fin, out_path.open(
        "w", encoding="utf-8"
    ) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            convo = Conversation.model_validate_json(line)
            for m in convo.messages:
                step_id: Optional[str] = None
                matches_script: Optional[bool] = None
                deviation_note: Optional[str] = None
                step_context: Literal["on_script", "off_script", "transition", "unknown"] = "unknown"

                if m.from_me:
                    tid = template_map.get(m.msg_id)
                    if tid is not None and tid in spa_labels:
                        lab = spa_labels[tid]
                        step_id = lab.step_id
                        matches_script = lab.matches_script
                        deviation_note = lab.deviation_note
                        step_context = _derive_step_context(matches_script)
                        n_spa_labeled += 1

                lm = LabeledMessage(
                    msg_id=m.msg_id,
                    chat_id=convo.chat_id,
                    from_me=m.from_me,
                    step_id=step_id,
                    step_context=step_context,
                    intent=None,
                    objection_type=None,
                    sentiment=None,
                    matches_script=matches_script,
                    deviation_note=deviation_note,
                )
                fout.write(lm.model_dump_json() + "\n")
                n += 1

    log.info(
        "stage4: %d labeled messages (%d spa labeled) → %s",
        n, n_spa_labeled, out_path,
    )

    api_cost = 0.0
    if ctx.client is not None:
        api_cost = float(ctx.client.get_usage_report().get("api", {}).get("cost_usd", 0.0))

    return {
        "stage": 4,
        "outputs": [out_path, spa_labels_path],
        "llm_usd_max": 0.0,
        "llm_usd_api": api_cost,
        "elapsed_s": time.time() - t0,
    }
