"""Optional Docker-backed end-to-end test; it never contacts OpenRouter."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from db_sanitizer.graph import run_sanitization
from db_sanitizer.llm import DeterministicSyntheticProvider
from db_sanitizer.postgres.connection import target_connection
from db_sanitizer.postgres.inspector import inspect_schema
from db_sanitizer.verify.checks import CheckStatus, check_foreign_keys

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DB_SANITIZER_RUN_INTEGRATION") != "1",
        reason="requires seeded source-db/target-db plus Greenmask in the Docker sanitizer image",
    ),
]


def test_full_fake_llm_workflow_restores_and_verifies() -> None:
    run_id = f"integration-{uuid4().hex[:12]}"

    state = run_sanitization(
        policy_path="config/policy.demo.yaml",
        run_id=run_id,
        provider_factory=DeterministicSyntheticProvider,
    )

    assert state["current_stage"] == "completed"
    assert state["mapping_counts"] == {
        "address": 59,
        "email": 59,
        "person_name": 59,
        "phone": 59,
    }


def test_resume_after_generation_does_not_repeat_fake_llm_calls() -> None:
    run_id = f"resume-{uuid4().hex[:12]}"
    provider = DeterministicSyntheticProvider()
    interrupted = run_sanitization(
        policy_path="config/policy.demo.yaml",
        run_id=run_id,
        provider_factory=lambda: provider,
        interrupt_after=("generate_replacements_agent",),
    )
    calls_after_generation = len(provider.requests)

    completed = run_sanitization(
        policy_path="config/policy.demo.yaml",
        run_id=run_id,
        resume=True,
        provider_factory=lambda: provider,
    )

    assert interrupted["current_stage"] == "generate_replacements_agent"
    assert completed["current_stage"] == "completed"
    assert len(provider.requests) == calls_after_generation


def test_foreign_key_inspection_preserves_pairing_and_match_simple_nulls() -> None:
    schema = f"audit_fk_{uuid4().hex[:12]}"
    target_dsn = os.environ["TARGET_DATABASE_URL"]
    try:
        with target_connection(target_dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(
                f'''CREATE TABLE "{schema}".parent_one (
                    left_id integer NOT NULL,
                    right_id integer NOT NULL,
                    PRIMARY KEY (left_id, right_id)
                )'''
            )
            cursor.execute(
                f'''CREATE TABLE "{schema}".parent_two (
                    left_id integer NOT NULL,
                    right_id integer NOT NULL,
                    PRIMARY KEY (left_id, right_id)
                )'''
            )
            cursor.execute(
                f'''CREATE TABLE "{schema}".child_one (
                    id integer PRIMARY KEY,
                    left_id integer,
                    right_id integer,
                    CONSTRAINT duplicate_fk_name FOREIGN KEY (left_id, right_id)
                      REFERENCES "{schema}".parent_one (left_id, right_id) MATCH SIMPLE
                )'''
            )
            cursor.execute(
                f'''CREATE TABLE "{schema}".child_two (
                    id integer PRIMARY KEY,
                    left_id integer,
                    right_id integer,
                    CONSTRAINT duplicate_fk_name FOREIGN KEY (left_id, right_id)
                      REFERENCES "{schema}".parent_two (left_id, right_id) MATCH SIMPLE
                )'''
            )
            # A partially-null MATCH SIMPLE row is valid without a parent row.
            cursor.execute(f'INSERT INTO "{schema}".child_one VALUES (1, 999, NULL)')
            snapshot = inspect_schema(connection)
            foreign_keys = tuple(
                foreign_key
                for foreign_key in snapshot.foreign_keys
                if foreign_key.source_columns[0].schema_name == schema
            )
            assert len(foreign_keys) == 2
            assert all(foreign_key.match_type == "SIMPLE" for foreign_key in foreign_keys)
            assert all(len(foreign_key.source_columns) == 2 for foreign_key in foreign_keys)
            assert all(
                tuple(column.column for column in foreign_key.source_columns)
                == ("left_id", "right_id")
                for foreign_key in foreign_keys
            )
            assert all(
                tuple(column.column for column in foreign_key.target_columns)
                == ("left_id", "right_id")
                for foreign_key in foreign_keys
            )
            result = check_foreign_keys(
                target=connection,
                source_snapshot=snapshot,
                target_snapshot=snapshot,
            )
            assert result.status is CheckStatus.PASS
    finally:
        with target_connection(target_dsn) as connection, connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
