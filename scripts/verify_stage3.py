"""Smoke test Stage 3 LLM script expansion (real API, ~$0.05–0.60).

Asserts:
- data/script_extensions.yaml exists.
- day_spa_pitch.steps has >= 3 items; mentions "escalda-pés" OR "banho de imersão".
- objection_replies has entries for all 9 taxonomy ids.
- data/script.yaml byte-identical before/after.
- API cost < $0.60.

Run: uv run python scripts/verify_stage3.py
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from src.context import Context
from src.llm import ClaudeClient
from src.script_index import TAXONOMY_IDS, run as stage3_run

REPO = Path(__file__).resolve().parent.parent
PROMPT_NAME = "escalda-pés", "banho de imersão"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    src_script = REPO / "data" / "script.yaml"
    if not src_script.exists():
        print(f"missing {src_script}", file=sys.stderr)
        return 2
    script_md = REPO / "input" / "script-comercial.md"
    if not script_md.exists():
        print(f"missing {script_md}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        data_dir.mkdir(parents=True)
        # Copy script.yaml to temp data_dir so we test mutation-safety there too.
        shutil.copyfile(src_script, data_dir / "script.yaml")
        script_yaml_tmp = data_dir / "script.yaml"
        before = _sha(script_yaml_tmp)
        before_real = _sha(src_script)

        client = ClaudeClient(llm_mode="hybrid", budget_usd=1.0)
        ctx = Context(
            db_path=REPO / "input" / "msgstore.db",
            script_path=script_md,
            data_dir=data_dir,
            output_dir=Path(td) / "out",
            prompts_dir=REPO / "prompts",
            chat_limit=None,
            phones_filter=None,
            phones_hash=None,
            llm_mode="hybrid",
            budget_usd=1.0,
            force=False,
            dry_run=False,
            client=client,
        )

        stage3_run(ctx)

        ext_path = data_dir / "script_extensions.yaml"
        assert ext_path.exists(), "script_extensions.yaml not written"

        # byte-identical check — both temp copy AND real repo file.
        assert _sha(script_yaml_tmp) == before, "temp script.yaml mutated"
        assert _sha(src_script) == before_real, "repo data/script.yaml mutated"

        ext = yaml.safe_load(ext_path.read_text(encoding="utf-8"))
        pitch = ext.get("day_spa_pitch") or {}
        steps = pitch.get("steps") or []
        assert len(steps) >= 3, f"day_spa_pitch.steps too short: {len(steps)}"
        joined = " ".join((s.get("phrase") or "") for s in steps).lower()
        joined += " " + (pitch.get("intro") or "").lower()
        joined += " " + (pitch.get("closing") or "").lower()
        assert any(k in joined for k in PROMPT_NAME), (
            f"day_spa_pitch missing 'escalda-pés' or 'banho de imersão' — got: {joined[:200]}"
        )

        replies = ext.get("objection_replies") or []
        ids = {r.get("objection_id") for r in replies}
        missing = set(TAXONOMY_IDS) - ids
        assert not missing, f"objection_replies missing ids: {sorted(missing)}"

        usage = client.get_usage_report()
        api_cost = float(usage.get("api", {}).get("cost_usd", 0.0))
        print(f"api_cost=${api_cost:.4f} max_calls={usage.get('max', {}).get('calls')} api_calls={usage.get('api', {}).get('calls')}")
        assert api_cost < 0.60, f"cost ${api_cost:.4f} over $0.60"

        print(f"OK — {len(steps)} pitch steps, {len(replies)} objection_replies, {len(ext.get('inconsistencies') or [])} inconsistencies")
        return 0


if __name__ == "__main__":
    sys.exit(main())
