"""Offline tests for src/llm.py. No network, no OAuth check."""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import BaseModel

import src.llm as llm_mod
from src.exceptions import BudgetExceeded, ConfigError, SchemaError
from src.llm import (
    ApiRateLimitError,
    ClaudeClient,
    MaxRateLimitError,
    UsageDelta,
)


class FakeClient:
    """Stand-in for ApiClient/MaxClient with a scripted response queue."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def _complete(self, model, messages, system, max_tokens, response_format):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "system": system,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_a, **_k: None)


def _msg(text: str = "hi") -> list[dict]:
    return [{"role": "user", "content": text}]


# ----------------------------------------------------------------- retry


def test_retry_on_rate_limit_api_path():
    api = FakeClient(
        [
            ApiRateLimitError("429"),
            ApiRateLimitError("429"),
            ("ok", UsageDelta(input_tokens=10, output_tokens=5)),
        ]
    )
    c = ClaudeClient(llm_mode="api", api_client=api, api_key="x", budget_usd=100)
    out = c.complete("claude-haiku-4-5", _msg())
    assert out == "ok"
    assert len(api.calls) == 3
    rep = c.get_usage_report()
    assert rep["api"]["calls"] == 1
    assert rep["api"]["input_tokens"] == 10
    assert rep["api"]["output_tokens"] == 5


# ----------------------------------------------------------------- hybrid fallback


def test_hybrid_fallback_on_max_rate_limit():
    max_c = FakeClient([MaxRateLimitError("rate"), MaxRateLimitError("rate")])
    api_c = FakeClient([("from_api", UsageDelta(input_tokens=5, output_tokens=5))])
    c = ClaudeClient(
        llm_mode="hybrid",
        max_client=max_c,
        api_client=api_c,
        has_oauth=True,
        api_key="x",
        budget_usd=100,
    )
    out = c.complete("claude-haiku-4-5", _msg())
    assert out == "from_api"
    rep = c.get_usage_report()
    assert len(rep["fallback_events"]) == 1
    assert c.max_exhausted is True

    # Next call goes straight to API (max not attempted again until reset)
    api_c.responses.append(("api2", UsageDelta(input_tokens=1, output_tokens=1)))
    out2 = c.complete("claude-haiku-4-5", _msg())
    assert out2 == "api2"
    assert len(max_c.calls) == 2  # no new max calls


def test_hybrid_resume_max_after_reset():
    max_c = FakeClient(
        [
            MaxRateLimitError("rate"),
            MaxRateLimitError("rate"),
            ("max_back", UsageDelta(input_tokens=3, output_tokens=3)),
        ]
    )
    api_c = FakeClient([("api_once", UsageDelta(input_tokens=1, output_tokens=1))])
    c = ClaudeClient(
        llm_mode="hybrid",
        max_client=max_c,
        api_client=api_c,
        has_oauth=True,
        api_key="x",
        budget_usd=100,
    )
    assert c.complete("claude-haiku-4-5", _msg()) == "api_once"
    assert c.max_exhausted
    # Force reset into the past
    c.reset_ts = time.time() - 1
    out = c.complete("claude-haiku-4-5", _msg())
    assert out == "max_back"
    assert c.max_exhausted is False
    assert len(max_c.calls) == 3


# ----------------------------------------------------------------- max-only mode


def test_max_mode_no_fallback_propagates():
    max_c = FakeClient([MaxRateLimitError("r")] * 10)
    c = ClaudeClient(
        llm_mode="max",
        max_client=max_c,
        has_oauth=True,
        budget_usd=100,
        max_retries=3,
    )
    with pytest.raises(MaxRateLimitError):
        c.complete("claude-haiku-4-5", _msg())
    # 3 attempts, then raise
    assert len(max_c.calls) == 3


# ----------------------------------------------------------------- api-only skips max


def test_api_mode_skips_max(monkeypatch):
    tripwire = {"built": 0}

    class Tripwire:
        def __init__(self):
            tripwire["built"] += 1

        def _complete(self, *a, **k):
            tripwire["built"] += 100

    monkeypatch.setattr(llm_mod, "MaxClient", Tripwire)
    api_c = FakeClient([("ok", UsageDelta(input_tokens=1, output_tokens=1))])
    c = ClaudeClient(
        llm_mode="api", api_client=api_c, api_key="x", budget_usd=100,
    )
    c.complete("claude-haiku-4-5", _msg())
    assert tripwire["built"] == 0
    assert c._max is None


# ----------------------------------------------------------------- token split buckets


def test_token_accounting_split_buckets():
    max_c = FakeClient(
        [
            ("a", UsageDelta(input_tokens=100, output_tokens=50, cache_read_input_tokens=20, cache_creation_input_tokens=10)),
            ("b", UsageDelta(input_tokens=200, output_tokens=20)),
        ]
    )
    api_c = FakeClient(
        [("c", UsageDelta(input_tokens=30, output_tokens=10))]
    )
    c = ClaudeClient(
        llm_mode="hybrid",
        max_client=max_c,
        api_client=api_c,
        has_oauth=True,
        api_key="x",
        budget_usd=100,
    )
    c.complete("claude-haiku-4-5", _msg())
    c.complete("claude-haiku-4-5", _msg())
    # route next call to api
    c.max_exhausted = True
    c.reset_ts = time.time() + 1000
    c.complete("claude-haiku-4-5", _msg())

    rep = c.get_usage_report()
    assert rep["max"]["calls"] == 2
    assert rep["max"]["input_tokens"] == 300
    assert rep["max"]["output_tokens"] == 70
    assert rep["max"]["cache_read_input_tokens"] == 20
    assert rep["max"]["cache_creation_input_tokens"] == 10
    assert "cost_usd" not in rep["max"]  # flat-rate

    assert rep["api"]["calls"] == 1
    assert rep["api"]["input_tokens"] == 30
    assert rep["api"]["cost_usd"] > 0


# ----------------------------------------------------------------- budget API only


def test_budget_guard_api_path_only():
    max_c = FakeClient(
        [("ok", UsageDelta(input_tokens=9_999_999, output_tokens=999_999))] * 5
    )
    api_c = FakeClient([])
    c = ClaudeClient(
        llm_mode="hybrid",
        max_client=max_c,
        api_client=api_c,
        has_oauth=True,
        api_key="x",
        budget_usd=0.0001,
    )
    for _ in range(5):
        c.complete("claude-haiku-4-5", _msg())
    assert c.get_usage_report()["max"]["calls"] == 5
    assert c.get_usage_report()["api"]["cost_usd"] == 0.0

    # Now force API
    c.max_exhausted = True
    c.reset_ts = time.time() + 1000
    with pytest.raises(BudgetExceeded):
        c.complete("claude-haiku-4-5", _msg())


def test_budget_abort_pre_call():
    api_c = FakeClient([("unused", UsageDelta(input_tokens=1, output_tokens=1))])
    c = ClaudeClient(
        llm_mode="api", api_client=api_c, api_key="x", budget_usd=0.001,
    )
    long_msg = "x" * 20_000
    with pytest.raises(BudgetExceeded):
        c.complete(
            "claude-sonnet-4-6",
            [{"role": "user", "content": long_msg}],
            max_tokens=5000,
        )
    assert api_c.calls == []


# ----------------------------------------------------------------- structured output


class Foo(BaseModel):
    x: int


def test_structured_output_schema_both_paths():
    max_c = FakeClient([SchemaError("bad json on max")])
    # API retries up to max_retries (5) — queue 5 SchemaErrors
    api_c = FakeClient([SchemaError("bad tool on api")] * 5)
    c = ClaudeClient(
        llm_mode="hybrid",
        max_client=max_c,
        api_client=api_c,
        has_oauth=True,
        api_key="x",
        budget_usd=100,
    )
    with pytest.raises(SchemaError):
        c.complete("claude-haiku-4-5", _msg(), response_format=Foo)
    # One fallback event logged
    assert any("schema" in e["reason"] for e in c.get_usage_report()["fallback_events"])
    assert len(api_c.calls) == 5


# ----------------------------------------------------------------- config errors


def test_config_error_no_creds(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Also prevent .env load from supplying one
    monkeypatch.setattr(llm_mod, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(ConfigError):
        ClaudeClient(llm_mode="hybrid", has_oauth=False, api_key=None)

    with pytest.raises(ConfigError):
        ClaudeClient(llm_mode="max", has_oauth=False)

    with pytest.raises(ConfigError):
        ClaudeClient(llm_mode="api", api_key=None)
