"""Pre-pipeline preparation: generate script.yaml from script-comercial.md.

Authoritative `input/script.yaml` is hand-authored by default, but this
module lets an LLM draft it from `input/script-comercial.md` when one
doesn't exist yet. User should review the output before running the
pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.context import Context
from src.script_index import load_script

log = logging.getLogger(__name__)

GEN_PROMPT_RELPATH = "prepare_generate_script.md"
GEN_MODEL = "claude-sonnet-4-6"
GEN_MAX_TOKENS = 8000


def _read_prompt(ctx: Context) -> str:
    p = ctx.prompts_dir / GEN_PROMPT_RELPATH
    if not p.exists():
        raise FileNotFoundError(f"missing prompt file: {p}")
    return p.read_text(encoding="utf-8")


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def generate_script_yaml(ctx: Context) -> Path:
    """LLM-generate `input/script.yaml` from `input/script-comercial.md`.

    Writes to `ctx.script_yaml_path`. Raises on missing input or schema
    failure. Overwrites existing file — caller must gate.
    """
    if ctx.client is None:
        raise RuntimeError("generate_script_yaml requires ctx.client")
    if not ctx.script_path.exists():
        raise FileNotFoundError(
            f"missing {ctx.script_path} — drop the Markdown script there first"
        )

    system = _read_prompt(ctx)
    script_md = ctx.script_path.read_text(encoding="utf-8")
    user_msg = (
        "## script-comercial.md\n\n```markdown\n"
        + script_md
        + "\n```\n\nProduza o `script.yaml` conforme instruído."
    )

    raw = ctx.client.complete(
        model=GEN_MODEL,
        messages=[{"role": "user", "content": user_msg}],
        system=system,
        max_tokens=GEN_MAX_TOKENS,
        response_format=None,
    )
    if not isinstance(raw, str):
        raise TypeError(f"expected str from LLM, got {type(raw).__name__}")

    yaml_text = _strip_fences(raw)

    parsed = yaml.safe_load(yaml_text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM output is not a YAML mapping")

    out = ctx.script_yaml_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml_text + ("\n" if not yaml_text.endswith("\n") else ""),
                   encoding="utf-8")

    load_script(out)
    log.info("prepare: wrote %s (validated)", out)
    return out
