"""Shared Context dataclass + CLI argument parsing.

See TECH_PLAN.md §M0-T3, §M1-T1, and Revision v4 (input/ folder + resume).
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


def _hash_file(path: Path) -> str:
    if not path.exists():
        return "missing"
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_excluded_labels(path: Path) -> tuple[frozenset[str], str]:
    if not path.exists():
        return frozenset(), "none"
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.add(line)
    if not names:
        return frozenset(), "none"
    frozen = frozenset(names)
    h = hashlib.sha256(",".join(sorted(frozen)).encode("utf-8")).hexdigest()[:16]
    return frozen, h


def compute_input_hash(
    db_path: Path,
    script_md: Path,
    script_yaml: Path,
    labels_hash: str = "none",
) -> str:
    parts = [
        f"db={_hash_file(db_path)}",
        f"md={_hash_file(script_md)}",
        f"yaml={_hash_file(script_yaml)}",
        f"labels={labels_hash}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class Context:
    db_path: Path
    script_path: Path
    data_dir: Path
    output_dir: Path
    prompts_dir: Path
    input_dir: Path = field(default_factory=lambda: _REPO_ROOT / "input")
    script_yaml_path: Optional[Path] = None
    input_hash: Optional[str] = None
    chat_limit: Optional[int] = None
    phones_filter: Optional[frozenset[str]] = None
    phones_hash: Optional[str] = None
    excluded_labels: frozenset[str] = field(default_factory=frozenset)
    labels_hash: str = "none"
    excluded_labels_path: Optional[Path] = None
    llm_mode: LlmMode = "hybrid"
    budget_usd: float = 10.0
    force: bool = False
    restart: bool = False
    dry_run: bool = False
    client: Optional[ClaudeClient] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.script_yaml_path is None:
            self.script_yaml_path = self.input_dir / "script.yaml"
        if self.excluded_labels_path is None:
            self.excluded_labels_path = self.input_dir / "excluded-labels.txt"
        if not self.excluded_labels and self.labels_hash == "none":
            self.excluded_labels, self.labels_hash = _load_excluded_labels(
                self.excluded_labels_path
            )
        if self.input_hash is None:
            self.input_hash = compute_input_hash(
                self.db_path,
                self.script_path,
                self.script_yaml_path,
                self.labels_hash,
            )

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
        p.add_argument("--force", action="store_true",
                       help="deprecated alias for --restart")
        p.add_argument("--restart", action="store_true",
                       help="discard prior sentinels + LLM cache; run from scratch")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--input-dir", type=Path, default=_REPO_ROOT / "input")
        p.add_argument("--db-path", type=Path, default=None)
        p.add_argument("--script-path", type=Path, default=None)
        p.add_argument("--script-yaml", type=Path, default=None)
        p.add_argument("--data-dir", type=Path, default=_REPO_ROOT / "data")
        p.add_argument("--output-dir", type=Path, default=_REPO_ROOT / "output")
        p.add_argument("--prompts-dir", type=Path, default=_REPO_ROOT / "prompts")

        ns = p.parse_args(list(argv) if argv is not None else None)

        if ns.chat_limit is not None and ns.phones_file is not None:
            p.error("--chat-limit and --phones-file are mutually exclusive")

        db_path = ns.db_path if ns.db_path is not None else ns.input_dir / "msgstore.db"
        script_path = ns.script_path if ns.script_path is not None else ns.input_dir / "script-comercial.md"
        script_yaml = ns.script_yaml if ns.script_yaml is not None else ns.input_dir / "script.yaml"

        phones_filter: Optional[frozenset[str]] = None
        phones_hash: Optional[str] = None
        if ns.phones_file is not None:
            phones_filter, phones_hash = _load_phones(ns.phones_file)

        ns.data_dir.mkdir(parents=True, exist_ok=True)
        ns.output_dir.mkdir(parents=True, exist_ok=True)

        excluded_labels_path = ns.input_dir / "excluded-labels.txt"
        excluded_labels, labels_hash = _load_excluded_labels(excluded_labels_path)

        input_hash = compute_input_hash(
            db_path, script_path, script_yaml, labels_hash
        )

        restart = ns.restart or ns.force
        client = None
        if build_client:
            client = ClaudeClient(llm_mode=ns.llm_mode, budget_usd=ns.budget_usd)
            cache_dir = ns.data_dir / "llm_cache"
            client.set_cache(cache_dir, input_hash)

        return cls(
            db_path=db_path,
            script_path=script_path,
            data_dir=ns.data_dir,
            output_dir=ns.output_dir,
            prompts_dir=ns.prompts_dir,
            input_dir=ns.input_dir,
            script_yaml_path=script_yaml,
            input_hash=input_hash,
            chat_limit=ns.chat_limit,
            phones_filter=phones_filter,
            phones_hash=phones_hash,
            excluded_labels=excluded_labels,
            labels_hash=labels_hash,
            excluded_labels_path=excluded_labels_path,
            llm_mode=ns.llm_mode,
            budget_usd=ns.budget_usd,
            force=ns.force,
            restart=restart,
            dry_run=ns.dry_run,
            client=client,
        )
