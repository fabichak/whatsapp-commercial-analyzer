"""Pre-pipeline preparation: generate `input/script.yaml` + label ground truth.

Run before `scripts.run_pipeline`. Does three things in order:

1. If `input/script.yaml` missing (or `--force-script`), LLM-draft it from
   `input/script-comercial.md`. User should review before proceeding.
2. If `data/conversations.jsonl` missing, run Stage 1 (`src.load`).
3. Launch the interactive ground-truth labeler
   (`scripts/label_ground_truth.py`), unless `--skip-ground-truth`.

After this completes, `scripts.run_pipeline` can run end-to-end.
"""

from __future__ import annotations

import argparse
import logging
import runpy
import sys
from pathlib import Path
from typing import Optional, Sequence

from src.context import Context
from src.prepare import generate_script_yaml

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="prepare",
        description="Generate script.yaml and label ground truth before running the pipeline.",
    )
    p.add_argument("--force-script", action="store_true",
                   help="regenerate input/script.yaml even if it exists")
    p.add_argument("--skip-script", action="store_true",
                   help="skip script.yaml generation step")
    p.add_argument("--skip-ground-truth", action="store_true",
                   help="skip interactive ground-truth labeling")
    p.add_argument("--llm-mode", choices=["max", "api", "hybrid"], default="hybrid")
    p.add_argument("--budget-usd", type=float, default=10.0)
    p.add_argument("--input-dir", type=Path, default=REPO / "input")
    p.add_argument("--data-dir", type=Path, default=REPO / "data")
    p.add_argument("--output-dir", type=Path, default=REPO / "output")
    p.add_argument("--prompts-dir", type=Path, default=REPO / "prompts")
    return p.parse_args(list(argv) if argv is not None else None)


def _build_ctx(ns: argparse.Namespace) -> Context:
    argv = [
        "--llm-mode", ns.llm_mode,
        "--budget-usd", str(ns.budget_usd),
        "--input-dir", str(ns.input_dir),
        "--data-dir", str(ns.data_dir),
        "--output-dir", str(ns.output_dir),
        "--prompts-dir", str(ns.prompts_dir),
    ]
    return Context.from_args(argv)


def _step_generate_script(ctx: Context, *, force: bool) -> None:
    out = ctx.script_yaml_path
    if out.exists() and not force:
        print(f"[prepare] {out} exists — skipping generation (use --force-script to overwrite)")
        return
    if not ctx.script_path.exists():
        print(
            f"[prepare] ERROR: {ctx.script_path} not found. Drop the Markdown script there first.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"[prepare] generating {out} from {ctx.script_path.name} via LLM...")
    generate_script_yaml(ctx)
    print(f"[prepare] wrote {out} — REVIEW IT before running the pipeline.")


def _step_run_stage1(ctx: Context) -> None:
    convos = ctx.data_dir / "conversations.jsonl"
    if convos.exists():
        print(f"[prepare] {convos} exists — skipping stage 1")
        return
    print("[prepare] running stage 1 (load msgstore.db)...")
    from src import load as stage_load
    stage_load.run(ctx)


def _step_label_ground_truth() -> None:
    script = REPO / "scripts" / "label_ground_truth.py"
    print(f"[prepare] launching {script.name} (interactive)...")
    runpy.run_path(str(script), run_name="__main__")


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ns = _parse_args(argv)
    ctx = _build_ctx(ns)

    if not ns.skip_script:
        _step_generate_script(ctx, force=ns.force_script)

    if not ns.skip_ground_truth:
        _step_run_stage1(ctx)
        _step_label_ground_truth()

    print("[prepare] done. Next: uv run python -m scripts.run_pipeline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
