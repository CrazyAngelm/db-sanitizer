"""Long-lived JSONL Cmd transformer process used by Greenmask.

The process has exactly one responsibility: map configured non-NULL text fields
through an already-complete SQLite registry.  It never imports or invokes an
LLM, and all error messages omit raw data.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db_sanitizer.mapping import HMACKey, MappingRegistry, NormalizationError, normalize


class MapperError(Exception):
    """Safe error reported to Greenmask without source values."""


@dataclass(frozen=True, slots=True)
class MapperGroup:
    normalization: str
    allow_empty: bool


@dataclass(frozen=True, slots=True)
class MapperConfig:
    registry_path: Path
    hmac_key_env: str
    groups: dict[str, MapperGroup]
    tables: dict[str, dict[str, str]]


def _parse_config(path: Path) -> MapperConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError("version")
        groups = {
            str(group_id): MapperGroup(
                normalization=str(settings["normalization"]),
                allow_empty=bool(settings["allow_empty"]),
            )
            for group_id, settings in payload["groups"].items()
        }
        tables = {
            str(table): {str(column): str(group) for column, group in columns.items()}
            for table, columns in payload["tables"].items()
        }
        registry_path = Path(str(payload["registry_path"]))
        hmac_key_env = str(payload["hmac_key_env"])
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MapperError("invalid mapper configuration") from error

    if not groups or not tables or not hmac_key_env:
        raise MapperError("invalid mapper configuration")
    for columns in tables.values():
        if not columns or any(group_id not in groups for group_id in columns.values()):
            raise MapperError("invalid mapper configuration")
    return MapperConfig(
        registry_path=registry_path,
        hmac_key_env=hmac_key_env,
        groups=groups,
        tables=tables,
    )


class JsonlMapper:
    """In-process mapper with a bounded opaque HMAC-to-replacement cache."""

    def __init__(self, *, config: MapperConfig, table: str, hmac_key: HMACKey) -> None:
        try:
            self._columns = config.tables[table]
        except KeyError as error:
            raise MapperError("mapper table is absent from configuration") from error
        self._table = table
        self._groups = config.groups
        self._hmac_key = hmac_key
        self._registry = MappingRegistry(config.registry_path)
        self._cache: dict[tuple[str, str], str] = {}
        self._max_cache_entries = 4_096

    def close(self) -> None:
        self._registry.close()

    def transform(self, record: dict[str, Any]) -> dict[str, Any]:
        """Transform configured text fields while preserving Greenmask JSON driver shape."""

        for column, group_id in self._columns.items():
            if column not in record:
                raise MapperError(
                    f"mapper input is missing configured column {self._table}.{column}"
                )
            attribute = record[column]
            if not isinstance(attribute, dict):
                raise MapperError(f"mapper input has invalid column shape {self._table}.{column}")
            if attribute.get("n") is True:
                continue
            raw_value = attribute.get("d")
            group = self._groups[group_id]
            try:
                normalized = normalize(
                    raw_value,
                    group.normalization,  # type: ignore[arg-type]
                    allow_empty=group.allow_empty,
                )
            except NormalizationError:
                raise MapperError(
                    f"mapper input has invalid value {self._table}.{column}"
                ) from None
            source_hmac = self._hmac_key.digest(group_id, normalized)
            cache_key = (group_id, source_hmac)
            replacement = self._cache.get(cache_key)
            if replacement is None:
                replacement = self._registry.lookup(group_id, source_hmac)
                if replacement is None:
                    raise MapperError(
                        "missing mapping for "
                        f"{self._table}.{column} (source-hmac={source_hmac[:12]})"
                    )
                if len(self._cache) >= self._max_cache_entries:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[cache_key] = replacement
            attribute["d"] = replacement
            attribute["n"] = False
        return record


def _line_records(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise MapperError("mapper received invalid JSONL input") from error
        if not isinstance(record, dict):
            raise MapperError("mapper received non-object JSONL input")
        yield record


def _graceful_termination(_signum: int, _frame: object) -> None:
    """Let Greenmask stop its long-lived child without turning SIGTERM into a failure."""

    raise SystemExit(0)


def run(config_path: Path, table: str) -> int:
    """Run the stdin/stdout protocol and return a process exit status."""

    # Greenmask terminates the healthy long-lived Cmd process with SIGTERM after
    # its stream completes. Python's default signal exit is non-zero, which Cmd
    # correctly treats as a transformer failure, so explicitly exit cleanly.
    signal.signal(signal.SIGTERM, _graceful_termination)
    mapper: JsonlMapper | None = None
    try:
        config = _parse_config(config_path)
        secret = os.environ.get(config.hmac_key_env)
        if not secret:
            raise MapperError("mapper HMAC key environment variable is not set")
        mapper = JsonlMapper(config=config, table=table, hmac_key=HMACKey(secret.encode("utf-8")))
        for record in _line_records(sys.stdin):
            transformed = mapper.transform(record)
            sys.stdout.write(
                json.dumps(transformed, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            sys.stdout.flush()
        return 0
    except MapperError as error:
        print(f"db-sanitizer mapper: {error}", file=sys.stderr)
        return 4
    except Exception:
        print("db-sanitizer mapper: internal mapper failure", file=sys.stderr)
        return 4
    finally:
        if mapper is not None:
            mapper.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB Sanitizer Greenmask JSONL mapper")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--table", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args.config, args.table)


if __name__ == "__main__":
    raise SystemExit(main())
