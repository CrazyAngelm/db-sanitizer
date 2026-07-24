"""Provider boundary for non-sensitive synthetic replacement generation."""

from __future__ import annotations

from typing import Protocol

from .models import GenerationRequest, GenerationResponse


class ReplacementProvider(Protocol):
    """Generate structured replacements from group metadata only."""

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Return provider structured output or raise a safe generation error."""


LLMProvider = ReplacementProvider
