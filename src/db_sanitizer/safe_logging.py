"""Structured logging helpers that never need raw source values."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_PASSWORD_IN_KEYWORD_DSN = re.compile(r"(?i)(\bpassword\s*=\s*)(?:'[^']*'|\"[^\"]*\"|\S+)")


def redact_dsn(value: str) -> str:
    """Return a DSN suitable for logs without exposing credentials.

    PostgreSQL accepts both URI and keyword conninfo forms, so both are handled.
    Unknown malformed input is never returned verbatim.
    """

    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"postgres", "postgresql"} and parsed.hostname:
            username = parsed.username or ""
            userinfo = f"{username}:***@" if username else ""
            host = parsed.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = f"{userinfo}{host}"
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    except (TypeError, ValueError):
        pass

    if "password" in value.casefold():
        return _PASSWORD_IN_KEYWORD_DSN.sub(r"\1***", value)
    return "<redacted-dsn>"


class JsonlLogger:
    """Small JSONL logger whose interface accepts only pre-sanitized details."""

    def __init__(self, *, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, level: str, stage: str, event: str, **details: Any) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "run_id": self._run_id,
            "stage": stage,
            "event": event,
            "details": details,
        }
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def configure_console_logging() -> None:
    """Configure intentionally terse stderr logging for the CLI."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
