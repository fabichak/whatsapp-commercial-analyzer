"""Shared Context dataclass + CLI argument parsing.

See TECH_PLAN.md §M0-T3, §M1-T1, and Revision v4 (input/ folder + resume).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Sequence

import yaml

from src.exceptions import ConfigError
from src.llm import ClaudeClient

LlmMode = Literal["max", "api", "hybrid"]

_PHONE_RE = re.compile(r"^\d{10,15}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DMY_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_CONFIG_FILENAME = "pipeline_config.yaml"


def parse_user_date(raw: str) -> str:
    """Accept dd.mm.yyyy or yyyy-mm-dd, return ISO yyyy-mm-dd. Empty → ''."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = _DMY_RE.match(s)
    if m:
        d, mo, y = m.groups()
        s = f"{y}-{mo}-{d}"
    if not _ISO_DATE_RE.match(s):
        raise ConfigError(f"invalid date {raw!r}: expected dd.mm.yyyy or yyyy-mm-dd")
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise ConfigError(f"invalid date {raw!r}: {e}") from e
    return s


def iso_date_to_ms(iso: str) -> int:
    dt = datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def format_iso_as_dmy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def _load_pipeline_config(path: Path) -> Optional[str]:
    """Return ISO from_date string from pipeline_config.yaml, or None."""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid {path}: {e}") from e
    if not isinstance(data, dict):
        return None
    raw = data.get("from_date")
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    return parse_user_date(raw) or None


def write_pipeline_config(path: Path, from_date_iso: Optional[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# pipeline_config.yaml — runtime filters for the analysis pipeline.\n"
        "# Edit via `scripts.prepare`. `from_date` accepts yyyy-mm-dd; empty = no filter.\n"
    )
    val = from_date_iso if from_date_iso else ""
    path.write_text(f"{header}from_date: '{val}'\n", encoding="utf-8")


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
    from_date: Optional[str] = None,
) -> str:
    parts = [
        f"db={_hash_file(db_path)}",
        f"md={_hash_file(script_md)}",
        f"yaml={_hash_file(script_yaml)}",
        f"labels={labels_hash}",
        f"from_date={from_date or 'none'}",
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
    pipeline_config_path: Optional[Path] = None
    from_date: Optional[str] = None  # ISO yyyy-mm-dd
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
        if self.pipeline_config_path is None:
            self.pipeline_config_path = self.input_dir / PIPELINE_CONFIG_FILENAME
        if not self.excluded_labels and self.labels_hash == "none":
            self.excluded_labels, self.labels_hash = _load_excluded_labels(
                self.excluded_labels_path
            )
        if self.from_date is None:
            self.from_date = _load_pipeline_config(self.pipeline_config_path)
        if self.input_hash is None:
            self.input_hash = compute_input_hash(
                self.db_path,
                self.script_path,
                self.script_yaml_path,
                self.labels_hash,
                self.from_date,
            )

    @property
    def from_date_ms(self) -> Optional[int]:
        return iso_date_to_ms(self.from_date) if self.from_date else None

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
        p.add_argument("--from-date", type=str, default=None,
                       help="filter messages on/after this date (dd.mm.yyyy or yyyy-mm-dd); "
                            "overrides input/pipeline_config.yaml")

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

        pipeline_config_path = ns.input_dir / PIPELINE_CONFIG_FILENAME
        if ns.from_date is not None:
            from_date = parse_user_date(ns.from_date) or None
        else:
            from_date = _load_pipeline_config(pipeline_config_path)

        input_hash = compute_input_hash(
            db_path, script_path, script_yaml, labels_hash, from_date
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
            pipeline_config_path=pipeline_config_path,
            from_date=from_date,
            llm_mode=ns.llm_mode,
            budget_usd=ns.budget_usd,
            force=ns.force,
            restart=restart,
            dry_run=ns.dry_run,
            client=client,
        )
