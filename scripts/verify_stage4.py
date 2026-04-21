"""Smoke test Stage 4 (M2-S4-T1 + M2-S4-T2) against real Haiku.

Runs full Stage 4 on the current data/ (expects stage1 + stage2 already
ran with --chat-limit 5). Asserts:
- every LabeledMessage validates.
- ≥80% of spa (from_me=True) msgs have non-null step_id.
- ≥50% of customer (from_me=False) msgs have non-null intent.
- ≥1 msg for each of the 3 most-common objection types
  (price, hesitation_vou_pensar, time_slot) appears — soft check,
  warn if missing.
- total api cost ≤ $0.15.

Run: uv run python scripts/verify_stage4.py
"""

from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from src.context import Context
from src.label import VALID_STEP_IDS, run as stage4_run
from src.llm import ClaudeClient
from src.schemas import Conversation, LabeledMessage

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    src_convos = REPO / "data" / "conversations.jsonl"
    src_templates = REPO / "data" / "spa_templates.json"
    src_map = REPO / "data" / "spa_message_template_map.json"
    src_script = REPO / "data" / "script.yaml"
    for p in (src_convos, src_templates, src_map, src_script):
        if not p.exists():
            print(f"missing {p} — run earlier stages first", file=sys.stderr)
            return 2

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        data_dir.mkdir(parents=True)
        for p in (src_convos, src_templates, src_map, src_script):
            shutil.copyfile(p, data_dir / p.name)

        client = ClaudeClient(llm_mode="api", budget_usd=0.50)
        ctx = Context(
            db_path=REPO / "msgstore.db",
            script_path=REPO / "script-comercial.md",
            data_dir=data_dir,
            output_dir=Path(td) / "out",
            prompts_dir=REPO / "prompts",
            chat_limit=5,
            phones_filter=None,
            phones_hash=None,
            llm_mode="api",
            budget_usd=0.50,
            force=True,
            dry_run=False,
            client=client,
        )

        stage4_run(ctx)

        labeled_path = data_dir / "labeled_messages.jsonl"
        assert labeled_path.exists(), "labeled_messages.jsonl not written"
        rows = [
            LabeledMessage.model_validate_json(ln)
            for ln in labeled_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert rows, "no labeled messages written"

        spa = [r for r in rows if r.from_me]
        cust = [r for r in rows if not r.from_me]

        spa_with_step = [r for r in spa if r.step_id is not None]
        cust_with_intent = [r for r in cust if r.intent is not None]

        frac_spa = len(spa_with_step) / max(1, len(spa))
        frac_cust = len(cust_with_intent) / max(1, len(cust))
        print(
            f"rows={len(rows)} spa={len(spa)} cust={len(cust)} "
            f"spa_step_frac={frac_spa:.2f} cust_intent_frac={frac_cust:.2f}"
        )
        assert frac_spa >= 0.80, f"spa step_id coverage {frac_spa:.2f} <0.80"
        assert frac_cust >= 0.50, f"customer intent coverage {frac_cust:.2f} <0.50"

        # step_context derivation check (spa side)
        for r in spa:
            if r.matches_script is True:
                assert r.step_context == "on_script", f"spa msg {r.msg_id} derive fail"
            elif r.matches_script is False:
                assert r.step_context == "off_script", f"spa msg {r.msg_id} derive fail"

        # step_id domain check
        for r in spa_with_step:
            assert r.step_id in VALID_STEP_IDS, f"bad step_id {r.step_id}"

        # objection type coverage (soft)
        obj_counter = Counter(r.objection_type for r in cust if r.objection_type)
        print(f"objection_counts={dict(obj_counter)}")
        want = {"price", "hesitation_vou_pensar", "time_slot"}
        missing = want - set(obj_counter)
        if missing:
            print(f"WARN: expected objection types not seen (soft): {sorted(missing)}")

        usage = client.get_usage_report()
        api_cost = float(usage.get("api", {}).get("cost_usd", 0.0))
        max_calls = usage.get("max", {}).get("calls", 0)
        api_calls = usage.get("api", {}).get("calls", 0)
        print(
            f"max_calls={max_calls} api_calls={api_calls} api_cost=${api_cost:.4f}"
        )
        assert api_cost < 0.15, f"cost ${api_cost:.4f} over $0.15"

        rnd = random.Random(42)
        sample = rnd.sample(rows, min(10, len(rows)))
        print("sample labels:")
        for r in sample:
            side = "spa" if r.from_me else "cust"
            extra = (
                f"step={r.step_id} match={r.matches_script}"
                if r.from_me
                else f"ctx={r.step_context} intent={r.intent!r} obj={r.objection_type} sent={r.sentiment}"
            )
            print(f"  [{side}] chat={r.chat_id} mid={r.msg_id} {extra}")
        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
