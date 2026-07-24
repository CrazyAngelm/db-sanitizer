"""Privacy-safe PostgreSQL verification checks.

The functions in this module only return structural metadata and aggregates.  They
may read a source or target value long enough to normalize, hash, or compare it,
but never retain that value in a check result or exception.
"""

from __future__ import annotations

import math
import re
from collections import Counter, OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import count, zip_longest
from types import MappingProxyType
from typing import Literal

from psycopg import Connection, sql
from psycopg.rows import tuple_row

from db_sanitizer.errors import VerificationError
from db_sanitizer.mapping import HMACKey, MappingRegistry, NormalizationError, normalize
from db_sanitizer.policy.models import ColumnRef, SanitizerPolicy
from db_sanitizer.postgres.inspector import ForeignKeyInfo, SchemaSnapshot

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERIFY_CURSOR_IDS = count()
_MISSING = object()

DetailValue = bool | int | float
KeyKind = Literal["primary_key", "unique"]


class CheckSeverity(StrEnum):
    """Whether a failed check blocks a successful run."""

    REQUIRED = "required"
    ADVISORY = "advisory"


class CheckStatus(StrEnum):
    """Machine-readable check outcome."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """A report-safe check outcome containing only aggregate detail values."""

    id: str
    name: str
    severity: CheckSeverity
    status: CheckStatus
    details: Mapping[str, DetailValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _CHECK_ID.fullmatch(self.id):
            raise ValueError("check ID must be a safe lowercase identifier")
        if not isinstance(self.name, str) or not self.name.strip() or "\n" in self.name:
            raise ValueError("check name must be a single non-empty line")
        object.__setattr__(self, "severity", CheckSeverity(self.severity))
        object.__setattr__(self, "status", CheckStatus(self.status))
        safe_details: dict[str, DetailValue] = {}
        for key, value in self.details.items():
            if not isinstance(key, str) or not _DETAIL_KEY.fullmatch(key):
                raise ValueError("check detail keys must be safe lowercase identifiers")
            if isinstance(value, (bool, int)) or (
                isinstance(value, float) and math.isfinite(value)
            ):
                safe_details[key] = value
            else:
                # Disallow strings and containers so result details cannot accidentally
                # become a vehicle for source or target values.
                raise ValueError("check details may contain only finite numeric values or booleans")
        object.__setattr__(self, "details", MappingProxyType(safe_details))

    @property
    def is_blocking_failure(self) -> bool:
        return self.severity is CheckSeverity.REQUIRED and self.status is not CheckStatus.PASS

    def to_report_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "severity": self.severity.value,
            "status": self.status.value,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class TableStats:
    """Safe row-count metrics for one user table."""

    schema_name: str
    table: str
    source_rows: int
    target_rows: int
    row_count_equal: bool

    def __post_init__(self) -> None:
        _validate_identifier(self.schema_name)
        _validate_identifier(self.table)
        _validate_count(self.source_rows)
        _validate_count(self.target_rows)

    @property
    def name(self) -> str:
        return f"{self.schema_name}.{self.table}"


@dataclass(frozen=True, slots=True)
class ColumnStats:
    """Safe cardinality metrics for one explicitly configured column."""

    group_id: str
    ref: ColumnRef
    source_nulls: int
    target_nulls: int
    source_distinct: int
    target_distinct: int
    available: bool = True

    def __post_init__(self) -> None:
        _validate_identifier(self.group_id)
        for value in (
            self.source_nulls,
            self.target_nulls,
            self.source_distinct,
            self.target_distinct,
        ):
            _validate_count(value)


@dataclass(frozen=True, slots=True)
class GroupHmacStats:
    """Opaque target-HMAC aggregate metrics for one consistency group."""

    group_id: str
    target_distinct: int
    intersection_keys: int
    invalid_values: int
    unavailable_columns: int

    def __post_init__(self) -> None:
        _validate_identifier(self.group_id)
        for value in (
            self.target_distinct,
            self.intersection_keys,
            self.invalid_values,
            self.unavailable_columns,
        ):
            _validate_count(value)


@dataclass(frozen=True, slots=True)
class KeyConstraint:
    """A PK or UNIQUE definition without constraint names or row values."""

    kind: KeyKind
    schema_name: str
    table: str
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"primary_key", "unique"}:
            raise ValueError("unsupported key constraint kind")
        _validate_identifier(self.schema_name)
        _validate_identifier(self.table)
        if not self.columns:
            raise ValueError("key constraints require at least one column")
        for column in self.columns:
            _validate_identifier(column)

    @property
    def table_key(self) -> tuple[str, str]:
        return (self.schema_name, self.table)

    @property
    def signature(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.kind, self.schema_name, self.table, self.columns)


class _ReplacementLookupCache:
    """Bound repeated registry lookups without retaining an unbounded key set."""

    def __init__(self, registry: MappingRegistry, group_id: str, *, limit: int = 4_096) -> None:
        self._registry = registry
        self._group_id = group_id
        self._limit = limit
        self._values: OrderedDict[str, str | None] = OrderedDict()

    def lookup(self, source_hmac: str) -> str | None:
        try:
            value = self._values.pop(source_hmac)
        except KeyError:
            value = self._registry.lookup(self._group_id, source_hmac)
            if len(self._values) >= self._limit:
                self._values.popitem(last=False)
        self._values[source_hmac] = value
        return value


@dataclass(slots=True)
class _MappingMetrics:
    columns_checked: int = 0
    primary_key_columns: int = 0
    aggregate_columns: int = 0
    unavailable_columns: int = 0
    rows_checked: int = 0
    primary_key_mismatches: int = 0
    null_mismatches: int = 0
    missing_mappings: int = 0
    mapping_mismatches: int = 0
    unchanged_values: int = 0
    invalid_source_values: int = 0
    invalid_target_values: int = 0

    @property
    def failed(self) -> bool:
        return any(
            (
                self.unavailable_columns,
                self.primary_key_mismatches,
                self.null_mismatches,
                self.missing_mappings,
                self.mapping_mismatches,
                self.unchanged_values,
                self.invalid_source_values,
                self.invalid_target_values,
            )
        )

    def details(self) -> dict[str, int]:
        return {
            "columns_checked": self.columns_checked,
            "primary_key_columns": self.primary_key_columns,
            "aggregate_columns": self.aggregate_columns,
            "unavailable_columns": self.unavailable_columns,
            "rows_checked": self.rows_checked,
            "primary_key_mismatches": self.primary_key_mismatches,
            "null_mismatches": self.null_mismatches,
            "missing_mappings": self.missing_mappings,
            "mapping_mismatches": self.mapping_mismatches,
            "unchanged_values": self.unchanged_values,
            "invalid_source_values": self.invalid_source_values,
            "invalid_target_values": self.invalid_target_values,
        }


def check_source_schema_fingerprint(
    source_snapshot: SchemaSnapshot,
    expected_fingerprint: str,
) -> CheckResult:
    """Ensure verification reads the same source schema that the run approved."""

    matches = source_snapshot.fingerprint == expected_fingerprint
    return CheckResult(
        id="source_schema_fingerprint",
        name="Source schema fingerprint",
        severity=CheckSeverity.REQUIRED,
        status=CheckStatus.PASS if matches else CheckStatus.FAIL,
        details={"matches_expected": matches},
    )


def check_schema_columns_types(
    source_snapshot: SchemaSnapshot,
    target_snapshot: SchemaSnapshot,
) -> CheckResult:
    """Compare every user-visible column, PostgreSQL type, and length limit."""

    source_columns = {
        column.ref.key: (_canonical_type(column.postgres_type), column.character_maximum_length)
        for column in source_snapshot.columns
    }
    target_columns = {
        column.ref.key: (_canonical_type(column.postgres_type), column.character_maximum_length)
        for column in target_snapshot.columns
    }
    source_keys = set(source_columns)
    target_keys = set(target_columns)
    common_keys = source_keys & target_keys
    type_mismatches = sum(source_columns[key][0] != target_columns[key][0] for key in common_keys)
    length_mismatches = sum(source_columns[key][1] != target_columns[key][1] for key in common_keys)
    missing_target = len(source_keys - target_keys)
    extra_target = len(target_keys - source_keys)
    passed = not any((missing_target, extra_target, type_mismatches, length_mismatches))
    return CheckResult(
        id="schema_columns_types",
        name="Schema columns and PostgreSQL types",
        severity=CheckSeverity.REQUIRED,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        details={
            "source_columns": len(source_columns),
            "target_columns": len(target_columns),
            "missing_target_columns": missing_target,
            "extra_target_columns": extra_target,
            "type_mismatches": type_mismatches,
            "length_mismatches": length_mismatches,
        },
    )


def check_table_row_counts(
    source: Connection[object],
    target: Connection[object],
    source_snapshot: SchemaSnapshot,
    target_snapshot: SchemaSnapshot,
) -> tuple[tuple[TableStats, ...], CheckResult]:
    """Count rows in every known user table without reading table values."""

    source_tables = set(source_snapshot.table_refs)
    target_tables = set(target_snapshot.table_refs)
    stats: list[TableStats] = []
    for schema_name, table in sorted(source_tables | target_tables):
        source_exists = (schema_name, table) in source_tables
        target_exists = (schema_name, table) in target_tables
        source_rows = _table_row_count(source, schema_name, table) if source_exists else 0
        target_rows = _table_row_count(target, schema_name, table) if target_exists else 0
        stats.append(
            TableStats(
                schema_name=schema_name,
                table=table,
                source_rows=source_rows,
                target_rows=target_rows,
                row_count_equal=source_exists and target_exists and source_rows == target_rows,
            )
        )
    mismatches = sum(not item.row_count_equal for item in stats)
    return (
        tuple(stats),
        CheckResult(
            id="table_row_counts",
            name="Per-table row counts",
            severity=CheckSeverity.REQUIRED,
            status=CheckStatus.PASS if mismatches == 0 else CheckStatus.FAIL,
            details={"tables_checked": len(stats), "mismatched_tables": mismatches},
        ),
    )


def check_configured_column_statistics(
    source: Connection[object],
    target: Connection[object],
    policy: SanitizerPolicy,
    source_snapshot: SchemaSnapshot,
    target_snapshot: SchemaSnapshot,
) -> tuple[tuple[ColumnStats, ...], CheckResult]:
    """Compare NULL and SQL-distinct counts for each configured column."""

    stats: list[ColumnStats] = []
    unavailable_columns = 0
    null_mismatches = 0
    distinct_mismatches = 0
    for group_id, _, ref in _configured_columns(policy):
        if source_snapshot.column_for(ref) is None or target_snapshot.column_for(ref) is None:
            unavailable_columns += 1
            stats.append(
                ColumnStats(
                    group_id=group_id,
                    ref=ref,
                    source_nulls=0,
                    target_nulls=0,
                    source_distinct=0,
                    target_distinct=0,
                    available=False,
                )
            )
            continue
        source_nulls, source_distinct = _column_counts(source, ref)
        target_nulls, target_distinct = _column_counts(target, ref)
        null_mismatches += source_nulls != target_nulls
        distinct_mismatches += source_distinct != target_distinct
        stats.append(
            ColumnStats(
                group_id=group_id,
                ref=ref,
                source_nulls=source_nulls,
                target_nulls=target_nulls,
                source_distinct=source_distinct,
                target_distinct=target_distinct,
            )
        )
    passed = not any((unavailable_columns, null_mismatches, distinct_mismatches))
    return (
        tuple(stats),
        CheckResult(
            id="configured_column_statistics",
            name="Configured column NULL and distinct counts",
            severity=CheckSeverity.REQUIRED,
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            details={
                "columns_checked": len(stats),
                "unavailable_columns": unavailable_columns,
                "null_mismatches": null_mismatches,
                "distinct_mismatches": distinct_mismatches,
            },
        ),
    )


def read_key_constraints(connection: Connection[object]) -> tuple[KeyConstraint, ...]:
    """Read PK and UNIQUE definitions as safe structural metadata."""

    query = """
        SELECT
            namespace.nspname,
            relation.relname,
            constraint_row.contype,
            array_agg(attribute.attname ORDER BY key_columns.ordinality)
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS relation
          ON relation.oid = constraint_row.conrelid
        JOIN pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        JOIN unnest(constraint_row.conkey) WITH ORDINALITY
             AS key_columns(attnum, ordinality)
          ON TRUE
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attnum = key_columns.attnum
        WHERE constraint_row.contype IN ('p', 'u')
          AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        GROUP BY namespace.nspname, relation.relname, constraint_row.contype, constraint_row.oid
        ORDER BY namespace.nspname, relation.relname, constraint_row.contype, constraint_row.oid
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()
        constraints: list[KeyConstraint] = []
        for schema_name, table, kind, columns in rows:
            if not isinstance(columns, (list, tuple)):
                raise TypeError("constraint column metadata is invalid")
            constraints.append(
                KeyConstraint(
                    kind="primary_key" if kind == "p" else "unique",
                    schema_name=str(schema_name),
                    table=str(table),
                    columns=tuple(str(column) for column in columns),
                )
            )
        return tuple(constraints)
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("unable to read key constraint metadata") from None


def check_primary_unique_constraints(
    source: Connection[object],
    target: Connection[object],
    policy: SanitizerPolicy,
    source_constraints: Sequence[KeyConstraint],
    target_constraints: Sequence[KeyConstraint],
    *,
    fetch_size: int = 1_000,
) -> CheckResult:
    """Verify PK/UNIQUE definitions, target key integrity, and stable surrogate PKs."""

    source_signatures = Counter(item.signature for item in source_constraints)
    target_signatures = Counter(item.signature for item in target_constraints)
    missing_constraints = sum((source_signatures - target_signatures).values())
    extra_constraints = sum((target_signatures - source_signatures).values())

    duplicate_key_groups = 0
    primary_key_null_rows = 0
    for constraint in target_constraints:
        duplicate_key_groups += _duplicate_key_group_count(target, constraint)
        if constraint.kind == "primary_key":
            primary_key_null_rows += _primary_key_null_count(target, constraint)

    target_primary_signatures = {
        constraint.signature
        for constraint in target_constraints
        if constraint.kind == "primary_key"
    }
    primary_keys_compared = 0
    transformed_primary_keys = 0
    primary_key_value_mismatches = 0
    for constraint in source_constraints:
        if (
            constraint.kind != "primary_key"
            or constraint.signature not in target_primary_signatures
        ):
            continue
        if _primary_key_is_configured(policy, constraint):
            transformed_primary_keys += 1
            continue
        primary_keys_compared += 1
        _, mismatches = _ordered_key_comparison(source, target, constraint, fetch_size=fetch_size)
        primary_key_value_mismatches += mismatches

    passed = not any(
        (
            missing_constraints,
            extra_constraints,
            duplicate_key_groups,
            primary_key_null_rows,
            primary_key_value_mismatches,
        )
    )
    return CheckResult(
        id="primary_unique_constraints",
        name="Primary-key and unique-constraint integrity",
        severity=CheckSeverity.REQUIRED,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        details={
            "source_constraints": len(source_constraints),
            "target_constraints": len(target_constraints),
            "missing_constraints": missing_constraints,
            "extra_constraints": extra_constraints,
            "duplicate_key_groups": duplicate_key_groups,
            "primary_key_null_rows": primary_key_null_rows,
            "primary_keys_compared": primary_keys_compared,
            "transformed_primary_keys": transformed_primary_keys,
            "primary_key_value_mismatches": primary_key_value_mismatches,
        },
    )


def check_foreign_keys(
    target: Connection[object],
    source_snapshot: SchemaSnapshot,
    target_snapshot: SchemaSnapshot,
) -> CheckResult:
    """Require equivalent, validated target FKs with no orphan rows."""

    source_signatures = Counter(
        _foreign_key_signature(item) for item in source_snapshot.foreign_keys
    )
    target_signatures = Counter(
        _foreign_key_signature(item) for item in target_snapshot.foreign_keys
    )
    missing_foreign_keys = sum((source_signatures - target_signatures).values())
    extra_foreign_keys = sum((target_signatures - source_signatures).values())
    unvalidated_foreign_keys = _unvalidated_foreign_key_count(target)
    orphan_rows = sum(
        _foreign_key_orphan_count(target, item) for item in target_snapshot.foreign_keys
    )
    passed = not any(
        (missing_foreign_keys, extra_foreign_keys, unvalidated_foreign_keys, orphan_rows)
    )
    return CheckResult(
        id="foreign_keys",
        name="Foreign-key validation and orphan checks",
        severity=CheckSeverity.REQUIRED,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        details={
            "source_foreign_keys": len(source_snapshot.foreign_keys),
            "target_foreign_keys": len(target_snapshot.foreign_keys),
            "missing_foreign_keys": missing_foreign_keys,
            "extra_foreign_keys": extra_foreign_keys,
            "unvalidated_foreign_keys": unvalidated_foreign_keys,
            "orphan_rows": orphan_rows,
        },
    )


def check_configured_mappings(
    source: Connection[object],
    target: Connection[object],
    policy: SanitizerPolicy,
    registry: MappingRegistry,
    hmac_key: HMACKey,
    source_snapshot: SchemaSnapshot,
    target_snapshot: SchemaSnapshot,
    source_constraints: Sequence[KeyConstraint],
    target_constraints: Sequence[KeyConstraint],
    *,
    fetch_size: int = 1_000,
) -> CheckResult:
    """Verify configured source-to-target replacements without emitting any values.

    Unconfigured primary keys are used as row alignment keys.  If a table has no
    safe unchanged PK (for example, a configured natural key), the check falls
    back to exact HMAC-multiset comparison of expected registry replacements.
    """

    source_primary_keys = {
        constraint.table_key: constraint
        for constraint in source_constraints
        if constraint.kind == "primary_key"
    }
    target_primary_signatures = {
        constraint.signature
        for constraint in target_constraints
        if constraint.kind == "primary_key"
    }
    metrics = _MappingMetrics()
    lookup_caches: dict[str, _ReplacementLookupCache] = {}

    for group_id, group, ref in _configured_columns(policy):
        metrics.columns_checked += 1
        if source_snapshot.column_for(ref) is None or target_snapshot.column_for(ref) is None:
            metrics.unavailable_columns += 1
            continue
        lookup_cache = lookup_caches.setdefault(
            group_id, _ReplacementLookupCache(registry, group_id)
        )
        primary_key = source_primary_keys.get((ref.schema_name, ref.table))
        if (
            primary_key is not None
            and primary_key.signature in target_primary_signatures
            and not _primary_key_is_configured(policy, primary_key)
        ):
            metrics.primary_key_columns += 1
            _check_mapping_by_primary_key(
                source=source,
                target=target,
                ref=ref,
                group_id=group_id,
                normalization=group.normalization.value,
                allow_empty=group.allow_empty,
                registry=registry,
                hmac_key=hmac_key,
                primary_key=primary_key,
                metrics=metrics,
                lookup_cache=lookup_cache,
                fetch_size=fetch_size,
            )
        else:
            metrics.aggregate_columns += 1
            _check_mapping_aggregate(
                source=source,
                target=target,
                ref=ref,
                group_id=group_id,
                normalization=group.normalization.value,
                allow_empty=group.allow_empty,
                registry=registry,
                hmac_key=hmac_key,
                metrics=metrics,
                lookup_cache=lookup_cache,
                fetch_size=fetch_size,
            )

    return CheckResult(
        id="configured_mappings",
        name="Configured source-to-target mappings",
        severity=CheckSeverity.REQUIRED,
        status=CheckStatus.FAIL if metrics.failed else CheckStatus.PASS,
        details=metrics.details(),
    )


def check_source_target_hmac_nonintersection(
    target: Connection[object],
    policy: SanitizerPolicy,
    registry: MappingRegistry,
    hmac_key: HMACKey,
    target_snapshot: SchemaSnapshot,
    *,
    fetch_size: int = 1_000,
) -> tuple[tuple[GroupHmacStats, ...], CheckResult]:
    """Ensure normalized target values never intersect opaque source keys."""

    group_stats: list[GroupHmacStats] = []
    total_intersections = 0
    invalid_values = 0
    unavailable_columns = 0
    for group_id, group in sorted(policy.groups.items()):
        group_invalid = 0
        group_unavailable = 0
        with registry.verification_hmac_accumulator(group_id) as accumulator:
            batch: list[str] = []
            for ref in sorted(group.columns, key=lambda item: item.key):
                if target_snapshot.column_for(ref) is None:
                    group_unavailable += 1
                    continue
                for value in _iter_column_values(target, ref, fetch_size=fetch_size):
                    if value is None:
                        continue
                    normalized = _normalize_for_check(
                        value,
                        group.normalization.value,
                        group.allow_empty,
                    )
                    if normalized is None:
                        group_invalid += 1
                        continue
                    batch.append(hmac_key.digest(group_id, normalized))
                    if len(batch) >= fetch_size:
                        accumulator.add_actual(batch)
                        batch.clear()
            if batch:
                accumulator.add_actual(batch)
            target_distinct = accumulator.actual_distinct_count()
            intersection_keys = accumulator.source_intersection_count()
        group_stat = GroupHmacStats(
            group_id=group_id,
            target_distinct=target_distinct,
            intersection_keys=intersection_keys,
            invalid_values=group_invalid,
            unavailable_columns=group_unavailable,
        )
        group_stats.append(group_stat)
        total_intersections += group_stat.intersection_keys
        invalid_values += group_invalid
        unavailable_columns += group_unavailable
    passed = not any((total_intersections, invalid_values, unavailable_columns))
    return (
        tuple(group_stats),
        CheckResult(
            id="source_target_hmac_nonintersection",
            name="Source and target HMAC nonintersection",
            severity=CheckSeverity.REQUIRED,
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            details={
                "groups_checked": len(group_stats),
                "intersection_keys": total_intersections,
                "invalid_values": invalid_values,
                "unavailable_columns": unavailable_columns,
            },
        ),
    )


def check_no_single_placeholder(column_stats: Sequence[ColumnStats]) -> CheckResult:
    """Reject a configured column collapsed to one value from multiple values."""

    unavailable_columns = sum(not item.available for item in column_stats)
    candidate_columns = [
        item for item in column_stats if item.available and item.source_distinct > 1
    ]
    collapsed_columns = sum(item.target_distinct <= 1 for item in candidate_columns)
    passed = not any((unavailable_columns, collapsed_columns))
    return CheckResult(
        id="no_single_placeholder",
        name="No single-placeholder sensitive columns",
        severity=CheckSeverity.REQUIRED,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        details={
            "columns_checked": len(column_stats),
            "candidate_columns": len(candidate_columns),
            "collapsed_columns": collapsed_columns,
            "unavailable_columns": unavailable_columns,
        },
    )


def _configured_columns(
    policy: SanitizerPolicy,
) -> Iterator[tuple[str, object, ColumnRef]]:
    for group_id, group in sorted(policy.groups.items()):
        for ref in sorted(group.columns, key=lambda item: item.key):
            yield group_id, group, ref


def _canonical_type(value: str) -> str:
    return " ".join(value.casefold().split())


def _table_row_count(connection: Connection[object], schema_name: str, table: str) -> int:
    statement = sql.SQL("SELECT COUNT(*) FROM {}").format(_table_identifier(schema_name, table))
    return _count_query(connection, statement)


def _column_counts(connection: Connection[object], ref: ColumnRef) -> tuple[int, int]:
    column = sql.Identifier(ref.column)
    statement = sql.SQL(
        "SELECT COUNT(*) FILTER (WHERE {column} IS NULL), "
        "COUNT(DISTINCT {column}) FILTER (WHERE {column} IS NOT NULL) "
        "FROM {table}"
    ).format(column=column, table=_ref_table_identifier(ref))
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
        if row is None:
            raise TypeError("aggregate query did not return a row")
        nulls, distinct_values = int(row[0]), int(row[1])
        _validate_count(nulls)
        _validate_count(distinct_values)
        return nulls, distinct_values
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("unable to read configured column aggregates") from None


def _duplicate_key_group_count(connection: Connection[object], constraint: KeyConstraint) -> int:
    columns = sql.SQL(", ").join(sql.Identifier(column) for column in constraint.columns)
    non_null = sql.SQL(" AND ").join(
        sql.SQL("{} IS NOT NULL").format(sql.Identifier(column)) for column in constraint.columns
    )
    statement = sql.SQL(
        "SELECT COUNT(*) FROM ("
        "SELECT 1 FROM {table} WHERE {non_null} GROUP BY {columns} HAVING COUNT(*) > 1"
        ") AS duplicate_keys"
    ).format(
        table=_table_identifier(constraint.schema_name, constraint.table),
        non_null=non_null,
        columns=columns,
    )
    return _count_query(connection, statement)


def _primary_key_null_count(connection: Connection[object], constraint: KeyConstraint) -> int:
    null_predicate = sql.SQL(" OR ").join(
        sql.SQL("{} IS NULL").format(sql.Identifier(column)) for column in constraint.columns
    )
    statement = sql.SQL("SELECT COUNT(*) FROM {table} WHERE {predicate}").format(
        table=_table_identifier(constraint.schema_name, constraint.table),
        predicate=null_predicate,
    )
    return _count_query(connection, statement)


def _ordered_key_comparison(
    source: Connection[object],
    target: Connection[object],
    constraint: KeyConstraint,
    *,
    fetch_size: int,
) -> tuple[int, int]:
    statement = _ordered_select_statement(
        schema_name=constraint.schema_name,
        table=constraint.table,
        key_columns=constraint.columns,
    )
    rows_checked = 0
    mismatches = 0
    try:
        with (
            _executed_cursor(source, statement, fetch_size=fetch_size) as source_cursor,
            _executed_cursor(target, statement, fetch_size=fetch_size) as target_cursor,
        ):
            for source_row, target_row in zip_longest(
                _cursor_rows(source_cursor, fetch_size),
                _cursor_rows(target_cursor, fetch_size),
                fillvalue=_MISSING,
            ):
                if source_row is _MISSING or target_row is _MISSING:
                    mismatches += 1
                    continue
                rows_checked += 1
                if tuple(source_row) != tuple(target_row):
                    mismatches += 1
        return rows_checked, mismatches
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("unable to compare unchanged primary keys") from None


def _unvalidated_foreign_key_count(connection: Connection[object]) -> int:
    query = """
        SELECT COUNT(*)
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS relation
          ON relation.oid = constraint_row.conrelid
        JOIN pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE constraint_row.contype = 'f'
          AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND NOT constraint_row.convalidated
    """
    return _count_query(connection, query)


def _foreign_key_orphan_count(connection: Connection[object], foreign_key: ForeignKeyInfo) -> int:
    if not foreign_key.source_columns or len(foreign_key.source_columns) != len(
        foreign_key.target_columns
    ):
        raise VerificationError("foreign key metadata is incomplete")
    source_first = foreign_key.source_columns[0]
    target_first = foreign_key.target_columns[0]
    # PostgreSQL MATCH SIMPLE (the default) treats any row with a NULL
    # referencing component as valid without looking for a parent row. MATCH
    # FULL instead requires all components to be NULL or all to be non-NULL.
    non_null_operator = " AND " if foreign_key.match_type == "SIMPLE" else " OR "
    non_null = sql.SQL(non_null_operator).join(
        sql.SQL("child.{} IS NOT NULL").format(sql.Identifier(column.column))
        for column in foreign_key.source_columns
    )
    matches = sql.SQL(" AND ").join(
        sql.SQL("parent.{} = child.{}").format(
            sql.Identifier(target_column.column),
            sql.Identifier(source_column.column),
        )
        for source_column, target_column in zip(
            foreign_key.source_columns, foreign_key.target_columns, strict=True
        )
    )
    statement = sql.SQL(
        "SELECT COUNT(*) FROM {child_table} AS child "
        "WHERE ({non_null}) AND NOT EXISTS ("
        "SELECT 1 FROM {parent_table} AS parent WHERE {matches}"
        ")"
    ).format(
        child_table=_ref_table_identifier(source_first),
        parent_table=_ref_table_identifier(target_first),
        non_null=non_null,
        matches=matches,
    )
    return _count_query(connection, statement)


def _foreign_key_signature(
    foreign_key: ForeignKeyInfo,
) -> tuple[str, tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]]:
    return (
        foreign_key.match_type,
        tuple(column.key for column in foreign_key.source_columns),
        tuple(column.key for column in foreign_key.target_columns),
    )


def _check_mapping_by_primary_key(
    *,
    source: Connection[object],
    target: Connection[object],
    ref: ColumnRef,
    group_id: str,
    normalization: str,
    allow_empty: bool,
    registry: MappingRegistry,
    hmac_key: HMACKey,
    primary_key: KeyConstraint,
    metrics: _MappingMetrics,
    lookup_cache: _ReplacementLookupCache,
    fetch_size: int,
) -> None:
    statement = _ordered_select_statement(
        schema_name=ref.schema_name,
        table=ref.table,
        key_columns=primary_key.columns,
        value_column=ref.column,
    )
    try:
        with (
            _executed_cursor(source, statement, fetch_size=fetch_size) as source_cursor,
            _executed_cursor(target, statement, fetch_size=fetch_size) as target_cursor,
        ):
            for source_row, target_row in zip_longest(
                _cursor_rows(source_cursor, fetch_size),
                _cursor_rows(target_cursor, fetch_size),
                fillvalue=_MISSING,
            ):
                if source_row is _MISSING or target_row is _MISSING:
                    metrics.primary_key_mismatches += 1
                    continue
                source_key, source_value = tuple(source_row[:-1]), source_row[-1]
                target_key, target_value = tuple(target_row[:-1]), target_row[-1]
                if source_key != target_key:
                    metrics.primary_key_mismatches += 1
                    continue
                metrics.rows_checked += 1
                _compare_mapping_values(
                    source_value=source_value,
                    target_value=target_value,
                    group_id=group_id,
                    normalization=normalization,
                    allow_empty=allow_empty,
                    registry=registry,
                    hmac_key=hmac_key,
                    metrics=metrics,
                    lookup_cache=lookup_cache,
                )
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("unable to verify primary-key-aligned mappings") from None


def _check_mapping_aggregate(
    *,
    source: Connection[object],
    target: Connection[object],
    ref: ColumnRef,
    group_id: str,
    normalization: str,
    allow_empty: bool,
    registry: MappingRegistry,
    hmac_key: HMACKey,
    metrics: _MappingMetrics,
    lookup_cache: _ReplacementLookupCache,
    fetch_size: int,
) -> None:
    source_nulls = 0
    target_nulls = 0
    with registry.verification_hmac_accumulator(group_id) as accumulator:
        expected_batch: list[str] = []
        for source_value in _iter_column_values(source, ref, fetch_size=fetch_size):
            metrics.rows_checked += 1
            if source_value is None:
                source_nulls += 1
                continue
            replacement = _expected_replacement(
                source_value=source_value,
                group_id=group_id,
                normalization=normalization,
                allow_empty=allow_empty,
                registry=registry,
                hmac_key=hmac_key,
                metrics=metrics,
                lookup_cache=lookup_cache,
            )
            if replacement is not None:
                # HMAC of the exact replacement is intentionally separate from the
                # normalized source key: this detects a target text mutation too.
                expected_batch.append(hmac_key.digest(group_id, replacement))
                if len(expected_batch) >= fetch_size:
                    accumulator.add_expected(expected_batch)
                    expected_batch.clear()
        if expected_batch:
            accumulator.add_expected(expected_batch)

        actual_batch: list[str] = []
        for target_value in _iter_column_values(target, ref, fetch_size=fetch_size):
            if target_value is None:
                target_nulls += 1
                continue
            if not isinstance(target_value, str):
                metrics.invalid_target_values += 1
                continue
            actual_batch.append(hmac_key.digest(group_id, target_value))
            if len(actual_batch) >= fetch_size:
                accumulator.add_actual(actual_batch)
                actual_batch.clear()
        if actual_batch:
            accumulator.add_actual(actual_batch)

        mapping_mismatches = accumulator.multiset_difference_count()

    metrics.null_mismatches += abs(source_nulls - target_nulls)
    metrics.mapping_mismatches += mapping_mismatches


def _compare_mapping_values(
    *,
    source_value: object,
    target_value: object,
    group_id: str,
    normalization: str,
    allow_empty: bool,
    registry: MappingRegistry,
    hmac_key: HMACKey,
    metrics: _MappingMetrics,
    lookup_cache: _ReplacementLookupCache,
) -> None:
    if source_value is None:
        if target_value is not None:
            metrics.null_mismatches += 1
        return
    replacement = _expected_replacement(
        source_value=source_value,
        group_id=group_id,
        normalization=normalization,
        allow_empty=allow_empty,
        registry=registry,
        hmac_key=hmac_key,
        metrics=metrics,
        lookup_cache=lookup_cache,
    )
    if replacement is None:
        return
    if target_value is None:
        metrics.null_mismatches += 1
        return
    if not isinstance(target_value, str):
        metrics.invalid_target_values += 1
        return
    if target_value != replacement:
        metrics.mapping_mismatches += 1
    if isinstance(source_value, str) and target_value == source_value:
        metrics.unchanged_values += 1


def _expected_replacement(
    *,
    source_value: object,
    group_id: str,
    normalization: str,
    allow_empty: bool,
    registry: MappingRegistry,
    hmac_key: HMACKey,
    metrics: _MappingMetrics,
    lookup_cache: _ReplacementLookupCache,
) -> str | None:
    normalized = _normalize_for_check(source_value, normalization, allow_empty)
    if normalized is None:
        metrics.invalid_source_values += 1
        return None
    replacement = lookup_cache.lookup(hmac_key.digest(group_id, normalized))
    if not isinstance(replacement, str) or not replacement:
        metrics.missing_mappings += 1
        return None
    return replacement


def _normalize_for_check(value: object, normalization: str, allow_empty: bool) -> str | None:
    try:
        return normalize(value, normalization, allow_empty=allow_empty)  # type: ignore[arg-type]
    except NormalizationError:
        return None


def _primary_key_is_configured(policy: SanitizerPolicy, constraint: KeyConstraint) -> bool:
    return any(
        policy.group_for_column(
            ColumnRef(schema=constraint.schema_name, table=constraint.table, column=column)
        )
        is not None
        for column in constraint.columns
    )


def _ordered_select_statement(
    *,
    schema_name: str,
    table: str,
    key_columns: Sequence[str],
    value_column: str | None = None,
) -> sql.Composed:
    if not key_columns:
        raise VerificationError("verification key columns are missing")
    identifiers = [sql.Identifier(column) for column in key_columns]
    selected = list(identifiers)
    if value_column is not None:
        selected.append(sql.Identifier(value_column))
    return sql.SQL("SELECT {selected} FROM {table} ORDER BY {ordering}").format(
        selected=sql.SQL(", ").join(selected),
        table=_table_identifier(schema_name, table),
        ordering=sql.SQL(", ").join(identifiers),
    )


@contextmanager
def _executed_cursor(
    connection: Connection[object], statement: object, *, fetch_size: int
) -> Iterator[object]:
    """Open a named, bounded server cursor for row-wise verification work."""

    if fetch_size < 1:
        raise VerificationError("verification fetch size must be positive")
    transaction = connection.transaction() if connection.autocommit else nullcontext()
    cursor: object | None = None
    try:
        with transaction:
            cursor = connection.cursor(
                name=f"sanitizer_verify_{next(_VERIFY_CURSOR_IDS)}",
                row_factory=tuple_row,
                # Target verification connections use autocommit; hold the cursor
                # for the explicit transaction opened above.
                withhold=connection.autocommit,
            )
            cursor.itersize = fetch_size  # type: ignore[attr-defined]
            cursor.execute(statement)  # type: ignore[attr-defined]
            try:
                yield cursor
            finally:
                with suppress(Exception):
                    cursor.close()  # type: ignore[attr-defined]
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("unable to execute streaming verification query") from None


def _cursor_rows(cursor: object, fetch_size: int) -> Iterator[object]:
    """Yield bounded fetch batches without materializing a complete result set."""

    while rows := cursor.fetchmany(fetch_size):  # type: ignore[attr-defined]
        yield from rows


def _iter_column_values(
    connection: Connection[object], ref: ColumnRef, *, fetch_size: int
) -> Iterator[object]:
    statement = sql.SQL("SELECT {column} FROM {table}").format(
        column=sql.Identifier(ref.column),
        table=_ref_table_identifier(ref),
    )
    try:
        with _executed_cursor(connection, statement, fetch_size=fetch_size) as cursor:
            for row in _cursor_rows(cursor, fetch_size):
                yield row[0]
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("unable to stream configured column values") from None


def _count_query(connection: Connection[object], statement: object) -> int:
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
            row = cursor.fetchone()
        if row is None:
            raise TypeError("count query did not return a row")
        value = int(row[0])
        _validate_count(value)
        return value
    except VerificationError:
        raise
    except Exception:
        raise VerificationError("unable to execute verification aggregate") from None


def _table_identifier(schema_name: str, table: str) -> sql.Composed:
    _validate_identifier(schema_name)
    _validate_identifier(table)
    return sql.SQL("{}.{}").format(sql.Identifier(schema_name), sql.Identifier(table))


def _ref_table_identifier(ref: ColumnRef) -> sql.Composed:
    return _table_identifier(ref.schema_name, ref.table)


def _validate_identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("verification identifiers must be safe PostgreSQL identifiers")


def _validate_count(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("verification counts must be non-negative integers")
