"""Runtime-only settings resolved from environment variables.

Secrets live only in this object while a command is executing.  It must never be
serialized into graph state, reports, checkpoints, or structured logs.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass

from psycopg import Error
from psycopg.conninfo import conninfo_to_dict

from db_sanitizer.errors import PolicyError


@dataclass(frozen=True)
class RuntimeSettings:
    source_dsn: str
    target_dsn: str
    hmac_key: bytes
    provider_base_url: str | None
    provider_api_key: str | None

    @property
    def hmac_key_fingerprint(self) -> str:
        return hashlib.sha256(self.hmac_key).hexdigest()


def require_environment(name: str, environment: Mapping[str, str]) -> str:
    """Read a required variable without placing its value in an error message."""

    value = environment.get(name)
    if value is None or not value.strip():
        raise PolicyError(f"required environment variable {name!r} is not set")
    return value


def _database_identity(dsn: str) -> tuple[str, int, str] | None:
    """Return a credential-free PostgreSQL identity for destructive-target protection.

    Different roles and the ``postgres``/``postgresql`` URI aliases can point at
    the same physical database.  Credentials must never make that database look
    like a safe, separate target.
    """

    try:
        values = conninfo_to_dict(dsn)
        host = str(values.get("host") or "").casefold()
        database_name = str(values.get("dbname") or "")
        if not host or not database_name:
            return None
        port = int(values.get("port") or 5432)
        return (host, port, database_name)
    except (Error, TypeError, ValueError):
        return None


def source_and_target_are_equal(source_dsn: str, target_dsn: str) -> bool:
    """Detect literally or canonically equal PostgreSQL source/target DSNs."""

    if source_dsn.strip() == target_dsn.strip():
        return True
    source = _database_identity(source_dsn)
    target = _database_identity(target_dsn)
    return source is not None and source == target


def resolve_runtime_settings(
    *,
    source_dsn_env: str,
    target_dsn_env: str,
    hmac_key_env: str,
    provider_base_url_env: str,
    provider_api_key_env: str | None,
    environment: Mapping[str, str] | None = None,
    require_provider_credentials: bool = True,
) -> RuntimeSettings:
    """Resolve runtime values without serializing credentials into run state.

    An explicitly selected fake provider is allowed to bypass provider endpoint
    credentials for offline tests. Database DSNs and the HMAC key are never
    optional, even in that mode.
    """

    env = os.environ if environment is None else environment
    source_dsn = require_environment(source_dsn_env, env)
    target_dsn = require_environment(target_dsn_env, env)
    hmac_key = require_environment(hmac_key_env, env).encode("utf-8")
    provider_base_url: str | None = None
    provider_api_key: str | None = None

    if require_provider_credentials:
        provider_base_url = require_environment(provider_base_url_env, env).rstrip("/")
        if provider_api_key_env is not None:
            provider_api_key = require_environment(provider_api_key_env, env)
        if not provider_base_url.startswith(("http://", "https://")):
            raise PolicyError("LLM provider base URL must be an http(s) URL")
    else:
        configured_base_url = env.get(provider_base_url_env, "").strip().rstrip("/")
        provider_base_url = configured_base_url or None
        if provider_api_key_env is not None:
            provider_api_key = env.get(provider_api_key_env) or None

    if len(hmac_key) < 32:
        raise PolicyError("SANITIZER_HMAC_KEY must contain at least 32 bytes")
    if source_and_target_are_equal(source_dsn, target_dsn):
        raise PolicyError("source and target database DSNs must identify different databases")

    return RuntimeSettings(
        source_dsn=source_dsn,
        target_dsn=target_dsn,
        hmac_key=hmac_key,
        provider_base_url=provider_base_url,
        provider_api_key=provider_api_key,
    )
