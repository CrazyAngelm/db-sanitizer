"""Policy file loading, hashing, and environment resolution."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from db_sanitizer.errors import PolicyError
from db_sanitizer.policy.models import SanitizerPolicy
from db_sanitizer.settings import RuntimeSettings, resolve_runtime_settings


@dataclass(frozen=True)
class LoadedPolicy:
    """Policy metadata safe to persist in graph state and reports."""

    path: Path
    policy: SanitizerPolicy
    sha256: str


def load_policy(path: str | Path) -> LoadedPolicy:
    """Load a policy without resolving any secret values."""

    policy_path = Path(path).expanduser().resolve()
    try:
        raw = policy_path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"cannot read policy file {policy_path}") from exc

    try:
        parsed: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError("policy YAML is invalid") from exc
    if not isinstance(parsed, dict):
        raise PolicyError("policy root must be a mapping")

    try:
        policy = SanitizerPolicy.model_validate(parsed)
    except ValidationError as exc:
        locations = ", ".join(
            ".".join(str(part) for part in error["loc"]) for error in exc.errors()
        )
        raise PolicyError(f"policy validation failed at: {locations}") from exc

    return LoadedPolicy(path=policy_path, policy=policy, sha256=hashlib.sha256(raw).hexdigest())


def resolve_policy_runtime(
    loaded: LoadedPolicy,
    environment: Mapping[str, str] | None = None,
) -> RuntimeSettings:
    """Resolve runtime credentials only immediately before a run starts."""

    policy = loaded.policy
    return resolve_runtime_settings(
        source_dsn_env=policy.connections.source_dsn_env,
        target_dsn_env=policy.connections.target_dsn_env,
        hmac_key_env=policy.mapping.hmac_key_env,
        provider_base_url_env=policy.llm.base_url_env,
        provider_api_key_env=policy.llm.api_key_env,
        environment=os.environ if environment is None else environment,
    )
