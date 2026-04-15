from unittest.mock import AsyncMock, patch

import pytest
import httpx

from config import Settings
from llm import LLMError, OllamaLLMClient


def test_settings_load_ollama_defaults_without_secrets():
    settings = Settings(
        _env_file=None,
        database_url="postgresql://local/test",
    )

    assert settings.llm_provider == "ollama"
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    assert settings.ollama_model == "qwen2.5:1.5b"


@pytest.mark.asyncio
async def test_ollama_client_retries_then_raises_on_transport_failure():
    client = OllamaLLMClient(
        base_url="http://127.0.0.1:11434",
        model="qwen2.5:1.5b",
        timeout_seconds=1,
        max_retries=1,
    )

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.post = AsyncMock(side_effect=httpx.ConnectError("boom"))

    with patch("llm.httpx.AsyncClient", return_value=mock_http):
        try:
            await client.generate_text(messages=[])
        except LLMError as exc:
            assert str(exc) == "ollama_transport_failure"
        else:
            raise AssertionError("Expected LLMError")

    assert mock_http.post.await_count == 2


def test_ollama_extract_message_content_rejects_error_payload():
    with pytest.raises(LLMError, match="ollama_error:model not found"):
        OllamaLLMClient._extract_message_content({"error": "model not found"})
