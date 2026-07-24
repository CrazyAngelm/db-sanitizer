"""Transactional SQLite storage for opaque source-to-replacement mappings."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from db_sanitizer.errors import SanitizerError


class RegistryError(SanitizerError):
    """Safe base error for mapping registry failures."""


class RegistryCompatibilityError(RegistryError):
    """A resumed run does not match the registry's immutable metadata."""


class RegistryConflictError(RegistryError):
    """A mapping would violate the one-to-one registry contract."""


@dataclass(frozen=True, slots=True)
class RunMetadata:
    run_id: str
    policy_sha256: str
    source_schema_sha256: str
    llm_provider: str
    llm_model: str
    hmac_key_fingerprint: str
    status: str = "initialized"


@dataclass(frozen=True, slots=True)
class MappingRecord:
    group_id: str
    source_hmac: str
    replacement: str | None
    replacement_hmac: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_meta (
    run_id TEXT PRIMARY KEY,
    policy_sha256 TEXT NOT NULL,
    source_schema_sha256 TEXT NOT NULL,
    llm_provider TEXT NOT NULL,
    llm_model TEXT NOT NULL,
    hmac_key_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings (
    group_id TEXT NOT NULL,
    source_hmac TEXT NOT NULL,
    replacement TEXT NULL,
    replacement_hmac TEXT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, source_hmac),
    UNIQUE (group_id, replacement),
    UNIQUE (group_id, replacement_hmac)
);
CREATE TABLE IF NOT EXISTS generation_stats (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    batches INTEGER NOT NULL DEFAULT 0,
    accepted_items INTEGER NOT NULL DEFAULT 0,
    rejected_items INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS verification_hmac_work (
    value_hmac TEXT PRIMARY KEY,
    expected_count INTEGER NOT NULL DEFAULT 0,
    actual_count INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
"""


class VerificationHmacAccumulator:
    """Disk-backed exact HMAC multiset used only while verifying one scope."""

    def __init__(self, registry: MappingRegistry, group_id: str) -> None:
        self._registry = registry
        self._group_id = group_id

    def add_expected(self, hmacs: Iterable[str]) -> None:
        self._add(hmacs, "expected_count")

    def add_actual(self, hmacs: Iterable[str]) -> None:
        self._add(hmacs, "actual_count")

    def actual_distinct_count(self) -> int:
        return self._count("SELECT COUNT(*) FROM verification_hmac_work WHERE actual_count > 0")

    def source_intersection_count(self) -> int:
        return self._count(
            """
            SELECT COUNT(*)
            FROM verification_hmac_work AS work
            WHERE work.actual_count > 0
              AND EXISTS (
                  SELECT 1
                  FROM mappings
                  WHERE mappings.group_id = ?
                    AND mappings.source_hmac = work.value_hmac
              )
            """,
            (self._group_id,),
        )

    def multiset_difference_count(self) -> int:
        return self._count(
            "SELECT COALESCE(SUM(ABS(expected_count - actual_count)), 0) "
            "FROM verification_hmac_work"
        )

    def _add(self, hmacs: Iterable[str], count_column: str) -> None:
        batch = tuple(hmacs)
        if not batch:
            return
        for value_hmac in batch:
            self._registry._validate_key(self._group_id, value_hmac)
        try:
            with self._registry._connection:
                self._registry._connection.executemany(
                    f"""
                    INSERT INTO verification_hmac_work (value_hmac, {count_column}) VALUES (?, 1)
                    ON CONFLICT(value_hmac) DO UPDATE
                    SET {count_column} = {count_column} + excluded.{count_column}
                    """,
                    ((value_hmac,) for value_hmac in batch),
                )
        except sqlite3.Error as error:
            raise RegistryError("unable to store verification HMAC work") from error

    def _count(self, statement: str, parameters: Sequence[object] = ()) -> int:
        try:
            row = self._registry._connection.execute(statement, parameters).fetchone()
            if row is None:
                raise TypeError("verification HMAC aggregate did not return a row")
            value = int(row[0])
            if value < 0:
                raise ValueError("verification HMAC aggregate is negative")
            return value
        except (TypeError, ValueError, sqlite3.Error) as error:
            raise RegistryError("unable to read verification HMAC work") from error


class MappingRegistry:
    """A single-run registry which never accepts raw source values."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            os.chmod(self.path, 0o600)
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            # Keep the SQLite page cache bounded; large verifier work spills to disk.
            self._connection.execute("PRAGMA cache_size = -8192")
            self._connection.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as error:
            raise RegistryError("unable to initialize mapping registry") from error

    def __enter__(self) -> MappingRegistry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def initialize(self, metadata: RunMetadata) -> None:
        """Create, or validate, immutable metadata before any mappings are used."""
        self._validate_metadata(metadata)
        try:
            rows = self._connection.execute("SELECT * FROM run_meta").fetchall()
            if not rows:
                with self._connection:
                    self._connection.execute(
                        """INSERT INTO run_meta (
                            run_id, policy_sha256, source_schema_sha256, llm_provider,
                            llm_model, hmac_key_fingerprint, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            metadata.run_id,
                            metadata.policy_sha256,
                            metadata.source_schema_sha256,
                            metadata.llm_provider,
                            metadata.llm_model,
                            metadata.hmac_key_fingerprint,
                            metadata.status,
                        ),
                    )
                    self._connection.execute(
                        "INSERT OR IGNORE INTO generation_stats (id) VALUES (1)"
                    )
                return
            expected = {
                "run_id": metadata.run_id,
                "policy_sha256": metadata.policy_sha256,
                "source_schema_sha256": metadata.source_schema_sha256,
                "llm_provider": metadata.llm_provider,
                "llm_model": metadata.llm_model,
                "hmac_key_fingerprint": metadata.hmac_key_fingerprint,
                "status": metadata.status,
            }
            actual = dict(rows[0])
            actual.pop("created_at", None)
            if len(rows) != 1 or actual != expected:
                raise RegistryCompatibilityError("mapping registry metadata is incompatible")
            with self._connection:
                self._connection.execute("INSERT OR IGNORE INTO generation_stats (id) VALUES (1)")
        except RegistryError:
            raise
        except sqlite3.Error as error:
            raise RegistryError("unable to validate mapping registry metadata") from error

    def insert_source_key(self, group_id: str, source_hmac: str) -> bool:
        """Persist one opaque key; return whether it was newly inserted."""
        self._validate_key(group_id, source_hmac)
        try:
            with self._connection:
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO mappings (group_id, source_hmac) VALUES (?, ?)",
                    (group_id, source_hmac),
                )
            return cursor.rowcount == 1
        except sqlite3.Error as error:
            raise RegistryError("unable to store mapping key") from error

    def insert_source_keys(self, group_id: str, source_hmacs: Iterable[str]) -> int:
        """Transactionally insert opaque keys and return the number newly stored."""
        keys = tuple(source_hmacs)
        for source_hmac in keys:
            self._validate_key(group_id, source_hmac)
        try:
            with self._connection:
                cursor = self._connection.executemany(
                    "INSERT OR IGNORE INTO mappings (group_id, source_hmac) VALUES (?, ?)",
                    ((group_id, source_hmac) for source_hmac in keys),
                )
            return cursor.rowcount if cursor.rowcount >= 0 else 0
        except sqlite3.Error as error:
            raise RegistryError("unable to store mapping keys") from error

    def list_unassigned(self, group_id: str, *, limit: int | None = None) -> list[str]:
        """List pending source keys in the stable generation order."""
        if not isinstance(group_id, str) or not group_id:
            raise RegistryError("mapping group identifier must be a non-empty string")
        if limit is not None and limit < 1:
            raise RegistryError("mapping list limit must be positive")
        query = (
            "SELECT source_hmac FROM mappings WHERE group_id = ? "
            "AND replacement IS NULL ORDER BY source_hmac"
        )
        parameters: Sequence[object] = (group_id,)
        if limit is not None:
            query += " LIMIT ?"
            parameters = (group_id, limit)
        try:
            return [row[0] for row in self._connection.execute(query, parameters)]
        except sqlite3.Error as error:
            raise RegistryError("unable to list pending mapping keys") from error

    def assign_replacements(
        self,
        group_id: str,
        assignments: Iterable[tuple[str, str, str]],
        *,
        record_generated_items: bool = False,
    ) -> None:
        """Atomically assign mappings and optionally persist accepted LLM evidence."""
        batch = tuple(assignments)
        if not batch:
            return
        sources: set[str] = set()
        replacements: set[str] = set()
        for source_hmac, replacement, replacement_hmac in batch:
            self._validate_key(group_id, source_hmac)
            self._validate_key(group_id, replacement_hmac)
            if not isinstance(replacement, str) or not replacement:
                raise RegistryConflictError("replacement must be a non-empty string")
            if source_hmac in sources or replacement in replacements:
                raise RegistryConflictError("mapping batch contains duplicate keys or replacements")
            sources.add(source_hmac)
            replacements.add(replacement)
        try:
            with self._connection:
                for source_hmac, _, replacement_hmac in batch:
                    source_row = self._connection.execute(
                        "SELECT replacement FROM mappings WHERE group_id = ? AND source_hmac = ?",
                        (group_id, source_hmac),
                    ).fetchone()
                    if source_row is None:
                        raise RegistryConflictError("mapping key is not registered")
                    if source_row[0] is not None:
                        raise RegistryConflictError("mapping key already has a replacement")
                    if (
                        self._connection.execute(
                            "SELECT 1 FROM mappings WHERE group_id = ? AND source_hmac = ?",
                            (group_id, replacement_hmac),
                        ).fetchone()
                        is not None
                    ):
                        raise RegistryConflictError("replacement collides with a source key")
                self._connection.executemany(
                    """UPDATE mappings SET replacement = ?, replacement_hmac = ?,
                    updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ? AND source_hmac = ? AND replacement IS NULL""",
                    (
                        (replacement, replacement_hmac, group_id, source_hmac)
                        for source_hmac, replacement, replacement_hmac in batch
                    ),
                )
                assigned = self._connection.execute(
                    "SELECT COUNT(*) FROM mappings WHERE group_id = ? AND source_hmac IN ("
                    + ", ".join("?" for _ in sources)
                    + ") AND replacement IS NOT NULL",
                    (group_id, *sources),
                ).fetchone()[0]
                if assigned != len(batch):
                    raise RegistryConflictError("mapping key already has a replacement")
                if record_generated_items:
                    self._connection.execute(
                        "UPDATE generation_stats SET accepted_items = accepted_items + ? "
                        "WHERE id = 1",
                        (len(batch),),
                    )
        except RegistryError:
            raise
        except sqlite3.IntegrityError as error:
            raise RegistryConflictError("replacement is already assigned in this group") from error
        except sqlite3.Error as error:
            raise RegistryError("unable to assign replacements") from error

    def assign_replacement(
        self, group_id: str, source_hmac: str, replacement: str, replacement_hmac: str
    ) -> None:
        self.assign_replacements(group_id, [(source_hmac, replacement, replacement_hmac)])

    def record_generation_stats(
        self,
        *,
        batches: int = 0,
        accepted_items: int = 0,
        rejected_items: int = 0,
        duration_seconds: float = 0.0,
    ) -> None:
        """Persist safe LLM evidence so a crash/resume does not erase it."""
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (batches, accepted_items, rejected_items)
            )
            or not isinstance(duration_seconds, (int, float))
            or duration_seconds < 0
        ):
            raise RegistryError("generation statistics must be non-negative")
        try:
            with self._connection:
                self._connection.execute(
                    """
                    UPDATE generation_stats
                    SET batches = batches + ?,
                        accepted_items = accepted_items + ?,
                        rejected_items = rejected_items + ?,
                        duration_seconds = duration_seconds + ?
                    WHERE id = 1
                    """,
                    (batches, accepted_items, rejected_items, float(duration_seconds)),
                )
        except sqlite3.Error as error:
            raise RegistryError("unable to persist generation statistics") from error

    def generation_stats(self) -> tuple[int, int, int, float]:
        """Return batches, accepted, rejected, and duration without generated values."""
        try:
            row = self._connection.execute(
                "SELECT batches, accepted_items, rejected_items, duration_seconds "
                "FROM generation_stats WHERE id = 1"
            ).fetchone()
            if row is None:
                raise RegistryError("generation statistics are unavailable")
            return (int(row[0]), int(row[1]), int(row[2]), float(row[3]))
        except RegistryError:
            raise
        except (TypeError, ValueError, sqlite3.Error) as error:
            raise RegistryError("generation statistics are invalid") from error

    def has_source_key(self, group_id: str, source_hmac: str) -> bool:
        """Return whether an opaque source key belongs to this group."""
        self._validate_key(group_id, source_hmac)
        try:
            row = self._connection.execute(
                "SELECT 1 FROM mappings WHERE group_id = ? AND source_hmac = ?",
                (group_id, source_hmac),
            ).fetchone()
            return row is not None
        except sqlite3.Error as error:
            raise RegistryError("unable to check mapping key") from error

    def has_replacement_hmac(self, group_id: str, replacement_hmac: str) -> bool:
        """Return whether a normalized replacement is already allocated in this group."""
        self._validate_key(group_id, replacement_hmac)
        try:
            row = self._connection.execute(
                "SELECT 1 FROM mappings WHERE group_id = ? AND replacement_hmac = ?",
                (group_id, replacement_hmac),
            ).fetchone()
            return row is not None
        except sqlite3.Error as error:
            raise RegistryError("unable to check replacement uniqueness") from error

    def mapping_count(self, group_id: str, *, assigned_only: bool = False) -> int:
        """Return a safe aggregate count for reporting and checkpoints."""
        if not isinstance(group_id, str) or not group_id:
            raise RegistryError("mapping group identifier must be a non-empty string")
        query = "SELECT COUNT(*) FROM mappings WHERE group_id = ?"
        if assigned_only:
            query += " AND replacement IS NOT NULL"
        try:
            return int(self._connection.execute(query, (group_id,)).fetchone()[0])
        except sqlite3.Error as error:
            raise RegistryError("unable to count mappings") from error

    @contextmanager
    def verification_hmac_accumulator(self, group_id: str) -> Iterator[VerificationHmacAccumulator]:
        """Provide one cleaned disk-backed HMAC work scope for verifier scans."""

        if not isinstance(group_id, str) or not group_id:
            raise RegistryError("mapping group identifier must be a non-empty string")
        try:
            with self._connection:
                self._connection.execute("DELETE FROM verification_hmac_work")
            yield VerificationHmacAccumulator(self, group_id)
        except RegistryError:
            raise
        except sqlite3.Error as error:
            raise RegistryError("unable to prepare verification HMAC work") from error
        finally:
            try:
                with self._connection:
                    self._connection.execute("DELETE FROM verification_hmac_work")
            except sqlite3.Error as error:
                raise RegistryError("unable to clear verification HMAC work") from error

    def lookup(self, group_id: str, source_hmac: str) -> str | None:
        """Return a replacement or ``None``; callers must fail closed when absent."""
        self._validate_key(group_id, source_hmac)
        try:
            row = self._connection.execute(
                "SELECT replacement FROM mappings WHERE group_id = ? AND source_hmac = ?",
                (group_id, source_hmac),
            ).fetchone()
            return None if row is None else row[0]
        except sqlite3.Error as error:
            raise RegistryError("unable to look up mapping") from error

    @staticmethod
    def _validate_key(group_id: str, value: str) -> None:
        if not isinstance(group_id, str) or not group_id:
            raise RegistryError("mapping group identifier must be a non-empty string")
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise RegistryError("mapping HMAC must be a lowercase SHA-256 hex digest")

    @staticmethod
    def _validate_metadata(metadata: RunMetadata) -> None:
        if not isinstance(metadata, RunMetadata) or not all(
            isinstance(value, str) and value
            for value in (
                metadata.run_id,
                metadata.policy_sha256,
                metadata.source_schema_sha256,
                metadata.llm_provider,
                metadata.llm_model,
                metadata.hmac_key_fingerprint,
                metadata.status,
            )
        ):
            raise RegistryError("mapping registry metadata must contain non-empty strings")
