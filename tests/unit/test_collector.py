"""Collector regression tests without a PostgreSQL server."""

from __future__ import annotations

from pathlib import Path

from db_sanitizer.mapping import HMACKey, MappingRegistry, RunMetadata
from db_sanitizer.policy.models import SanitizerPolicy
from db_sanitizer.postgres.collector import collect_mapping_keys

_SECRET = b"c" * 32


class _StreamingCursor:
    def __init__(self, batches: list[list[tuple[str]]]) -> None:
        self._batches = iter(batches)
        self.itersize: int | None = None
        self.fetch_sizes: list[int] = []
        self.executed = False

    def __enter__(self) -> _StreamingCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, _statement: object) -> None:
        self.executed = True

    def fetchmany(self, size: int) -> list[tuple[str]]:
        self.fetch_sizes.append(size)
        return next(self._batches, [])


class _StreamingConnection:
    def __init__(self) -> None:
        self.cursors: list[_StreamingCursor] = []

    def cursor(self, *, name: str, row_factory: object) -> _StreamingCursor:
        assert name.startswith("sanitizer_collect_")
        assert row_factory is not None
        cursor = _StreamingCursor([[(" Alice ",), ("ALICE",)], [("Bob",)]])
        self.cursors.append(cursor)
        return cursor


def _policy() -> SanitizerPolicy:
    return SanitizerPolicy.model_validate(
        {
            "version": 1,
            "run": {
                "directory": ".runs",
                "allow_target_recreate": True,
                "collector_fetch_size": 2,
            },
            "connections": {
                "source_dsn_env": "SOURCE_DATABASE_URL",
                "target_dsn_env": "TARGET_DATABASE_URL",
            },
            "mapping": {"hmac_key_env": "SANITIZER_HMAC_KEY"},
            "llm": {
                "provider": "openrouter",
                "base_url_env": "OPENROUTER_BASE_URL",
                "api_key_env": "OPENROUTER_API_KEY",
                "model": "test-model",
            },
            "greenmask": {},
            "groups": {
                "person_name": {
                    "entity_type": "person_name",
                    "locale": "en-US",
                    "normalization": "human_text",
                    "generation": {
                        "description": "synthetic person name",
                        "min_length": 3,
                        "max_length": 100,
                    },
                    "columns": [{"schema": "public", "table": "people", "column": "full_name"}],
                }
            },
            "report": {},
        }
    )


def _metadata() -> RunMetadata:
    return RunMetadata(
        run_id="collector-unit",
        policy_sha256="a" * 64,
        source_schema_sha256="b" * 64,
        llm_provider="fake",
        llm_model="test-fake",
        hmac_key_fingerprint=HMACKey(_SECRET).fingerprint,
    )


def test_collector_uses_named_cursor_and_bounded_fetch_batches(tmp_path: Path) -> None:
    connection = _StreamingConnection()
    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        result = collect_mapping_keys(connection, _policy(), HMACKey(_SECRET), registry)  # type: ignore[arg-type]

        assert result["person_name"].distinct_values_seen == 3
        assert result["person_name"].inserted_keys == 2
        assert registry.mapping_count("person_name") == 2
        assert all(cursor.itersize == 2 for cursor in connection.cursors)
        assert all(cursor.fetch_sizes == [2, 2, 2] for cursor in connection.cursors)
        assert b"Alice" not in (tmp_path / "mappings.sqlite3").read_bytes()
