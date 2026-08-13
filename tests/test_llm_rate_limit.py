"""Rate-limit handling must not permanently burn classify rows."""

from __future__ import annotations

from gmscraper.llm import RateLimitError, is_rate_limit_error, llm_max_concurrency


def test_is_rate_limit_error_detects_429() -> None:
    assert is_rate_limit_error("failed after 6 attempts: HTTP 429")
    assert is_rate_limit_error(RateLimitError("HTTP 429"))
    assert not is_rate_limit_error("schema mismatch: missing 'in_icp'")


def test_llm_max_concurrency_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "4")
    assert llm_max_concurrency() == 4
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "0")
    assert llm_max_concurrency() == 1
