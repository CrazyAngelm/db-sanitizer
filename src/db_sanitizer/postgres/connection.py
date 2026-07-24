"""Safe PostgreSQL connection helpers for source inspection and target restore."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import psycopg
from psycopg import Connection, sql
from psycopg.conninfo import conninfo_to_dict

from db_sanitizer.errors import DataPlaneError, PolicyError


@contextmanager
def source_connection(dsn: str) -> Iterator[Connection[tuple[object, ...]]]:
    """Open a transaction-scoped read-only source connection."""

    try:
        connection = psycopg.connect(dsn, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
    except psycopg.Error:
        raise PolicyError("unable to connect to source database in read-only mode") from None

    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def target_connection(dsn: str) -> Iterator[Connection[tuple[object, ...]]]:
    """Open the separate target connection used only for reset/verification."""

    try:
        connection = psycopg.connect(dsn, autocommit=True)
    except psycopg.Error:
        raise DataPlaneError("unable to connect to target database") from None

    try:
        yield connection
    finally:
        connection.close()


def reset_target_database(dsn: str) -> None:
    """Clear non-system schemas before restore when policy explicitly permits it."""

    try:
        with target_connection(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT nspname
                FROM pg_namespace
                WHERE nspname NOT LIKE 'pg_%'
                  AND nspname <> 'information_schema'
                ORDER BY nspname
                """
            )
            schemas = [str(row[0]) for row in cursor.fetchall()]
            for schema_name in schemas:
                statement = sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
                cursor.execute(statement)
            cursor.execute("CREATE SCHEMA public")
            cursor.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
    except DataPlaneError:
        raise
    except psycopg.Error:
        raise DataPlaneError("unable to reset target database before restore") from None


def dsn_to_pg_environment(dsn: str) -> dict[str, str]:
    """Convert a DSN into Greenmask's PostgreSQL client environment variables.

    The returned password is intentionally short-lived process environment only;
    callers must not log or serialize this mapping.
    """

    try:
        values = conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        raise DataPlaneError("database DSN cannot be converted for Greenmask") from None

    name_map: dict[str, tuple[str, Literal["required", "optional"]]] = {
        "host": ("PGHOST", "required"),
        "port": ("PGPORT", "optional"),
        "dbname": ("PGDATABASE", "required"),
        "user": ("PGUSER", "required"),
        "password": ("PGPASSWORD", "optional"),
        "sslmode": ("PGSSLMODE", "optional"),
    }
    environment: dict[str, str] = {}
    for input_name, (output_name, required) in name_map.items():
        value = values.get(input_name)
        if value is None or value == "":
            if required == "required":
                raise DataPlaneError("database DSN lacks a Greenmask connection field")
            continue
        environment[output_name] = str(value)
    return environment
