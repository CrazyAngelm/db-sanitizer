"""PostgreSQL adapters for safe inspection, collection, and connections."""

from db_sanitizer.postgres.collector import GroupCollectionStats, collect_mapping_keys
from db_sanitizer.postgres.connection import (
    dsn_to_pg_environment,
    reset_target_database,
    source_connection,
    target_connection,
)
from db_sanitizer.postgres.inspector import SchemaSnapshot, inspect_schema

__all__ = [
    "GroupCollectionStats",
    "SchemaSnapshot",
    "collect_mapping_keys",
    "dsn_to_pg_environment",
    "inspect_schema",
    "reset_target_database",
    "source_connection",
    "target_connection",
]
