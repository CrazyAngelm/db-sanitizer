"""Non-sensitive contracts used to request and validate synthetic replacements."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from db_sanitizer.policy.models import EntityType, GenerationConstraints


class GenerationRequest(BaseModel):
    """The complete, deliberately non-sensitive input sent to a provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: EntityType
    locale: Annotated[str, Field(min_length=2, max_length=32)]
    constraints: GenerationConstraints
    count: Annotated[int, Field(ge=1, le=1_000)]


class GeneratedItem(BaseModel):
    """One model-produced replacement before local validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str


class GenerationResponse(BaseModel):
    """Structured provider response.  Values are validated again by the generator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[GeneratedItem] = Field(min_length=1)


GENERATION_RESPONSE_SCHEMA = GenerationResponse.model_json_schema()

SYSTEM_GENERATION_MESSAGE = "Generate only synthetic replacement values. Return the requested JSON."


def generation_prompt(request: GenerationRequest) -> str:
    """Serialize only the non-sensitive provider input contract."""

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
