from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from db_sanitizer.errors import PolicyError
from db_sanitizer.policy.loader import load_policy, resolve_policy_runtime
from db_sanitizer.policy.models import ColumnRef, SanitizerPolicy
from db_sanitizer.policy.validator import validate_policy_against_schema, validate_run_directory
from db_sanitizer.postgres.inspector import ColumnInfo, ForeignKeyInfo, SchemaSnapshot
from db_sanitizer.safe_logging import redact_dsn


def _policy_data() -> dict[str, object]:
    return {
        "version": 1,
        "run": {
            "directory": ".runs",
            "allow_target_recreate": True,
            "collector_fetch_size": 100,
        },
        "connections": {
            "source_dsn_env": "SOURCE_DATABASE_URL",
            "target_dsn_env": "TARGET_DATABASE_URL",
        },
        "mapping": {
            "registry_filename": "mappings.sqlite3",
            "hmac_key_env": "SANITIZER_HMAC_KEY",
            "hmac_algorithm": "sha256",
        },
        "llm": {
            "provider": "openrouter",
            "base_url_env": "OPENROUTER_BASE_URL",
            "api_key_env": "OPENROUTER_API_KEY",
            "model": "deepseek/deepseek-v4-flash",
            "batch_size": 5,
            "max_retries": 1,
            "timeout_seconds": 10,
            "structured_output": True,
        },
        "greenmask": {
            "binary": "greenmask",
            "storage_dirname": "dump",
            "mapper_timeout_seconds": 10,
            "validate_output": True,
        },
        "groups": {
            "person_name": {
                "entity_type": "person_name",
                "locale": "ru-RU",
                "normalization": "human_text",
                "allow_empty": False,
                "generation": {
                    "description": "Synthetic Russian full name",
                    "min_length": 5,
                    "max_length": 100,
                    "regex": "^[A-Z][a-z]+ [A-Z][a-z]+$",
                },
                "columns": [{"schema": "public", "table": "customers", "column": "full_name"}],
            }
        },
        "report": {
            "json_filename": "report.json",
            "markdown_filename": "report.md",
            "include_synthetic_demo_before_after": True,
            "sample_rows": 5,
        },
    }


def _snapshot(
    *columns: ColumnInfo, foreign_keys: tuple[ForeignKeyInfo, ...] = ()
) -> SchemaSnapshot:
    return SchemaSnapshot(columns=columns, foreign_keys=foreign_keys)


def _text_column(
    schema: str, table: str, column: str, *, maximum: int | None = None, primary: bool = False
) -> ColumnInfo:
    return ColumnInfo(
        ref=ColumnRef(schema=schema, table=table, column=column),
        postgres_type="text",
        character_maximum_length=maximum,
        is_primary_key=primary,
    )


def _write_policy(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_valid_demo_policy_loads() -> None:
    loaded = load_policy("config/policy.demo.yaml")

    assert loaded.policy.version == 1
    assert set(loaded.policy.groups) == {"person_name", "email", "phone", "address"}
    assert loaded.policy.llm.provider == "openrouter"


def test_policy_rejects_duplicate_column(tmp_path: Path) -> None:
    data = _policy_data()
    duplicate_group = deepcopy(data["groups"]["person_name"])
    data["groups"]["address"] = duplicate_group

    with pytest.raises(PolicyError, match="policy validation failed"):
        load_policy(_write_policy(tmp_path, data))


def test_policy_rejects_unknown_column() -> None:
    policy = SanitizerPolicy.model_validate(_policy_data())
    snapshot = _snapshot(_text_column("public", "customers", "email"))

    with pytest.raises(PolicyError, match="configured column does not exist"):
        validate_policy_against_schema(policy, snapshot)


def test_policy_rejects_unsupported_type() -> None:
    policy = SanitizerPolicy.model_validate(_policy_data())
    snapshot = SchemaSnapshot(
        columns=(
            ColumnInfo(
                ref=ColumnRef(schema="public", table="customers", column="full_name"),
                postgres_type="jsonb",
                character_maximum_length=None,
            ),
        ),
        foreign_keys=(),
    )

    with pytest.raises(PolicyError, match="unsupported PostgreSQL type"):
        validate_policy_against_schema(policy, snapshot)


def test_policy_rejects_generation_that_does_not_fit_column() -> None:
    policy = SanitizerPolicy.model_validate(_policy_data())
    snapshot = _snapshot(_text_column("public", "customers", "full_name", maximum=20))

    with pytest.raises(PolicyError, match="generation max_length"):
        validate_policy_against_schema(policy, snapshot)


def test_policy_rejects_partial_natural_fk_group() -> None:
    data = _policy_data()
    data["groups"]["person_name"]["columns"] = [
        {"schema": "public", "table": "accounts", "column": "external_code"}
    ]
    policy = SanitizerPolicy.model_validate(data)
    parent = _text_column("public", "accounts", "external_code", primary=True)
    child = _text_column("public", "events", "account_external_code")
    foreign_key = ForeignKeyInfo(
        name="events_account_external_code_fkey",
        source_columns=(child.ref,),
        target_columns=(parent.ref,),
    )

    with pytest.raises(PolicyError, match="natural PK/FK"):
        validate_policy_against_schema(
            policy, _snapshot(parent, child, foreign_keys=(foreign_key,))
        )


@pytest.mark.parametrize(
    ("source_dsn", "target_dsn"),
    (
        (
            "postgresql://user:password@db:5432/app",
            "postgresql://user:password@db:5432/app",
        ),
        (
            "postgresql://source_role:source_password@db:5432/app",
            "postgres://target_role:target_password@db:5432/app",
        ),
        (
            "host=db port=5432 dbname=app user=source_role password=source_password",
            "host=db dbname=app user=target_role password=target_password",
        ),
    ),
)
def test_policy_rejects_same_source_target(source_dsn: str, target_dsn: str) -> None:
    loaded = load_policy("config/policy.demo.yaml")
    environment = {
        "SOURCE_DATABASE_URL": source_dsn,
        "TARGET_DATABASE_URL": target_dsn,
        "SANITIZER_HMAC_KEY": "x" * 32,
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY": "not-a-real-key",
    }

    with pytest.raises(PolicyError, match="different databases"):
        resolve_policy_runtime(loaded, environment)


@pytest.mark.parametrize(
    ("section", "field", "unsafe_value"),
    (
        ("mapping", "registry_filename", "../../escaped.sqlite3"),
        ("greenmask", "storage_dirname", "/tmp/escaped-dump"),
        ("report", "json_filename", r"..\\escaped.json"),
        ("report", "markdown_filename", "nested/report.md"),
    ),
)
def test_policy_rejects_artifact_paths_that_escape_the_run(
    section: str, field: str, unsafe_value: str
) -> None:
    data = _policy_data()
    data[section][field] = unsafe_value

    with pytest.raises(ValidationError, match="single safe artifact filename"):
        SanitizerPolicy.model_validate(data)


def test_policy_rejects_unknown_version() -> None:
    data = _policy_data()
    data["version"] = 2

    with pytest.raises(ValidationError):
        SanitizerPolicy.model_validate(data)


def test_run_directory_is_created_with_validation(tmp_path: Path) -> None:
    data = _policy_data()
    data["run"]["directory"] = str(tmp_path / "runs")
    policy = SanitizerPolicy.model_validate(data)

    root = validate_run_directory(policy)

    assert root.is_dir()


def test_dsn_password_is_redacted() -> None:
    password = "secret-value-that-must-not-leak"
    redacted = redact_dsn(f"postgresql://alice:{password}@database:5432/app")

    assert password not in redacted
    assert redacted == "postgresql://alice:***@database:5432/app"
