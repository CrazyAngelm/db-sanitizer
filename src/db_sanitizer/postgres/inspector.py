"""PostgreSQL schema metadata contracts and safe introspection helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from psycopg import Connection, sql
from psycopg.rows import dict_row

from db_sanitizer.policy.models import ColumnRef


@dataclass(frozen=True)
class ColumnInfo:
    ref: ColumnRef
    postgres_type: str
    character_maximum_length: int | None
    is_primary_key: bool = False
    is_unique: bool = False


@dataclass(frozen=True)
class ForeignKeyInfo:
    name: str
    source_columns: tuple[ColumnRef, ...]
    target_columns: tuple[ColumnRef, ...]
    match_type: str = "SIMPLE"

    def __post_init__(self) -> None:
        if self.match_type not in {"SIMPLE", "FULL"}:
            raise ValueError("unsupported foreign-key match type")
        if not self.source_columns or len(self.source_columns) != len(self.target_columns):
            raise ValueError("foreign key columns must be non-empty and paired")


@dataclass(frozen=True)
class SchemaSnapshot:
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]

    def column_for(self, ref: ColumnRef) -> ColumnInfo | None:
        return next((column for column in self.columns if column.ref == ref), None)

    @property
    def fingerprint(self) -> str:
        payload = {
            "columns": [
                {
                    "ref": column.ref.key,
                    "type": column.postgres_type,
                    "max_length": column.character_maximum_length,
                    "primary": column.is_primary_key,
                    "unique": column.is_unique,
                }
                for column in sorted(self.columns, key=lambda item: item.ref.key)
            ],
            "foreign_keys": [
                {
                    "name": foreign_key.name,
                    "source": [column.key for column in foreign_key.source_columns],
                    "target": [column.key for column in foreign_key.target_columns],
                    "match_type": foreign_key.match_type,
                }
                for foreign_key in sorted(self.foreign_keys, key=lambda item: item.name)
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def table_refs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted({(item.ref.schema_name, item.ref.table) for item in self.columns}))


def inspect_schema(connection: Connection[object]) -> SchemaSnapshot:
    """Read non-sensitive PostgreSQL structural metadata from catalog tables."""

    column_query = """
        SELECT
            cols.table_schema AS schema,
            cols.table_name AS table,
            cols.column_name AS column,
            cols.data_type AS postgres_type,
            cols.character_maximum_length AS character_maximum_length,
            EXISTS (
                SELECT 1
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND kcu.table_schema = cols.table_schema
                  AND kcu.table_name = cols.table_name
                  AND kcu.column_name = cols.column_name
            ) AS is_primary_key,
            EXISTS (
                SELECT 1
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'UNIQUE'
                  AND kcu.table_schema = cols.table_schema
                  AND kcu.table_name = cols.table_name
                  AND kcu.column_name = cols.column_name
            ) AS is_unique
        FROM information_schema.columns cols
        WHERE cols.table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY cols.table_schema, cols.table_name, cols.ordinal_position
    """
    foreign_key_query = """
        SELECT
            constraint_row.oid AS constraint_oid,
            source_namespace.nspname AS source_schema,
            source_relation.relname AS source_table,
            constraint_row.conname AS constraint_name,
            source_attribute.attname AS source_column,
            target_namespace.nspname AS target_schema,
            target_relation.relname AS target_table,
            target_attribute.attname AS target_column,
            CASE constraint_row.confmatchtype
                WHEN 'f' THEN 'FULL'
                ELSE 'SIMPLE'
            END AS match_type,
            key_columns.ordinality AS ordinal_position
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS source_relation
          ON source_relation.oid = constraint_row.conrelid
        JOIN pg_namespace AS source_namespace
          ON source_namespace.oid = source_relation.relnamespace
        JOIN pg_class AS target_relation
          ON target_relation.oid = constraint_row.confrelid
        JOIN pg_namespace AS target_namespace
          ON target_namespace.oid = target_relation.relnamespace
        JOIN unnest(constraint_row.conkey, constraint_row.confkey) WITH ORDINALITY
          AS key_columns(source_attnum, target_attnum, ordinality)
          ON TRUE
        JOIN pg_attribute AS source_attribute
          ON source_attribute.attrelid = source_relation.oid
         AND source_attribute.attnum = key_columns.source_attnum
        JOIN pg_attribute AS target_attribute
          ON target_attribute.attrelid = target_relation.oid
         AND target_attribute.attnum = key_columns.target_attnum
        WHERE constraint_row.contype = 'f'
          AND source_namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY source_namespace.nspname, source_relation.relname,
                 constraint_row.conname, constraint_row.oid, key_columns.ordinality
    """

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(column_query)
        columns = tuple(
            ColumnInfo(
                ref=ColumnRef(schema=row["schema"], table=row["table"], column=row["column"]),
                postgres_type=row["postgres_type"],
                character_maximum_length=row["character_maximum_length"],
                is_primary_key=row["is_primary_key"],
                is_unique=row["is_unique"],
            )
            for row in cursor.fetchall()
        )
        cursor.execute(foreign_key_query)
        foreign_rows = cursor.fetchall()

    grouped: dict[int, list[dict[str, object]]] = {}
    for row in foreign_rows:
        grouped.setdefault(int(row["constraint_oid"]), []).append(row)
    foreign_keys = tuple(
        ForeignKeyInfo(
            name=(
                f"{rows[0]['source_schema']}.{rows[0]['source_table']}.{rows[0]['constraint_name']}"
            ),
            source_columns=tuple(
                ColumnRef(
                    schema=str(row["source_schema"]),
                    table=str(row["source_table"]),
                    column=str(row["source_column"]),
                )
                for row in rows
            ),
            target_columns=tuple(
                ColumnRef(
                    schema=str(row["target_schema"]),
                    table=str(row["target_table"]),
                    column=str(row["target_column"]),
                )
                for row in rows
            ),
            match_type=str(rows[0]["match_type"]),
        )
        for _, rows in sorted(grouped.items())
    )
    return SchemaSnapshot(columns=columns, foreign_keys=foreign_keys)


def quote_table(ref: ColumnRef) -> sql.Composed:
    """Produce a safe, quoted qualified table identifier."""

    return sql.SQL("{}.{}").format(sql.Identifier(ref.schema_name), sql.Identifier(ref.table))


def unique_table_columns(
    columns: Iterable[ColumnRef],
) -> dict[tuple[str, str], tuple[ColumnRef, ...]]:
    """Group configured columns by table while preserving sorted deterministic order."""

    grouped: dict[tuple[str, str], list[ColumnRef]] = {}
    for column in columns:
        grouped.setdefault((column.schema_name, column.table), []).append(column)
    return {
        table: tuple(sorted(items, key=lambda item: item.column))
        for table, items in sorted(grouped.items())
    }
