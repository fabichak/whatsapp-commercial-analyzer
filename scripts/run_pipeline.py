"""Pipeline orchestrator.

See TECH_PLAN.md §M1-T1. Each stage module exposes `run(ctx) -> StageResult`,
where StageResult is a dict with keys: stage, outputs, llm_usd_max,
llm_usd_api, elapsed_s.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

# WSL2: SDK stream-json path deadlocks on some inputs. Oneshot invokes
# bundled CLI with `-p --output-format json` — subproc runs to completion.
os.environ.setdefault("CLAUDE_MAX_ONESHOT", "1")
# Sweep stray `claude` CLI subprocs from prior crashed runs so Stage 3
# doesn't hang on a stuck pipe. Safe: MaxClient._protected_pids spares
# this process tree (ancestor Claude Code session + sibling workers).
os.environ.setdefault("CLAUDE_MAX_KILL_OTHERS", "1")

from src.context import Context
from src.exceptions import BudgetExceeded

log = logging.getLogger(__name__)

STAGE_MODULES: dict[int, str] = {
    1: "src.load",
    2: "src.dedupe",
    3: "src.script_index",
    4: "src.label",
    5: "src.sentiment",
    6: "src.conversion",
    7: "src.cluster",
    8: "src.report",
}

# Per-stage prereq files (relative to ctx.data_dir unless absolute).
STAGE_PREREQS: dict[int, list[str]] = {
    1: [],
    2: ["conversations.jsonl"],
    3: [],
    4: ["conversations.jsonl", "spa_templates.json"],
    5: ["spa_templates.json"],
    6: ["conversations.jsonl", "labeled_messages.jsonl"],
    7: ["labeled_messages.jsonl"],
    8: [
        "conversations.jsonl",
        "labeled_messages.jsonl",
        "template_sentiment.json",
        "conversions.jsonl",
        "aggregations.json",
    ],
}

PREREQ_STAGE_HINT: dict[str, int] = {
    "conversations.jsonl": 1,
    "spa_templates.json": 2,
    "spa_message_template_map.json": 2,
    "script.yaml": 3,
    "labeled_messages.jsonl": 4,
    "template_sentiment.json": 5,
    "conversions.jsonl": 6,
    "turnarounds.json": 6,
    "lost_deals.json": 6,
    "aggregations.json": 7,
}

MODULE_VERSION = "0.1.0"


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def sentinel_path(ctx: Context, stage: int) -> Path:
    return ctx.data_dir / f"stage{stage}.done"


def read_sentinel(ctx: Context, stage: int) -> Optional[dict]:
    p = sentinel_path(ctx, stage)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_sentinel(ctx: Context, stage: int, result: dict) -> None:
    payload = {
        "ts": time.time(),
        "git_sha": _git_sha(),
        "module_version": MODULE_VERSION,
        "chat_limit": ctx.chat_limit,
        "phones_hash": ctx.phones_hash,
        "input_hash": ctx.input_hash,
        "llm_mode": ctx.llm_mode,
        "outputs": [str(p) for p in result.get("outputs", [])],
    }
    sentinel_path(ctx, stage).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def sentinel_valid(ctx: Context, sentinel: dict) -> bool:
    return (
        sentinel.get("chat_limit") == ctx.chat_limit
        and sentinel.get("phones_hash") == ctx.phones_hash
        and sentinel.get("input_hash") == ctx.input_hash
    )


def purge_state(ctx: Context) -> None:
    """--restart: wipe all derived state in data_dir + llm_cache. Inputs
    under input/ are untouched. After purge every stage re-runs fresh.
    """
    import shutil
    if ctx.data_dir.exists():
        for entry in ctx.data_dir.iterdir():
            if entry.name == "ground_truth_outcomes.csv":
                continue  # hand-labeled, preserve
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError:
                pass
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    log.info("restart: cleared %s (kept ground_truth_outcomes.csv)", ctx.data_dir)


def check_prereqs(ctx: Context, stage: int) -> None:
    for rel in STAGE_PREREQS.get(stage, []):
        p = Path(rel)
        if not p.is_absolute():
            p = ctx.data_dir / rel
        if not p.exists():
            hint = PREREQ_STAGE_HINT.get(rel, max(1, stage - 1))
            sys.stderr.write(
                f"Stage {stage} requires {p} from Stage {hint}. "
                f"Run: python -m scripts.run_pipeline --stage {hint}\n"
            )
            sys.exit(2)


def load_stage_module(stage: int):
    name = STAGE_MODULES[stage]
    return importlib.import_module(name)


def _fmt_usage_line(stage: int, result: dict, total_api: float, budget: float) -> str:
    return (
        f"[stage {stage}] elapsed={result.get('elapsed_s', 0):.1f}s "
        f"max=${result.get('llm_usd_max', 0):.2f} "
        f"api=${result.get('llm_usd_api', 0):.4f} "
        f"total_api=${total_api:.4f} budget=${budget:.2f}"
    )


def select_stages(
    ns_stage: Optional[int], ns_from: Optional[int], ns_to: Optional[int]
) -> list[int]:
    if ns_stage is not None:
        return [ns_stage]
    lo = ns_from if ns_from is not None else 1
    hi = ns_to if ns_to is not None else 8
    return list(range(lo, hi + 1))


def run_pipeline(ctx: Context, stages: list[int]) -> dict:
    summary: list[dict] = []
    total_api = 0.0
    t0 = time.time()
    if ctx.restart:
        purge_state(ctx)
    for stage in stages:
        sent = read_sentinel(ctx, stage)
        if sent is not None and not ctx.restart and sentinel_valid(ctx, sent):
            print(f"[stage {stage}] skipped (sentinel match)")
            continue
        # Sentinel present but invalid (input_hash / chat_limit / phones changed):
        # prior partials are stale. Clear listed outputs + the sentinel so the
        # stage starts clean. No --restart required — input change is the signal.
        if sent is not None and not ctx.restart:
            for op in sent.get("outputs", []) or []:
                try:
                    Path(op).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            try:
                sentinel_path(ctx, stage).unlink()
            except OSError:
                pass
            log.info("stage %d: prior sentinel invalid (input change), cleared partials", stage)

        check_prereqs(ctx, stage)
        mod = load_stage_module(stage)
        start = time.time()
        print(f"\n{'=' * 60}\n>>> STAGE {stage} START ({STAGE_MODULES[stage]})\n{'=' * 60}")
        try:
            result = mod.run(ctx)
        except BudgetExceeded as e:
            print(f"<<< STAGE {stage} ABORT (budget): {e}", file=sys.stderr)
            raise
        if not isinstance(result, dict):
            result = {"stage": stage, "outputs": []}
        result.setdefault("stage", stage)
        result.setdefault("outputs", [])
        result.setdefault("llm_usd_max", 0.0)
        result.setdefault("llm_usd_api", 0.0)
        result.setdefault("elapsed_s", time.time() - start)

        total_api += float(result["llm_usd_api"])
        print(_fmt_usage_line(stage, result, total_api, ctx.budget_usd))
        print(f"<<< STAGE {stage} END\n")
        write_sentinel(ctx, stage, result)
        summary.append(result)

    elapsed = time.time() - t0
    usage = ctx.client.get_usage_report() if ctx.client is not None else {}
    print(f"\n=== pipeline done in {elapsed:.1f}s ===")
    print(f"usage: {json.dumps(usage, indent=2, default=str)}")
    return {"stages": summary, "elapsed_s": elapsed, "usage": usage}


def _sweep_stray_claude_at_startup() -> None:
    """Proactive sweep BEFORE ClaudeClient init. Uses MaxClient's own
    protected-pid logic so ancestor Claude Code session survives.
    """
    if os.environ.get("CLAUDE_MAX_KILL_OTHERS") != "1":
        return
    try:
        from src.llm import MaxClient
        mc = MaxClient()
        mc._kill_stray_claude()
        print("[pipeline] stray claude sweep done")
    except Exception as e:
        print(f"[pipeline] stray claude sweep skipped: {e}", file=sys.stderr)


def check_prepare_artifacts(ctx: Context) -> None:
    """Fail fast if pre-step artifacts are missing.

    `input/script.yaml` and `data/ground_truth_outcomes.csv` are produced by
    `scripts.prepare`. Pipeline refuses to run without them.
    """
    missing: list[str] = []
    if not ctx.script_yaml_path.exists():
        missing.append(str(ctx.script_yaml_path))
    gt = ctx.data_dir / "ground_truth_outcomes.csv"
    if not gt.exists():
        missing.append(str(gt))
    if missing:
        sys.stderr.write(
            "Missing prerequisite files:\n"
            + "".join(f"  - {p}\n" for p in missing)
            + "Run: uv run python -m scripts.prepare\n"
        )
        sys.exit(2)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _sweep_stray_claude_at_startup()
    import argparse

    # Peek for stage/from/to without re-parsing everything — Context.from_args owns the full parser.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--stage", type=int, default=None)
    pre.add_argument("--from", dest="from_stage", type=int, default=None)
    pre.add_argument("--to", dest="to_stage", type=int, default=None)
    pre_ns, _ = pre.parse_known_args(list(argv) if argv is not None else None)

    ctx = Context.from_args(argv)
    check_prepare_artifacts(ctx)
    stages = select_stages(pre_ns.stage, pre_ns.from_stage, pre_ns.to_stage)
    run_pipeline(ctx, stages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
