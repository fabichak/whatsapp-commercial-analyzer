"""Probe MAX with minimal prompts. Test model, size, structured-output."""
from __future__ import annotations

import logging
import sys
import time

from pydantic import BaseModel

from src.llm import ClaudeClient

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("probe")


class Tag(BaseModel):
    mood: str


def call(client, label, model, text, structured=False, max_tokens=64, timeout=60):
    log.info("---- %s: model=%s text_len=%d structured=%s ----", label, model, len(text), structured)
    t0 = time.time()
    try:
        res = client.complete(
            model=model,
            messages=[{"role": "user", "content": text}],
            system="Reply briefly.",
            max_tokens=max_tokens,
            response_format=Tag if structured else None,
        )
        log.info("%s done %.2fs → %r", label, time.time() - t0, res)
    except Exception as e:
        log.exception("%s FAILED after %.2fs: %s", label, time.time() - t0, e)


def main():
    client = ClaudeClient(llm_mode="max", budget_usd=1.0)
    call(client, "A sonnet short", "claude-sonnet-4-5", "Say 'ok'.")
    call(client, "B haiku short", "claude-haiku-4-5", "Say 'ok'.")
    call(client, "C haiku structured", "claude-haiku-4-5", "I am happy. Reply JSON.", structured=True)
    call(client, "D haiku medium", "claude-haiku-4-5", "hello\n" * 400)
    log.info("usage=%s", client.get_usage_report())


if __name__ == "__main__":
    main()
