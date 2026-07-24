from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from db_sanitizer.verify import (
    ArtifactPaths,
    CheckResult,
    CheckSeverity,
    CheckStatus,
    GroupReport,
    LLMStats,
    ReportError,
    RunReport,
    RunStatus,
    TableReport,
    validate_report,
    validate_report_payload,
)


def _report(*checks: CheckResult) -> RunReport:
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    return RunReport(
        run_id="unit-verify-report",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        policy_sha256="a" * 64,
        source_schema_sha256="b" * 64,
        llm=LLMStats(
            provider="fake",
            model="unit-model",
            batches=1,
            accepted_items=2,
            rejected_items=0,
            duration_seconds=0.01,
        ),
        tables=(
            TableReport(
                name="public.customers",
                source_rows=2,
                target_rows=2,
                row_count_equal=True,
            ),
        ),
        groups=(
            GroupReport(
                id="email",
                entity_type="email",
                mapping_count=2,
                distinct_source=2,
                distinct_target=2,
                nulls_source=0,
                nulls_target=0,
            ),
        ),
        checks=checks,
        artifacts=ArtifactPaths(
            sanitized_dump="dump/latest",
            generated_greenmask_config="greenmask.generated.yaml",
            mapping_registry="mappings.sqlite3",
            markdown_report="report.md",
        ),
    )


def _check(
    *,
    identifier: str,
    severity: CheckSeverity = CheckSeverity.REQUIRED,
    status: CheckStatus = CheckStatus.PASS,
) -> CheckResult:
    return CheckResult(
        id=identifier,
        name=identifier.replace("_", " "),
        severity=severity,
        status=status,
        details={"items_checked": 1, "items_failed": 0},
    )


def test_report_status_passes_only_when_every_required_check_passes() -> None:
    passed = _report(
        _check(identifier="schema"),
        _check(identifier="advisory", severity=CheckSeverity.ADVISORY, status=CheckStatus.WARN),
    )
    failed = _report(
        _check(identifier="schema"),
        _check(identifier="mapping", status=CheckStatus.FAIL),
    )
    warned_required = _report(_check(identifier="schema", status=CheckStatus.WARN))

    assert passed.status is RunStatus.PASSED
    assert failed.status is RunStatus.FAILED
    assert warned_required.status is RunStatus.FAILED


def test_report_payload_conforms_to_normative_schema_without_database() -> None:
    report = _report(_check(identifier="schema"), _check(identifier="rows"))

    validate_report(report)
    payload = report.to_payload()
    validate_report_payload(payload)
    assert payload["status"] == "passed"


def test_schema_validation_rejects_nonconforming_report_payload_without_echoing_data() -> None:
    payload = _report(_check(identifier="schema")).to_payload()
    payload["unexpected"] = "PII_LEAK_MARKER_NAME_7F3A"

    with pytest.raises(ReportError) as error:
        validate_report_payload(payload)

    assert "PII_LEAK_MARKER_NAME_7F3A" not in str(error.value)
