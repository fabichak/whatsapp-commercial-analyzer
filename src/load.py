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
    conn: sqlite3.Connection,
    phones: frozenset[str] | None,
    excluded_labels: frozenset[str] = frozenset(),
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
    if excluded_labels:
        lbl_placeholders = ",".join("?" for _ in excluded_labels)
        base += (
            " AND c.jid_row_id NOT IN ("
            "SELECT lj.jid_row_id FROM labeled_jid lj "
            "JOIN labels l ON lj.label_id = l._id "
            f"WHERE l.type = 0 AND l.label_name IN ({lbl_placeholders}))"
        )
        params = params + tuple(sorted(excluded_labels))
    base += " ORDER BY c._id ASC, m.timestamp ASC, m._id ASC"
    return list(conn.execute(base, params))


def _fetch_chat_labels(
    conn: sqlite3.Connection, kept_phones: set[str]
) -> dict[str, list[str]]:
    if not kept_phones:
        return {}
    out: dict[str, set[str]] = {}
    phones = sorted(kept_phones)
    # chunk IN(...) to avoid SQLite variable limit
    CHUNK = 500
    for i in range(0, len(phones), CHUNK):
        sub = phones[i : i + CHUNK]
        placeholders = ",".join("?" for _ in sub)
        q = (
            "SELECT j.user AS phone, l.label_name AS label_name "
            "FROM labeled_jid lj "
            "JOIN jid j ON lj.jid_row_id = j._id "
            "JOIN labels l ON lj.label_id = l._id "
            f"WHERE l.type = 0 AND j.user IN ({placeholders})"
        )
        for r in conn.execute(q, tuple(sub)):
            phone = r["phone"] or ""
            if not phone:
                continue
            out.setdefault(phone, set()).add(r["label_name"])
    return {p: sorted(s) for p, s in out.items()}


def _count_excluded_chats(
    conn: sqlite3.Connection, excluded_labels: frozenset[str]
) -> int:
    if not excluded_labels:
        return 0
    placeholders = ",".join("?" for _ in excluded_labels)
    q = (
        "SELECT COUNT(DISTINCT c._id) FROM chat c "
        "JOIN labeled_jid lj ON lj.jid_row_id = c.jid_row_id "
        "JOIN labels l ON lj.label_id = l._id "
        f"WHERE c.group_type = 0 AND l.type = 0 AND l.label_name IN ({placeholders})"
    )
    row = conn.execute(q, tuple(sorted(excluded_labels))).fetchone()
    return int(row[0]) if row else 0


def _warn_missing_labels(
    conn: sqlite3.Connection, excluded_labels: frozenset[str]
) -> None:
    if not excluded_labels:
        return
    rows = conn.execute(
        "SELECT label_name FROM labels WHERE type = 0"
    ).fetchall()
    present = {r[0] for r in rows}
    missing = sorted(excluded_labels - present)
    if missing:
        log.warning(
            "excluded_labels: %d name(s) not found in DB: %s",
            len(missing),
            missing,
        )


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
        _warn_missing_labels(conn, ctx.excluded_labels)
        excluded_chats_count = _count_excluded_chats(conn, ctx.excluded_labels)
        rows = _fetch_rows(conn, ctx.phones_filter, ctx.excluded_labels)

        grouped: dict[int, list[sqlite3.Row]] = {}
        for r in rows:
            grouped.setdefault(int(r["chat_row_id"]), []).append(r)

        kept_phones = {
            (rs[0]["phone"] or "") for rs in grouped.values() if rs
        }
        kept_phones.discard("")
        chat_labels = _fetch_chat_labels(conn, kept_phones)
    finally:
        conn.close()

    long_path = ctx.data_dir / "conversations.jsonl"
    short_path = ctx.data_dir / "conversations_short.jsonl"
    chat_labels_path = ctx.data_dir / "chat_labels.json"
    outputs: list[Path] = []

    chat_labels_path.parent.mkdir(parents=True, exist_ok=True)
    chat_labels_path.write_text(
        json.dumps(chat_labels, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

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

    outputs.append(chat_labels_path)

    # Count kept long chats for reporting.
    if ctx.phones_filter is not None:
        kept_chats_count = len(grouped)
    else:
        kept_chats_count = sum(
            1 for rs in grouped.values() if len(rs) >= MIN_MESSAGES
        )
        if ctx.chat_limit is not None:
            kept_chats_count = min(kept_chats_count, ctx.chat_limit)

    summary_path = ctx.data_dir / "stage1_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "excluded_labels": sorted(ctx.excluded_labels),
                "excluded_chats_count": excluded_chats_count,
                "kept_chats_count": kept_chats_count,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    outputs.append(summary_path)

    return {
        "stage": 1,
        "outputs": outputs,
        "llm_usd_max": 0.0,
        "llm_usd_api": 0.0,
        "elapsed_s": time.time() - t0,
        "excluded_chats_count": excluded_chats_count,
        "kept_chats_count": kept_chats_count,
        "excluded_labels": sorted(ctx.excluded_labels),
    }
