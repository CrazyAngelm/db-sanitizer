"""Fail-closed batch generation and registry assignment."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from pydantic import ValidationError

from db_sanitizer.errors import GenerationError
from db_sanitizer.mapping import (
    HMACKey,
    MappingRegistry,
    NormalizationError,
    RegistryConflictError,
    normalize,
)
from db_sanitizer.policy.models import ConsistencyGroup

from .base import ReplacementProvider
from .models import GenerationRequest, GenerationResponse


@dataclass(frozen=True, slots=True)
class GenerationStats:
    """Safe aggregate evidence only; generated values are deliberately omitted."""

    batches: int
    accepted_items: int
    rejected_items: int
    duration_seconds: float


class ReplacementGenerator:
    """Generate synthetic mappings without reading source values from the registry."""

    def __init__(
        self,
        *,
        provider: ReplacementProvider,
        registry: MappingRegistry,
        hmac_key: HMACKey,
        batch_size: int,
        max_retries: int,
    ) -> None:
        if batch_size < 1 or max_retries < 0:
            raise GenerationError("generation batch settings are invalid")
        self._provider = provider
        self._registry = registry
        self._hmac_key = hmac_key
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._batches = 0
        self._accepted_items = 0
        self._rejected_items = 0
        self._duration_seconds = 0.0
        self._request_sequence = 0

    @property
    def stats(self) -> GenerationStats:
        return GenerationStats(
            batches=self._batches,
            accepted_items=self._accepted_items,
            rejected_items=self._rejected_items,
            duration_seconds=self._duration_seconds,
        )

    def generate_group(self, group_id: str, group: ConsistencyGroup) -> int:
        """Fill every pending key in stable HMAC order and return assignments made."""

        started = time.monotonic()
        assigned = 0
        try:
            while pending := self._registry.list_unassigned(group_id, limit=self._batch_size):
                self._generate_batch(group_id, group, pending)
                assigned += len(pending)
        finally:
            duration = time.monotonic() - started
            self._duration_seconds += duration
            self._registry.record_generation_stats(duration_seconds=duration)
        return assigned

    def _generate_batch(
        self, group_id: str, group: ConsistencyGroup, source_keys: list[str]
    ) -> None:
        accepted: list[str] = []
        accepted_hmacs: set[str] = set()
        for _ in range(self._max_retries + 1):
            try:
                remaining = len(source_keys) - len(accepted)
                self._request_sequence += 1
                request = GenerationRequest(
                    entity_type=group.entity_type,
                    locale=group.locale,
                    constraints=self._request_constraints(group, self._request_sequence),
                    # A bounded synthetic surplus compensates for invalid/source-colliding
                    # outputs without exposing any source value to the model.
                    count=self._request_count(remaining),
                )
                self._batches += 1
                self._registry.record_generation_stats(batches=1)
                raw_response = self._provider.generate(request)
                response = GenerationResponse.model_validate(raw_response)
                replacements, rejected = self._valid_replacements(
                    group_id,
                    group,
                    response,
                    remaining,
                    accepted_hmacs,
                )
                self._rejected_items += rejected
                self._registry.record_generation_stats(rejected_items=rejected)
                for replacement in replacements:
                    replacement_hmac = self._replacement_hmac(group_id, group, replacement)
                    accepted.append(replacement)
                    accepted_hmacs.add(replacement_hmac)
                if len(accepted) != len(source_keys):
                    continue
                assignments = [
                    (source_hmac, replacement, self._replacement_hmac(group_id, group, replacement))
                    for source_hmac, replacement in zip(source_keys, accepted, strict=True)
                ]
                self._registry.assign_replacements(
                    group_id,
                    assignments,
                    record_generated_items=True,
                )
                self._accepted_items += len(assignments)
                return
            except (GenerationError, RegistryConflictError, ValidationError):
                # Provider and validation details may contain model text; never expose them.
                continue
        raise GenerationError("unable to generate a valid replacement batch after retries")

    def _valid_replacements(
        self,
        group_id: str,
        group: ConsistencyGroup,
        response: GenerationResponse,
        needed: int,
        reserved_hmacs: set[str],
    ) -> tuple[list[str], int]:
        regex = re.compile(group.generation.regex) if group.generation.regex else None
        accepted: list[str] = []
        seen_hmacs = set(reserved_hmacs)
        rejected = 0
        for item in response.items:
            value = item.value
            if not self._is_valid_value(value, group, regex):
                rejected += 1
                continue
            replacement_hmac = self._replacement_hmac(group_id, group, value)
            if (
                replacement_hmac in seen_hmacs
                or self._registry.has_source_key(group_id, replacement_hmac)
                or self._registry.has_replacement_hmac(group_id, replacement_hmac)
            ):
                rejected += 1
                continue
            if len(accepted) >= needed:
                rejected += 1
                continue
            seen_hmacs.add(replacement_hmac)
            accepted.append(value)
        return accepted, rejected

    def _request_count(self, remaining: int) -> int:
        """Request a bounded surplus so invalid synthetic outputs do not waste a batch."""

        surplus = max(2, (remaining + 3) // 4)
        return min(1_000, self._batch_size * 2, remaining + surplus)

    @staticmethod
    def _request_constraints(group: ConsistencyGroup, sequence: int):
        """Add only non-sensitive variation to the permitted format description."""

        variation = (
            f" Synthetic generation batch {sequence}: return values distinct from common "
            "default examples and previous synthetic batches."
        )
        if group.entity_type.value == "phone":
            start = ((sequence - 1) % 9) * 100 + 100
            variation += (
                " For this batch use the three-digit block after '+7 000' in the range "
                f"{start:03d}-{start + 99:03d}."
            )
        elif group.entity_type.value == "email":
            variation += f" Use local parts beginning with synthetic{sequence}."
        return group.generation.model_copy(
            update={"description": group.generation.description + variation}
        )

    @staticmethod
    def _is_valid_value(value: str, group: ConsistencyGroup, regex: re.Pattern[str] | None) -> bool:
        return (
            bool(value)
            and group.generation.min_length <= len(value) <= group.generation.max_length
            and (regex is None or regex.fullmatch(value) is not None)
        )

    def _replacement_hmac(self, group_id: str, group: ConsistencyGroup, value: str) -> str:
        try:
            normalized = normalize(value, group.normalization.value, allow_empty=group.allow_empty)
        except NormalizationError as exc:
            raise GenerationError("model returned an invalid replacement") from exc
        return self._hmac_key.digest(group_id, normalized)


LLMGenerator = ReplacementGenerator
