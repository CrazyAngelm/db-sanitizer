from __future__ import annotations

import os

import pytest

from db_sanitizer.mapping import (
    HMACKey,
    MappingRegistry,
    NormalizationError,
    RegistryCompatibilityError,
    RegistryConflictError,
    RunMetadata,
    normalize,
)

_SECRET = b"s" * 32


def _metadata(*, model: str = "test-model") -> RunMetadata:
    return RunMetadata(
        run_id="unit-run",
        policy_sha256="a" * 64,
        source_schema_sha256="b" * 64,
        llm_provider="fake",
        llm_model=model,
        hmac_key_fingerprint=HMACKey(_SECRET).fingerprint,
    )


def test_normalization_strategies_are_deterministic() -> None:
    assert normalize("  Ada\tLOVELACE  ", "human_text") == "ada lovelace"
    assert normalize("  A\uff24\uff21@EXAMPLE.TEST ", "email") == "ada@example.test"
    assert normalize(" +7 (999) 123-45-67 ", "phone") == "79991234567"


def test_normalization_rejects_null_and_disallowed_empty_values() -> None:
    with pytest.raises(NormalizationError):
        normalize(None, "human_text")  # type: ignore[arg-type]
    with pytest.raises(NormalizationError):
        normalize(" \t", "human_text", allow_empty=False)


def test_hmac_is_group_scoped_after_normalization() -> None:
    key = HMACKey(_SECRET)
    normalized = normalize(" Ada  Lovelace ", "human_text")

    assert key.digest("names", normalized) == key.digest("names", "ada lovelace")
    assert key.digest("names", normalized) != key.digest("addresses", normalized)


def test_registry_deduplicates_keys_and_assigns_in_stable_order(tmp_path) -> None:
    key = HMACKey(_SECRET)
    first = key.digest("names", "ada")
    second = key.digest("names", "grace")
    replacement = key.digest("names", "synthetic ada")

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        assert registry.insert_source_key("names", second)
        assert registry.insert_source_key("names", first)
        assert not registry.insert_source_key("names", first)
        assert registry.list_unassigned("names") == sorted([first, second])

        registry.assign_replacement("names", first, "Synthetic Ada", replacement)
        assert registry.lookup("names", first) == "Synthetic Ada"
        assert registry.lookup("names", second) is None


def test_registry_rejects_duplicate_replacement_and_source_hmac_collision(tmp_path) -> None:
    key = HMACKey(_SECRET)
    first = key.digest("names", "ada")
    second = key.digest("names", "grace")
    replacement_hmac = key.digest("names", "synthetic")

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        registry.insert_source_keys("names", [first, second])
        registry.assign_replacement("names", first, "Synthetic", replacement_hmac)
        with pytest.raises(RegistryConflictError):
            registry.assign_replacement("names", second, "Synthetic", key.digest("names", "other"))
        with pytest.raises(RegistryConflictError):
            registry.assign_replacement("names", second, "Other", first)


def test_registry_assignment_atomically_records_accepted_generation_items(tmp_path) -> None:
    key = HMACKey(_SECRET)
    source_hmac = key.digest("email", "source@example.test")
    replacement_hmac = key.digest("email", "synthetic@example.test")

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        registry.insert_source_key("email", source_hmac)
        registry.assign_replacements(
            "email",
            [(source_hmac, "synthetic@example.test", replacement_hmac)],
            record_generated_items=True,
        )

        assert registry.lookup("email", source_hmac) == "synthetic@example.test"
        assert registry.generation_stats() == (0, 1, 0, 0.0)


def test_registry_persists_safe_generation_statistics(tmp_path) -> None:
    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        registry.record_generation_stats(
            batches=2,
            accepted_items=5,
            rejected_items=3,
            duration_seconds=1.25,
        )
        registry.record_generation_stats(batches=1, accepted_items=2, duration_seconds=0.75)

        assert registry.generation_stats() == (3, 7, 3, 2.0)


def test_registry_verification_accumulator_is_exact_and_cleared(tmp_path) -> None:
    key = HMACKey(_SECRET)
    first = key.digest("email", "first@example.test")
    second = key.digest("email", "second@example.test")
    extra = key.digest("email", "extra@example.test")

    with MappingRegistry(tmp_path / "mappings.sqlite3") as registry:
        registry.initialize(_metadata())
        registry.insert_source_keys("email", [first, second])
        with registry.verification_hmac_accumulator("email") as accumulator:
            accumulator.add_expected([first, first, second])
            accumulator.add_actual([first, extra])

            assert accumulator.actual_distinct_count() == 2
            assert accumulator.source_intersection_count() == 1
            assert accumulator.multiset_difference_count() == 3

        with registry.verification_hmac_accumulator("email") as accumulator:
            assert accumulator.actual_distinct_count() == 0
            assert accumulator.multiset_difference_count() == 0


def test_registry_checks_resume_metadata_and_never_stores_raw_source(tmp_path) -> None:
    source = "RAW-PII-MUST-NOT-REACH-SQLITE"
    key = HMACKey(_SECRET)
    path = tmp_path / "mappings.sqlite3"

    with MappingRegistry(path) as registry:
        registry.initialize(_metadata())
        registry.insert_source_key("names", key.digest("names", normalize(source, "human_text")))
        with pytest.raises(RegistryCompatibilityError):
            registry.initialize(_metadata(model="different-model"))

    assert source.encode() not in path.read_bytes()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
