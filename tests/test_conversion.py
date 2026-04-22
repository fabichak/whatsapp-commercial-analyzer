"""Tests for Stage 6 — M2-S6-T1 truncation + M2-S6-T2 conversion detection."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.conversion import (
    DEFAULT_MAX_TOKENS,
    ConversionDetection,
    count_tokens,
    detect_conversions,
    truncate_for_llm,
)
from src.schemas import Conversation, Message

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"


def _mk_convo(n: int, chat_id: int = 1) -> Conversation:
    msgs = [
        Message(
            msg_id=i,
            ts_ms=1_000_000 + i,
            from_me=(i % 2 == 0),
            text=f"mensagem numero {i} com algum conteudo de exemplo",
            text_raw=f"mensagem numero {i} com algum conteudo de exemplo",
        )
        for i in range(n)
    ]
    return Conversation(chat_id=chat_id, phone="+55000", messages=msgs)


def test_truncation_of_long_chat():
    convo = _mk_convo(200)
    out = truncate_for_llm(convo, objection_indices=[80])

    assert count_tokens(out) <= DEFAULT_MAX_TOKENS

    # first window: msgs 0..14
    for i in range(0, 15):
        assert f"[{i}] " in out
    # objection window: 70..90 inclusive
    for i in range(70, 91):
        assert f"[{i}] " in out
    # last window: 185..199
    for i in range(185, 200):
        assert f"[{i}] " in out

    # a message outside every window must be absent
    assert "[50] " not in out
    assert "[150] " not in out

    # elisions present
    assert "mensagens ...]" in out


def test_truncation_short_chat_passthrough():
    convo = _mk_convo(30)
    out = truncate_for_llm(convo, objection_indices=[])

    for i in range(30):
        assert f"[{i}] " in out
    assert "mensagens ...]" not in out


def test_truncation_no_objections():
    convo = _mk_convo(100)
    out = truncate_for_llm(convo, objection_indices=None)

    # first + last windows only
    for i in range(0, 15):
        assert f"[{i}] " in out
    for i in range(85, 100):
        assert f"[{i}] " in out
    # middle absent
    for i in (20, 40, 60, 80):
        assert f"[{i}] " not in out
    # single elision between the two windows
    assert out.count("mensagens ...]") == 1


def test_truncation_empty_chat():
    convo = _mk_convo(0)
    assert truncate_for_llm(convo, objection_indices=[]) == ""


def test_truncation_windows_merge():
    # objection near the start overlaps the first window
    convo = _mk_convo(100)
    out = truncate_for_llm(convo, objection_indices=[10])
    # msgs 0..20 all present, no elision at the very top
    assert not out.startswith("[...")
    for i in range(0, 21):
        assert f"[{i}] " in out


# ---------------- M2-S6-T2 · conversion detection ----------------


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
    shutil.copyfile(REPO_ROOT / "input" / "script.yaml", input_dir / "script.yaml")
    return MiniCtx(
        db_path=tmp_path / "msgstore.db",
        script_path=REPO_ROOT / "input" / "script-comercial.md",
        data_dir=data_dir,
        output_dir=tmp_path / "out",
        prompts_dir=PROMPTS_DIR,
        input_dir=input_dir,
        script_yaml_path=input_dir / "script.yaml",
        client=client,
    )


def _write_convos(data_dir: Path, convos: list[Conversation]) -> None:
    with (data_dir / "conversations.jsonl").open("w", encoding="utf-8") as f:
        for c in convos:
            f.write(c.model_dump_json() + "\n")


def _write_labeled(data_dir: Path, records: list[dict]) -> None:
    with (data_dir / "labeled_messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _mk_simple_convo(chat_id: int, phone: str = "+55011999") -> Conversation:
    msgs = [
        Message(msg_id=100 + i, ts_ms=1_000 + i, from_me=(i % 2 == 0),
                text=f"t{i}", text_raw=f"t{i}")
        for i in range(10)
    ]
    return Conversation(chat_id=chat_id, phone=phone, messages=msgs)


def _ok_detection(score=3, outcome="booked", obj_mid=103, obj_type="price",
                  res_mid=104, excerpt="virada"):
    return ConversionDetection(
        conversion_score=score,
        conversion_evidence="evid",
        first_objection_msg_id=obj_mid,
        first_objection_type=obj_type,
        resolution_msg_id=res_mid,
        winning_reply_excerpt=excerpt,
        final_outcome=outcome,
    )


def test_conversion_score_parsed(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    convo = _mk_simple_convo(1)
    _write_convos(ctx.data_dir, [convo])
    _write_labeled(ctx.data_dir, [])  # no objections
    client = FakeClient([_ok_detection(score=3)])
    ctx.client = client

    out = detect_conversions(ctx)
    assert len(out) == 1
    cc = out[0]
    assert cc.conversion_score == 3
    assert cc.chat_id == 1
    assert cc.phone == "+55011999"
    assert cc.final_outcome == "booked"
    # msg_id → index resolution
    assert cc.first_objection_idx == 3  # msg_id 103 at index 3
    assert cc.resolution_idx == 4
    assert client.calls[0]["model"] == "claude-haiku-4-5"
    assert client.calls[0]["response_format"] is ConversionDetection


@pytest.mark.parametrize("otype", [
    "price", "location", "time_slot", "competitor",
    "hesitation_vou_pensar", "delegated_talk_to_someone",
    "delayed_response_te_falo", "trust_boundary_male", "other",
])
def test_all_objection_types_covered(tmp_path, otype):
    ctx = _prep_ctx(tmp_path, None)
    convo = _mk_simple_convo(7)
    _write_convos(ctx.data_dir, [convo])
    _write_labeled(ctx.data_dir, [])
    client = FakeClient([_ok_detection(obj_type=otype, score=0, outcome="lost",
                                       res_mid=None, excerpt=None)])
    ctx.client = client
    out = detect_conversions(ctx)
    assert out[0].first_objection_type == otype


def test_phone_number_attached(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    convo = _mk_simple_convo(42, phone="+5511987654321")
    _write_convos(ctx.data_dir, [convo])
    _write_labeled(ctx.data_dir, [])
    client = FakeClient([_ok_detection()])
    ctx.client = client
    out = detect_conversions(ctx)
    assert out[0].phone == "+5511987654321"


def test_resume_skips_existing(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    c1 = _mk_simple_convo(1)
    c2 = _mk_simple_convo(2)
    _write_convos(ctx.data_dir, [c1, c2])
    _write_labeled(ctx.data_dir, [])
    # pre-seed one conversion (non-stub) on disk
    (ctx.data_dir / "conversions.jsonl").write_text(
        json.dumps({
            "chat_id": 1, "phone": "+55011999", "conversion_score": 2,
            "conversion_evidence": "pre", "first_objection_idx": None,
            "first_objection_type": None, "resolution_idx": None,
            "winning_reply_excerpt": None, "final_outcome": "booked",
        }) + "\n",
        encoding="utf-8",
    )
    client = FakeClient([_ok_detection()])  # only 1 call — for chat 2
    ctx.client = client
    detect_conversions(ctx)
    assert len(client.calls) == 1


def test_objection_indices_fed_from_labeled(tmp_path):
    """Labeled customer-msg objections drive the truncation window."""
    ctx = _prep_ctx(tmp_path, None)
    # Build a 60-msg convo, mark msg_id=130 (idx 30) as price objection.
    msgs = [
        Message(msg_id=100 + i, ts_ms=1000 + i, from_me=(i % 2 == 0),
                text=f"t{i}", text_raw=f"t{i}")
        for i in range(60)
    ]
    convo = Conversation(chat_id=9, phone="+55", messages=msgs)
    _write_convos(ctx.data_dir, [convo])
    _write_labeled(ctx.data_dir, [{
        "msg_id": 130, "chat_id": 9, "from_me": False,
        "step_id": None, "step_context": "off_script",
        "objection_type": "price", "intent": None, "sentiment": "neg",
        "matches_script": None, "deviation_note": None,
    }])
    client = FakeClient([_ok_detection(obj_mid=130, res_mid=131)])
    ctx.client = client
    detect_conversions(ctx)
    user_msg = client.calls[0]["messages"][0]["content"]
    # objection window (±10 around idx 30) must be in the truncation
    assert "[130]" in user_msg
    assert "[125]" in user_msg
    assert "[135]" in user_msg


def test_validation_rejects_bad_score(tmp_path):
    ctx = _prep_ctx(tmp_path, None)
    _write_convos(ctx.data_dir, [_mk_simple_convo(1)])
    _write_labeled(ctx.data_dir, [])
    bad = ConversionDetection.model_construct(
        conversion_score=7,
        conversion_evidence="x",
        first_objection_msg_id=None,
        first_objection_type=None,
        resolution_msg_id=None,
        winning_reply_excerpt=None,
        final_outcome="booked",
    )
    ctx.client = FakeClient([bad])
    with pytest.raises(ValueError, match="conversion_score"):
        detect_conversions(ctx)
