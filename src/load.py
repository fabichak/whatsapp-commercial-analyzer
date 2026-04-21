"""Stage 1: load msgstore.db → conversations.jsonl.

See TECH_PLAN.md §M1-T2.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from src.context import Context
from src.schemas import Conversation, Message

log = logging.getLogger(__name__)

MIN_MESSAGES = 20
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")


def _clean_text(t: str) -> str:
    stripped = _URL_RE.sub("", t)
    return _WS_RE.sub(" ", stripped).strip()


def _fetch_rows(
    conn: sqlite3.Connection, phones: frozenset[str] | None
) -> list[sqlite3.Row]:
    base = (
        "SELECT m._id AS msg_id, m.chat_row_id AS chat_row_id, "
        "m.from_me AS from_me, m.timestamp AS ts_ms, m.text_data AS text_data, "
        "c.jid_row_id AS jid_row_id, j.user AS phone, j.raw_string AS raw_jid "
        "FROM message m "
        "JOIN chat c ON m.chat_row_id = c._id "
        "JOIN jid j ON c.jid_row_id = j._id "
        "WHERE m.message_type = 0 AND m.text_data IS NOT NULL AND c.group_type = 0"
    )
    params: tuple[Any, ...] = ()
    if phones is not None:
        placeholders = ",".join("?" for _ in phones)
        base += f" AND j.user IN ({placeholders})"
        params = tuple(sorted(phones))
    base += " ORDER BY c._id ASC, m.timestamp ASC, m._id ASC"
    return list(conn.execute(base, params))


def _build_conversation(chat_row_id: int, rows: list[sqlite3.Row]) -> Conversation:
    phone = rows[0]["phone"] or ""
    msgs: list[Message] = []
    for r in rows:
        raw = r["text_data"] or ""
        msgs.append(
            Message(
                msg_id=int(r["msg_id"]),
                ts_ms=int(r["ts_ms"] or 0),
                from_me=bool(r["from_me"]),
                text=_clean_text(raw),
                text_raw=raw,
            )
        )
    return Conversation(chat_id=chat_row_id, phone=phone, messages=msgs)


def _write_jsonl(path: Path, convos: list[Conversation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in convos:
            f.write(c.model_dump_json())
            f.write("\n")


def run(ctx: Context) -> dict:
    t0 = time.time()
    if not ctx.db_path.exists():
        raise FileNotFoundError(f"db not found: {ctx.db_path}")

    uri = f"file:{ctx.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = _fetch_rows(conn, ctx.phones_filter)
    finally:
        conn.close()

    grouped: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        grouped.setdefault(int(r["chat_row_id"]), []).append(r)

    long_path = ctx.data_dir / "conversations.jsonl"
    short_path = ctx.data_dir / "conversations_short.jsonl"
    outputs: list[Path] = []

    if ctx.phones_filter is not None:
        matched_phones = {
            (rows_[0]["phone"] or "") for rows_ in grouped.values()
        }
        missing = sorted(ctx.phones_filter - matched_phones)
        if missing:
            log.warning(
                "phones_filter: %d phone(s) not found in DB: %s",
                len(missing),
                missing,
            )

        convos = [
            _build_conversation(cid, rs)
            for cid, rs in sorted(grouped.items())
        ]
        _write_jsonl(long_path, convos)
        outputs.append(long_path)
        log.info(
            "stage1 phones mode: %d chats, %d msgs → %s",
            len(convos),
            sum(len(c.messages) for c in convos),
            long_path,
        )
    else:
        long_convos: list[Conversation] = []
        short_convos: list[Conversation] = []
        for cid in sorted(grouped):
            rs = grouped[cid]
            convo = _build_conversation(cid, rs)
            if len(convo.messages) >= MIN_MESSAGES:
                long_convos.append(convo)
            else:
                short_convos.append(convo)

        if ctx.chat_limit is not None:
            long_convos = long_convos[: ctx.chat_limit]

        _write_jsonl(long_path, long_convos)
        _write_jsonl(short_path, short_convos)
        outputs.extend([long_path, short_path])
        log.info(
            "stage1: %d long, %d short",
            len(long_convos),
            len(short_convos),
        )

    return {
        "stage": 1,
        "outputs": outputs,
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
    }
