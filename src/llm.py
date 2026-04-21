"""Dual-client dispatcher: Max (claude-agent-sdk) primary, API fallback.

See TECH_PLAN.md §M0-T2 for the spec this module implements.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Type

import anthropic
from anthropic import APIConnectionError, APITimeoutError, RateLimitError
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from src.exceptions import BudgetExceeded, ConfigError, SchemaError

log = logging.getLogger(__name__)


def _extract_json(text: str) -> str:
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1]
    return s

# USD per 1M tokens, keyed by aliased (undated) model id.
PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {
        "input": 1.00,
        "output": 5.00,
        "cache_read": 0.10,
        "cache_write": 1.25,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
}


def _cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    p = PRICES.get(model)
    if p is None:
        return 0.0
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_read * p["cache_read"]
        + cache_write * p["cache_write"]
    ) / 1_000_000


class MaxRateLimitError(Exception):
    """Raised by MaxClient when the Agent SDK signals quota/rate exhaustion."""

    def __init__(self, message: str = "", reset_ts: Optional[float] = None):
        super().__init__(message or "max rate limited")
        self.reset_ts = reset_ts


class ApiRateLimitError(Exception):
    """Internal retryable marker — tests raise this instead of constructing
    an anthropic.RateLimitError (which needs a live httpx.Response)."""


@dataclass
class UsageDelta:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def _new_bucket(with_cost: bool = False) -> dict:
    b: dict[str, Any] = {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    if with_cost:
        b["cost_usd"] = 0.0
    return b


def _schema_as_tool(model_cls: Type[BaseModel]) -> dict:
    return {
        "name": f"emit_{model_cls.__name__}",
        "description": f"Emit a validated {model_cls.__name__} object",
        "input_schema": model_cls.model_json_schema(),
    }


class ApiClient:
    """Raw anthropic.Anthropic wrapper. Per-token pricing applies."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ConfigError("ApiClient requires api_key")
        self.api_key = api_key
        self._client = anthropic.Anthropic(api_key=api_key)

    def _complete(
        self,
        model: str,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_format: Optional[Type[BaseModel]],
    ) -> tuple[Any, UsageDelta]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        structured = response_format is not None and issubclass(response_format, BaseModel)
        if structured:
            tool = _schema_as_tool(response_format)
            kwargs["tools"] = [tool]
            kwargs["tool_choice"] = {"type": "tool", "name": tool["name"]}

        resp = self._client.messages.create(**kwargs)
        u = resp.usage
        delta = UsageDelta(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )

        if structured:
            tool_block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
            if tool_block is None:
                raise SchemaError("API response missing tool_use block")
            try:
                return response_format.model_validate(tool_block.input), delta
            except ValidationError as e:
                raise SchemaError(f"API tool_use failed schema validation: {e}") from e

        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
        return text, delta


class MaxClient:
    """claude-agent-sdk wrapper. OAuth session from `claude login`. Flat-rate."""

    def __init__(self):
        import claude_agent_sdk  # noqa: F401 — verify install

        self._sdk = claude_agent_sdk

    def _complete(
        self,
        model: str,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_format: Optional[Type[BaseModel]],
    ) -> tuple[Any, UsageDelta]:
        import anyio
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        prompt_parts: list[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c) for c in content
                )
            prompt_parts.append(f"[{role}]\n{content}")
        prompt = "\n\n".join(prompt_parts)

        structured = response_format is not None and issubclass(response_format, BaseModel)
        if structured:
            schema_json = json.dumps(response_format.model_json_schema(), ensure_ascii=False)
            prompt += (
                "\n\nReply ONLY with a single JSON object matching this schema "
                "(no prose, no code fences):\n" + schema_json
            )

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system or None,
            max_turns=1,
            allowed_tools=[],
        )

        text_out = ""
        usage = {"in": 0, "out": 0, "cr": 0, "cw": 0}

        async def _run():
            nonlocal text_out
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_out += block.text
                elif isinstance(msg, ResultMessage):
                    u = getattr(msg, "usage", None)
                    if u is None:
                        continue
                    get = (lambda k: u.get(k, 0)) if isinstance(u, dict) else (lambda k: getattr(u, k, 0) or 0)
                    usage["in"] = get("input_tokens") or 0
                    usage["out"] = get("output_tokens") or 0
                    usage["cr"] = get("cache_read_input_tokens") or 0
                    usage["cw"] = get("cache_creation_input_tokens") or 0

        try:
            anyio.run(_run)
        except Exception as e:
            msg = str(e).lower()
            if ("rate" in msg and "limit" in msg) or "quota" in msg or "exhaust" in msg:
                raise MaxRateLimitError(message=str(e)) from e
            raise

        delta = UsageDelta(
            input_tokens=usage["in"],
            output_tokens=usage["out"],
            cache_read_input_tokens=usage["cr"],
            cache_creation_input_tokens=usage["cw"],
        )

        if structured:
            raw = _extract_json(text_out)
            try:
                data = json.loads(raw)
                return response_format.model_validate(data), delta
            except (json.JSONDecodeError, ValidationError) as e:
                raise SchemaError(
                    f"Max response failed schema validation: {e}; raw={text_out!r}"
                ) from e

        return text_out, delta


def _detect_oauth() -> bool:
    """Heuristic: claude-agent-sdk stores an OAuth session under ~/.claude/."""
    candidates = [
        Path.home() / ".claude" / ".credentials.json",
        Path.home() / ".claude" / "credentials.json",
        Path.home() / ".config" / "claude" / "credentials.json",
    ]
    return any(p.exists() for p in candidates)


LlmMode = Literal["max", "api", "hybrid"]

# Retryable error tuple for the API path. Exposed as a module attribute so tests
# can extend if needed.
RETRYABLE_API = (RateLimitError, APIConnectionError, APITimeoutError, ApiRateLimitError, SchemaError)


class ClaudeClient:
    """Dispatcher with Max primary + API fallback in hybrid mode."""

    def __init__(
        self,
        *,
        llm_mode: LlmMode = "hybrid",
        budget_usd: float = 10.0,
        api_key: Optional[str] = None,
        has_oauth: Optional[bool] = None,
        max_client: Optional[Any] = None,
        api_client: Optional[Any] = None,
        max_retries: int = 5,
    ):
        load_dotenv()
        if api_key is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        if has_oauth is None:
            has_oauth = _detect_oauth()

        self.llm_mode = llm_mode
        self.budget_usd = budget_usd
        self.max_retries = max_retries
        self.max_exhausted = False
        self.reset_ts = 0.0
        self._usage = {
            "max": _new_bucket(with_cost=False),
            "api": _new_bucket(with_cost=True),
            "fallback_events": [],
        }

        oauth_available = has_oauth or max_client is not None
        api_available = bool(api_key) or api_client is not None

        if llm_mode == "hybrid":
            if not oauth_available and not api_available:
                raise ConfigError(
                    "hybrid mode needs OAuth session (run `claude login`) or ANTHROPIC_API_KEY"
                )
            self._max = max_client if max_client is not None else (MaxClient() if has_oauth else None)
            self._api = api_client if api_client is not None else (ApiClient(api_key) if api_key else None)
        elif llm_mode == "max":
            if not oauth_available:
                raise ConfigError("max mode requires OAuth session (run `claude login`)")
            self._max = max_client if max_client is not None else MaxClient()
            self._api = None
        elif llm_mode == "api":
            if not api_available:
                raise ConfigError("api mode requires ANTHROPIC_API_KEY")
            self._max = None
            self._api = api_client if api_client is not None else ApiClient(api_key)
        else:
            raise ConfigError(f"unknown llm_mode: {llm_mode!r}")

    # ---------- usage / budget ----------

    def _estimate_input_tokens(self, messages: list[dict], system: str) -> int:
        text = system or ""
        for m in messages:
            c = m.get("content", "")
            text += c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        return max(1, len(text) // 4)

    def _guard_budget(
        self, model: str, messages: list[dict], system: str, max_tokens: int
    ) -> None:
        est_in = self._estimate_input_tokens(messages, system)
        projected = _cost(model, est_in, max_tokens)
        spent = self._usage["api"]["cost_usd"]
        if spent + projected > self.budget_usd:
            raise BudgetExceeded(
                f"projected ${projected:.4f} + spent ${spent:.4f} > budget ${self.budget_usd:.4f}"
            )

    def _record(self, bucket: str, model: str, delta: UsageDelta) -> None:
        b = self._usage[bucket]
        b["calls"] += 1
        b["input_tokens"] += delta.input_tokens
        b["output_tokens"] += delta.output_tokens
        b["cache_read_input_tokens"] += delta.cache_read_input_tokens
        b["cache_creation_input_tokens"] += delta.cache_creation_input_tokens
        if bucket == "api":
            b["cost_usd"] += _cost(
                model,
                delta.input_tokens,
                delta.output_tokens,
                delta.cache_read_input_tokens,
                delta.cache_creation_input_tokens,
            )

    # ---------- path callers ----------

    def _api_call(
        self,
        model: str,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_format: Optional[Type[BaseModel]],
    ) -> Any:
        self._guard_budget(model, messages, system, max_tokens)
        attempt = 0
        delay = 1.0
        last_err: Optional[Exception] = None
        while attempt < self.max_retries:
            attempt += 1
            try:
                result, delta = self._api._complete(
                    model, messages, system, max_tokens, response_format
                )
                self._record("api", model, delta)
                return result
            except RETRYABLE_API as e:
                last_err = e
                if attempt >= self.max_retries:
                    raise
                time.sleep(delay)
                delay *= 2
        assert last_err is not None
        raise last_err  # pragma: no cover

    def _max_call(
        self,
        model: str,
        messages: list[dict],
        system: str,
        max_tokens: int,
        response_format: Optional[Type[BaseModel]],
        *,
        max_attempts: int,
    ) -> Any:
        attempt = 0
        delay = 1.0
        while True:
            attempt += 1
            try:
                result, delta = self._max._complete(
                    model, messages, system, max_tokens, response_format
                )
                self._record("max", model, delta)
                return result
            except MaxRateLimitError:
                if attempt >= max_attempts:
                    raise
                time.sleep(delay)
                delay *= 2

    # ---------- dispatcher ----------

    def complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 1024,
        response_format: Optional[Type[BaseModel]] = None,
    ) -> Any:
        if self.llm_mode == "api":
            return self._api_call(model, messages, system, max_tokens, response_format)

        if self.llm_mode == "max":
            return self._max_call(
                model, messages, system, max_tokens, response_format,
                max_attempts=self.max_retries,
            )

        # hybrid
        if self._max is None:
            return self._api_call(model, messages, system, max_tokens, response_format)
        if self.max_exhausted:
            if time.time() < self.reset_ts:
                return self._api_call(model, messages, system, max_tokens, response_format)
            # reset expired — try max again
            self.max_exhausted = False

        try:
            # Hybrid Max gets one attempt + one retry = 2 before falling back, per spec
            # ("don't burn all 5 attempts on Max").
            return self._max_call(
                model, messages, system, max_tokens, response_format,
                max_attempts=2,
            )
        except MaxRateLimitError as e:
            self.max_exhausted = True
            self.reset_ts = e.reset_ts if e.reset_ts else time.time() + 3600
            self._usage["fallback_events"].append(
                {"ts": time.time(), "reason": f"max_rate_limit: {e}", "model": model}
            )
            if self._api is None:
                raise
            return self._api_call(model, messages, system, max_tokens, response_format)
        except SchemaError as e:
            self._usage["fallback_events"].append(
                {"ts": time.time(), "reason": f"max_schema_error: {e}", "model": model}
            )
            if self._api is None:
                raise
            return self._api_call(model, messages, system, max_tokens, response_format)

    # ---------- reporting ----------

    def get_usage_report(self) -> dict:
        return {
            "max": dict(self._usage["max"]),
            "api": dict(self._usage["api"]),
            "fallback_events": list(self._usage["fallback_events"]),
        }

    def reset_usage(self) -> None:
        self._usage = {
            "max": _new_bucket(with_cost=False),
            "api": _new_bucket(with_cost=True),
            "fallback_events": [],
        }
