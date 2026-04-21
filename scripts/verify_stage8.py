"""Smoke verifier for Stage 8.

See TECH_PLAN.md §M1-T6 acceptance. Runs Stage 8 on whatever artifacts
exist in `data/` (stubbed is fine for M1), then asserts:

  * `output/report.md` contains all 7 PT-BR H2 section headers.
  * All 5 CSVs exist with correct headers + UTF-8 round-trip.
  * Single Sonnet call cost < $0.30.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import report as report_mod  # noqa: E402
from src.context import Context  # noqa: E402


def main() -> int:
    ctx = Context.from_args([])

    result = report_mod.run(ctx)

    report_path = ctx.output_dir / "report.md"
    text = report_path.read_text(encoding="utf-8")

    missing = [h for h in report_mod.SECTION_HEADERS if h not in text]
    if missing:
        print(f"FAIL: missing section headers in report.md:\n  " + "\n  ".join(missing))
        return 1
    print(f"OK: all 7 section headers present ({len(text)} bytes)")

    for name, expected_header in report_mod.CSV_FILES.items():
        p = ctx.output_dir / name
        if not p.exists():
            print(f"FAIL: missing CSV {p}")
            return 1
        with p.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if not rows or rows[0] != expected_header:
            print(f"FAIL: {name} header mismatch: {rows[0] if rows else '(empty)'}")
            return 1
        # UTF-8 round-trip: re-read as bytes → decode
        raw = p.read_bytes().decode("utf-8")
        assert "�" not in raw, f"replacement char in {name}"
        print(f"OK: {name} header {expected_header}, {len(rows) - 1} rows")

    cost = result.get("llm_usd_api", 0.0) + result.get("llm_usd_max", 0.0)
    print(f"cost: api=${result.get('llm_usd_api', 0):.4f} max=${result.get('llm_usd_max', 0):.4f}")
    if result.get("llm_usd_api", 0.0) >= 0.30:
        print(f"FAIL: Stage 8 API cost ${result['llm_usd_api']:.4f} >= $0.30")
        return 1
    print("OK: cost under budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
