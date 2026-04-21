"""Build tests/fixtures/tiny.db — matches msgstore.db schema subset.

Schema taken from `.schema message chat jid` on real msgstore.db.
Three chats:
  - chat A (25 msgs, phone 5511000000001) — converting, long
  - chat B (22 msgs, phone 5511000000002) — lost, long
  - chat C (5 msgs,  phone 5511000000003) — stub, below threshold
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MESSAGE_SQL = """
CREATE TABLE message(
  _id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_row_id INTEGER NOT NULL,
  from_me INTEGER NOT NULL,
  key_id TEXT NOT NULL,
  sender_jid_row_id INTEGER,
  timestamp INTEGER,
  message_type INTEGER,
  text_data TEXT,
  sort_id INTEGER NOT NULL DEFAULT 0
)
"""

CHAT_SQL = """
CREATE TABLE chat(
  _id INTEGER PRIMARY KEY AUTOINCREMENT,
  jid_row_id INTEGER UNIQUE,
  group_type INTEGER NOT NULL DEFAULT 0
)
"""

JID_SQL = """
CREATE TABLE jid(
  _id INTEGER PRIMARY KEY AUTOINCREMENT,
  user TEXT NOT NULL,
  server TEXT NOT NULL,
  raw_string TEXT
)
"""


def build(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(MESSAGE_SQL + ";" + CHAT_SQL + ";" + JID_SQL + ";")

        # Jids
        jids = [
            (1, "5511000000001", "s.whatsapp.net", "5511000000001@s.whatsapp.net"),
            (2, "5511000000002", "s.whatsapp.net", "5511000000002@s.whatsapp.net"),
            (3, "5511000000003", "s.whatsapp.net", "5511000000003@s.whatsapp.net"),
        ]
        conn.executemany(
            "INSERT INTO jid(_id,user,server,raw_string) VALUES (?,?,?,?)", jids
        )

        # Chats — all group_type=0 (1-to-1)
        chats = [(1, 1, 0), (2, 2, 0), (3, 3, 0)]
        conn.executemany(
            "INSERT INTO chat(_id,jid_row_id,group_type) VALUES (?,?,?)", chats
        )

        base_ts = 1_700_000_000_000
        msgs: list[tuple] = []
        mid = 1

        # Chat 1: 25 msgs, converting
        for i in range(25):
            text = f"converting msg {i} https://example.com/x" if i % 5 == 0 else f"msg {i}"
            msgs.append(
                (mid, 1, i % 2, f"kA{i}", 1, base_ts + i * 1000, 0, text, mid)
            )
            mid += 1

        # Chat 2: 22 msgs, lost
        for i in range(22):
            text = f"   lost   reply   {i}   " if i == 0 else f"text {i}"
            msgs.append(
                (mid, 2, i % 2, f"kB{i}", 2, base_ts + i * 1000, 0, text, mid)
            )
            mid += 1

        # Chat 3: 5 msgs, stub
        for i in range(5):
            msgs.append(
                (mid, 3, i % 2, f"kC{i}", 3, base_ts + i * 1000, 0, f"stub {i}", mid)
            )
            mid += 1

        conn.executemany(
            "INSERT INTO message(_id,chat_row_id,from_me,key_id,sender_jid_row_id,"
            "timestamp,message_type,text_data,sort_id) VALUES (?,?,?,?,?,?,?,?,?)",
            msgs,
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "tiny.db"
    build(out)
    print(f"built {out}")
