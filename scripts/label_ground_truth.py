"""Ground-truth labeling helper — M3-T1.

Loads `data/conversations.jsonl`, picks 20 chats stratified by message count
(6 short, 8 medium, 6 long), prints each chat, and prompts the user to label
the outcome. Writes incrementally to `data/ground_truth_outcomes.csv` so the
session is resumable.

Run: uv run python scripts/label_ground_truth.py
"""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONVERSATIONS = REPO / "data" / "conversations.jsonl"
OUT_CSV = REPO / "data" / "ground_truth_outcomes.csv"

SHORT_N = 6
MED_N = 8
LONG_N = 6
TARGET_N = SHORT_N + MED_N + LONG_N
SEED = 42

OUTCOME_MAP = {
    "b": "booked",
    "l": "lost",
    "a": "ambiguous",
}


def load_conversations(path: Path) -> list[dict]:
    if not path.exists():
        print(f"missing {path}", file=sys.stderr)
        sys.exit(2)
    chats = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            chats.append(json.loads(line))
    return chats


def stratify(chats: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split chats into short/medium/long by message-count tertiles."""
    if len(chats) < TARGET_N:
        print(
            f"only {len(chats)} chats in {CONVERSATIONS.name}; need >= {TARGET_N}",
            file=sys.stderr,
        )
        sys.exit(2)

    by_len = sorted(chats, key=lambda c: len(c["messages"]))
    n = len(by_len)
    lo = n // 3
    hi = 2 * n // 3
    short = by_len[:lo]
    medium = by_len[lo:hi]
    long_ = by_len[hi:]
    return short, medium, long_


def sample(bucket: list[dict], k: int, rng: random.Random) -> list[dict]:
    if len(bucket) < k:
        print(
            f"bucket has {len(bucket)} chats, need {k}",
            file=sys.stderr,
        )
        sys.exit(2)
    return rng.sample(bucket, k)


def load_labeled_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    ids: set[int] = set()
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ids.add(int(row["chat_id"]))
            except (KeyError, ValueError):
                continue
    return ids


def ensure_csv_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["chat_id", "phone", "outcome", "notes"])


def format_chat(chat: dict) -> str:
    lines = [
        f"chat_id={chat['chat_id']}  phone={chat['phone']}  msgs={len(chat['messages'])}",
        "-" * 60,
    ]
    for m in chat["messages"]:
        who = "ME" if m["from_me"] else "THEM"
        ts = datetime.fromtimestamp(m["ts_ms"] / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        text = m.get("text_raw") or m.get("text") or ""
        text = text.replace("\r", "").rstrip()
        lines.append(f"[{ts}] {who:<4}: {text}")
    return "\n".join(lines)


def prompt_outcome(chat: dict) -> tuple[str, str] | None:
    """Return (outcome, notes) or None if user wants to quit."""
    while True:
        try:
            raw = input("[b]ooked / [l]ost / [a]mbiguous / [s]kip / [q]uit: ").strip().lower()
        except EOFError:
            return None
        if raw == "q":
            return None
        if raw == "s":
            return ("", "")
        if raw in OUTCOME_MAP:
            try:
                notes = input("notes: ").strip()
            except EOFError:
                notes = ""
            return (OUTCOME_MAP[raw], notes)
        print("  invalid. try again.")


def append_row(path: Path, chat_id: int, phone: str, outcome: str, notes: str) -> None:
    with path.open("a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([chat_id, phone, outcome, notes])


def main() -> int:
    chats = load_conversations(CONVERSATIONS)
    short, medium, long_ = stratify(chats)

    rng = random.Random(SEED)
    picks = (
        sample(short, SHORT_N, rng)
        + sample(medium, MED_N, rng)
        + sample(long_, LONG_N, rng)
    )

    ensure_csv_header(OUT_CSV)
    already = load_labeled_ids(OUT_CSV)

    remaining = [c for c in picks if c["chat_id"] not in already]
    done = TARGET_N - len(remaining)
    print(f"{done}/{TARGET_N} already labeled in {OUT_CSV.name}")
    if not remaining:
        print("nothing to label.")
        return 0

    for i, chat in enumerate(remaining, start=1):
        print()
        print("=" * 72)
        print(f"chat {done + i}/{TARGET_N}")
        print(format_chat(chat))
        print("=" * 72)
        result = prompt_outcome(chat)
        if result is None:
            print("quit — progress saved.")
            return 0
        outcome, notes = result
        if outcome == "":
            print("skipped (not written).")
            continue
        append_row(OUT_CSV, chat["chat_id"], chat["phone"], outcome, notes)
        print(f"wrote: {chat['chat_id']} -> {outcome}")

    print()
    print(f"done. CSV: {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
