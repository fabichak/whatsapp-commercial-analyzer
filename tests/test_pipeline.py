"""Tests for scripts/run_pipeline.py — see TECH_PLAN.md §M1-T1."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from scripts import run_pipeline as rp
from src.context import Context
from src.exceptions import BudgetExceeded


def _make_ctx(tmp_path: Path, **overrides) -> Context:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    kwargs = dict(
        db_path=tmp_path / "msgstore.db",
        script_path=tmp_path / "script-comercial.md",
        data_dir=data_dir,
        output_dir=tmp_path / "output",
        prompts_dir=tmp_path / "prompts",
        chat_limit=None,
        phones_filter=None,
        phones_hash=None,
        llm_mode="api",
        budget_usd=1.0,
        force=False,
        restart=False,
        dry_run=False,
        client=None,
    )
    kwargs.update(overrides)
    return Context(**kwargs)


def _install_fake_stage(monkeypatch, stage: int, run_fn):
    mod = types.ModuleType(f"fake_stage_{stage}")
    mod.run = run_fn
    monkeypatch.setitem(sys.modules, f"fake_stage_{stage}", mod)
    monkeypatch.setitem(rp.STAGE_MODULES, stage, f"fake_stage_{stage}")


# -- sentinel & resume --

def test_stage_sentinels_written(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path, chat_limit=5)
    _install_fake_stage(monkeypatch, 1, lambda c: {"outputs": [c.data_dir / "conversations.jsonl"]})
    monkeypatch.setitem(rp.STAGE_PREREQS, 1, [])
    rp.run_pipeline(ctx, [1])
    sp = rp.sentinel_path(ctx, 1)
    assert sp.exists()
    body = json.loads(sp.read_text())
    assert body["chat_limit"] == 5
    assert body["phones_hash"] is None
    assert "ts" in body and "git_sha" in body


def test_skip_completed_stages(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    # Pre-write matching sentinel (must include input_hash).
    rp.sentinel_path(ctx, 1).write_text(
        json.dumps({"chat_limit": None, "phones_hash": None,
                    "input_hash": ctx.input_hash, "llm_mode": "api"})
    )
    calls = []
    _install_fake_stage(monkeypatch, 1, lambda c: calls.append("ran") or {"outputs": []})
    monkeypatch.setitem(rp.STAGE_PREREQS, 1, [])
    rp.run_pipeline(ctx, [1])
    assert calls == []


def test_restart_flag_reruns(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path, restart=True)
    rp.sentinel_path(ctx, 1).write_text(
        json.dumps({"chat_limit": None, "phones_hash": None, "input_hash": ctx.input_hash})
    )
    calls = []
    _install_fake_stage(monkeypatch, 1, lambda c: calls.append("ran") or {"outputs": []})
    monkeypatch.setitem(rp.STAGE_PREREQS, 1, [])
    rp.run_pipeline(ctx, [1])
    assert calls == ["ran"]


def test_sentinel_invalidated_by_phones_hash_change(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path, phones_hash="BBBB")
    rp.sentinel_path(ctx, 1).write_text(
        json.dumps({"chat_limit": None, "phones_hash": "AAAA", "input_hash": ctx.input_hash})
    )
    calls = []
    _install_fake_stage(monkeypatch, 1, lambda c: calls.append("ran") or {"outputs": []})
    monkeypatch.setitem(rp.STAGE_PREREQS, 1, [])
    rp.run_pipeline(ctx, [1])
    assert calls == ["ran"]


def test_sentinel_invalidated_by_chat_limit_change(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path, chat_limit=None)
    rp.sentinel_path(ctx, 1).write_text(
        json.dumps({"chat_limit": 5, "phones_hash": None, "input_hash": ctx.input_hash})
    )
    calls = []
    _install_fake_stage(monkeypatch, 1, lambda c: calls.append("ran") or {"outputs": []})
    monkeypatch.setitem(rp.STAGE_PREREQS, 1, [])
    rp.run_pipeline(ctx, [1])
    assert calls == ["ran"]


def test_sentinel_invalidated_by_input_hash_change(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)
    rp.sentinel_path(ctx, 1).write_text(
        json.dumps({"chat_limit": None, "phones_hash": None, "input_hash": "stale"})
    )
    calls = []
    _install_fake_stage(monkeypatch, 1, lambda c: calls.append("ran") or {"outputs": []})
    monkeypatch.setitem(rp.STAGE_PREREQS, 1, [])
    rp.run_pipeline(ctx, [1])
    assert calls == ["ran"]


# -- prereqs --

def test_missing_prior_output_fails_loudly(monkeypatch, tmp_path, capsys):
    ctx = _make_ctx(tmp_path)
    # Stage 2 requires conversations.jsonl — not present.
    _install_fake_stage(monkeypatch, 2, lambda c: {"outputs": []})
    with pytest.raises(SystemExit) as exc:
        rp.run_pipeline(ctx, [2])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Stage 1" in err
    assert "conversations.jsonl" in err


# -- CLI propagation via Context.from_args --

def test_chat_limit_propagates(tmp_path):
    ctx = Context.from_args(
        ["--chat-limit", "5", "--llm-mode", "api", "--data-dir", str(tmp_path / "d"),
         "--output-dir", str(tmp_path / "o")],
        build_client=False,
    )
    assert ctx.chat_limit == 5


def test_phones_file_loaded(tmp_path):
    pf = tmp_path / "phones.txt"
    pf.write_text("5511962719203\n# comment\n\n5511987654321\n5511912345678\n")
    ctx = Context.from_args(
        ["--phones-file", str(pf), "--llm-mode", "api",
         "--data-dir", str(tmp_path / "d"), "--output-dir", str(tmp_path / "o")],
        build_client=False,
    )
    assert ctx.phones_filter == frozenset(
        {"5511962719203", "5511987654321", "5511912345678"}
    )
    assert ctx.phones_hash and len(ctx.phones_hash) == 16
    # Deterministic
    ctx2 = Context.from_args(
        ["--phones-file", str(pf), "--llm-mode", "api",
         "--data-dir", str(tmp_path / "d"), "--output-dir", str(tmp_path / "o")],
        build_client=False,
    )
    assert ctx.phones_hash == ctx2.phones_hash


def test_chat_limit_phones_file_mutex(tmp_path):
    pf = tmp_path / "phones.txt"
    pf.write_text("5511962719203\n")
    with pytest.raises(SystemExit):
        Context.from_args(
            ["--chat-limit", "5", "--phones-file", str(pf), "--llm-mode", "api",
             "--data-dir", str(tmp_path / "d"), "--output-dir", str(tmp_path / "o")],
            build_client=False,
        )


def test_llm_mode_propagates(tmp_path):
    ctx = Context.from_args(
        ["--llm-mode", "api",
         "--data-dir", str(tmp_path / "d"), "--output-dir", str(tmp_path / "o")],
        build_client=False,
    )
    assert ctx.llm_mode == "api"


# -- budget abort --

def test_budget_abort_stops_pipeline(monkeypatch, tmp_path):
    ctx = _make_ctx(tmp_path)

    def raise_budget(c):
        raise BudgetExceeded("over $1.00")

    stage3_called = []
    _install_fake_stage(monkeypatch, 2, raise_budget)
    _install_fake_stage(monkeypatch, 3, lambda c: stage3_called.append("x") or {"outputs": []})
    monkeypatch.setitem(rp.STAGE_PREREQS, 2, [])
    monkeypatch.setitem(rp.STAGE_PREREQS, 3, [])

    with pytest.raises(BudgetExceeded):
        rp.run_pipeline(ctx, [2, 3])
    assert stage3_called == []
