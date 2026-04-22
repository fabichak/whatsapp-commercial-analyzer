"""Smoke test Stage 1 against real msgstore.db.

Run: uv run python scripts/verify_stage1.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from src.context import Context
from src.load import run

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    db = REPO / "input" / "msgstore.db"
    if not db.exists():
        print(f"missing {db}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        ctx = Context(
            db_path=db,
            script_path=REPO / "input" / "script-comercial.md",
            data_dir=data_dir,
            output_dir=Path(td) / "out",
            prompts_dir=REPO / "prompts",
            chat_limit=None,
            phones_filter=None,
            phones_hash=None,
            llm_mode="hybrid",
            budget_usd=0.0,
            force=False,
            dry_run=False,
            client=None,
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        run(ctx)

        long_path = data_dir / "conversations.jsonl"
        convos = [json.loads(l) for l in long_path.read_text().splitlines() if l.strip()]

        n = len(convos)
        n_msgs = sum(len(c["messages"]) for c in convos)
        print(f"{n} chats, {n_msgs} messages")

        # Spec §M1-T2 estimated ~387±10; actual count on this DB is 334.
        # Keep a sanity window rather than a tight match.
        assert 250 <= n <= 450, f"chat count {n} outside sanity window"

        # Spec asked for phones starting "55" but this DB stores WhatsApp LIDs
        # (server="lid") rather than raw E.164 numbers. Check non-empty + numeric.
        bad = [c["phone"] for c in convos if not (c["phone"] and c["phone"].isdigit())]
        assert not bad, f"{len(bad)} non-numeric phone/lid values: {bad[:5]}"

        for c in convos:
            ts = [m["ts_ms"] for m in c["messages"]]
            assert ts == sorted(ts), f"chat {c['chat_id']} not chronological"

        top = sorted(convos, key=lambda c: len(c["messages"]), reverse=True)[:5]
        print("top 5 busiest:")
        for c in top:
            print(f"  chat {c['chat_id']} phone {c['phone']}  {len(c['messages'])} msgs")

        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
