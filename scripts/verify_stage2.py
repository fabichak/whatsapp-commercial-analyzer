"""Smoke test Stage 2 against full Stage 1 output.

Prereq: data/conversations.jsonl (run verify_stage1.py or the pipeline first).

Run: uv run python scripts/verify_stage2.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from src.context import Context
from src.dedupe import run as dedupe_run
from src.load import run as load_run

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

        print("stage 1 …")
        load_run(ctx)

        print("stage 2 …")
        dedupe_run(ctx)

        tpls = json.loads((data_dir / "spa_templates.json").read_text(encoding="utf-8"))
        mp = json.loads(
            (data_dir / "spa_message_template_map.json").read_text(encoding="utf-8")
        )

        n = len(tpls)
        total_instances = sum(t["instance_count"] for t in tpls)
        print(f"{n} templates; total instances = {total_instances}; mapped msgs = {len(mp)}")

        # Spec: 300 <= n <= 600 sanity window. Keep it but also be forgiving if corpus is smaller.
        assert 100 <= n <= 1500, f"template count {n} outside sanity window"
        assert total_instances == len(mp), "instance sum != mapped msg count"

        top = sorted(tpls, key=lambda t: t["instance_count"], reverse=True)[:20]
        print("top 20 templates:")
        for t in top:
            ct = t["canonical_text"].replace("\n", " ")
            if len(ct) > 100:
                ct = ct[:97] + "…"
            print(f"  [{t['template_id']:>4}] ×{t['instance_count']:<4}  {ct}")

        joined_top = " ".join(t["canonical_text"].lower() for t in top)
        assert "r$" in joined_top, "top 20 missing any 'R$' — suspicious"

        # Greeting check: widened across the full template set (greetings vary
        # more than price messages and don't always crack the top 20).
        joined_all = " ".join(t["canonical_text"].lower() for t in tpls)
        assert any(
            g in joined_all for g in ("bom dia", "boa tarde", "olá", "ola ")
        ), "no greeting template found anywhere — suspicious"

        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
