from __future__ import annotations

import json

import pytest
import yaml

from db_sanitizer.greenmask.config_builder import build_greenmask_config
from db_sanitizer.greenmask.mapper_process import (
    JsonlMapper,
    MapperConfig,
    MapperError,
    MapperGroup,
)
from db_sanitizer.mapping import HMACKey, MappingRegistry, RunMetadata, normalize
from db_sanitizer.policy.loader import load_policy

_SECRET = b"m" * 32


def _metadata() -> RunMetadata:
    return RunMetadata(
        run_id="mapper-unit-run",
        policy_sha256="a" * 64,
        source_schema_sha256="b" * 64,
        llm_provider="fake",
        llm_model="fake-model",
        hmac_key_fingerprint=HMACKey(_SECRET).fingerprint,
    )


def _assign(
    registry: MappingRegistry,
    key: HMACKey,
    group_id: str,
    raw_value: str,
    normalization: str,
    replacement: str,
) -> None:
    source_hmac = key.digest(group_id, normalize(raw_value, normalization))
    replacement_hmac = key.digest(group_id, normalize(replacement, normalization))
    registry.insert_source_key(group_id, source_hmac)
    registry.assign_replacement(group_id, source_hmac, replacement, replacement_hmac)


def test_mapper_transforms_multiple_columns_and_preserves_null(tmp_path) -> None:
    key = HMACKey(_SECRET)
    registry_path = tmp_path / "mappings.sqlite3"
    with MappingRegistry(registry_path) as registry:
        registry.initialize(_metadata())
        _assign(registry, key, "person_name", "Jane Doe", "human_text", "Anna Smith")
        _assign(registry, key, "email", "jane@source.test", "email", "anna@example.test")

    config = MapperConfig(
        registry_path=registry_path,
        hmac_key_env="SANITIZER_HMAC_KEY",
        groups={
            "person_name": MapperGroup(normalization="human_text", allow_empty=False),
            "email": MapperGroup(normalization="email", allow_empty=False),
            "phone": MapperGroup(normalization="phone", allow_empty=False),
        },
        tables={
            "public.customers": {
                "full_name": "person_name",
                "email": "email",
                "phone": "phone",
            }
        },
    )
    mapper = JsonlMapper(config=config, table="public.customers", hmac_key=key)
    try:
        output = mapper.transform(
            {
                "full_name": {"d": "Jane Doe", "n": False},
                "email": {"d": "JANE@source.test", "n": False},
                "phone": {"d": None, "n": True},
            }
        )
    finally:
        mapper.close()

    assert output["full_name"] == {"d": "Anna Smith", "n": False}
    assert output["email"] == {"d": "anna@example.test", "n": False}
    assert output["phone"] == {"d": None, "n": True}


def test_mapper_fails_closed_without_leaking_missing_raw_value(tmp_path) -> None:
    raw_source = "PII_LEAK_MARKER_NAME_7F3A"
    registry_path = tmp_path / "mappings.sqlite3"
    with MappingRegistry(registry_path) as registry:
        registry.initialize(_metadata())

    mapper = JsonlMapper(
        config=MapperConfig(
            registry_path=registry_path,
            hmac_key_env="SANITIZER_HMAC_KEY",
            groups={"person_name": MapperGroup(normalization="human_text", allow_empty=False)},
            tables={"public.customers": {"full_name": "person_name"}},
        ),
        table="public.customers",
        hmac_key=HMACKey(_SECRET),
    )
    try:
        with pytest.raises(MapperError) as error:
            mapper.transform({"full_name": {"d": raw_source, "n": False}})
    finally:
        mapper.close()

    assert raw_source not in str(error.value)
    assert "missing mapping" in str(error.value)


def test_generated_greenmask_config_is_secret_free_and_groups_columns(tmp_path) -> None:
    policy = load_policy("config/policy.demo.yaml").policy
    generated = build_greenmask_config(
        policy=policy,
        run_dir=tmp_path,
        registry_path=tmp_path / "mappings.sqlite3",
        mapper_python="/app/.venv/bin/python",
    )

    yaml_payload = yaml.safe_load(generated.config_path.read_text(encoding="utf-8"))
    mapper_payload = json.loads(generated.mapper_config_path.read_text(encoding="utf-8"))
    combined = generated.config_path.read_text(
        encoding="utf-8"
    ) + generated.mapper_config_path.read_text(encoding="utf-8")

    assert _SECRET.decode() not in combined
    assert set(mapper_payload["tables"]) == {
        "public.customers",
        "public.orders",
        "public.support_tickets",
    }
    assert len(yaml_payload["dump"]["transformation"]) == 3
    assert yaml_payload["dump"]["transformation"][0]["transformers"][0]["name"] == "Cmd"
