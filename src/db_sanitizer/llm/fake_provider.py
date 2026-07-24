"""Deterministic provider for tests and offline workflow checks."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Any

from db_sanitizer.errors import GenerationError

from .models import GenerationRequest, GenerationResponse


class DeterministicSyntheticProvider:
    """Explicit test/performance provider; never selected as a silent fallback."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        entity = request.entity_type.value
        start = self._counts.get(entity, 0)
        self._counts[entity] = start + request.count
        return GenerationResponse(
            items=[
                {"value": self._value(entity, sequence)}
                for sequence in range(start + 1, start + request.count + 1)
            ]
        )

    @staticmethod
    def _value(entity: str, sequence: int) -> str:
        if entity == "person_name":
            return f"Анна Синтетическа{DeterministicSyntheticProvider._letters(sequence)}"
        if entity == "email":
            return f"synthetic{sequence}@example.test"
        if entity == "phone":
            return f"+7 000 {100 + sequence:03d}-00-{sequence % 100:02d}"
        if entity == "address":
            return f"\\u0433. Синтетический, ул. Проверочная, дом {sequence}"
        raise GenerationError("fake provider received unsupported entity type")

    @staticmethod
    def _letters(value: int) -> str:
        alphabet = "абвгдежзийклмнопрстуфхцчшщыэюя"
        result = ""
        while value:
            value, remainder = divmod(value - 1, len(alphabet))
            result = alphabet[remainder] + result
        return result


class FakeProvider:
    """Return preconfigured responses in order; it never invents replacements."""

    def __init__(self, responses: Iterable[GenerationResponse | object | Exception]) -> None:
        self._responses = deque(responses)
        self.requests: list[GenerationRequest] = []

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        if not self._responses:
            raise GenerationError("fake provider has no configured response")
        response: Any = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response
