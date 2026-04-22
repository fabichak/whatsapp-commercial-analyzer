"""Smoke test Stage 6 (M2-S6-T2) against 20 ground-truth chats.

Reads `data/ground_truth_outcomes.csv` and `data/conversations.jsonl`,
runs Haiku detection on just the ground-truth chats, and checks that
≥16/20 match the user's `outcome` (booked vs lost; ambiguous on either
side is excluded from the denominator).

Uses `--llm-mode max` by default (Claude Max plan). Override via env
`STAGE6_LLM_MODE` (max|api|hybrid).

Run: uv run python scripts/verify_stage6.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from src.context import Context
from src.conversion import detect_conversions
from src.llm import ClaudeClient
from src.schemas import Conversation

REPO = Path(__file__).resolve().parent.parent
GT_CSV = REPO / "data" / "ground_truth_outcomes.csv"
CONVOS = REPO / "data" / "conversations.jsonl"
LABELED = REPO / "data" / "labeled_messages.jsonl"
SCRIPT_YAML = REPO / "input" / "script.yaml"
SCRIPT_MD = REPO / "input" / "script-comercial.md"

THRESHOLD = 16
BUDGET = 0.20


def _load_gt() -> list[dict]:
    if not GT_CSV.exists():
        print(f"missing {GT_CSV} — run scripts/label_ground_truth.py first", file=sys.stderr)
        sys.exit(2)
    rows = []
    with GT_CSV.open() as f:
        for row in csv.DictReader(f):
            if row.get("outcome") not in {"booked", "lost", "ambiguous"}:
                continue
            rows.append({
                "chat_id": int(row["chat_id"]),
                "phone": row.get("phone", ""),
                "outcome": row["outcome"],
                "notes": row.get("notes", ""),
            })
    return rows


def _load_convos_by_id(ids: set[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    with CONVOS.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            if c["chat_id"] in ids:
                out[c["chat_id"]] = c
    return out


def _filter_labeled(ids: set[int], dest: Path) -> None:
    if not LABELED.exists():
        dest.write_text("", encoding="utf-8")
        return
    with LABELED.open() as fin, dest.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("chat_id") in ids:
                fout.write(line + "\n")


def main() -> int:
    gt = _load_gt()
    if len(gt) < 1:
        print("no ground-truth rows labeled; nothing to verify", file=sys.stderr)
        return 2
    print(f"ground truth: {len(gt)} chats "
          f"({sum(1 for r in gt if r['outcome']=='booked')} booked, "
          f"{sum(1 for r in gt if r['outcome']=='lost')} lost, "
          f"{sum(1 for r in gt if r['outcome']=='ambiguous')} ambiguous)")

    gt_ids = {r["chat_id"] for r in gt}
    convos = _load_convos_by_id(gt_ids)
    missing = gt_ids - set(convos)
    if missing:
        print(f"warn: {len(missing)} gt chats absent from conversations.jsonl: {sorted(missing)}")

    llm_mode = os.environ.get("STAGE6_LLM_MODE", "max")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        data_dir = tmp / "data"
        data_dir.mkdir()
        input_dir = tmp / "input"
        input_dir.mkdir()
        shutil.copyfile(SCRIPT_YAML, input_dir / "script.yaml")

        # subset conversations + labeled_messages to gt chats
        with (data_dir / "conversations.jsonl").open("w", encoding="utf-8") as f:
            for cid in sorted(convos):
                f.write(json.dumps(convos[cid], ensure_ascii=False) + "\n")
        _filter_labeled(gt_ids, data_dir / "labeled_messages.jsonl")

        client = ClaudeClient(llm_mode=llm_mode, budget_usd=BUDGET)
        ctx = Context(
            db_path=REPO / "input" / "msgstore.db",
            script_path=SCRIPT_MD,
            data_dir=data_dir,
            output_dir=tmp / "out",
            prompts_dir=REPO / "prompts",
            input_dir=input_dir,
            script_yaml_path=input_dir / "script.yaml",
            llm_mode=llm_mode,
            budget_usd=BUDGET,
            force=True,
            dry_run=False,
            client=client,
        )

        detected = detect_conversions(ctx)
        by_cid = {cc.chat_id: cc for cc in detected}

        matches = 0
        denom = 0
        mismatches: list[tuple[int, str, str, str]] = []
        for row in gt:
            cc = by_cid.get(row["chat_id"])
            if cc is None:
                continue
            gt_out = row["outcome"]
            pred = cc.final_outcome
            if gt_out == "ambiguous" or pred == "ambiguous":
                continue
            denom += 1
            if gt_out == pred:
                matches += 1
            else:
                mismatches.append((row["chat_id"], gt_out, pred, cc.conversion_evidence))

        print()
        print(f"match: {matches}/{denom} (ambiguous on either side excluded)")
        if mismatches:
            print("mismatches:")
            for cid, gt_out, pred, evid in mismatches:
                print(f"  chat={cid} gt={gt_out} pred={pred} :: {evid[:120]}")

        usage = client.get_usage_report()
        api_cost = float(usage.get("api", {}).get("cost_usd", 0.0))
        max_calls = int(usage.get("max", {}).get("calls", 0))
        print(f"api_cost=${api_cost:.4f} max_calls={max_calls}")

        if matches < THRESHOLD:
            print(f"FAIL: matches {matches} < threshold {THRESHOLD}. "
                  "Tune prompts/stage6_conversion.md (see M3-T2) and re-run.",
                  file=sys.stderr)
            return 1
        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
