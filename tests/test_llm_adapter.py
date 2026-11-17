"""Tests for the provider-agnostic LLM adapter with disk cache, usage ledger, and budget cap."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engine.llm.adapter import (
    BudgetConfig,
    BudgetExceeded,
    FakeProvider,
    LLMAdapter,
    LLMResponse,
)


def test_cache_hit_avoids_provider_call(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    ledger_path = tmp_path / "ledger.jsonl"
    provider = FakeProvider(default_response="Generated hero copy")

    config = BudgetConfig(
        max_calls_per_job=10,
        max_tokens_per_job=10000,
        cache_dir=cache_dir,
        ledger_path=ledger_path,
    )
    adapter = LLMAdapter(provider=provider, config=config)

    # First call: executes provider and populates cache
    resp1 = adapter.complete(
        prompt="Write a hero headline for luxury dog grooming",
        project_id="test-paws",
        stage="art_direction",
        model="fake-model-v1",
    )
    assert resp1.content == "Generated hero copy"
    assert provider.call_count == 1

    # Second call with identical prompt and model: must hit disk cache and NOT call provider
    resp2 = adapter.complete(
        prompt="Write a hero headline for luxury dog grooming",
        project_id="test-paws",
        stage="art_direction",
        model="fake-model-v1",
    )
    assert resp2.content == "Generated hero copy"
    assert provider.call_count == 1  # Provider call count must NOT increase


def test_cache_hit_with_images_avoids_provider_call(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    ledger_path = tmp_path / "ledger.jsonl"
    provider = FakeProvider(default_response="Curated image ranking")
    config = BudgetConfig(cache_dir=cache_dir, ledger_path=ledger_path)
    adapter = LLMAdapter(provider=provider, config=config)

    img_file = tmp_path / "sample.jpg"
    img_file.write_bytes(b"fake-image-bytes-12345")

    resp1 = adapter.complete(
        prompt="Critique composition",
        images=[img_file],
        project_id="test-paws",
        stage="visual_curator",
    )
    assert resp1.content == "Curated image ranking"
    assert provider.call_count == 1

    # Same image and prompt hits cache
    resp2 = adapter.complete(
        prompt="Critique composition",
        images=[img_file],
        project_id="test-paws",
        stage="visual_curator",
    )
    assert resp2.content == "Curated image ranking"
    assert provider.call_count == 1

    # Different image bytes results in a cache miss and new provider call
    other_img = tmp_path / "other.jpg"
    other_img.write_bytes(b"different-image-bytes-67890")
    resp3 = adapter.complete(
        prompt="Critique composition",
        images=[other_img],
        project_id="test-paws",
        stage="visual_curator",
    )
    assert resp3.content == "Curated image ranking"
    assert provider.call_count == 2


def test_per_job_call_cap_enforced_with_budget_exceeded(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    ledger_path = tmp_path / "ledger.jsonl"
    provider = FakeProvider()

    # Cap set to exactly 2 calls for the job
    config = BudgetConfig(
        max_calls_per_job=2,
        max_tokens_per_job=100000,
        cache_dir=cache_dir,
        ledger_path=ledger_path,
    )
    adapter = LLMAdapter(provider=provider, config=config)

    adapter.complete("Prompt 1", project_id="capped-job", stage="stage1")
    adapter.complete("Prompt 2", project_id="capped-job", stage="stage2")

    # 3rd call must raise BudgetExceeded and never silently downgrade
    with pytest.raises(BudgetExceeded, match="call cap exceeded"):
        adapter.complete("Prompt 3", project_id="capped-job", stage="stage3")


def test_per_job_token_ceiling_enforced_with_budget_exceeded(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    ledger_path = tmp_path / "ledger.jsonl"
    provider = FakeProvider(
        canned_responses=[
            LLMResponse(content="First", prompt_tokens=300, completion_tokens=200),
            LLMResponse(content="Second", prompt_tokens=400, completion_tokens=200),
        ]
    )

    # Token ceiling is 600 tokens
    config = BudgetConfig(
        max_calls_per_job=10,
        max_tokens_per_job=600,
        cache_dir=cache_dir,
        ledger_path=ledger_path,
    )
    adapter = LLMAdapter(provider=provider, config=config)

    # First call: 500 total tokens (under 600 ceiling)
    adapter.complete("Prompt 1", project_id="token-job", stage="stage1")

    # Second call would exceed 600 tokens -> BudgetExceeded
    with pytest.raises(BudgetExceeded, match="token ceiling exceeded"):
        adapter.complete("Prompt 2", project_id="token-job", stage="stage2")


def test_usage_ledger_records_metadata_and_never_logs_api_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = tmp_path / "cache"
    ledger_path = tmp_path / "ledger.jsonl"
    secret_key = "sk-super-secret-production-key-998877"
    monkeypatch.setenv("LLM_API_KEY", secret_key)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-12345")

    provider = FakeProvider(
        default_response="Structured output",
        model_name="mock-gemini-pro",
    )
    config = BudgetConfig(cache_dir=cache_dir, ledger_path=ledger_path)
    adapter = LLMAdapter(provider=provider, config=config)

    adapter.complete(
        prompt="Analyze salon brand colors",
        project_id="ledger-test-project",
        stage="art_direction",
        model="mock-gemini-pro",
    )

    assert ledger_path.is_file()
    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["project_id"] == "ledger-test-project"
    assert entry["stage"] == "art_direction"
    assert entry["model"] == "mock-gemini-pro"
    assert "tokens" in entry
    assert "prompt" in entry["tokens"]
    assert "completion" in entry["tokens"]
    assert "total" in entry["tokens"]
    assert "latency_ms" in entry
    assert isinstance(entry["latency_ms"], int | float)

    # CRITICAL: Verify NO secret or API key appears anywhere in ledger content
    full_ledger_content = ledger_path.read_text(encoding="utf-8")
    assert secret_key not in full_ledger_content
    assert "gemini-secret-12345" not in full_ledger_content
    assert "api_key" not in full_ledger_content.lower()


def test_cap_survives_simulated_restart(tmp_path: Path) -> None:
    """Budget counters persisted in SQLite survive adapter re-instantiation."""
    db_path = tmp_path / "budget.sqlite3"
    cache_dir = tmp_path / "cache"
    ledger_path = tmp_path / "ledger.jsonl"
    config = BudgetConfig(
        max_calls_per_job=2,
        db_path=db_path,
        cache_dir=cache_dir,
        ledger_path=ledger_path,
    )

    provider1 = FakeProvider()
    adapter1 = LLMAdapter(provider=provider1, config=config)
    adapter1.complete("Prompt 1", project_id="persisted-job", stage="s1")
    adapter1.complete("Prompt 2", project_id="persisted-job", stage="s2")
    assert provider1.call_count == 2

    # Simulate restart: new adapter instance pointing to same db_path
    provider2 = FakeProvider()
    adapter2 = LLMAdapter(provider=provider2, config=config)
    with pytest.raises(BudgetExceeded, match="call cap exceeded"):
        adapter2.complete("Prompt 3", project_id="persisted-job", stage="s3")
    assert provider2.call_count == 0


def test_different_temperature_causes_cache_miss(tmp_path: Path) -> None:
    """Cache key includes temperature, so differing temperatures cause cache miss."""
    config = BudgetConfig(
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "ledger.jsonl",
        db_path=tmp_path / "budget.sqlite3",
    )
    provider = FakeProvider(default_response="Temperature response")
    adapter = LLMAdapter(provider=provider, config=config)

    # First call with temperature 0.0
    r1 = adapter.complete(
        "Headline prompt",
        project_id="temp-job",
        stage="s1",
        temperature=0.0,
    )
    assert r1.content == "Temperature response"
    assert provider.call_count == 1

    # Second call with same prompt but temperature 0.7 -> must miss cache and call provider
    r2 = adapter.complete(
        "Headline prompt",
        project_id="temp-job",
        stage="s1",
        temperature=0.7,
    )
    assert r2.content == "Temperature response"
    assert provider.call_count == 2


def test_provider_error_containing_key_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider exceptions leaking API keys or secrets are sanitized with [REDACTED]."""
    secret = "sk-super-secret-production-token-7766"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    class LeakyProvider:
        model_name = "leaky-model"

        def complete(
            self,
            prompt: str,
            images: Any = (),
            model: str | None = None,
            **kwargs: Any,
        ) -> LLMResponse:
            raise RuntimeError(f"401 Unauthorized for secret credentials: {secret}")

    config = BudgetConfig(
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "ledger.jsonl",
        db_path=tmp_path / "budget.sqlite3",
    )
    adapter = LLMAdapter(provider=LeakyProvider(), config=config)

    with pytest.raises(Exception) as exc_info:
        adapter.complete("Prompt", project_id="redact-job", stage="s1")

    err_msg = str(exc_info.value)
    assert secret not in err_msg
    assert "[REDACTED]" in err_msg


def test_path_traversal_rejected_for_image_paths(tmp_path: Path) -> None:
    """Images referencing paths outside project directory are rejected."""
    project_dir = tmp_path / "projects" / "test-salon"
    project_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside_secret.jpg"
    outside_file.write_bytes(b"outside-bytes")

    config = BudgetConfig(
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "ledger.jsonl",
        db_path=tmp_path / "budget.sqlite3",
    )
    adapter = LLMAdapter(provider=FakeProvider(), config=config)

    with pytest.raises(ValueError, match="traversal|outside"):
        adapter.complete(
            "Check image",
            images=[outside_file],
            project_id="test-salon",
            project_dir=project_dir,
            stage="s1",
        )


def test_cost_estimate_blocks_call_before_spending(tmp_path: Path) -> None:
    """If estimated cost exceeds max_cost_usd, BudgetExceeded is raised before provider call."""
    provider = FakeProvider(default_response="Expensive output")
    # Set max cost very low ($0.0001)
    config = BudgetConfig(
        max_cost_usd=0.0001,
        price_table={
            "expensive-model": {"prompt_per_1k": 1.0, "completion_per_1k": 2.0},
        },
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "ledger.jsonl",
        db_path=tmp_path / "budget.sqlite3",
    )
    adapter = LLMAdapter(provider=provider, config=config)

    with pytest.raises(BudgetExceeded, match="cost|budget"):
        adapter.complete(
            "A long prompt describing complex grooming services in extensive detail",
            project_id="cost-job",
            stage="s1",
            model="expensive-model",
        )

    # Provider MUST NOT have been called
    assert provider.call_count == 0


def test_retries_and_timeouts_count_toward_call_cap(tmp_path: Path) -> None:
    """Failed calls (timeouts or provider errors) consume call cap budget."""
    class FailingProvider:
        model_name = "failing-model"

        def complete(
            self,
            prompt: str,
            images: Any = (),
            model: str | None = None,
            **kwargs: Any,
        ) -> LLMResponse:
            raise TimeoutError("Simulated upstream provider timeout")

    config = BudgetConfig(
        max_calls_per_job=2,
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "ledger.jsonl",
        db_path=tmp_path / "budget.sqlite3",
    )
    adapter = LLMAdapter(provider=FailingProvider(), config=config)

    # 1st attempt fails
    with pytest.raises(Exception, match="timeout"):
        adapter.complete("Attempt 1", project_id="timeout-job", stage="s1")

    # 2nd attempt fails
    with pytest.raises(Exception, match="timeout"):
        adapter.complete("Attempt 2", project_id="timeout-job", stage="s1")

    # 3rd attempt is blocked by call cap before provider is even called
    with pytest.raises(BudgetExceeded, match="call cap exceeded"):
        adapter.complete("Attempt 3", project_id="timeout-job", stage="s1")


def test_groq_provider_builds_openai_payload_and_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GroqProvider must format OpenAI-compatible messages and connect to api.groq.com."""
    import requests

    from engine.llm.adapter import GroqProvider

    observed_requests: list[dict[str, Any]] = []

    class MockResponse:
        status_code = 200
        ok = True

        def json(self) -> dict[str, Any]:
            return {
                "choices": [{"message": {"content": "Fast Llama response from Groq"}}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 18, "total_tokens": 60},
            }

    def fake_post(url: str, **kwargs: Any) -> MockResponse:
        observed_requests.append({"url": url, **kwargs})
        return MockResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    provider = GroqProvider(api_key="gsk-test-mock-key-12345")
    res = provider.complete("Design luxury color palette", model="llama-3.3-70b-versatile")

    assert res.content == "Fast Llama response from Groq"
    assert res.prompt_tokens == 42
    assert res.completion_tokens == 18
    assert res.model == "llama-3.3-70b-versatile"

    assert len(observed_requests) == 1
    req = observed_requests[0]
    assert req["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer gsk-test-mock-key-12345"
    assert req["json"]["model"] == "llama-3.3-70b-versatile"
    assert req["json"]["messages"][0]["content"] == [
        {"type": "text", "text": "Design luxury color palette"}
    ]


def test_default_max_tokens_is_free_tier_safe(tmp_path: Path) -> None:
    """Default max_tokens must stay under common free-tier OTPM limits (e.g. Groq 1000)."""
    from collections.abc import Sequence

    from engine.llm.adapter import LLMAdapter

    class CapturingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(default_response="ok")
            self.seen_max_tokens = 0

        def complete(
            self,
            prompt: str,
            images: Sequence[bytes | Path] = (),
            model: str | None = None,
            **kwargs: object,
        ) -> LLMResponse:
            raw = kwargs.get("max_tokens")
            self.seen_max_tokens = int(raw) if isinstance(raw, int | str) else 0
            return super().complete(prompt, images, model, **kwargs)

    provider = CapturingProvider()
    adapter = LLMAdapter(provider=provider, config=BudgetConfig(cache_dir=tmp_path / "cache"))
    adapter.complete("prompt", project_id="p", stage="s")
    assert 0 < provider.seen_max_tokens <= 1000


def test_get_default_adapter_selects_groq_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_default_adapter must instantiate GroqProvider when GROQ_API_KEY is present."""
    from engine.llm.adapter import GroqProvider, get_default_adapter

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-active-free-key")

    adapter = get_default_adapter()
    assert isinstance(adapter.provider, GroqProvider)
    assert adapter.provider.api_key == "gsk-active-free-key"


