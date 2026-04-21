"""Tiny stage 4 MAX smoke: 3 templates, 1 customer batch. Verbose logs."""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from src.context import Context
from src.label import (
    LABEL_MODEL,
    SpaTemplateLabel,
    _build_user_msg,
    _read_prompt,
    _steps_summary,
)
from src.llm import ClaudeClient

REPO = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("stage4_mini")


def main() -> int:
    log.info("boot cwd=%s", Path.cwd())
    src = {p.name: p for p in [
        REPO / "data" / "conversations.jsonl",
        REPO / "data" / "spa_templates.json",
        REPO / "data" / "spa_message_template_map.json",
        REPO / "data" / "script.yaml",
    ]}
    for name, p in src.items():
        if not p.exists():
            log.error("missing %s", p)
            return 2
        log.info("have %s (%d bytes)", name, p.stat().st_size)

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        data_dir.mkdir(parents=True)
        for name, p in src.items():
            shutil.copyfile(p, data_dir / name)
        log.info("copied inputs → %s", data_dir)

        log.info("build ClaudeClient(max)")
        t0 = time.time()
        client = ClaudeClient(llm_mode="max", budget_usd=1.0)
        log.info("client ready in %.2fs", time.time() - t0)

        ctx = Context(
            db_path=REPO / "msgstore.db",
            script_path=REPO / "script-comercial.md",
            data_dir=data_dir,
            output_dir=Path(td) / "out",
            prompts_dir=REPO / "prompts",
            chat_limit=5,
            phones_filter=None,
            phones_hash=None,
            llm_mode="max",
            budget_usd=1.0,
            force=True,
            dry_run=False,
            client=client,
        )

        # ---- step A: single-template MAX call ----
        log.info("load prompt + steps summary")
        system = _read_prompt(ctx)
        steps_summary = _steps_summary(ctx)

        templates = json.loads((data_dir / "spa_templates.json").read_text(encoding="utf-8"))
        log.info("templates in file: %d", len(templates))
        templates = templates[:3]
        log.info("using first %d templates", len(templates))

        for i, tmpl in enumerate(templates):
            text = tmpl["canonical_text"]
            user_msg = _build_user_msg(steps_summary, text)
            log.info("call %d → MAX: text=%r (user_msg=%d chars)", i, text[:60], len(user_msg))
            t_call = time.time()
            result = client.complete(
                model=LABEL_MODEL,
                messages=[{"role": "user", "content": user_msg}],
                system=system,
                max_tokens=256,
                response_format=SpaTemplateLabel,
            )
            log.info("call %d done in %.2fs → %s", i, time.time() - t_call, result)

        usage = client.get_usage_report()
        log.info("usage=%s", usage)
        log.info("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
