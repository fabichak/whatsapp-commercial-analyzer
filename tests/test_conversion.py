"""Tests for Stage 6 — M2-S6-T1 truncation utility."""

from __future__ import annotations

from src.conversion import DEFAULT_MAX_TOKENS, count_tokens, truncate_for_llm
from src.schemas import Conversation, Message


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
