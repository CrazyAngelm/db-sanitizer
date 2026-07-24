"""Streaming distinct-key collection without retaining source PII in application state."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import Connection, sql
from psycopg.rows import tuple_row

from db_sanitizer.errors import PolicyError
from db_sanitizer.mapping import HMACKey, MappingRegistry, NormalizationError, normalize
from db_sanitizer.policy.models import SanitizerPolicy


@dataclass(frozen=True, slots=True)
class GroupCollectionStats:
    group_id: str
    configured_columns: int
    distinct_values_seen: int
    inserted_keys: int


def _distinct_values_query(schema: str, table: str, column: str) -> sql.Composed:
    identifier = sql.Identifier(column)
    return sql.SQL(
        "SELECT DISTINCT {column} FROM {schema}.{table} WHERE {column} IS NOT NULL"
    ).format(
        column=identifier,
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
    )


def collect_mapping_keys(
    connection: Connection[object],
    policy: SanitizerPolicy,
    hmac_key: HMACKey,
    registry: MappingRegistry,
) -> dict[str, GroupCollectionStats]:
    """Stream every explicit sensitive column into opaque registry keys.

    Named cursors and bounded ``fetchmany`` batches guarantee that Python never
    materializes a table or a group of raw source values in memory.
    """

    result: dict[str, GroupCollectionStats] = {}
    cursor_number = 0
    for group_id, group in policy.groups.items():
        distinct_values_seen = 0
        inserted_keys = 0
        for column in group.columns:
            cursor_number += 1
            cursor_name = f"sanitizer_collect_{cursor_number}"
            try:
                with connection.cursor(name=cursor_name, row_factory=tuple_row) as cursor:
                    cursor.itersize = policy.run.collector_fetch_size
                    cursor.execute(
                        _distinct_values_query(column.schema_name, column.table, column.column)
                    )
                    while rows := cursor.fetchmany(policy.run.collector_fetch_size):
                        batch_keys: list[str] = []
                        for row in rows:
                            raw_value = row[0]
                            try:
                                normalized = normalize(
                                    raw_value,
                                    group.normalization.value,
                                    allow_empty=group.allow_empty,
                                )
                            except NormalizationError:
                                raise PolicyError(
                                    "configured sensitive column contains an invalid value: "
                                    f"{column.display_name}"
                                ) from None
                            source_hmac = hmac_key.digest(group_id, normalized)
                            batch_keys.append(source_hmac)
                            distinct_values_seen += 1
                            # Do not accidentally retain the source value beyond this iteration.
                            del raw_value
                        inserted_keys += registry.insert_source_keys(group_id, batch_keys)
            except PolicyError:
                raise
            except Exception:
                # Driver details can contain query data in some implementations; do not surface it.
                raise PolicyError(
                    f"unable to collect mapping keys from configured column {column.display_name}"
                ) from None
        result[group_id] = GroupCollectionStats(
            group_id=group_id,
            configured_columns=len(group.columns),
            distinct_values_seen=distinct_values_seen,
            inserted_keys=inserted_keys,
        )
    return result
