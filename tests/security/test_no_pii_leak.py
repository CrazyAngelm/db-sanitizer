from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from db_sanitizer.greenmask.config_builder import build_greenmask_config
from db_sanitizer.mapping import HMACKey, MappingRegistry, RunMetadata, normalize
from db_sanitizer.policy.loader import load_policy
from db_sanitizer.safe_logging import JsonlLogger
from db_sanitizer.verify import (
    ArtifactPaths,
    CheckResult,
    CheckSeverity,
    CheckStatus,
    GroupReport,
    LLMStats,
    RunReport,
    TableReport,
    write_report,
)

pytestmark = pytest.mark.security

_SECRET = b"z" * 32
_MARKERS = (
    "PII_LEAK_MARKER_NAME_7F3A",
    "pii-leak-marker@example.org",
    "+79990001122",
    "PII_LEAK_MARKER_ADDRESS_91BC",
)


def test_run_artifact_building_blocks_raw_pii_markers(tmp_path: Path) -> None:
    run_dir = tmp_path / ".runs" / "security"
    run_dir.mkdir(parents=True)
    policy = load_policy("config/policy.demo.yaml").policy
    key = HMACKey(_SECRET)
    markers_by_group = {
        "person_name": _MARKERS[0],
        "email": _MARKERS[1],
        "phone": _MARKERS[2],
        "address": _MARKERS[3],
    }
    registry_path = run_dir / "mappings.sqlite3"

    with MappingRegistry(registry_path) as registry:
        registry.initialize(
            RunMetadata(
                run_id="security",
                policy_sha256="a" * 64,
                source_schema_sha256="b" * 64,
                llm_provider="fake",
                llm_model="test-fake",
                hmac_key_fingerprint=key.fingerprint,
            )
        )
        for group_id, marker in markers_by_group.items():
            group = policy.groups[group_id]
            registry.insert_source_key(
                group_id,
                key.digest(
                    group_id,
                    normalize(marker, group.normalization.value, allow_empty=group.allow_empty),
                ),
            )

    build_greenmask_config(policy=policy, run_dir=run_dir, registry_path=registry_path)
    logger = JsonlLogger(path=run_dir / "logs.jsonl", run_id="security")
    logger.event("info", "collect", "keys_collected", inserted_keys=4)
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    report = RunReport(
        run_id="security",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        policy_sha256="a" * 64,
        source_schema_sha256="b" * 64,
        llm=LLMStats(
            provider="fake",
            model="test-fake",
            batches=1,
            accepted_items=4,
            rejected_items=0,
            duration_seconds=0.01,
        ),
        tables=(TableReport("public.customers", 4, 4, True),),
        groups=tuple(
            GroupReport(group_id, group.entity_type, 1, 1, 1, 0, 0)
            for group_id, group in policy.groups.items()
        ),
        checks=(
            CheckResult(
                id="safe_artifacts",
                name="Safe artifacts",
                severity=CheckSeverity.REQUIRED,
                status=CheckStatus.PASS,
                details={"items_checked": 4},
            ),
        ),
        artifacts=ArtifactPaths(
            "dump", "greenmask.generated.yaml", "mappings.sqlite3", "report.md"
        ),
    )
    write_report(report, json_path=run_dir / "report.json", markdown_path=run_dir / "report.md")

    files = [path for path in run_dir.rglob("*") if path.is_file()]
    for marker in _MARKERS:
        assert all(marker.encode("utf-8") not in path.read_bytes() for path in files)
