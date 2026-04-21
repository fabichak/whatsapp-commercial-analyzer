"""Smoke test Stage 5 (M2-S5-T1) against real Haiku.

Picks top 6 SpaTemplates by instance_count, scores them, prints.
Asserts:
- rubric fields present, scores in range, polarity valid.
- polarity not all 'neu' (rubric differentiates).
- api cost < $0.05.

Run: uv run python scripts/verify_stage5.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from src.context import Context
from src.llm import ClaudeClient
from src.schemas import SpaTemplate, TemplateSentiment
from src.sentiment import run as stage5_run

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    src_templates = REPO / "data" / "spa_templates.json"
    if not src_templates.exists():
        print(f"missing {src_templates} — run stage 2 first", file=sys.stderr)
        return 2

    raw = json.loads(src_templates.read_text(encoding="utf-8"))
    templates = [SpaTemplate.model_validate(t) for t in raw]
    top = sorted(templates, key=lambda t: t.instance_count, reverse=True)[:6]
    print(f"selected top {len(top)} templates by instance_count")

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "spa_templates.json").write_text(
            json.dumps([t.model_dump() for t in top], ensure_ascii=False),
            encoding="utf-8",
        )
        # script.yaml not strictly required by stage5 but keep parity
        shutil.copyfile(REPO / "data" / "script.yaml", data_dir / "script.yaml")

        client = ClaudeClient(llm_mode="hybrid", budget_usd=0.10)
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
            budget_usd=0.10,
            force=True,
            dry_run=False,
            client=client,
        )

        stage5_run(ctx)

        out_path = data_dir / "template_sentiment.json"
        assert out_path.exists(), "template_sentiment.json not written"
        scored = [TemplateSentiment.model_validate(d) for d in
                  json.loads(out_path.read_text(encoding="utf-8"))]
        assert len(scored) == len(top), f"scored {len(scored)} != {len(top)}"

        print("sample scores:")
        pols: list[str] = []
        for ts in scored:
            for field in ("warmth", "clarity", "script_adherence"):
                v = getattr(ts, field)
                assert 1 <= v <= 5, f"bad {field}={v}"
            assert ts.polarity in {"pos", "neu", "neg"}, f"bad polarity {ts.polarity}"
            assert ts.critique and ts.critique.strip(), "empty critique"
            pols.append(ts.polarity)
            tmpl_text = next(t.canonical_text for t in top if t.template_id == ts.template_id)
            snippet = tmpl_text[:80].replace("\n", " ")
            print(f"  tid={ts.template_id} w={ts.warmth} c={ts.clarity} "
                  f"sa={ts.script_adherence} pol={ts.polarity} :: "
                  f"{snippet!r} -> {ts.critique!r}")

        assert len(set(pols)) > 1, (
            f"polarity constant across {len(top)} templates ({pols}); rubric not differentiating"
        )

        usage = client.get_usage_report()
        api_cost = float(usage.get("api", {}).get("cost_usd", 0.0))
        calls = usage.get("api", {}).get("calls", 0)
        print(f"api_calls={calls} api_cost=${api_cost:.4f}")
        assert api_cost < 0.05, f"cost ${api_cost:.4f} over $0.05"

        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
