"""Native Ollama Chat API implementation for optional local generation."""

from __future__ import annotations

import secrets
from typing import Any

import httpx
from pydantic import ValidationError

from db_sanitizer.errors import GenerationError

from .models import (
    GENERATION_RESPONSE_SCHEMA,
    SYSTEM_GENERATION_MESSAGE,
    GenerationRequest,
    GenerationResponse,
    generation_prompt,
)


class OllamaProvider:
    """Call a local Ollama endpoint without ever sending source data to it."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int = 120,
        temperature: float = 1.15,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise GenerationError("Ollama base URL must be an http(s) URL")
        if not model:
            raise GenerationError("Ollama model is not configured")
        if not 0 <= temperature <= 2:
            raise GenerationError("Ollama temperature must be between 0 and 2")
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self._model = model
        self._temperature = temperature
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OllamaProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Request one non-streaming, JSON-schema-constrained synthetic batch."""

        payload = {
            "model": self._model,
            "stream": False,
            "format": GENERATION_RESPONSE_SCHEMA,
            "messages": [
                {"role": "system", "content": SYSTEM_GENERATION_MESSAGE},
                {"role": "user", "content": generation_prompt(request)},
            ],
            "options": {
                "temperature": self._temperature,
                # Randomness is provider-only and contains no source-derived value.
                "seed": secrets.randbelow(2_147_483_647),
            },
        }
        try:
            response = self._client.post(self._url, json=payload)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            content = body["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not a string")
            return GenerationResponse.model_validate_json(content)
        except (
            httpx.HTTPError,
            KeyError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise GenerationError("Ollama returned an invalid structured response") from exc
