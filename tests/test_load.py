"""Tests for src/load.py (Stage 1). See TECH_PLAN.md §M1-T2."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from src.context import Context
from src.load import run
from tools.build_tiny_db import build as build_tiny


def _ctx(tmp_path: Path, db: Path, **overrides) -> Context:
    kw = dict(
        db_path=db,
        script_path=tmp_path / "script.md",
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "out",
        prompts_dir=tmp_path / "prompts",
        chat_limit=None,
        phones_filter=None,
        phones_hash=None,
        llm_mode="hybrid",
        budget_usd=0.0,
        force=False,
        dry_run=False,
        client=None,
    )
    kw.update(overrides)
    kw["data_dir"].mkdir(parents=True, exist_ok=True)
    return Context(**kw)


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


@pytest.fixture
def tiny_db(tmp_path: Path) -> Path:
    p = tmp_path / "tiny.db"
    build_tiny(p)
    return p


# ---- direct-injection fixture for low-level filter tests ----

def _fresh_db(path: Path) -> sqlite3.Connection:
    from tools.build_tiny_db import MESSAGE_SQL, CHAT_SQL, JID_SQL

    conn = sqlite3.connect(path)
    conn.executescript(MESSAGE_SQL + ";" + CHAT_SQL + ";" + JID_SQL + ";")
    return conn


def test_load_filters_message_type_0(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = _fresh_db(db)
    conn.execute("INSERT INTO jid(_id,user,server,raw_string) VALUES (1,'5511999','s','5511999@s')")
    conn.execute("INSERT INTO chat(_id,jid_row_id,group_type) VALUES (1,1,0)")
    # 20 type-0 + mix of type 1/7 that must be excluded
    for i in range(20):
        conn.execute(
            "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,message_type,text_data,sort_id) "
            "VALUES (?,1,0,?,1,?,0,?,?)",
            (i + 1, f"k{i}", 1000 + i, f"txt {i}", i + 1),
        )
    conn.execute(
        "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,message_type,text_data,sort_id) "
        "VALUES (100,1,0,'k100',1,2000,1,'audio!',100)"
    )
    conn.execute(
        "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,message_type,text_data,sort_id) "
        "VALUES (101,1,0,'k101',1,2001,7,'sys!',101)"
    )
    conn.commit()
    conn.close()

    ctx = _ctx(tmp_path, db)
    run(ctx)
    convos = _read_jsonl(ctx.data_dir / "conversations.jsonl")
    assert len(convos) == 1
    assert len(convos[0]["messages"]) == 20
    texts = [m["text_raw"] for m in convos[0]["messages"]]
    assert "audio!" not in texts and "sys!" not in texts


def test_load_drops_null_text(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = _fresh_db(db)
    conn.execute("INSERT INTO jid(_id,user,server,raw_string) VALUES (1,'5511','s','x')")
    conn.execute("INSERT INTO chat(_id,jid_row_id,group_type) VALUES (1,1,0)")
    for i in range(20):
        conn.execute(
            "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,message_type,text_data,sort_id) "
            "VALUES (?,1,0,?,1,?,0,?,?)",
            (i + 1, f"k{i}", i, f"t{i}", i + 1),
        )
    conn.execute(
        "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,message_type,text_data,sort_id) "
        "VALUES (99,1,0,'kn',1,999,0,NULL,99)"
    )
    conn.commit()
    conn.close()

    ctx = _ctx(tmp_path, db)
    run(ctx)
    convos = _read_jsonl(ctx.data_dir / "conversations.jsonl")
    ids = [m["msg_id"] for m in convos[0]["messages"]]
    assert 99 not in ids
    assert len(convos[0]["messages"]) == 20


def test_load_orders_by_timestamp(tmp_path: Path, tiny_db: Path):
    ctx = _ctx(tmp_path, tiny_db)
    run(ctx)
    for c in _read_jsonl(ctx.data_dir / "conversations.jsonl"):
        ts = [m["ts_ms"] for m in c["messages"]]
        assert ts == sorted(ts)


@pytest.mark.parametrize("n,expect_long", [(19, False), (20, True)])
def test_load_threshold_min_messages(tmp_path: Path, n: int, expect_long: bool):
    db = tmp_path / "t.db"
    conn = _fresh_db(db)
    conn.execute("INSERT INTO jid(_id,user,server,raw_string) VALUES (1,'5511','s','x')")
    conn.execute("INSERT INTO chat(_id,jid_row_id,group_type) VALUES (1,1,0)")
    for i in range(n):
        conn.execute(
            "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,message_type,text_data,sort_id) "
            "VALUES (?,1,0,?,1,?,0,?,?)",
            (i + 1, f"k{i}", i, f"t{i}", i + 1),
        )
    conn.commit()
    conn.close()

    ctx = _ctx(tmp_path, db)
    run(ctx)
    long_ = _read_jsonl(ctx.data_dir / "conversations.jsonl")
    short = _read_jsonl(ctx.data_dir / "conversations_short.jsonl")
    if expect_long:
        assert len(long_) == 1 and not short
    else:
        assert not long_ and len(short) == 1


def test_load_strips_urls_and_whitespace(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = _fresh_db(db)
    conn.execute("INSERT INTO jid(_id,user,server,raw_string) VALUES (1,'5511','s','x')")
    conn.execute("INSERT INTO chat(_id,jid_row_id,group_type) VALUES (1,1,0)")
    raw = "hi https://x.com\n\n hi"
    for i in range(20):
        text = raw if i == 0 else f"m{i}"
        conn.execute(
            "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,timestamp,message_type,text_data,sort_id) "
            "VALUES (?,1,0,?,1,?,0,?,?)",
            (i + 1, f"k{i}", i, text, i + 1),
        )
    conn.commit()
    conn.close()

    ctx = _ctx(tmp_path, db)
    run(ctx)
    convo = _read_jsonl(ctx.data_dir / "conversations.jsonl")[0]
    m = convo["messages"][0]
    assert m["text_raw"] == raw
    assert "http" not in m["text"]
    assert "\n" not in m["text"]
    assert "  " not in m["text"]
    assert "hi" in m["text"]


def test_load_extracts_phone(tmp_path: Path, tiny_db: Path):
    ctx = _ctx(tmp_path, tiny_db)
    run(ctx)
    phones = {c["phone"] for c in _read_jsonl(ctx.data_dir / "conversations.jsonl")}
    assert "5511000000001" in phones
    assert "5511000000002" in phones


def test_chat_limit(tmp_path: Path, tiny_db: Path):
    # tiny has 2 long chats — limit to 1
    ctx = _ctx(tmp_path, tiny_db, chat_limit=1)
    run(ctx)
    long_ = _read_jsonl(ctx.data_dir / "conversations.jsonl")
    assert len(long_) == 1
    assert long_[0]["chat_id"] == 1  # first by chat_id asc


def test_phones_filter_keeps_only_matched(tmp_path: Path, tiny_db: Path):
    ctx = _ctx(
        tmp_path,
        tiny_db,
        phones_filter=frozenset({"5511000000001", "5511000000003"}),
        phones_hash="abc",
    )
    run(ctx)
    long_ = _read_jsonl(ctx.data_dir / "conversations.jsonl")
    phones = {c["phone"] for c in long_}
    assert phones == {"5511000000001", "5511000000003"}
    assert not (ctx.data_dir / "conversations_short.jsonl").exists()


def test_phones_filter_bypasses_min_messages(tmp_path: Path, tiny_db: Path):
    # chat C has only 5 msgs — must still land in long file under phones mode
    ctx = _ctx(
        tmp_path,
        tiny_db,
        phones_filter=frozenset({"5511000000003"}),
        phones_hash="xyz",
    )
    run(ctx)
    long_ = _read_jsonl(ctx.data_dir / "conversations.jsonl")
    assert len(long_) == 1
    assert long_[0]["phone"] == "5511000000003"
    assert len(long_[0]["messages"]) == 5
    assert not (ctx.data_dir / "conversations_short.jsonl").exists()


def test_phones_filter_warns_on_missing(tmp_path: Path, tiny_db: Path, caplog):
    ctx = _ctx(
        tmp_path,
        tiny_db,
        phones_filter=frozenset({"5511000000001", "5599999999999"}),
        phones_hash="m",
    )
    with caplog.at_level(logging.WARNING, logger="src.load"):
        run(ctx)
    assert any("5599999999999" in r.getMessage() for r in caplog.records)


def test_phones_filter_sql_injection_safe(tmp_path: Path, tiny_db: Path):
    ctx = _ctx(
        tmp_path,
        tiny_db,
        phones_filter=frozenset({"1' OR 1=1 --"}),
        phones_hash="inj",
    )
    run(ctx)  # must not raise
    long_ = _read_jsonl(ctx.data_dir / "conversations.jsonl")
    assert long_ == []
