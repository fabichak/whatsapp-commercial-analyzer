"""MAX-only variant of verify_stage3. Forces llm_mode='max' (OAuth)."""
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
KEYWORDS = ("escalda-pés", "banho de imersão")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    src_script = REPO / "data" / "script.yaml"
    script_md = REPO / "script-comercial.md"
    for p in (src_script, script_md):
        if not p.exists():
            print(f"missing {p}", file=sys.stderr)
            return 2

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        data_dir.mkdir(parents=True)
        shutil.copyfile(src_script, data_dir / "script.yaml")
        before = _sha(data_dir / "script.yaml")
        before_real = _sha(src_script)

        client = ClaudeClient(llm_mode="max", budget_usd=1.0)
        ctx = Context(
            db_path=REPO / "msgstore.db",
            script_path=script_md,
            data_dir=data_dir,
            output_dir=Path(td) / "out",
            prompts_dir=REPO / "prompts",
            chat_limit=None,
            phones_filter=None,
            phones_hash=None,
            llm_mode="max",
            budget_usd=1.0,
            force=False,
            dry_run=False,
            client=client,
        )
        stage3_run(ctx)

        ext_path = data_dir / "script_extensions.yaml"
        assert ext_path.exists(), "script_extensions.yaml not written"
        assert _sha(data_dir / "script.yaml") == before, "temp script.yaml mutated"
        assert _sha(src_script) == before_real, "repo script.yaml mutated"

        ext = yaml.safe_load(ext_path.read_text(encoding="utf-8"))
        pitch = ext.get("day_spa_pitch") or {}
        steps = pitch.get("steps") or []
        assert len(steps) >= 3, f"steps too short: {len(steps)}"
        joined = " ".join((s.get("phrase") or "") for s in steps).lower()
        joined += " " + (pitch.get("intro") or "").lower() + " " + (pitch.get("closing") or "").lower()
        assert any(k in joined for k in KEYWORDS), f"missing keywords — got: {joined[:200]}"

        replies = ext.get("objection_replies") or []
        ids = {r.get("objection_id") for r in replies}
        missing = set(TAXONOMY_IDS) - ids
        assert not missing, f"objection_replies missing ids: {sorted(missing)}"

        usage = client.get_usage_report()
        max_calls = usage.get("max", {}).get("calls", 0)
        api_calls = usage.get("api", {}).get("calls", 0)
        api_cost = float(usage.get("api", {}).get("cost_usd", 0.0))
        print(f"max_calls={max_calls} api_calls={api_calls} api_cost=${api_cost:.4f}")
        assert api_calls == 0, f"API used when MAX required (calls={api_calls})"
        assert max_calls >= 1, "MAX not used"

        print(f"OK — {len(steps)} pitch steps, {len(replies)} objection_replies, "
              f"{len(ext.get('inconsistencies') or [])} inconsistencies")
        return 0


if __name__ == "__main__":
    sys.exit(main())
