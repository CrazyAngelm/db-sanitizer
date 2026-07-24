"""Pydantic contracts for the explicit DB Sanitizer YAML policy."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")

Identifier = Annotated[str, Field(min_length=1, max_length=63)]
EnvironmentName = Annotated[str, Field(min_length=1, max_length=128)]


def _validate_artifact_leaf_name(value: str) -> str:
    """Keep generated artifacts inside an isolated run directory.

    These fields are documented as filenames/directory names, not arbitrary
    paths.  Reject both POSIX and Windows separators so policy behavior remains
    safe when a policy is authored on one platform and run in a container on
    another.
    """

    if (
        value in {".", ".."}
        or Path(value).is_absolute()
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("must be a single safe artifact filename")
    return value


class EntityType(StrEnum):
    PERSON_NAME = "person_name"
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"


class NormalizationType(StrEnum):
    HUMAN_TEXT = "human_text"
    EMAIL = "email"
    PHONE = "phone"


class ColumnRef(BaseModel):
    """A fully qualified policy-owned scalar text column."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Identifier = Field(alias="schema")
    table: Identifier
    column: Identifier

    @field_validator("schema_name", "table", "column")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("must be a safe PostgreSQL identifier")
        return value

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.schema_name, self.table, self.column)

    @property
    def display_name(self) -> str:
        return f"{self.schema_name}.{self.table}.{self.column}"


class GenerationConstraints(BaseModel):
    """Non-sensitive format limits supplied to the LLM and batch validator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: Annotated[str, Field(min_length=1, max_length=500)]
    min_length: Annotated[int, Field(ge=1, le=10_000)]
    max_length: Annotated[int, Field(ge=1, le=10_000)]
    regex: str | None = None

    @model_validator(mode="after")
    def validate_lengths_and_regex(self) -> GenerationConstraints:
        if self.min_length > self.max_length:
            raise ValueError("min_length must not exceed max_length")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as exc:
                raise ValueError("regex is invalid") from exc
        return self


class ConsistencyGroup(BaseModel):
    """Columns that must share one normalized-HMAC mapping namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: EntityType
    locale: Annotated[str, Field(min_length=2, max_length=32)]
    normalization: NormalizationType
    allow_empty: bool = False
    generation: GenerationConstraints
    columns: Annotated[tuple[ColumnRef, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_semantics(self) -> ConsistencyGroup:
        expected_normalization = {
            EntityType.PERSON_NAME: NormalizationType.HUMAN_TEXT,
            EntityType.EMAIL: NormalizationType.EMAIL,
            EntityType.PHONE: NormalizationType.PHONE,
            EntityType.ADDRESS: NormalizationType.HUMAN_TEXT,
        }[self.entity_type]
        if self.normalization != expected_normalization:
            raise ValueError("entity_type requires its defined normalization")
        if len({column.key for column in self.columns}) != len(self.columns):
            raise ValueError("a group cannot contain the same column twice")
        return self


class RunSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: Path = Path(".runs")
    allow_target_recreate: bool
    collector_fetch_size: Annotated[int, Field(ge=1, le=100_000)] = 1_000
    verifier_fetch_size: Annotated[int, Field(ge=1, le=100_000)] = 1_000


class ConnectionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_dsn_env: EnvironmentName
    target_dsn_env: EnvironmentName

    @field_validator("source_dsn_env", "target_dsn_env")
    @classmethod
    def validate_env_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value


class MappingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_filename: Annotated[str, Field(min_length=1, max_length=255)] = "mappings.sqlite3"
    hmac_key_env: EnvironmentName
    hmac_algorithm: Literal["sha256"] = "sha256"

    @field_validator("registry_filename")
    @classmethod
    def validate_registry_filename(cls, value: str) -> str:
        return _validate_artifact_leaf_name(value)

    @field_validator("hmac_key_env")
    @classmethod
    def validate_env_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value


class BaseLLMSettings(BaseModel):
    """Shared, non-sensitive contract for a concrete replacement provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url_env: EnvironmentName
    model: Annotated[str, Field(min_length=1, max_length=255)]
    temperature: Annotated[float, Field(ge=0, le=2)] = 1.15
    batch_size: Annotated[int, Field(ge=1, le=1_000)] = 25
    max_retries: Annotated[int, Field(ge=0, le=20)] = 3
    timeout_seconds: Annotated[int, Field(ge=1, le=3_600)] = 120
    structured_output: Literal[True] = True

    @field_validator("base_url_env")
    @classmethod
    def validate_base_url_env_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value


class OpenRouterLLMSettings(BaseLLMSettings):
    """Default remote provider selected explicitly by the demo policy."""

    provider: Literal["openrouter"]
    api_key_env: EnvironmentName

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("must be an uppercase environment variable name")
        return value


class OllamaLLMSettings(BaseLLMSettings):
    """Optional local Ollama provider; it never needs an API key."""

    provider: Literal["ollama"]


LLMSettings = Annotated[
    OpenRouterLLMSettings | OllamaLLMSettings,
    Field(discriminator="provider"),
]


class GreenmaskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binary: Annotated[str, Field(min_length=1, max_length=255)] = "greenmask"
    storage_dirname: Annotated[str, Field(min_length=1, max_length=255)] = "dump"
    mapper_timeout_seconds: Annotated[int, Field(ge=1, le=3_600)] = 60
    parallel_jobs: Annotated[int, Field(ge=1, le=32)] = 1
    validate_output: bool = True

    @field_validator("storage_dirname")
    @classmethod
    def validate_storage_dirname(cls, value: str) -> str:
        return _validate_artifact_leaf_name(value)


class ReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    json_filename: Annotated[str, Field(min_length=1, max_length=255)] = "report.json"
    markdown_filename: Annotated[str, Field(min_length=1, max_length=255)] = "report.md"
    include_synthetic_demo_before_after: bool = False
    sample_rows: Annotated[int, Field(ge=0, le=100)] = 5

    @field_validator("json_filename", "markdown_filename")
    @classmethod
    def validate_report_filename(cls, value: str) -> str:
        return _validate_artifact_leaf_name(value)


class SanitizerPolicy(BaseModel):
    """Complete validated policy, intentionally free of DSNs and secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    run: RunSettings
    connections: ConnectionsSettings
    mapping: MappingSettings
    llm: LLMSettings
    greenmask: GreenmaskSettings
    groups: Annotated[dict[str, ConsistencyGroup], Field(min_length=1)]
    report: ReportSettings

    @field_validator("groups")
    @classmethod
    def validate_group_ids(cls, groups: dict[str, ConsistencyGroup]) -> dict[str, ConsistencyGroup]:
        for group_id in groups:
            if not _IDENTIFIER.fullmatch(group_id):
                raise ValueError("group IDs must be safe identifiers")
        return groups

    @model_validator(mode="after")
    def validate_cross_group_columns(self) -> SanitizerPolicy:
        seen: dict[tuple[str, str, str], str] = {}
        for group_id, group in self.groups.items():
            for column in group.columns:
                previous = seen.setdefault(column.key, group_id)
                if previous != group_id:
                    raise ValueError(
                        f"column {column.display_name} is declared in more than one group"
                    )
        return self

    def group_for_column(self, column: ColumnRef) -> str | None:
        for group_id, group in self.groups.items():
            if column in group.columns:
                return group_id
        return None
