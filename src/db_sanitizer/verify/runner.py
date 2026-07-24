"""Composable orchestration for fail-closed verification and reporting."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from psycopg import Connection

from db_sanitizer.errors import VerificationError
from db_sanitizer.mapping import HMACKey, MappingRegistry
from db_sanitizer.policy.models import SanitizerPolicy
from db_sanitizer.postgres.connection import source_connection, target_connection
from db_sanitizer.postgres.inspector import SchemaSnapshot, inspect_schema
from db_sanitizer.verify.checks import (
    CheckResult,
    ColumnStats,
    GroupHmacStats,
    KeyConstraint,
    TableStats,
    check_configured_column_statistics,
    check_configured_mappings,
    check_foreign_keys,
    check_no_single_placeholder,
    check_primary_unique_constraints,
    check_schema_columns_types,
    check_source_schema_fingerprint,
    check_source_target_hmac_nonintersection,
    check_table_row_counts,
    read_key_constraints,
)
from db_sanitizer.verify.report import (
    ArtifactPaths,
    GroupReport,
    LLMStats,
    RunReport,
    TableReport,
    write_report,
)


@dataclass(frozen=True, slots=True)
class VerificationContext:
    """Non-secret run metadata supplied by the future graph/CLI layer."""

    run_id: str
    policy_sha256: str
    source_schema_sha256: str
    started_at: datetime
    llm: LLMStats
    artifacts: ArtifactPaths


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Verification evidence that graph code can persist without raw database values."""

    report: RunReport
    source_snapshot: SchemaSnapshot
    target_snapshot: SchemaSnapshot
    table_stats: tuple[TableStats, ...]
    column_stats: tuple[ColumnStats, ...]
    group_hmac_stats: tuple[GroupHmacStats, ...]
    source_constraints: tuple[KeyConstraint, ...]
    target_constraints: tuple[KeyConstraint, ...]

    @property
    def passed(self) -> bool:
        return self.report.status.value == "passed"

    @property
    def checks(self) -> tuple[CheckResult, ...]:
        return tuple(self.report.checks)

    def raise_for_failure(self) -> None:
        """Map a failed required check to the stable verification exit category."""

        if not self.passed:
            raise VerificationError("one or more required verification checks failed")


def verify_connections(
    *,
    policy: SanitizerPolicy,
    source: Connection[object],
    target: Connection[object],
    registry: MappingRegistry,
    hmac_key: HMACKey,
    context: VerificationContext,
    source_snapshot: SchemaSnapshot | None = None,
    target_snapshot: SchemaSnapshot | None = None,
    finished_at: datetime | None = None,
) -> VerificationOutcome:
    """Run all required checks using already-open source and target connections.

    Connections are intentionally injected so a graph may reuse its existing
    transaction boundaries.  This function never writes files or logs values.
    """

    try:
        source_schema = source_snapshot or inspect_schema(source)
        target_schema = target_snapshot or inspect_schema(target)
        source_constraints = read_key_constraints(source)
        target_constraints = read_key_constraints(target)

        checks: list[CheckResult] = [
            check_source_schema_fingerprint(source_schema, context.source_schema_sha256),
            check_schema_columns_types(source_schema, target_schema),
        ]
        table_stats, table_check = check_table_row_counts(
            source, target, source_schema, target_schema
        )
        checks.append(table_check)
        column_stats, column_check = check_configured_column_statistics(
            source, target, policy, source_schema, target_schema
        )
        checks.append(column_check)
        checks.append(
            check_foreign_keys(
                target=target,
                source_snapshot=source_schema,
                target_snapshot=target_schema,
            )
        )
        checks.append(
            check_primary_unique_constraints(
                source=source,
                target=target,
                policy=policy,
                source_constraints=source_constraints,
                target_constraints=target_constraints,
                fetch_size=policy.run.verifier_fetch_size,
            )
        )
        checks.append(
            check_configured_mappings(
                source=source,
                target=target,
                policy=policy,
                registry=registry,
                hmac_key=hmac_key,
                source_snapshot=source_schema,
                target_snapshot=target_schema,
                source_constraints=source_constraints,
                target_constraints=target_constraints,
                fetch_size=policy.run.verifier_fetch_size,
            )
        )
        group_hmac_stats, hmac_check = check_source_target_hmac_nonintersection(
            target=target,
            policy=policy,
            registry=registry,
            hmac_key=hmac_key,
            target_snapshot=target_schema,
            fetch_size=policy.run.verifier_fetch_size,
        )
        checks.extend((hmac_check, check_no_single_placeholder(column_stats)))

        report = RunReport(
            run_id=context.run_id,
            started_at=context.started_at,
            finished_at=finished_at or datetime.now(UTC),
            policy_sha256=context.policy_sha256,
            source_schema_sha256=context.source_schema_sha256,
            llm=context.llm,
            tables=tuple(_table_report(item) for item in table_stats),
            groups=_group_reports(
                policy=policy,
                registry=registry,
                column_stats=column_stats,
                group_hmac_stats=group_hmac_stats,
            ),
            checks=tuple(checks),
            artifacts=context.artifacts,
        )
        return VerificationOutcome(
            report=report,
            source_snapshot=source_schema,
            target_snapshot=target_schema,
            table_stats=table_stats,
            column_stats=column_stats,
            group_hmac_stats=group_hmac_stats,
            source_constraints=source_constraints,
            target_constraints=target_constraints,
        )
    except VerificationError:
        raise
    except Exception:
        # Driver messages may embed query parameters or row content.  Expose only
        # a stable, value-free failure to graph/CLI callers.
        raise VerificationError("verification could not complete") from None


def verify_databases(
    *,
    policy: SanitizerPolicy,
    source_dsn: str,
    target_dsn: str,
    registry: MappingRegistry,
    hmac_key: HMACKey,
    context: VerificationContext,
    source_snapshot: SchemaSnapshot | None = None,
    target_snapshot: SchemaSnapshot | None = None,
    finished_at: datetime | None = None,
) -> VerificationOutcome:
    """Open the prescribed read-only/read-target connections and run verification."""

    with source_connection(source_dsn) as source, target_connection(target_dsn) as target:
        return verify_connections(
            policy=policy,
            source=source,
            target=target,
            registry=registry,
            hmac_key=hmac_key,
            context=context,
            source_snapshot=source_snapshot,
            target_snapshot=target_snapshot,
            finished_at=finished_at,
        )


def verify_and_write_report(
    *,
    policy: SanitizerPolicy,
    source: Connection[object],
    target: Connection[object],
    registry: MappingRegistry,
    hmac_key: HMACKey,
    context: VerificationContext,
    json_path: str | Path,
    markdown_path: str | Path,
    source_snapshot: SchemaSnapshot | None = None,
    target_snapshot: SchemaSnapshot | None = None,
    finished_at: datetime | None = None,
    raise_on_failure: bool = True,
) -> VerificationOutcome:
    """Run checks, write both reports, then fail closed by default on required failure."""

    outcome = verify_connections(
        policy=policy,
        source=source,
        target=target,
        registry=registry,
        hmac_key=hmac_key,
        context=context,
        source_snapshot=source_snapshot,
        target_snapshot=target_snapshot,
        finished_at=finished_at,
    )
    write_report(outcome.report, json_path=json_path, markdown_path=markdown_path)
    if raise_on_failure:
        outcome.raise_for_failure()
    return outcome


def _table_report(stats: TableStats) -> TableReport:
    return TableReport(
        name=stats.name,
        source_rows=stats.source_rows,
        target_rows=stats.target_rows,
        row_count_equal=stats.row_count_equal,
    )


def _group_reports(
    *,
    policy: SanitizerPolicy,
    registry: MappingRegistry,
    column_stats: Sequence[ColumnStats],
    group_hmac_stats: Sequence[GroupHmacStats],
) -> tuple[GroupReport, ...]:
    hmac_by_group: Mapping[str, GroupHmacStats] = {item.group_id: item for item in group_hmac_stats}
    columns_by_group: dict[str, list[ColumnStats]] = {}
    for item in column_stats:
        columns_by_group.setdefault(item.group_id, []).append(item)

    reports: list[GroupReport] = []
    for group_id, group in sorted(policy.groups.items()):
        stats = columns_by_group.get(group_id, [])
        hmac_stats = hmac_by_group.get(group_id)
        mapping_count = registry.mapping_count(group_id)
        reports.append(
            GroupReport(
                id=group_id,
                entity_type=group.entity_type,
                mapping_count=mapping_count,
                distinct_source=mapping_count,
                distinct_target=0 if hmac_stats is None else hmac_stats.target_distinct,
                nulls_source=sum(item.source_nulls for item in stats),
                nulls_target=sum(item.target_nulls for item in stats),
            )
        )
    return tuple(reports)
