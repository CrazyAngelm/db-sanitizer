"""Generate a narrow Greenmask 0.2.22 Cmd-transformer configuration."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from db_sanitizer.errors import DataPlaneError
from db_sanitizer.policy.models import SanitizerPolicy


@dataclass(frozen=True, slots=True)
class GeneratedGreenmaskConfig:
    config_path: Path
    mapper_config_path: Path
    dump_dir: Path


def _restrict_file(path: Path) -> None:
    # Windows does not expose POSIX mode bits; the containing run directory is restricted.
    with suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _table_mapping(policy: SanitizerPolicy) -> dict[str, dict[str, str]]:
    tables: dict[str, dict[str, str]] = {}
    for group_id, group in policy.groups.items():
        for column in group.columns:
            table_key = f"{column.schema_name}.{column.table}"
            table_columns = tables.setdefault(table_key, {})
            if column.column in table_columns:
                raise DataPlaneError("generated mapper configuration has duplicate table column")
            table_columns[column.column] = group_id
    return {table: dict(sorted(columns.items())) for table, columns in sorted(tables.items())}


def build_greenmask_config(
    *,
    policy: SanitizerPolicy,
    run_dir: Path,
    registry_path: Path,
    mapper_python: str | None = None,
) -> GeneratedGreenmaskConfig:
    """Write safe generated YAML and mapper metadata; never write an HMAC secret."""

    dump_dir = run_dir / policy.greenmask.storage_dirname
    mapper_config_path = run_dir / "mapper-config.json"
    config_path = run_dir / "greenmask.generated.yaml"
    try:
        dump_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        table_mapping = _table_mapping(policy)
        mapper_payload = {
            "version": 1,
            "registry_path": str(registry_path.resolve()),
            "hmac_key_env": policy.mapping.hmac_key_env,
            "groups": {
                group_id: {
                    "normalization": group.normalization.value,
                    "allow_empty": group.allow_empty,
                }
                for group_id, group in sorted(policy.groups.items())
            },
            "tables": table_mapping,
        }
        mapper_config_path.write_text(
            json.dumps(mapper_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        _restrict_file(mapper_config_path)

        executable = mapper_python or sys.executable
        transformations: list[dict[str, object]] = []
        for table_key, columns in table_mapping.items():
            schema, table = table_key.split(".", maxsplit=1)
            transformations.append(
                {
                    "schema": schema,
                    "name": table,
                    "transformers": [
                        {
                            "name": "Cmd",
                            "params": {
                                "executable": executable,
                                "args": [
                                    "-m",
                                    "db_sanitizer.greenmask.mapper_process",
                                    "--config",
                                    str(mapper_config_path.resolve()),
                                    "--table",
                                    table_key,
                                ],
                                "driver": {
                                    "name": "json",
                                    "json_data_format": "text",
                                    "json_attributes_format": "names",
                                },
                                "validate": policy.greenmask.validate_output,
                                "timeout": f"{policy.greenmask.mapper_timeout_seconds}s",
                                "expected_exit_code": 0,
                                "columns": [
                                    {"name": column_name, "skip_original_data": False}
                                    for column_name in sorted(columns)
                                ],
                            },
                        }
                    ],
                }
            )
        greenmask_payload = {
            "common": {
                "pg_bin_path": "/usr/lib/postgresql/16/bin/",
                "tmp_dir": "/tmp",
            },
            "storage": {"type": "directory", "directory": {"path": str(dump_dir.resolve())}},
            "log": {"level": "info", "format": "text"},
            "dump": {
                "pg_dump_options": {"jobs": 1, "no-owner": True, "no-privileges": True},
                "transformation": transformations,
            },
            "restore": {
                "pg_restore_options": {"jobs": 1, "no-owner": True, "no-privileges": True},
            },
        }
        config_path.write_text(
            yaml.safe_dump(greenmask_payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        _restrict_file(config_path)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise DataPlaneError("unable to create generated Greenmask configuration") from error
    return GeneratedGreenmaskConfig(
        config_path=config_path,
        mapper_config_path=mapper_config_path,
        dump_dir=dump_dir,
    )
