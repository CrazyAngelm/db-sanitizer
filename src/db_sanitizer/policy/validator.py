"""Fail-fast policy validation that is aware of PostgreSQL schema metadata."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from db_sanitizer.errors import PolicyError
from db_sanitizer.policy.models import ColumnRef, SanitizerPolicy
from db_sanitizer.postgres.inspector import ForeignKeyInfo, SchemaSnapshot

_SUPPORTED_TEXT_TYPES = {"text", "character varying", "character", "varchar", "char"}


def validate_run_directory(policy: SanitizerPolicy) -> Path:
    """Create the run root with restrictive permissions or fail before a run."""

    root = policy.run.directory.expanduser()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root.is_dir():
            raise OSError("not a directory")
        # Windows does not implement POSIX mode bits. Existence remains the relevant check.
        with suppress(OSError):
            os.chmod(root, 0o700)
    except OSError as exc:
        raise PolicyError(f"cannot create configured run directory {root}") from exc
    return root.resolve()


def _group_membership(policy: SanitizerPolicy) -> dict[tuple[str, str, str], str]:
    return {
        column.key: group_id
        for group_id, group in policy.groups.items()
        for column in group.columns
    }


def _validate_foreign_key_coverage(
    foreign_key: ForeignKeyInfo,
    membership: dict[tuple[str, str, str], str],
) -> None:
    relationship_columns = (*foreign_key.source_columns, *foreign_key.target_columns)
    assigned = {
        membership[column.key] for column in relationship_columns if column.key in membership
    }
    if not assigned:
        return
    if len(assigned) != 1 or any(column.key not in membership for column in relationship_columns):
        raise PolicyError(
            "configured natural PK/FK column must cover every column of foreign key "
            f"{foreign_key.name!r} in one consistency group"
        )


def validate_policy_against_schema(policy: SanitizerPolicy, snapshot: SchemaSnapshot) -> None:
    """Reject unsafe, incomplete, or unsupported mappings before side effects."""

    if not policy.run.allow_target_recreate:
        raise PolicyError("run.allow_target_recreate must be true for dump restore")

    membership = _group_membership(policy)
    for group_id, group in policy.groups.items():
        for column in group.columns:
            info = snapshot.column_for(column)
            if info is None:
                raise PolicyError(f"configured column does not exist: {column.display_name}")
            if info.postgres_type.casefold() not in _SUPPORTED_TEXT_TYPES:
                raise PolicyError(
                    f"configured column has unsupported PostgreSQL type: {column.display_name}"
                )
            maximum = info.character_maximum_length
            if maximum is not None and group.generation.max_length > maximum:
                raise PolicyError(
                    "generation max_length exceeds configured column limit: "
                    f"{column.display_name} in group {group_id!r}"
                )

    for foreign_key in snapshot.foreign_keys:
        _validate_foreign_key_coverage(foreign_key, membership)


def configured_columns(policy: SanitizerPolicy) -> tuple[ColumnRef, ...]:
    """Return every configured column in deterministic policy-independent order."""

    return tuple(
        sorted(
            (column for group in policy.groups.values() for column in group.columns),
            key=lambda column: column.key,
        )
    )
