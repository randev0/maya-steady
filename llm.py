from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import structlog

from config import settings

log = structlog.get_logger()


class LLMError(Exception):
    """Raised when the configured LLM provider cannot return a usable result."""


@dataclass
class LLMMessage:
    role: str
    content: str


class BaseLLMClient:
    provider_name = "unknown"

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    async def generate_text(
        self,
        *,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        raise NotImplementedError

    async def generate_json(
        self,
        *,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        text = await self.generate_text(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("invalid_json_response") from exc
        if not isinstance(parsed, dict):
            raise LLMError("json_response_not_object")
        return parsed


class DisabledLLMClient(BaseLLMClient):
    provider_name = "disabled"

    def __init__(self, reason: str, model_name: str = "unavailable") -> None:
        self.reason = reason
        self._model = model_name

    @property
    def model_name(self) -> str:
        return self._model

    async def generate_text(
        self,
        *,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        raise LLMError(self.reason)


class OllamaLLMClient(BaseLLMClient):
    provider_name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)

    @property
    def model_name(self) -> str:
        return self._model

    @staticmethod
    def _extract_message_content(data: dict[str, Any]) -> str:
        if not isinstance(data, dict):
            raise LLMError("invalid_ollama_response")
        if data.get("error"):
            raise LLMError(f"ollama_error:{data['error']}")
        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            raise LLMError("empty_model_output")
        return content

    async def generate_text(
        self,
        *,
        messages: list[LLMMessage],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
                content = self._extract_message_content(data)
                return content
            except (httpx.HTTPError, httpx.TimeoutException, ValueError, LLMError) as exc:
                last_error = exc
                log.warning(
                    "ollama_request_failed",
                    attempt=attempt + 1,
                    max_attempts=self.max_retries + 1,
                    model=self._model,
                    error=str(exc),
                )
        raise LLMError("ollama_transport_failure") from last_error

    async def generate_json(
        self,
        *,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
                content = self._extract_message_content(data)
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise LLMError("json_response_not_object")
                return parsed
            except (httpx.HTTPError, httpx.TimeoutException, ValueError, LLMError) as exc:
                last_error = exc
                log.warning(
                    "ollama_json_request_failed",
                    attempt=attempt + 1,
                    max_attempts=self.max_retries + 1,
                    model=self._model,
                    error=str(exc),
                )
        raise LLMError("ollama_transport_failure") from last_error


def build_llm_client(provider: Optional[str] = None) -> BaseLLMClient:
    selected = (provider or settings.llm_provider or "ollama").strip().lower()
    if selected == "ollama":
        return OllamaLLMClient(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {selected}")
