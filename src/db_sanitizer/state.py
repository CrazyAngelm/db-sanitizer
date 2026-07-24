"""LangGraph state contract: explicitly safe for checkpoint serialization."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class RunState(TypedDict):
    """No DSNs, HMAC secrets, raw PII, prompts, or model outputs belong here."""

    run_id: str
    policy_path: str
    policy_sha256: str
    run_dir: str
    started_at: str
    current_stage: str
    errors_without_raw_data: list[str]
    schema_fingerprint: NotRequired[str]
    mapping_counts: NotRequired[dict[str, int]]
    llm: NotRequired[dict[str, object]]
    generated_config_path: NotRequired[str]
    mapper_config_path: NotRequired[str]
    dump_id: NotRequired[str]
    greenmask_durations: NotRequired[dict[str, float]]
    runtime_metrics: NotRequired[dict[str, int]]
    report_json_path: NotRequired[str]
    report_markdown_path: NotRequired[str]
    finished_at: NotRequired[str]
