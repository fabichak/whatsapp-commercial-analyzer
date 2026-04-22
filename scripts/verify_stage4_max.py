"""MAX-only variant of verify_stage4. Forces llm_mode='max' (OAuth)."""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from src.context import Context
from src.label import VALID_STEP_IDS, run as stage4_run
from src.llm import ClaudeClient
from src.schemas import LabeledMessage

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
        for p in (src_convos, src_map, src_script):
            shutil.copyfile(p, data_dir / p.name)

        # Truncate templates for test speed. Default 50, override via env.
        limit = int(os.environ.get("STAGE4_VERIFY_TEMPLATE_LIMIT", "50"))
        all_tmpls = json.loads(src_templates.read_text(encoding="utf-8"))
        kept = all_tmpls if limit <= 0 or limit >= len(all_tmpls) else all_tmpls[:limit]
        (data_dir / src_templates.name).write_text(
            json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        kept_ids = {t["template_id"] for t in kept}
        # Filter message→template map to only kept templates (drop orphan rows).
        full_map = json.loads(src_map.read_text(encoding="utf-8"))
        filt_map = {k: v for k, v in full_map.items() if int(v) in kept_ids}
        (data_dir / src_map.name).write_text(
            json.dumps(filt_map, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"verify: using {len(kept)}/{len(all_tmpls)} templates, "
              f"{len(filt_map)}/{len(full_map)} map entries")

        client = ClaudeClient(llm_mode="max", budget_usd=1.0)
        ctx = Context(
            db_path=REPO / "input" / "msgstore.db",
            script_path=REPO / "input" / "script-comercial.md",
            data_dir=data_dir,
            output_dir=Path(td) / "out",
            prompts_dir=REPO / "prompts",
            chat_limit=5,
            phones_filter=None,
            phones_hash=None,
            llm_mode="max",
            budget_usd=1.0,
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

        # Only judge spa coverage among messages whose template was kept in
        # the truncated test set.
        spa_kept = [r for r in rows if r.from_me and r.msg_id in {int(k) for k in filt_map}]
        cust = [r for r in rows if not r.from_me]
        spa = [r for r in rows if r.from_me]
        spa_with_step = [r for r in spa_kept if r.step_id is not None]
        cust_with_intent = [r for r in cust if r.intent is not None]
        frac_spa = len(spa_with_step) / max(1, len(spa_kept))
        frac_cust = len(cust_with_intent) / max(1, len(cust))
        print(f"rows={len(rows)} spa={len(spa)} spa_kept={len(spa_kept)} cust={len(cust)} "
              f"spa_step_frac={frac_spa:.2f} cust_intent_frac={frac_cust:.2f}")
        assert frac_spa >= 0.80, f"spa step_id coverage {frac_spa:.2f} <0.80 (over kept templates)"
        assert frac_cust >= 0.50, f"customer intent coverage {frac_cust:.2f} <0.50"

        for r in spa:
            if r.matches_script is True:
                assert r.step_context == "on_script", f"spa msg {r.msg_id} derive fail"
            elif r.matches_script is False:
                assert r.step_context == "off_script", f"spa msg {r.msg_id} derive fail"
        for r in spa_with_step:
            assert r.step_id in VALID_STEP_IDS, f"bad step_id {r.step_id}"

        obj_counter = Counter(r.objection_type for r in cust if r.objection_type)
        print(f"objection_counts={dict(obj_counter)}")
        want = {"price", "hesitation_vou_pensar", "time_slot"}
        missing = want - set(obj_counter)
        if missing:
            print(f"WARN: expected objection types not seen (soft): {sorted(missing)}")

        usage = client.get_usage_report()
        max_calls = usage.get("max", {}).get("calls", 0)
        api_calls = usage.get("api", {}).get("calls", 0)
        api_cost = float(usage.get("api", {}).get("cost_usd", 0.0))
        print(f"max_calls={max_calls} api_calls={api_calls} api_cost=${api_cost:.4f}")
        assert api_calls == 0, f"API used when MAX required (calls={api_calls})"
        assert max_calls >= 1, "MAX not used"

        rnd = random.Random(42)
        sample = rnd.sample(rows, min(10, len(rows)))
        print("sample labels:")
        for r in sample:
            side = "spa" if r.from_me else "cust"
            extra = (f"step={r.step_id} match={r.matches_script}" if r.from_me
                     else f"ctx={r.step_context} intent={r.intent!r} obj={r.objection_type} sent={r.sentiment}")
            print(f"  [{side}] chat={r.chat_id} mid={r.msg_id} {extra}")
        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
