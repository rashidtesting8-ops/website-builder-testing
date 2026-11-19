"""Provider-agnostic LLM adapter with disk cache, usage ledger, and budget enforcement.

Rules:
- Hard stop on budget limits (raises BudgetExceeded; never silently downgrades the model).
- Cache key includes system prompt, temperature, max_tokens, response_schema id, adapter version.
- Budget counters persisted in SQLite per project_id with thread locking (survives restart/resume).
- max_cost_usd with price table in config; pre-call estimate (BudgetExceeded before spending).
- Retries and timeouts count toward the call cap.
- Secrets redacted in exceptions, repr, and headers.
- Image paths must resolve strictly inside the project directory.
- FakeProvider included for isolated testing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests
from pydantic import BaseModel, ConfigDict, Field

ADAPTER_VERSION = "2.0"
_DB_LOCK = threading.Lock()

DEFAULT_PRICE_TABLE: dict[str, dict[str, float]] = {
    "default": {"prompt_per_1k": 0.0015, "completion_per_1k": 0.002},
    "gemini-1.5-pro": {"prompt_per_1k": 0.00125, "completion_per_1k": 0.005},
    "gemini-1.5-flash": {"prompt_per_1k": 0.000075, "completion_per_1k": 0.0003},
    "gpt-4o": {"prompt_per_1k": 0.005, "completion_per_1k": 0.015},
    "fake-model-v1": {"prompt_per_1k": 0.001, "completion_per_1k": 0.002},
    "openai/gpt-oss-120b": {"prompt_per_1k": 0.0, "completion_per_1k": 0.0},
    "openai/gpt-oss-20b": {"prompt_per_1k": 0.0, "completion_per_1k": 0.0},
    "qwen/qwen3.8-27b": {"prompt_per_1k": 0.0, "completion_per_1k": 0.0},
    "llama-3.3-70b-versatile": {"prompt_per_1k": 0.0, "completion_per_1k": 0.0},
    "llama-3.1-8b-instant": {"prompt_per_1k": 0.0, "completion_per_1k": 0.0},
    "llama-3.2-11b-vision-preview": {"prompt_per_1k": 0.0, "completion_per_1k": 0.0},
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gsk_[A-Za-z0-9_-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z-_]{35}"),
    re.compile(r"(?i)bearer\s+[a-zA-Z0-9._~+/-]+=*"),
]


class BudgetExceeded(Exception):
    """Raised when an LLM call would exceed the configured per-job call, token, or cost budget."""


class LLMResponse(BaseModel):
    """Normalized response payload from an LLM provider."""

    model_config = ConfigDict(extra="forbid")

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    cached: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.total_tokens == 0:
            self.total_tokens = self.prompt_tokens + self.completion_tokens


class LLMProvider(Protocol):
    """Interface protocol for LLM providers."""

    def complete(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Execute a text/vision completion call."""
        ...


class FakeProvider:
    """Deterministic, configurable fake provider for testing and offline runs."""

    def __init__(
        self,
        default_response: str = "Fake completion",
        canned_responses: Sequence[LLMResponse] | None = None,
        model_name: str = "fake-model-v1",
    ) -> None:
        self.default_response = default_response
        self._canned_responses = list(canned_responses) if canned_responses else []
        self.model_name = model_name
        self.call_count: int = 0
        self.recorded_calls: list[dict[str, Any]] = []

    def complete(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.call_count += 1
        active_model = model or self.model_name
        self.recorded_calls.append(
            {
                "prompt": prompt,
                "images_count": len(images),
                "model": active_model,
                "extra": kwargs,
            }
        )

        if self._canned_responses:
            resp = self._canned_responses.pop(0)
            if not resp.model:
                resp.model = active_model
            return resp

        # Default fallback response with realistic mock token counts
        prompt_tokens = max(1, len(prompt.split())) * 2
        completion_tokens = max(1, len(self.default_response.split())) * 2
        return LLMResponse(
            content=self.default_response,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            model=active_model,
        )


class AnthropicProvider:
    """Anthropic Claude provider implementation using Messages API."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "claude-3-5-sonnet-20241022",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key or get_env_api_key("ANTHROPIC_API_KEY")
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds

    def _build_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _build_request_payload(
        self,
        prompt: str,
        system_prompt: str = "",
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> dict[str, Any]:
        content_blocks: list[dict[str, Any]] = []
        for img in images:
            data_bytes = img.read_bytes() if isinstance(img, Path) else img
            media_type = "image/png" if data_bytes.startswith(b"\x89PNG") else "image/jpeg"
            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(data_bytes).decode("utf-8"),
                    },
                }
            )
        content_blocks.append({"type": "text", "text": prompt})

        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": content_blocks}],
        }
        if system_prompt:
            payload["system"] = system_prompt
        return payload

    def complete(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY environment variable")
        active_model = model or self.default_model
        payload = self._build_request_payload(prompt, images=images, model=active_model, **kwargs)
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=self._build_headers(),
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not resp.ok:
            raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text}")
        data = resp.json()
        text_parts = [
            c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"
        ]
        usage = data.get("usage", {})
        p_tokens = int(usage.get("input_tokens", 0))
        c_tokens = int(usage.get("output_tokens", 0))
        return LLMResponse(
            content="".join(text_parts),
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            model=active_model,
        )


class OpenAIProvider:
    """OpenAI GPT provider implementation using Chat Completions API."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "gpt-4o",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key or get_env_api_key("OPENAI_API_KEY")
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key or ''}",
            "Content-Type": "application/json",
        }

    def _build_request_payload(
        self,
        prompt: str,
        system_prompt: str = "",
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        user_content: list[dict[str, Any]] = []
        for img in images:
            data_bytes = img.read_bytes() if isinstance(img, Path) else img
            mime = "image/png" if data_bytes.startswith(b"\x89PNG") else "image/jpeg"
            b64 = base64.b64encode(data_bytes).decode("utf-8")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
        user_content.append({"type": "text", "text": prompt})
        messages.append({"role": "user", "content": user_content})

        return {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    def complete(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("Missing OPENAI_API_KEY environment variable")
        active_model = model or self.default_model
        payload = self._build_request_payload(prompt, images=images, model=active_model, **kwargs)
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=self._build_headers(),
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not resp.ok:
            raise RuntimeError(f"OpenAI API error ({resp.status_code}): {resp.text}")
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})
        p_tokens = int(usage.get("prompt_tokens", 0))
        c_tokens = int(usage.get("completion_tokens", 0))
        return LLMResponse(
            content=text,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            model=active_model,
        )


class GroqProvider(OpenAIProvider):
    """Groq provider using OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout_seconds: float = 60.0,
        base_url: str = "https://api.groq.com/openai/v1/chat/completions",
    ) -> None:
        key = api_key or get_env_api_key("GROQ_API_KEY")
        chosen_model = default_model or os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        super().__init__(api_key=key, default_model=chosen_model, timeout_seconds=timeout_seconds)
        self.base_url = base_url

    def complete(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("Missing GROQ_API_KEY environment variable")
        active_model = model or self.default_model
        payload = self._build_request_payload(prompt, images=images, model=active_model, **kwargs)
        resp = requests.post(
            self.base_url,
            headers=self._build_headers(),
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not resp.ok:
            raise RuntimeError(f"Groq API error ({resp.status_code}): {resp.text}")
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})
        p_tokens = int(usage.get("prompt_tokens", 0))
        c_tokens = int(usage.get("completion_tokens", 0))
        return LLMResponse(
            content=text,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            model=active_model,
        )


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter provider using OpenAI-compatible API (any model via OpenRouter)."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout_seconds: float = 60.0,
        base_url: str = "https://openrouter.ai/api/v1/chat/completions",
    ) -> None:
        key = api_key or get_env_api_key("OPENROUTER_API_KEY")
        chosen_model = default_model or os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o")
        super().__init__(api_key=key, default_model=chosen_model, timeout_seconds=timeout_seconds)
        self.base_url = base_url

    def complete(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("Missing OPENROUTER_API_KEY environment variable")
        active_model = model or self.default_model
        payload = self._build_request_payload(prompt, images=images, model=active_model, **kwargs)
        resp = requests.post(
            self.base_url,
            headers=self._build_headers(),
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not resp.ok:
            raise RuntimeError(f"OpenRouter API error ({resp.status_code}): {resp.text}")
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message") or {}
        # Reasoning models return content=null with the answer in `reasoning`.
        text = message.get("content") or message.get("reasoning") or ""
        usage = data.get("usage", {})
        p_tokens = int(usage.get("prompt_tokens", 0))
        c_tokens = int(usage.get("completion_tokens", 0))
        return LLMResponse(
            content=text,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            model=active_model,
        )


class GeminiProvider:
    """Google Gemini provider implementation using REST generateContent endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key or get_env_api_key("GEMINI_API_KEY")
        self.default_model = default_model or os.environ.get("GEMINI_MODEL", "gemini-1.5-pro")
        self.timeout_seconds = timeout_seconds

    def _build_url(self, model: str) -> str:
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self.api_key or ''}"
        )

    def _build_request_payload(
        self,
        prompt: str,
        system_prompt: str = "",
        images: Sequence[bytes | Path] = (),
        temperature: float = 0.2,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        for img in images:
            data_bytes = img.read_bytes() if isinstance(img, Path) else img
            mime = "image/png" if data_bytes.startswith(b"\x89PNG") else "image/jpeg"
            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime,
                        "data": base64.b64encode(data_bytes).decode("utf-8"),
                    }
                }
            )
        parts.append({"text": prompt})

        payload: dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        return payload

    def complete(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("Missing GEMINI_API_KEY environment variable")
        active_model = model or self.default_model
        url = self._build_url(active_model)
        payload = self._build_request_payload(prompt, images=images, **kwargs)
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout_seconds,
        )
        if not resp.ok:
            raise RuntimeError(f"Gemini API error ({resp.status_code}): {resp.text}")
        data = resp.json()
        candidates = data.get("candidates", [])
        text = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        metadata = data.get("usageMetadata", {})
        p_tokens = int(metadata.get("promptTokenCount", 0))
        c_tokens = int(metadata.get("candidatesTokenCount", 0))
        return LLMResponse(
            content=text,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            model=active_model,
        )


class BudgetConfig(BaseModel):
    """Budget ceilings, pricing tables, and cache/ledger paths for LLM execution."""

    model_config = ConfigDict(extra="forbid")

    max_calls_per_job: int = 30
    max_tokens_per_job: int = 150_000
    max_cost_usd: float = 10.0
    price_table: dict[str, dict[str, float]] = Field(
        default_factory=lambda: dict(DEFAULT_PRICE_TABLE)
    )
    cache_dir: Path = Field(default_factory=lambda: Path(".cache/llm"))
    ledger_path: Path = Field(default_factory=lambda: Path(".cache/llm_usage.jsonl"))
    db_path: Path = Field(default_factory=lambda: Path(".cache/llm_budget.sqlite3"))

    def model_post_init(self, __context: Any) -> None:
        # If db_path was left as default but cache_dir was customized, place db inside cache_dir
        is_default_db = self.db_path == Path(".cache/llm_budget.sqlite3")
        if is_default_db and self.cache_dir != Path(".cache/llm"):
            self.db_path = self.cache_dir / "llm_budget.sqlite3"


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


_DOTENV_LOADED = False


def load_env_file(dotenv_path: Path | None = None, *, force: bool = False) -> None:
    """Load key-value pairs from .env into os.environ once at module/startup time."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED and not force:
        return
    _DOTENV_LOADED = True
    target = dotenv_path or Path(".env")
    if not target.is_file():
        return
    try:
        content = target.read_text(encoding="utf-8")
        for line in content.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#") or "=" not in clean:
                continue
            k, v = clean.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and v and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


load_env_file()


def get_env_api_key(provider_env_var: str) -> str | None:
    """Read an API key strictly from environment variables."""
    val = os.environ.get(provider_env_var)
    if val and val.strip():
        return val.strip()
    return None


def redact_secrets(text: str) -> str:
    """Sanitize strings by redacting environment API keys and sensitive tokens."""
    sanitized = text
    # 1. Match known values from environment variables
    for env_var, env_val in os.environ.items():
        if any(term in env_var.upper() for term in ("KEY", "SECRET", "TOKEN", "AUTH", "PASS")):
            val_clean = env_val.strip()
            if len(val_clean) >= 4 and val_clean in sanitized:
                sanitized = sanitized.replace(val_clean, "[REDACTED]")

    # 2. Match common API key regex formats
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)

    return sanitized


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of HTTP headers with authorization credentials redacted."""
    safe_headers: dict[str, str] = {}
    for k, v in headers.items():
        if any(term in k.lower() for term in ("authorization", "key", "token", "secret")):
            safe_headers[k] = "[REDACTED]"
        else:
            safe_headers[k] = redact_secrets(v)
    return safe_headers


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    price_table: dict[str, dict[str, float]],
) -> float:
    """Calculate USD cost for prompt and completion token counts using price table."""
    rates = price_table.get(model) or price_table.get(
        "default", {"prompt_per_1k": 0.0015, "completion_per_1k": 0.002}
    )
    prompt_cost = (prompt_tokens / 1000.0) * rates.get("prompt_per_1k", 0.0015)
    completion_cost = (completion_tokens / 1000.0) * rates.get("completion_per_1k", 0.002)
    return prompt_cost + completion_cost


def estimate_tokens(
    prompt: str,
    system_prompt: str | None = None,
    images: Sequence[bytes | Path] = (),
) -> int:
    """Heuristic token estimation: ~2 tokens per word for text + 258 tokens per image."""
    full_text = f"{system_prompt or ''} {prompt}".strip()
    est = max(1, len(full_text.split())) * 2
    est += len(images) * 258
    return est


class LLMAdapter:
    """Provider-agnostic LLM adapter with caching, SQLite budget persistence, and redaction."""

    def __init__(
        self,
        provider: LLMProvider,
        config: BudgetConfig | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or BudgetConfig()
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.config.ledger_path.parent:
            self.config.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config.db_path.parent:
            self.config.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def __repr__(self) -> str:
        provider_name = getattr(self.provider, "model_name", self.provider.__class__.__name__)
        return (
            f"<LLMAdapter provider={redact_secrets(str(provider_name))} "
            f"version={ADAPTER_VERSION} db={self.config.db_path.name}>"
        )

    def _init_db(self) -> None:
        with _DB_LOCK:
            with sqlite3.connect(self.config.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS llm_budget (
                        project_id TEXT PRIMARY KEY,
                        calls_count INTEGER NOT NULL DEFAULT 0,
                        tokens_count INTEGER NOT NULL DEFAULT 0,
                        cost_usd REAL NOT NULL DEFAULT 0.0,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()

    def _get_budget_state(self, project_id: str) -> tuple[int, int, float]:
        with _DB_LOCK:
            with sqlite3.connect(self.config.db_path) as conn:
                row = conn.execute(
                    "SELECT calls_count, tokens_count, cost_usd "
                    "FROM llm_budget WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row:
                    return int(row[0]), int(row[1]), float(row[2])
                return 0, 0, 0.0

    def _record_call_attempt(self, project_id: str) -> int:
        now_iso = datetime.now(UTC).isoformat()
        with _DB_LOCK:
            with sqlite3.connect(self.config.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO llm_budget (
                        project_id, calls_count, tokens_count, cost_usd, updated_at
                    )
                    VALUES (?, 1, 0, 0.0, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        calls_count = calls_count + 1,
                        updated_at = excluded.updated_at
                    """,
                    (project_id, now_iso),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT calls_count FROM llm_budget WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                return int(row[0]) if row else 1

    def _record_completion_budget(
        self,
        project_id: str,
        added_tokens: int,
        added_cost: float,
    ) -> tuple[int, int, float]:
        now_iso = datetime.now(UTC).isoformat()
        with _DB_LOCK:
            with sqlite3.connect(self.config.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO llm_budget (
                        project_id, calls_count, tokens_count, cost_usd, updated_at
                    )
                    VALUES (?, 1, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        tokens_count = tokens_count + excluded.tokens_count,
                        cost_usd = cost_usd + excluded.cost_usd,
                        updated_at = excluded.updated_at
                    """,
                    (project_id, added_tokens, added_cost, now_iso),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT calls_count, tokens_count, cost_usd "
                    "FROM llm_budget WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row:
                    return int(row[0]), int(row[1]), float(row[2])
                return 1, added_tokens, added_cost

    def compute_cache_key(
        self,
        prompt: str,
        images: Sequence[bytes | Path] = (),
        model: str = "",
        *,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_schema_id: str | None = None,
        adapter_version: str = ADAPTER_VERSION,
    ) -> str:
        """Deterministic sha256 digest across prompt, schema, system, temp, version, and images."""
        hasher = hashlib.sha256()
        hasher.update(f"version:{adapter_version}\n".encode())
        hasher.update(f"model:{model}\n".encode())
        hasher.update(f"system:{system_prompt or ''}\n".encode())
        hasher.update(f"temp:{temperature:.4f}\n".encode())
        hasher.update(f"max_tokens:{max_tokens or ''}\n".encode())
        hasher.update(f"schema_id:{response_schema_id or ''}\n".encode())
        hasher.update(f"prompt:{prompt}\n".encode())

        for img in images:
            hasher.update(b"img:")
            if isinstance(img, str | Path):
                img_path = Path(img)
                if img_path.is_file():
                    hasher.update(hashlib.sha256(img_path.read_bytes()).digest())
                else:
                    hasher.update(f"missing:{img_path.name}".encode())
            elif isinstance(img, bytes):
                hasher.update(hashlib.sha256(img).digest())
            hasher.update(b"\n")

        return hasher.hexdigest()

    def _record_ledger_entry(
        self,
        *,
        project_id: str,
        stage: str,
        model: str,
        tokens: dict[str, int],
        latency_ms: float,
        cached: bool,
    ) -> None:
        """Append a clean usage record to the JSONL ledger without any credentials."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "project_id": project_id,
            "stage": stage,
            "model": model,
            "tokens": tokens,
            "latency_ms": round(latency_ms, 2),
            "cached": cached,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(self.config.ledger_path, "a", encoding="utf-8") as f:
            f.write(line)

    def complete(
        self,
        prompt: str,
        *,
        project_id: str,
        stage: str,
        images: Sequence[bytes | Path] = (),
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_schema: Any = None,
        response_schema_id: str | None = None,
        project_dir: str | Path | None = None,
        **extra_kwargs: Any,
    ) -> LLMResponse:
        """Execute completion through cache and provider, enforcing hard budget ceilings."""
        # 1. Enforce path traversal boundaries on images
        if project_dir is not None:
            resolved_proj = Path(project_dir).resolve()
            for img in images:
                if isinstance(img, str | Path):
                    img_path = Path(img).resolve()
                    if not img_path.is_relative_to(resolved_proj):
                        raise ValueError(
                            f"Path traversal rejected: image '{img}' resolves to '{img_path}', "
                            f"outside project directory '{resolved_proj}'"
                        )

        # Infer schema id if schema is supplied without an id
        schema_id = response_schema_id
        if schema_id is None and response_schema is not None:
            schema_id = getattr(response_schema, "__name__", str(response_schema))

        active_model = (
            model
            or getattr(self.provider, "model_name", None)
            or getattr(self.provider, "default_model", None)
            or "default-model"
        )
        # 1024 would exceed Groq free-tier OTPM (1000) on common text models.
        effective_max_tokens = max_tokens if max_tokens is not None else 800
        cache_key = self.compute_cache_key(
            prompt,
            images,
            active_model,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_schema_id=schema_id,
        )
        cache_file = self.config.cache_dir / f"{cache_key}.json"

        # 2. Check disk cache
        if cache_file.is_file():
            try:
                cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
                resp = LLMResponse(
                    content=str(cached_data["content"]),
                    prompt_tokens=int(cached_data.get("prompt_tokens", 0)),
                    completion_tokens=int(cached_data.get("completion_tokens", 0)),
                    total_tokens=int(cached_data.get("total_tokens", 0)),
                    model=str(cached_data.get("model", active_model)),
                    cached=True,
                )
                self._record_ledger_entry(
                    project_id=project_id,
                    stage=stage,
                    model=resp.model,
                    tokens={
                        "prompt": resp.prompt_tokens,
                        "completion": resp.completion_tokens,
                        "total": resp.total_tokens,
                    },
                    latency_ms=0.0,
                    cached=True,
                )
                return resp
            except (json.JSONDecodeError, KeyError, OSError):
                pass

        # 3. Read persisted budget state and check hard caps
        calls_used, tokens_used, cost_used = self._get_budget_state(project_id)
        if calls_used >= self.config.max_calls_per_job:
            raise BudgetExceeded(
                f"Per-job call cap exceeded ({calls_used}/{self.config.max_calls_per_job} "
                f"calls for project '{project_id}'). Never silently downgrading model."
            )

        if tokens_used >= self.config.max_tokens_per_job:
            raise BudgetExceeded(
                f"Per-job token ceiling exceeded ({tokens_used}/{self.config.max_tokens_per_job} "
                f"tokens for project '{project_id}'). Never silently downgrading model."
            )

        if cost_used >= self.config.max_cost_usd:
            raise BudgetExceeded(
                f"Per-job cost ceiling exceeded (${cost_used:.4f}/${self.config.max_cost_usd:.4f} "
                f"USD for project '{project_id}'). Never silently downgrading model."
            )

        # 4. Pre-call estimation: block before spending if estimate would exceed budget
        est_prompt_tokens = estimate_tokens(prompt, system_prompt, images)
        est_completion_tokens = (
            max_tokens if max_tokens is not None else max(100, est_prompt_tokens)
        )
        est_cost = calculate_cost(
            active_model, est_prompt_tokens, est_completion_tokens, self.config.price_table
        )

        if tokens_used + est_prompt_tokens > self.config.max_tokens_per_job:
            raise BudgetExceeded(
                f"Estimated tokens ({tokens_used} + {est_prompt_tokens} prompt tokens) exceed "
                f"per-job token ceiling ({self.config.max_tokens_per_job}) for '{project_id}'."
            )

        if cost_used + est_cost > self.config.max_cost_usd:
            raise BudgetExceeded(
                f"Estimated call cost ${est_cost:.4f} would exceed budget ceiling "
                f"(${cost_used:.4f} + ${est_cost:.4f} > ${self.config.max_cost_usd:.4f}) "
                f"for project '{project_id}'. Blocked before spending."
            )

        # 5. Record call attempt (retries and timeouts count toward call cap)
        self._record_call_attempt(project_id)

        # 6. Invoke provider with secret redaction on exceptions
        start_time = time.monotonic()
        try:
            response = self.provider.complete(
                prompt=prompt,
                images=images,
                model=active_model,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=effective_max_tokens,
                response_schema=response_schema,
                stage=stage,
                project_id=project_id,
                **extra_kwargs,
            )
        except Exception as exc:
            sanitized_err = redact_secrets(str(exc))
            raise RuntimeError(f"LLM provider error: {sanitized_err}") from None

        latency_ms = (time.monotonic() - start_time) * 1000.0

        # 7. Update persisted budget state
        actual_cost = calculate_cost(
            active_model,
            response.prompt_tokens,
            response.completion_tokens,
            self.config.price_table,
        )
        _, new_total_tokens, new_total_cost = self._record_completion_budget(
            project_id,
            response.total_tokens,
            actual_cost,
        )

        if new_total_tokens > self.config.max_tokens_per_job:
            raise BudgetExceeded(
                f"Per-job token ceiling exceeded ({new_total_tokens}/"
                f"{self.config.max_tokens_per_job} tokens for project '{project_id}')."
            )

        if new_total_cost > self.config.max_cost_usd:
            raise BudgetExceeded(
                f"Per-job cost ceiling exceeded (${new_total_cost:.4f}/"
                f"${self.config.max_cost_usd:.4f} USD for project '{project_id}')."
            )

        # 8. Atomic write to disk cache
        cache_payload = {
            "content": response.content,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.total_tokens,
            "model": response.model or active_model,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_text(cache_file, json.dumps(cache_payload, indent=2))

        # 9. Append record to JSONL usage ledger
        self._record_ledger_entry(
            project_id=project_id,
            stage=stage,
            model=response.model or active_model,
            tokens={
                "prompt": response.prompt_tokens,
                "completion": response.completion_tokens,
                "total": response.total_tokens,
            },
            latency_ms=latency_ms,
            cached=False,
        )

        return response


def get_default_adapter(
    project_id: str | None = None,
    config: BudgetConfig | None = None,
) -> LLMAdapter:
    """Instantiate the appropriate LLM adapter based on environment variables.

    An explicit ``LLM_PROVIDER`` environment variable (one of: anthropic, openai,
    gemini, groq, openrouter) overrides the default priority order, which is
    anthropic > openai > gemini > groq > openrouter.
    """
    cfg = config or BudgetConfig()
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if provider == "anthropic" and get_env_api_key("ANTHROPIC_API_KEY"):
        return LLMAdapter(AnthropicProvider(), config=cfg)
    if provider == "openai" and get_env_api_key("OPENAI_API_KEY"):
        return LLMAdapter(OpenAIProvider(), config=cfg)
    if provider == "gemini" and get_env_api_key("GEMINI_API_KEY"):
        return LLMAdapter(GeminiProvider(), config=cfg)
    if provider == "groq" and get_env_api_key("GROQ_API_KEY"):
        return LLMAdapter(GroqProvider(), config=cfg)
    if provider == "openrouter" and get_env_api_key("OPENROUTER_API_KEY"):
        return LLMAdapter(OpenRouterProvider(), config=cfg)
    # Default priority order
    if get_env_api_key("ANTHROPIC_API_KEY"):
        return LLMAdapter(AnthropicProvider(), config=cfg)
    if get_env_api_key("OPENAI_API_KEY"):
        return LLMAdapter(OpenAIProvider(), config=cfg)
    if get_env_api_key("GEMINI_API_KEY"):
        return LLMAdapter(GeminiProvider(), config=cfg)
    if get_env_api_key("GROQ_API_KEY"):
        return LLMAdapter(GroqProvider(), config=cfg)
    if get_env_api_key("OPENROUTER_API_KEY"):
        return LLMAdapter(OpenRouterProvider(), config=cfg)
    return LLMAdapter(FakeProvider(), config=cfg)
