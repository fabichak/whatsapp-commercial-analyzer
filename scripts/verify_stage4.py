"""Smoke test Stage 4 — M2-S4-T1 spa-template labeling (real API).

Scope (T1 only): picks top-6 spa templates by instance_count, labels
each via Haiku, asserts schema + valid step_id. T2 adds customer
batching; that check lands in this script once T2 ships.

Asserts:
- spa_template_labels.json written for the 6 sampled templates.
- each label: step_id ∈ {"1","2","3","3.5","5","6","7","fup1","fup2"}.
- if matches_script=false then deviation_note non-empty.
- cost < $0.15.

Run: uv run python scripts/verify_stage4.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from src.context import Context
from src.label import VALID_STEP_IDS, label_spa_templates
from src.llm import ClaudeClient

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    src_templates = REPO / "data" / "spa_templates.json"
    src_map = REPO / "data" / "spa_message_template_map.json"
    src_script = REPO / "data" / "script.yaml"
    for p in (src_templates, src_map, src_script):
        if not p.exists():
            print(f"missing {p} — run earlier stages first", file=sys.stderr)
            return 2

    all_tmpls = json.loads(src_templates.read_text(encoding="utf-8"))
    top = sorted(all_tmpls, key=lambda t: t["instance_count"], reverse=True)[:6]
    top_ids = {t["template_id"] for t in top}

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "spa_templates.json").write_text(
            json.dumps(top, ensure_ascii=False), encoding="utf-8"
        )
        # tiny map — only entries for the 6 sampled templates.
        full_map = json.loads(src_map.read_text(encoding="utf-8"))
        trimmed_map = {k: v for k, v in full_map.items() if v in top_ids}
        (data_dir / "spa_message_template_map.json").write_text(
            json.dumps(trimmed_map), encoding="utf-8"
        )
        shutil.copyfile(src_script, data_dir / "script.yaml")

        client = ClaudeClient(llm_mode="hybrid", budget_usd=0.50)
        ctx = Context(
            db_path=REPO / "msgstore.db",
            script_path=REPO / "script-comercial.md",
            data_dir=data_dir,
            output_dir=Path(td) / "out",
            prompts_dir=REPO / "prompts",
            chat_limit=None,
            phones_filter=None,
            phones_hash=None,
            llm_mode="hybrid",
            budget_usd=0.50,
            force=False,
            dry_run=False,
            client=client,
        )

        labels = label_spa_templates(ctx)
        assert len(labels) == len(top), f"expected {len(top)} labels, got {len(labels)}"

        for tid, lab in labels.items():
            assert lab.step_id in VALID_STEP_IDS, f"bad step_id {lab.step_id} for tmpl {tid}"
            if lab.matches_script is False:
                assert lab.deviation_note, f"tmpl {tid} off_script but deviation_note empty"

        written = data_dir / "spa_template_labels.json"
        assert written.exists()
        raw = json.loads(written.read_text(encoding="utf-8"))
        assert len(raw) == len(top)

        usage = client.get_usage_report()
        api_cost = float(usage.get("api", {}).get("cost_usd", 0.0))
        max_calls = usage.get("max", {}).get("calls", 0)
        api_calls = usage.get("api", {}).get("calls", 0)
        print(
            f"labels={len(labels)} max_calls={max_calls} api_calls={api_calls} "
            f"api_cost=${api_cost:.4f}"
        )
        assert api_cost < 0.15, f"cost ${api_cost:.4f} over $0.15"

        print("sample labels:")
        for tid, lab in list(labels.items())[:6]:
            tmpl_text = next(t["canonical_text"] for t in top if t["template_id"] == tid)
            snip = tmpl_text[:80].replace("\n", " ")
            print(f"  tmpl {tid} step={lab.step_id} match={lab.matches_script} :: {snip}")
        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
