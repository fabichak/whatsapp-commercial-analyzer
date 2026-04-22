"""Stage 4: labeling.

M2-S4-T1: spa-template step labeling via Haiku. One LLM call per
unique SpaTemplate; labels propagate to every instance via
`data/spa_message_template_map.json`.

M2-S4-T2: customer-message labeling via Haiku. Cross-chat batches of
30 customer messages per call; each item carries its own chat_id and
step_context_hint (last 3 spa msgs from same chat).

See TECH_PLAN.md §M2-S4-T1, §M2-S4-T2.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
SPA_BATCH_PROMPT_RELPATH = "stage4_spa_template_batch.md"
CUSTOMER_PROMPT_RELPATH = "stage4_customer_batch.md"

TEMPLATES_RELPATH = "spa_templates.json"
TEMPLATE_MAP_RELPATH = "spa_message_template_map.json"
SPA_LABELS_RELPATH = "spa_template_labels.json"
CUSTOMER_LABELS_RELPATH = "customer_labels.json"
LABELED_RELPATH = "labeled_messages.jsonl"
SCRIPT_YAML_RELPATH = "script.yaml"

VALID_STEP_IDS = {"1", "2", "3", "3.5", "5", "6", "7", "fup1", "fup2"}
VALID_STEP_CONTEXTS = {"on_script", "off_script", "transition", "unknown"}
VALID_OBJECTION_IDS = {
    "price", "location", "time_slot", "competitor",
    "hesitation_vou_pensar", "delegated_talk_to_someone",
    "delayed_response_te_falo", "trust_boundary_male", "other",
}
VALID_SENTIMENTS = {"pos", "neu", "neg"}

CUSTOMER_BATCH_SIZE = 30
CUSTOMER_BATCH_MAX_TOKENS = 2048
CUSTOMER_HINT_WINDOW = 3
CUSTOMER_HINT_CHARS = 140


class SpaTemplateLabel(BaseModel):
    step_id: str
    matches_script: bool
    deviation_note: Optional[str] = None


class SpaTemplateBatchItem(BaseModel):
    template_id: int
    step_id: str
    matches_script: bool
    deviation_note: Optional[str] = None


class SpaTemplateBatchResult(BaseModel):
    items: list[SpaTemplateBatchItem]


class CustomerLabel(BaseModel):
    msg_id: int
    step_context: Literal["on_script", "off_script", "transition", "unknown"]
    intent: Optional[str] = None
    objection_type: Optional[str] = None
    sentiment: Optional[Literal["pos", "neu", "neg"]] = None


class CustomerBatchResult(BaseModel):
    items: list[CustomerLabel]


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


def _label_template_batch(
    ctx: Context, system: str, steps_summary: str, batch: list[SpaTemplate]
) -> dict[int, SpaTemplateLabel]:
    payload = [
        {"template_id": t.template_id, "text": t.canonical_text}
        for t in batch
    ]
    user_msg = (
        "SCRIPT_STEPS:\n" + steps_summary
        + "\n\nTEMPLATES (JSON):\n" + json.dumps(payload, ensure_ascii=False)
        + "\n\nClassifique cada item conforme instruído."
    )
    result = ctx.client.complete(
        model=LABEL_MODEL,
        messages=[{"role": "user", "content": user_msg}],
        system=system,
        max_tokens=min(8192, 256 * max(1, len(batch))),
        response_format=SpaTemplateBatchResult,
    )
    if not isinstance(result, SpaTemplateBatchResult):
        raise TypeError(f"expected SpaTemplateBatchResult, got {type(result).__name__}")
    want = {t.template_id for t in batch}
    out: dict[int, SpaTemplateLabel] = {}
    for it in result.items:
        if it.template_id not in want:
            log.warning("stage4: batch returned unknown template_id %d", it.template_id)
            continue
        if it.step_id not in VALID_STEP_IDS:
            raise ValueError(f"invalid step_id {it.step_id!r}")
        out[it.template_id] = SpaTemplateLabel(
            step_id=it.step_id,
            matches_script=it.matches_script,
            deviation_note=it.deviation_note,
        )
    missing = want - set(out)
    if missing:
        log.warning("stage4: batch missing %d labels: %s", len(missing), sorted(missing)[:5])
    return out


def label_spa_templates(ctx: Context) -> dict[int, SpaTemplateLabel]:
    """Haiku template labeling. Writes data/spa_template_labels.json.

    Env:
    - STAGE4_TEMPLATE_BATCH_SIZE (default 1): templates per LLM call.
    - STAGE4_CONCURRENCY (default 8): parallel workers.
    """
    if ctx.client is None:
        raise RuntimeError("label_spa_templates requires ctx.client")

    templates = _load_templates(ctx)
    steps_summary = _steps_summary(ctx)
    batch_size = max(1, int(os.environ.get("STAGE4_TEMPLATE_BATCH_SIZE", "1")))
    workers = max(1, int(os.environ.get("STAGE4_CONCURRENCY", "8")))
    log.info(
        "stage4: templates=%d, batch_size=%d, workers=%d",
        len(templates), batch_size, workers,
    )

    labels: dict[int, SpaTemplateLabel] = {}

    if batch_size == 1:
        system = _read_prompt(ctx)
        if workers == 1:
            for tmpl in tqdm(templates, desc="stage4: spa templates", disable=None):
                labels[tmpl.template_id] = _label_one_template(
                    ctx, system, steps_summary, tmpl.canonical_text
                )
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(_label_one_template, ctx, system, steps_summary, t.canonical_text): t.template_id
                    for t in templates
                }
                for fut in tqdm(as_completed(futs), total=len(futs), desc="stage4: spa templates", disable=None):
                    labels[futs[fut]] = fut.result()
    else:
        batch_prompt_path = ctx.prompts_dir / SPA_BATCH_PROMPT_RELPATH
        if not batch_prompt_path.exists():
            raise FileNotFoundError(f"missing batch prompt: {batch_prompt_path}")
        system = batch_prompt_path.read_text(encoding="utf-8")
        batches = [templates[i:i + batch_size] for i in range(0, len(templates), batch_size)]
        log.info("stage4: spa templates batched — %d batches × up to %d", len(batches), batch_size)
        if workers == 1:
            for b in tqdm(batches, desc="stage4: spa template batches", disable=None):
                labels.update(_label_template_batch(ctx, system, steps_summary, b))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_label_template_batch, ctx, system, steps_summary, b) for b in batches]
                for fut in tqdm(as_completed(futs), total=len(futs), desc="stage4: spa template batches", disable=None):
                    labels.update(fut.result())

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


# ---------------- customer batching (M2-S4-T2) ----------------


def _read_customer_prompt(ctx: Context) -> str:
    p = ctx.prompts_dir / CUSTOMER_PROMPT_RELPATH
    if not p.exists():
        raise FileNotFoundError(f"missing prompt: {p}")
    return p.read_text(encoding="utf-8")


def _objection_triggers_block(ctx: Context) -> str:
    """PT-BR list of objection ids + example triggers, pulled from script.yaml."""
    path = ctx.data_dir / SCRIPT_YAML_RELPATH
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    tax = doc.get("objection_taxonomy", []) or []
    lines: list[str] = []
    for obj in tax:
        oid = obj.get("id")
        if oid not in VALID_OBJECTION_IDS:
            continue
        trig = obj.get("triggers", []) or []
        sample = ", ".join(str(t) for t in trig[:5])
        lines.append(f'- id="{oid}" ({obj.get("name_pt","")}) — gatilhos: {sample}')
    return "\n".join(lines) if lines else "(taxonomia não disponível)"


def _collect_customer_items(convos_path: Path) -> list[dict]:
    """Flatten all customer (from_me=False, non-empty text) msgs into
    envelope dicts carrying chat_id + rolling last-3-spa hint.
    """
    items: list[dict] = []
    with convos_path.open("r", encoding="utf-8") as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            convo = Conversation.model_validate_json(line)
            hint: list[str] = []
            for m in convo.messages:
                if m.from_me:
                    txt = m.text.strip()
                    if txt:
                        snip = txt[:CUSTOMER_HINT_CHARS]
                        hint.append(snip)
                        if len(hint) > CUSTOMER_HINT_WINDOW:
                            hint = hint[-CUSTOMER_HINT_WINDOW:]
                    continue
                text = m.text.strip()
                if not text:
                    continue
                items.append({
                    "msg_id": m.msg_id,
                    "chat_id": convo.chat_id,
                    "text": text,
                    "step_context_hint": list(hint),
                })
    return items


def _pack_customer_batches(
    items: list[dict], size: int = CUSTOMER_BATCH_SIZE
) -> list[list[dict]]:
    """Greedy cross-chat pack: fill every batch to `size`; last batch may be smaller."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _build_customer_user_msg(
    steps_summary: str, objections_block: str, batch: list[dict]
) -> str:
    return (
        "SCRIPT_STEPS:\n"
        + steps_summary
        + "\n\nOBJECTION_TYPES:\n"
        + objections_block
        + "\n\nBATCH (JSON):\n"
        + json.dumps(batch, ensure_ascii=False)
        + "\n\nRotule cada item do BATCH conforme instruído. Retorne "
        "um objeto com a chave `items` contendo um objeto por entrada, "
        "na mesma ordem, com os mesmos `msg_id`."
    )


def _validate_customer_label(lb: CustomerLabel) -> None:
    if lb.step_context not in VALID_STEP_CONTEXTS:
        raise ValueError(f"invalid step_context {lb.step_context!r}")
    if lb.objection_type is not None and lb.objection_type not in VALID_OBJECTION_IDS:
        raise ValueError(f"invalid objection_type {lb.objection_type!r}")
    if lb.sentiment is not None and lb.sentiment not in VALID_SENTIMENTS:
        raise ValueError(f"invalid sentiment {lb.sentiment!r}")


def label_customer_messages(ctx: Context) -> dict[int, CustomerLabel]:
    """Cross-chat batches of 30 customer messages per Haiku call.
    Writes data/customer_labels.json keyed by msg_id (string).
    """
    if ctx.client is None:
        raise RuntimeError("label_customer_messages requires ctx.client")

    convos_path = ctx.data_dir / "conversations.jsonl"
    if not convos_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {convos_path}")

    items = _collect_customer_items(convos_path)
    if not items:
        log.info("stage4: no customer messages to label")
        out = ctx.data_dir / CUSTOMER_LABELS_RELPATH
        out.write_text("{}", encoding="utf-8")
        return {}

    system = _read_customer_prompt(ctx)
    steps_summary = _steps_summary(ctx)
    objections_block = _objection_triggers_block(ctx)
    batches = _pack_customer_batches(items, CUSTOMER_BATCH_SIZE)
    workers_info = os.environ.get("STAGE4_CONCURRENCY", "8")
    log.info(
        "stage4: customer msgs=%d, batches=%d (size=%d), workers=%s",
        len(items), len(batches), CUSTOMER_BATCH_SIZE, workers_info,
    )

    def _run_batch(batch: list[dict]) -> tuple[list[dict], CustomerBatchResult]:
        user_msg = _build_customer_user_msg(steps_summary, objections_block, batch)
        result = ctx.client.complete(
            model=LABEL_MODEL,
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            max_tokens=CUSTOMER_BATCH_MAX_TOKENS,
            response_format=CustomerBatchResult,
        )
        if not isinstance(result, CustomerBatchResult):
            raise TypeError(
                f"expected CustomerBatchResult, got {type(result).__name__}"
            )
        return batch, result

    labels: dict[int, CustomerLabel] = {}
    workers = max(1, int(os.environ.get("STAGE4_CONCURRENCY", "8")))

    def _absorb(batch: list[dict], result: CustomerBatchResult) -> None:
        expected_ids = {it["msg_id"] for it in batch}
        for lb in result.items:
            _validate_customer_label(lb)
            if lb.msg_id not in expected_ids:
                log.warning("stage4: customer batch returned unknown msg_id %d", lb.msg_id)
                continue
            labels[lb.msg_id] = lb
        missing = expected_ids - {lb.msg_id for lb in result.items}
        if missing:
            log.warning("stage4: customer batch missing %d labels: %s", len(missing), sorted(missing)[:5])

    if workers == 1:
        for batch in tqdm(batches, desc="stage4: customer batches", disable=None):
            _absorb(*_run_batch(batch))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_run_batch, b) for b in batches]
            for fut in tqdm(as_completed(futs), total=len(futs), desc="stage4: customer batches", disable=None):
                _absorb(*fut.result())

    out = ctx.data_dir / CUSTOMER_LABELS_RELPATH
    out.write_text(
        json.dumps(
            {str(mid): lb.model_dump() for mid, lb in labels.items()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info(
        "stage4: wrote %d customer labels (%d batches) → %s",
        len(labels), len(batches), out,
    )
    return labels


def _load_customer_labels(ctx: Context) -> dict[int, CustomerLabel]:
    p = ctx.data_dir / CUSTOMER_LABELS_RELPATH
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {int(k): CustomerLabel.model_validate(v) for k, v in raw.items()}


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
    log.info(
        "stage4: STAGE4_CONCURRENCY=%s, STAGE4_TEMPLATE_BATCH_SIZE=%s",
        os.environ.get("STAGE4_CONCURRENCY", "8"),
        os.environ.get("STAGE4_TEMPLATE_BATCH_SIZE", "1"),
    )
    convos_path = ctx.data_dir / "conversations.jsonl"
    if not convos_path.exists():
        raise FileNotFoundError(f"missing stage 1 output: {convos_path}")

    spa_labels_path = ctx.data_dir / SPA_LABELS_RELPATH
    if not spa_labels_path.exists() or ctx.force:
        log.info("stage4: --- spa template labeling START (model=%s) ---", LABEL_MODEL)
        t_spa = time.time()
        label_spa_templates(ctx)
        log.info("stage4: --- spa template labeling END (%.1fs) ---", time.time() - t_spa)
    else:
        log.info("stage4: spa_template_labels.json exists, skipping (use --force)")

    customer_labels_path = ctx.data_dir / CUSTOMER_LABELS_RELPATH
    if not customer_labels_path.exists() or ctx.force:
        log.info("stage4: --- customer message labeling START (model=%s) ---", LABEL_MODEL)
        t_cust = time.time()
        label_customer_messages(ctx)
        log.info("stage4: --- customer message labeling END (%.1fs) ---", time.time() - t_cust)
    else:
        log.info("stage4: customer_labels.json exists, skipping (use --force)")

    log.info("stage4: --- merging labels into labeled_messages.jsonl ---")

    template_map = _load_template_map(ctx)
    spa_labels = _load_spa_labels(ctx)
    customer_labels = _load_customer_labels(ctx)

    out_path = ctx.data_dir / LABELED_RELPATH
    ctx.data_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    n_spa_labeled = 0
    n_cust_labeled = 0
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
                intent: Optional[str] = None
                objection_type: Optional[str] = None
                sentiment: Optional[Literal["pos", "neu", "neg"]] = None
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
                else:
                    clab = customer_labels.get(m.msg_id)
                    if clab is not None:
                        step_context = clab.step_context
                        intent = clab.intent
                        objection_type = clab.objection_type
                        sentiment = clab.sentiment
                        n_cust_labeled += 1

                lm = LabeledMessage(
                    msg_id=m.msg_id,
                    chat_id=convo.chat_id,
                    from_me=m.from_me,
                    step_id=step_id,
                    step_context=step_context,
                    intent=intent,
                    objection_type=objection_type,
                    sentiment=sentiment,
                    matches_script=matches_script,
                    deviation_note=deviation_note,
                )
                fout.write(lm.model_dump_json() + "\n")
                n += 1

    log.info(
        "stage4: %d labeled messages (spa=%d, cust=%d) → %s",
        n, n_spa_labeled, n_cust_labeled, out_path,
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
