"""Tests for Stage 4 — M2-S4-T1 spa-template step labeling."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.label import (
    CUSTOMER_BATCH_SIZE,
    CustomerBatchResult,
    CustomerLabel,
    SpaTemplateLabel,
    VALID_STEP_IDS,
    _collect_customer_items,
    _pack_customer_batches,
    label_customer_messages,
    label_spa_templates,
    run as stage4_run,
)
from src.schemas import LabeledMessage

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_YAML = REPO_ROOT / "input" / "script.yaml"
SCRIPT_MD = REPO_ROOT / "input" / "script-comercial.md"
PROMPTS_DIR = REPO_ROOT / "prompts"


class FakeClient:
    """Collects call args; returns queued results one-by-one."""

    def __init__(self, results):
        self.results = list(results)
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


def _write_templates(data_dir: Path, templates: list[dict]) -> None:
    (data_dir / "spa_templates.json").write_text(
        json.dumps(templates, ensure_ascii=False), encoding="utf-8"
    )


def _write_map(data_dir: Path, mapping: dict[int, int]) -> None:
    (data_dir / "spa_message_template_map.json").write_text(
        json.dumps({str(k): v for k, v in mapping.items()}), encoding="utf-8"
    )


def _write_convos(data_dir: Path, convos: list[dict]) -> None:
    with (data_dir / "conversations.jsonl").open("w", encoding="utf-8") as f:
        for c in convos:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def _prep_ctx(tmp_path: Path, client) -> MiniCtx:
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


def _mk_tmpl(tid: int, text: str, count: int, ids: list[int]) -> dict:
    return {
        "template_id": tid,
        "canonical_text": text,
        "instance_count": count,
        "example_msg_ids": ids[:3],
        "first_seen_ts": 1000,
        "last_seen_ts": 2000,
    }


def _mk_msg(mid: int, from_me: bool, text: str = "x") -> dict:
    return {"msg_id": mid, "ts_ms": 1000 + mid, "from_me": from_me, "text": text, "text_raw": text}


def _mk_convo(cid: int, phone: str, msgs: list[dict]) -> dict:
    return {"chat_id": cid, "phone": phone, "messages": msgs}


# ---------------- tests ----------------


def test_spa_template_labeled_once(tmp_path):
    """2 messages share template → 1 complete() call."""
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "Olá bom dia!", 2, [10, 11])])
    _write_map(ctx.data_dir, {10: 0, 11: 0})

    client = FakeClient([SpaTemplateLabel(step_id="1", matches_script=True, deviation_note=None)])
    ctx.client = client

    labels = label_spa_templates(ctx)
    assert len(client.calls) == 1
    assert 0 in labels
    assert labels[0].step_id == "1"


def test_label_uses_haiku_and_structured_output(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "Olá bom dia!", 1, [10])])
    _write_map(ctx.data_dir, {10: 0})
    client = FakeClient([SpaTemplateLabel(step_id="1", matches_script=True)])
    ctx.client = client

    label_spa_templates(ctx)
    call = client.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["response_format"] is SpaTemplateLabel


def test_invalid_step_id_raises(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "x", 1, [10])])
    _write_map(ctx.data_dir, {10: 0})
    client = FakeClient([SpaTemplateLabel(step_id="99", matches_script=True)])
    ctx.client = client
    with pytest.raises(ValueError, match="invalid step_id"):
        label_spa_templates(ctx)


def test_run_propagates_labels_to_all_instances(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    # 2 templates, 3 spa messages (mids 10,11 → tmpl 0; mid 20 → tmpl 1). 1 customer.
    _write_templates(
        ctx.data_dir,
        [
            _mk_tmpl(0, "Bom dia! Seja bem-vinda.", 2, [10, 11]),
            _mk_tmpl(1, "Segue valor: R$420.", 1, [20]),
        ],
    )
    _write_map(ctx.data_dir, {10: 0, 11: 0, 20: 1})
    _write_convos(
        ctx.data_dir,
        [
            _mk_convo(
                100,
                "5511999999999",
                [
                    _mk_msg(10, True, "Bom dia!"),
                    _mk_msg(11, True, "Bom dia!"),
                    _mk_msg(15, False, "oi"),
                    _mk_msg(20, True, "Segue valor: R$420."),
                ],
            )
        ],
    )

    # pre-fill customer cache with the single customer msg so this T1 test
    # isolates spa labeling (incremental resume skips batches whose items are
    # all already labeled).
    (ctx.data_dir / "customer_labels.json").write_text(
        json.dumps({"15": {"msg_id": 15, "step_context": "unknown"}}),
        encoding="utf-8",
    )

    client = FakeClient(
        [
            SpaTemplateLabel(step_id="1", matches_script=True),
            SpaTemplateLabel(step_id="5", matches_script=False, deviation_note="preço sem contexto"),
        ]
    )
    ctx.client = client
    stage4_run(ctx)

    # exactly 2 LLM calls (once per template); customer cache pre-filled.
    assert len(client.calls) == 2

    rows = [
        LabeledMessage.model_validate_json(ln)
        for ln in (ctx.data_dir / "labeled_messages.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(rows) == 4
    by_id = {r.msg_id: r for r in rows}

    # spa msgs 10 & 11 share template 0 → identical labels, on_script.
    assert by_id[10].step_id == "1" and by_id[10].matches_script is True
    assert by_id[10].step_context == "on_script"
    assert by_id[11].step_id == "1" and by_id[11].matches_script is True
    assert by_id[11].step_context == "on_script"

    # spa msg 20 template 1 → off_script with deviation note.
    assert by_id[20].step_id == "5"
    assert by_id[20].matches_script is False
    assert by_id[20].step_context == "off_script"
    assert by_id[20].deviation_note == "preço sem contexto"

    # customer msg 15 stays stubbed (T2 will fill).
    assert by_id[15].step_id is None
    assert by_id[15].matches_script is None
    assert by_id[15].step_context == "unknown"


def test_run_skips_labeling_when_cache_exists(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "Olá", 1, [10])])
    _write_map(ctx.data_dir, {10: 0})
    _write_convos(
        ctx.data_dir,
        [_mk_convo(1, "5511000000000", [_mk_msg(10, True)])],
    )
    # pre-existing spa_template_labels.json → LLM must not be called
    (ctx.data_dir / "spa_template_labels.json").write_text(
        json.dumps({"0": {"step_id": "2", "matches_script": True, "deviation_note": None}}),
        encoding="utf-8",
    )
    client = FakeClient([])  # empty → any call explodes
    ctx.client = client
    stage4_run(ctx)
    assert client.calls == []

    row = LabeledMessage.model_validate_json(
        (ctx.data_dir / "labeled_messages.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert row.step_id == "2"


def test_run_force_reruns_labeling(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "Olá", 1, [10])])
    _write_map(ctx.data_dir, {10: 0})
    _write_convos(
        ctx.data_dir,
        [_mk_convo(1, "5511000000000", [_mk_msg(10, True)])],
    )
    (ctx.data_dir / "spa_template_labels.json").write_text(
        json.dumps({"0": {"step_id": "2", "matches_script": True, "deviation_note": None}}),
        encoding="utf-8",
    )
    client = FakeClient([SpaTemplateLabel(step_id="1", matches_script=True)])
    ctx.client = client
    ctx.force = True
    stage4_run(ctx)
    assert len(client.calls) == 1
    row = LabeledMessage.model_validate_json(
        (ctx.data_dir / "labeled_messages.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert row.step_id == "1"


def test_valid_step_ids_constant():
    assert VALID_STEP_IDS == {"1", "2", "3", "3.5", "5", "6", "7", "fup1", "fup2"}


# ---------------- M2-S4-T2 customer batching ----------------


def test_customer_batching_packs_cross_chat(tmp_path):
    """3 chats of 10/20/5 customer msgs → batches of 30, 5."""
    ctx = _prep_ctx(tmp_path, None)

    def _mk_chat(cid, n):
        msgs = [_mk_msg(cid * 1000 + i, False, f"msg {i}") for i in range(n)]
        return _mk_convo(cid, f"55110000{cid:04d}", msgs)

    _write_convos(ctx.data_dir, [_mk_chat(1, 10), _mk_chat(2, 20), _mk_chat(3, 5)])
    items = _collect_customer_items(ctx.data_dir / "conversations.jsonl")
    assert len(items) == 35
    batches = _pack_customer_batches(items, CUSTOMER_BATCH_SIZE)
    assert [len(b) for b in batches] == [30, 5]
    # cross-chat: batch0 packs chat1(10)+chat2(20); chat3(5) lands in batch1
    assert {it["chat_id"] for it in batches[0]} == {1, 2}
    assert {it["chat_id"] for it in batches[1]} == {3}


def test_batch_envelope_has_chat_id_and_hint(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    convo = _mk_convo(
        7, "5511000000007",
        [
            _mk_msg(1, True, "bom dia, seja bem-vinda"),
            _mk_msg(2, True, "somos um spa day"),
            _mk_msg(3, True, "temos day spa e massagens"),
            _mk_msg(4, False, "quanto custa?"),
        ],
    )
    _write_convos(ctx.data_dir, [convo])
    items = _collect_customer_items(ctx.data_dir / "conversations.jsonl")
    assert len(items) == 1
    it = items[0]
    assert it["chat_id"] == 7
    assert it["msg_id"] == 4
    assert it["text"] == "quanto custa?"
    # last 3 spa msgs, chronological
    assert it["step_context_hint"] == [
        "bom dia, seja bem-vinda",
        "somos um spa day",
        "temos day spa e massagens",
    ]


def test_customer_label_applied_in_run(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "Bom dia", 1, [10])])
    _write_map(ctx.data_dir, {10: 0})
    _write_convos(
        ctx.data_dir,
        [_mk_convo(1, "5511000000001", [
            _mk_msg(10, True, "Bom dia"),
            _mk_msg(11, False, "achei caro"),
        ])],
    )
    client = FakeClient([
        SpaTemplateLabel(step_id="1", matches_script=True),
        CustomerBatchResult(items=[
            CustomerLabel(
                msg_id=11, step_context="off_script",
                intent="reclama do preço", objection_type="price", sentiment="neg",
            ),
        ]),
    ])
    ctx.client = client
    stage4_run(ctx)

    rows = [
        LabeledMessage.model_validate_json(ln)
        for ln in (ctx.data_dir / "labeled_messages.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    by_id = {r.msg_id: r for r in rows}
    cust = by_id[11]
    assert cust.step_context == "off_script"
    assert cust.intent == "reclama do preço"
    assert cust.objection_type == "price"
    assert cust.sentiment == "neg"
    # spa side still works
    assert by_id[10].step_id == "1"
    assert by_id[10].step_context == "on_script"


def test_label_customer_messages_uses_haiku_and_schema(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_convos(
        ctx.data_dir,
        [_mk_convo(1, "5511000000001", [
            _mk_msg(10, True, "oi"),
            _mk_msg(11, False, "quanto custa?"),
        ])],
    )
    client = FakeClient([
        CustomerBatchResult(items=[
            CustomerLabel(msg_id=11, step_context="on_script", intent="pergunta preço"),
        ]),
    ])
    ctx.client = client
    labels = label_customer_messages(ctx)
    assert 11 in labels
    call = client.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["response_format"] is CustomerBatchResult
    # prompt carries batch JSON with chat_id + hint
    user = call["messages"][0]["content"]
    assert '"chat_id": 1' in user
    assert "BATCH" in user and "OBJECTION_TYPES" in user and "SCRIPT_STEPS" in user


def test_invalid_objection_type_raises(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_convos(
        ctx.data_dir,
        [_mk_convo(1, "5511000000001", [_mk_msg(11, False, "x")])],
    )
    client = FakeClient([
        CustomerBatchResult(items=[
            CustomerLabel(msg_id=11, step_context="off_script", objection_type="bogus"),
        ]),
    ])
    ctx.client = client
    with pytest.raises(ValueError, match="invalid objection_type"):
        label_customer_messages(ctx)


def test_customer_label_run_skips_when_cache_exists(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_templates(ctx.data_dir, [_mk_tmpl(0, "Olá", 1, [10])])
    _write_map(ctx.data_dir, {10: 0})
    _write_convos(
        ctx.data_dir,
        [_mk_convo(1, "5511000000001", [
            _mk_msg(10, True, "Olá"),
            _mk_msg(11, False, "oi"),
        ])],
    )
    (ctx.data_dir / "spa_template_labels.json").write_text(
        json.dumps({"0": {"step_id": "1", "matches_script": True, "deviation_note": None}}),
        encoding="utf-8",
    )
    (ctx.data_dir / "customer_labels.json").write_text(
        json.dumps({"11": {
            "msg_id": 11, "step_context": "on_script",
            "intent": "cumprimento", "objection_type": None, "sentiment": "neu",
        }}),
        encoding="utf-8",
    )
    client = FakeClient([])
    ctx.client = client
    stage4_run(ctx)
    assert client.calls == []
    rows = [
        LabeledMessage.model_validate_json(ln)
        for ln in (ctx.data_dir / "labeled_messages.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    by_id = {r.msg_id: r for r in rows}
    assert by_id[11].intent == "cumprimento"
    assert by_id[11].step_context == "on_script"
