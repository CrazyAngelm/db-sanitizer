"""Typed, schema-validated, PII-safe verification reports."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from db_sanitizer.errors import VerificationError
from db_sanitizer.policy.models import EntityType
from db_sanitizer.verify.checks import CheckResult, CheckSeverity, CheckStatus

_HASH = re.compile(r"^[a-f0-9]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class ReportError(VerificationError):
    """A report cannot safely be serialized or does not match its contract."""


class RunStatus(StrEnum):
    """Top-level report status."""

    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LLMStats:
    """Non-sensitive LLM evidence required by the report schema."""

    provider: str
    model: str
    batches: int
    accepted_items: int
    rejected_items: int
    duration_seconds: float
    structured_output: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("LLM provider must be non-empty")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("LLM model must be non-empty")
        if self.structured_output is not True:
            raise ValueError("reports require structured LLM output")
        if not isinstance(self.batches, int) or isinstance(self.batches, bool) or self.batches < 1:
            raise ValueError("LLM batches must be at least one")
        if (
            not isinstance(self.accepted_items, int)
            or isinstance(self.accepted_items, bool)
            or self.accepted_items < 1
        ):
            raise ValueError("accepted LLM items must be at least one")
        if (
            not isinstance(self.rejected_items, int)
            or isinstance(self.rejected_items, bool)
            or self.rejected_items < 0
        ):
            raise ValueError("rejected LLM items must be non-negative")
        if not isinstance(self.duration_seconds, (int, float)) or isinstance(
            self.duration_seconds, bool
        ):
            raise ValueError("LLM duration must be numeric")
        if not math.isfinite(float(self.duration_seconds)) or self.duration_seconds < 0:
            raise ValueError("LLM duration must be finite and non-negative")

    def to_report_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "structured_output": True,
            "batches": self.batches,
            "accepted_items": self.accepted_items,
            "rejected_items": self.rejected_items,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class TableReport:
    """Report-safe aggregate row metrics for one table."""

    name: str
    source_rows: int
    target_rows: int
    row_count_equal: bool

    def __post_init__(self) -> None:
        _validate_table_name(self.name)
        _validate_non_negative_int(self.source_rows, "source rows")
        _validate_non_negative_int(self.target_rows, "target rows")
        if not isinstance(self.row_count_equal, bool):
            raise ValueError("row_count_equal must be boolean")

    def to_report_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source_rows": self.source_rows,
            "target_rows": self.target_rows,
            "row_count_equal": self.row_count_equal,
        }


@dataclass(frozen=True, slots=True)
class GroupReport:
    """Report-safe aggregate evidence for one configured consistency group."""

    id: str
    entity_type: EntityType
    mapping_count: int
    distinct_source: int
    distinct_target: int
    nulls_source: int
    nulls_target: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("group ID must be a safe identifier")
        object.__setattr__(self, "entity_type", EntityType(self.entity_type))
        for value, name in (
            (self.mapping_count, "mapping count"),
            (self.distinct_source, "source distinct count"),
            (self.distinct_target, "target distinct count"),
            (self.nulls_source, "source NULL count"),
            (self.nulls_target, "target NULL count"),
        ):
            _validate_non_negative_int(value, name)

    def to_report_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "entity_type": self.entity_type.value,
            "mapping_count": self.mapping_count,
            "distinct_source": self.distinct_source,
            "distinct_target": self.distinct_target,
            "nulls_source": self.nulls_source,
            "nulls_target": self.nulls_target,
        }


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    """Artifact references, never database values or credentials."""

    sanitized_dump: str | Path
    generated_greenmask_config: str | Path
    mapping_registry: str | Path
    markdown_report: str | Path

    def __post_init__(self) -> None:
        for field_name in (
            "sanitized_dump",
            "generated_greenmask_config",
            "mapping_registry",
            "markdown_report",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"{field_name} must be a non-empty artifact path")
            object.__setattr__(self, field_name, str(value))

    def to_report_dict(self) -> dict[str, str]:
        return {
            "sanitized_dump": str(self.sanitized_dump),
            "generated_greenmask_config": str(self.generated_greenmask_config),
            "mapping_registry": str(self.mapping_registry),
            "markdown_report": str(self.markdown_report),
        }


@dataclass(frozen=True, slots=True)
class RunReport:
    """The complete JSON report model.

    Status is derived rather than accepted from callers: a report can be passed
    only when it contains at least one required check and every required check
    has ``pass`` status.
    """

    run_id: str
    started_at: datetime
    finished_at: datetime
    policy_sha256: str
    source_schema_sha256: str
    llm: LLMStats
    tables: Sequence[TableReport]
    groups: Sequence[GroupReport]
    checks: Sequence[CheckResult]
    artifacts: ArtifactPaths
    schema_version: str = field(default="1.0", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run ID must contain only safe filename characters")
        _validate_datetime(self.started_at, "started_at")
        _validate_datetime(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        _validate_hash(self.policy_sha256, "policy SHA-256")
        _validate_hash(self.source_schema_sha256, "source schema SHA-256")
        if not isinstance(self.llm, LLMStats):
            raise ValueError("llm must be LLMStats")
        if not isinstance(self.artifacts, ArtifactPaths):
            raise ValueError("artifacts must be ArtifactPaths")

        tables = tuple(self.tables)
        groups = tuple(self.groups)
        checks = tuple(self.checks)
        if not tables or not groups or not checks:
            raise ValueError("reports require tables, groups, and checks")
        if not all(isinstance(item, TableReport) for item in tables):
            raise ValueError("tables must contain TableReport instances")
        if not all(isinstance(item, GroupReport) for item in groups):
            raise ValueError("groups must contain GroupReport instances")
        if not all(isinstance(item, CheckResult) for item in checks):
            raise ValueError("checks must contain CheckResult instances")
        if len({item.name for item in tables}) != len(tables):
            raise ValueError("report table names must be unique")
        if len({item.id for item in groups}) != len(groups):
            raise ValueError("report group IDs must be unique")
        if len({item.id for item in checks}) != len(checks):
            raise ValueError("report check IDs must be unique")
        object.__setattr__(self, "tables", tables)
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "checks", checks)

    @property
    def status(self) -> RunStatus:
        required_checks = [
            check for check in self.checks if check.severity is CheckSeverity.REQUIRED
        ]
        if required_checks and all(check.status is CheckStatus.PASS for check in required_checks):
            return RunStatus.PASSED
        return RunStatus.FAILED

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status.value,
            "started_at": _isoformat(self.started_at),
            "finished_at": _isoformat(self.finished_at),
            "policy_sha256": self.policy_sha256,
            "source_schema_sha256": self.source_schema_sha256,
            "llm": self.llm.to_report_dict(),
            "tables": [item.to_report_dict() for item in self.tables],
            "groups": [item.to_report_dict() for item in self.groups],
            "checks": [item.to_report_dict() for item in self.checks],
            "artifacts": self.artifacts.to_report_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        """Alias for callers that use dictionary-oriented report handling."""

        return self.to_payload()

    def to_json(self) -> str:
        """Return deterministic JSON after contract validation."""

        payload = self.to_payload()
        validate_report_payload(payload)
        return (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n"
        )


def load_report_schema(schema_path: str | Path | None = None) -> dict[str, object]:
    """Load the normative run-report JSON Schema without exposing file contents on error."""

    path = Path(schema_path) if schema_path is not None else _default_schema_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ReportError("run report schema is unavailable") from None
    if not isinstance(payload, dict):
        raise ReportError("run report schema is invalid")
    return payload


def validate_report_payload(
    payload: Mapping[str, object],
    *,
    schema_path: str | Path | None = None,
) -> None:
    """Require a JSON-compatible payload to conform to the normative schema."""

    if not isinstance(payload, Mapping):
        raise ReportError("run report payload must be an object")
    try:
        validator = Draft202012Validator(
            load_report_schema(schema_path), format_checker=FormatChecker()
        )
        errors = tuple(validator.iter_errors(dict(payload)))
    except ReportError:
        raise
    except Exception:
        raise ReportError("unable to validate run report") from None
    if errors:
        # jsonschema error messages may echo invalid instance values, so never expose them.
        raise ReportError("run report does not conform to the required schema")


def validate_report(report: RunReport, *, schema_path: str | Path | None = None) -> None:
    """Validate a typed report object against the JSON Schema."""

    if not isinstance(report, RunReport):
        raise ReportError("report must be a RunReport")
    validate_report_payload(report.to_payload(), schema_path=schema_path)


def render_markdown(report: RunReport) -> str:
    """Render the same safe aggregate evidence as a human-readable Markdown report."""

    validate_report(report)
    lines = [
        "# DB Sanitizer run report",
        "",
        f"- Run ID: `{_markdown_cell(report.run_id)}`",
        f"- Status: **{report.status.value}**",
        f"- Started: `{_markdown_cell(_isoformat(report.started_at))}`",
        f"- Finished: `{_markdown_cell(_isoformat(report.finished_at))}`",
        f"- Policy SHA-256: `{report.policy_sha256}`",
        f"- Source schema SHA-256: `{report.source_schema_sha256}`",
        "",
        "## LLM evidence",
        "",
        "| Provider | Model | Structured output | Batches | Accepted | Rejected | Duration (s) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        "| "
        + " | ".join(
            (
                _markdown_cell(report.llm.provider),
                _markdown_cell(report.llm.model),
                "true",
                str(report.llm.batches),
                str(report.llm.accepted_items),
                str(report.llm.rejected_items),
                _format_number(report.llm.duration_seconds),
            )
        )
        + " |",
        "",
        "## Tables",
        "",
        "| Table | Source rows | Target rows | Equal |",
        "| --- | ---: | ---: | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            (
                _markdown_cell(table.name),
                str(table.source_rows),
                str(table.target_rows),
                _boolean_label(table.row_count_equal),
            )
        )
        + " |"
        for table in report.tables
    )
    lines.extend(
        [
            "",
            "## Consistency groups",
            "",
            "| Group | Entity type | Mappings | Source distinct | Target distinct | "
            "Source NULLs | Target NULLs |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        "| "
        + " | ".join(
            (
                _markdown_cell(group.id),
                group.entity_type.value,
                str(group.mapping_count),
                str(group.distinct_source),
                str(group.distinct_target),
                str(group.nulls_source),
                str(group.nulls_target),
            )
        )
        + " |"
        for group in report.groups
    )
    lines.extend(
        [
            "",
            "## Checks",
            "",
            "| ID | Check | Severity | Status | Safe aggregate details |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        "| "
        + " | ".join(
            (
                check.id,
                _markdown_cell(check.name),
                check.severity.value,
                check.status.value,
                _format_details(check.details),
            )
        )
        + " |"
        for check in report.checks
    )
    artifacts = report.artifacts.to_report_dict()
    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            f"- Sanitized dump: `{_markdown_cell(artifacts['sanitized_dump'])}`",
            "- Generated Greenmask configuration: "
            f"`{_markdown_cell(artifacts['generated_greenmask_config'])}`",
            f"- Mapping registry: `{_markdown_cell(artifacts['mapping_registry'])}`",
            f"- Markdown report: `{_markdown_cell(artifacts['markdown_report'])}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    report: RunReport,
    *,
    json_path: str | Path,
    markdown_path: str | Path,
    schema_path: str | Path | None = None,
) -> None:
    """Validate then atomically write JSON and Markdown reports with private file modes."""

    if not isinstance(report, RunReport):
        raise ReportError("report must be a RunReport")
    json_destination = Path(json_path)
    markdown_destination = Path(markdown_path)
    if json_destination.resolve() == markdown_destination.resolve():
        raise ReportError("JSON and Markdown report paths must be different")
    validate_report(report, schema_path=schema_path)
    payload = report.to_payload()
    json_content = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    markdown_content = render_markdown(report)
    try:
        _write_private_text(json_destination, json_content)
        _write_private_text(markdown_destination, markdown_content)
    except OSError:
        raise ReportError("unable to write verification reports") from None


def _default_schema_path() -> Path:
    candidates = [Path.cwd() / "templates" / "run-report.schema.json"]
    candidates.extend(
        parent / "templates" / "run-report.schema.json"
        for parent in Path(__file__).resolve().parents
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ReportError("run report schema is unavailable")


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        with suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        with suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with suppress(OSError):
            temporary.unlink()


def _validate_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _validate_datetime(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _validate_non_negative_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_table_name(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("table name must be a string")
    parts = value.split(".")
    if len(parts) != 2 or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError("table name must be a safe schema.table identifier")


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _format_number(value: int | float) -> str:
    return str(value) if isinstance(value, int) else f"{value:.6f}".rstrip("0").rstrip(".")


def _boolean_label(value: bool) -> str:
    return "yes" if value else "no"


def _format_details(details: Mapping[str, bool | int | float]) -> str:
    return "; ".join(
        f"{key}={_boolean_label(value) if isinstance(value, bool) else _format_number(value)}"
        for key, value in sorted(details.items())
    )
