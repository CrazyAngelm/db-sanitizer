"""Regression tests for bounded server-cursor verifier scans."""

from __future__ import annotations

from collections import deque
from contextlib import AbstractContextManager

from db_sanitizer.policy.models import ColumnRef
from db_sanitizer.verify.checks import KeyConstraint, _iter_column_values, _ordered_key_comparison


class _Cursor:
    def __init__(self, batches: list[list[tuple[object, ...]]]) -> None:
        self._batches = deque(batches)
        self.itersize: int | None = None
        self.fetch_sizes: list[int] = []
        self.executed = False
        self.closed = False

    def execute(self, _statement: object) -> None:
        self.executed = True

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        self.fetch_sizes.append(size)
        return self._batches.popleft() if self._batches else []

    def fetchone(self) -> object:
        raise AssertionError("row-wise verifier scans must not use client-buffered fetchone")

    def close(self) -> None:
        self.closed = True


class _Transaction(AbstractContextManager[None]):
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.transactions_started += 1
        return None

    def __exit__(self, *_: object) -> None:
        self._connection.transactions_finished += 1
        return None


class _Connection:
    def __init__(self, batches: list[list[tuple[object, ...]]], *, autocommit: bool) -> None:
        self.autocommit = autocommit
        self._batches = batches
        self.cursors: list[_Cursor] = []
        self.cursor_names: list[str] = []
        self.withhold_flags: list[bool] = []
        self.transactions_started = 0
        self.transactions_finished = 0

    def cursor(self, *, name: str, row_factory: object, withhold: bool) -> _Cursor:
        assert name.startswith("sanitizer_verify_")
        assert row_factory is not None
        cursor = _Cursor(self._batches)
        self.cursors.append(cursor)
        self.cursor_names.append(name)
        self.withhold_flags.append(withhold)
        return cursor

    def transaction(self) -> _Transaction:
        return _Transaction(self)


def test_column_scan_uses_named_cursor_and_bounded_fetchmany() -> None:
    connection = _Connection(
        [[("one",), ("two",)], [("three",)], []],
        autocommit=True,
    )
    ref = ColumnRef(schema="public", table="people", column="email")

    assert list(_iter_column_values(connection, ref, fetch_size=2)) == ["one", "two", "three"]
    cursor = connection.cursors[0]
    assert cursor.itersize == 2
    assert cursor.fetch_sizes == [2, 2, 2]
    assert cursor.closed
    assert connection.withhold_flags == [True]
    assert (connection.transactions_started, connection.transactions_finished) == (1, 1)


def test_paired_key_scan_streams_uneven_fetch_batches() -> None:
    source = _Connection([[(1,), (2,)], [(3,)], []], autocommit=False)
    target = _Connection([[(1,)], [(2,), (3,)], []], autocommit=False)
    constraint = KeyConstraint(
        kind="primary_key",
        schema_name="public",
        table="people",
        columns=("id",),
    )

    rows_checked, mismatches = _ordered_key_comparison(
        source,
        target,
        constraint,
        fetch_size=2,
    )

    assert (rows_checked, mismatches) == (3, 0)
    assert all(cursor.fetch_sizes for cursor in (*source.cursors, *target.cursors))
    assert source.transactions_started == target.transactions_started == 0
