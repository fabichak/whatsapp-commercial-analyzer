"""Shared Context dataclass + CLI argument parsing.

See TECH_PLAN.md §M0-T3 and §M1-T1 for spec.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Sequence

from src.exceptions import ConfigError
from src.llm import ClaudeClient

LlmMode = Literal["max", "api", "hybrid"]

_PHONE_RE = re.compile(r"^\d{10,15}$")
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_phones(path: Path) -> tuple[frozenset[str], str]:
    if not path.exists():
        raise ConfigError(f"phones file not found: {path}")
    phones: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _PHONE_RE.match(line):
            raise ConfigError(f"invalid phone in {path}: {line!r} (expected 10-15 digits)")
        phones.add(line)
    if not phones:
        raise ConfigError(f"phones file empty: {path}")
    frozen = frozenset(phones)
    h = hashlib.sha256(",".join(sorted(frozen)).encode("utf-8")).hexdigest()[:16]
    return frozen, h


@dataclass
class Context:
    db_path: Path
    script_path: Path
    data_dir: Path
    output_dir: Path
    prompts_dir: Path
    chat_limit: Optional[int] = None
    phones_filter: Optional[frozenset[str]] = None
    phones_hash: Optional[str] = None
    llm_mode: LlmMode = "hybrid"
    budget_usd: float = 10.0
    force: bool = False
    dry_run: bool = False
    client: Optional[ClaudeClient] = field(default=None, repr=False)

    @classmethod
    def from_args(
        cls,
        argv: Optional[Sequence[str]] = None,
        *,
        build_client: bool = True,
    ) -> "Context":
        p = argparse.ArgumentParser(prog="run_pipeline")
        p.add_argument("--stage", type=int, default=None)
        p.add_argument("--from", dest="from_stage", type=int, default=None)
        p.add_argument("--to", dest="to_stage", type=int, default=None)
        p.add_argument("--chat-limit", type=int, default=None)
        p.add_argument("--phones-file", type=Path, default=None)
        p.add_argument(
            "--llm-mode",
            choices=["max", "api", "hybrid"],
            default="hybrid",
        )
        p.add_argument("--budget-usd", type=float, default=10.0)
        p.add_argument("--force", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--db-path", type=Path, default=_REPO_ROOT / "msgstore.db")
        p.add_argument("--script-path", type=Path, default=_REPO_ROOT / "script-comercial.md")
        p.add_argument("--data-dir", type=Path, default=_REPO_ROOT / "data")
        p.add_argument("--output-dir", type=Path, default=_REPO_ROOT / "output")
        p.add_argument("--prompts-dir", type=Path, default=_REPO_ROOT / "prompts")

        ns = p.parse_args(list(argv) if argv is not None else None)

        if ns.chat_limit is not None and ns.phones_file is not None:
            p.error("--chat-limit and --phones-file are mutually exclusive")

        phones_filter: Optional[frozenset[str]] = None
        phones_hash: Optional[str] = None
        if ns.phones_file is not None:
            phones_filter, phones_hash = _load_phones(ns.phones_file)

        ns.data_dir.mkdir(parents=True, exist_ok=True)
        ns.output_dir.mkdir(parents=True, exist_ok=True)

        client = ClaudeClient(llm_mode=ns.llm_mode, budget_usd=ns.budget_usd) if build_client else None

        return cls(
            db_path=ns.db_path,
            script_path=ns.script_path,
            data_dir=ns.data_dir,
            output_dir=ns.output_dir,
            prompts_dir=ns.prompts_dir,
            chat_limit=ns.chat_limit,
            phones_filter=phones_filter,
            phones_hash=phones_hash,
            llm_mode=ns.llm_mode,
            budget_usd=ns.budget_usd,
            force=ns.force,
            dry_run=ns.dry_run,
            client=client,
        )
