"""OpenRouter Chat Completions implementation of the replacement provider."""

from __future__ import annotations

import json
import secrets
from typing import Any

import httpx
from pydantic import ValidationError

from db_sanitizer.errors import GenerationError

from .models import GENERATION_RESPONSE_SCHEMA, GenerationRequest, GenerationResponse


class OpenRouterProvider:
    """Call OpenRouter without ever adding registry keys or source data to a request."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 120,
        temperature: float = 1.15,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise GenerationError("OpenRouter base URL must be an http(s) URL")
        if not api_key or not model:
            raise GenerationError("OpenRouter credentials or model are not configured")
        if not 0 <= temperature <= 2:
            raise GenerationError("OpenRouter temperature must be between 0 and 2")
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._temperature = temperature
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenRouterProvider:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Generate only synthetic replacement values. Return the requested JSON."
                    ),
                },
                {"role": "user", "content": self._prompt(request)},
            ],
            "temperature": self._temperature,
            # This changes only provider sampling; it contains no source data.
            "seed": secrets.randbelow(2_147_483_647),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "synthetic_replacements",
                    "strict": True,
                    "schema": GENERATION_RESPONSE_SCHEMA,
                },
            },
        }
        try:
            response = self._client.post(self._url, headers=self._headers, json=payload)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not a string")
            return GenerationResponse.model_validate_json(content)
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise GenerationError("OpenRouter returned an invalid structured response") from exc

    @staticmethod
    def _prompt(request: GenerationRequest) -> str:
        """Serialize precisely the permitted group metadata, and nothing else."""
        return json.dumps(
            {
                "entity_type": request.entity_type.value,
                "locale": request.locale,
                "constraints": request.constraints.model_dump(),
                "count": request.count,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
