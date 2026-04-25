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
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Sequence

from src.context import (
    Context,
    format_iso_as_dmy,
    parse_user_date,
    write_pipeline_config,
)
from src.exceptions import ConfigError
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
    p.add_argument("--skip-labels", action="store_true",
                   help="skip interactive label-exclusion prompt")
    p.add_argument("--skip-from-date", action="store_true",
                   help="skip interactive from-date prompt")
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


def _parse_indices(raw: str, n: int) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit():
            raise ValueError(f"not a number: {tok!r}")
        i = int(tok)
        if i < 1 or i > n:
            raise ValueError(f"out of range 1..{n}: {i}")
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def _read_existing_labels(path: Path) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.add(line)
    return frozenset(names)


def _step_select_excluded_labels(ctx: Context) -> None:
    if not ctx.db_path.exists():
        print(
            f"[prepare] ERROR: {ctx.db_path} not found. Drop msgstore.db there first.",
            file=sys.stderr,
        )
        sys.exit(2)

    uri = f"file:{ctx.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = list(conn.execute(
            "SELECT label_name FROM labels WHERE type = 0 "
            "ORDER BY sort_id, label_name"
        ))
    finally:
        conn.close()
    names = [r[0] for r in rows if r[0]]
    if not names:
        print("[prepare] no user labels in DB — skipping label exclusion")
        return

    print("\n[prepare] User labels in msgstore.db:")
    for i, n in enumerate(names, 1):
        print(f"  {i:2d}. {n}")
    print(
        "\nWhich labels should be EXCLUDED from analysis?\n"
        "Chats tagged with any selected label will be dropped from every stage.\n"
    )
    while True:
        raw = input("Exclude which labels? (comma-sep nums, blank=none): ").strip()
        try:
            indices = _parse_indices(raw, len(names))
            break
        except ValueError as e:
            print(f"  invalid: {e}. Try again.")

    selected = sorted({names[i - 1] for i in indices})

    labels_file = ctx.excluded_labels_path
    existing = _read_existing_labels(labels_file)
    if frozenset(selected) == existing:
        print(f"[prepare] selection unchanged ({len(selected)} label(s)) — no-op")
        return

    if selected:
        body_lines = [
            "# excluded-labels.txt — labels whose chats are dropped from analysis.",
            "# Regenerate via `scripts.prepare`. One label per line. `#` = comment.",
            "",
            *selected,
            "",
        ]
    else:
        body_lines = [
            "# excluded-labels.txt — empty: no labels excluded.",
            "",
        ]
    labels_file.parent.mkdir(parents=True, exist_ok=True)
    labels_file.write_text("\n".join(body_lines), encoding="utf-8")
    print(f"[prepare] wrote {labels_file} ({len(selected)} label(s))")

    # Invalidate Stage 1 artifacts + all sentinels so pipeline reruns cleanly.
    cleared: list[Path] = []
    for rel in ("conversations.jsonl", "conversations_short.jsonl", "chat_labels.json"):
        p = ctx.data_dir / rel
        if p.exists():
            p.unlink()
            cleared.append(p)
    for p in ctx.data_dir.glob("stage*.done"):
        p.unlink()
        cleared.append(p)
    if cleared:
        print(f"[prepare] invalidated {len(cleared)} artifact(s); Stage 1 will rerun")


def _step_from_date(ctx: Context) -> None:
    cfg_path = ctx.pipeline_config_path
    current = ctx.from_date
    if current:
        print(
            f"\n[prepare] Current from-date filter: "
            f"{format_iso_as_dmy(current)} ({current})"
        )
        ans = input("Keep this date? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes", "s", "sim"):
            print("[prepare] keeping existing from-date")
            return

    print(
        "\n[prepare] Enter the earliest message date to include in the analysis.\n"
        "Format: dd.mm.yyyy (e.g. 01.03.2026). Blank = no filter (process all)."
    )
    while True:
        raw = input("From-date: ").strip()
        try:
            iso = parse_user_date(raw)
            break
        except ConfigError as e:
            print(f"  invalid: {e}. Try again.")

    new_val = iso or None
    if new_val == current:
        print("[prepare] from-date unchanged — no-op")
        return

    write_pipeline_config(cfg_path, new_val)
    if new_val:
        print(f"[prepare] wrote {cfg_path} (from_date = {format_iso_as_dmy(new_val)})")
    else:
        print(f"[prepare] wrote {cfg_path} (no date filter)")

    # Invalidate Stage 1 artifacts + sentinels so date filter takes effect.
    cleared: list[Path] = []
    for rel in ("conversations.jsonl", "conversations_short.jsonl", "chat_labels.json"):
        p = ctx.data_dir / rel
        if p.exists():
            p.unlink()
            cleared.append(p)
    for p in ctx.data_dir.glob("stage*.done"):
        p.unlink()
        cleared.append(p)
    if cleared:
        print(f"[prepare] invalidated {len(cleared)} artifact(s); Stage 1 will rerun")


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

    if not ns.skip_labels:
        _step_select_excluded_labels(ctx)
        # Re-build context so ctx.excluded_labels / labels_hash / input_hash
        # reflect the freshly written file.
        ctx = _build_ctx(ns)

    if not ns.skip_from_date:
        _step_from_date(ctx)
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
