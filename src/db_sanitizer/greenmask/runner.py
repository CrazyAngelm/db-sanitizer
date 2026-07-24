"""Fail-closed Greenmask 0.2.22 subprocess adapter."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

from db_sanitizer.errors import DataPlaneError
from db_sanitizer.greenmask.config_builder import GeneratedGreenmaskConfig
from db_sanitizer.postgres.connection import dsn_to_pg_environment
from db_sanitizer.safe_logging import JsonlLogger


@dataclass(frozen=True, slots=True)
class GreenmaskResult:
    dump_id: str
    validate_seconds: float
    dump_seconds: float
    restore_seconds: float


class GreenmaskRunner:
    """Invoke Greenmask with argument lists only and never persist its raw output."""

    def __init__(
        self,
        *,
        binary: str,
        generated: GeneratedGreenmaskConfig,
        logger: JsonlLogger,
        mapper_secret_environment: dict[str, str] | None = None,
        command_timeout_seconds: int = 3_600,
    ) -> None:
        self._binary = binary
        self._generated = generated
        self._logger = logger
        self._mapper_secret_environment = dict(mapper_secret_environment or {})
        self._command_timeout_seconds = command_timeout_seconds

    def _command(
        self,
        *,
        operation: str,
        arguments: list[str],
        database_dsn: str,
    ) -> float:
        command = [self._binary, f"--config={self._generated.config_path}", *arguments]
        environment = os.environ.copy()
        environment.update(self._mapper_secret_environment)
        environment.update(dsn_to_pg_environment(database_dsn))
        started = time.monotonic()
        self._logger.event("info", "greenmask", "command_started", operation=operation)
        try:
            completed = subprocess.run(
                command,
                check=False,
                cwd=self._generated.config_path.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                # Greenmask validation diagnostics may contain source values.
                # Discard both streams rather than retaining them in memory or logs.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._command_timeout_seconds,
            )
        except FileNotFoundError:
            raise DataPlaneError("Greenmask binary is not available") from None
        except subprocess.TimeoutExpired:
            raise DataPlaneError(f"Greenmask {operation} timed out") from None
        duration = time.monotonic() - started
        # Greenmask output can contain source-row diagnostics. Do not log or save it.
        if completed.returncode != 0:
            self._logger.event(
                "error",
                "greenmask",
                "command_failed",
                operation=operation,
                returncode=completed.returncode,
                duration_seconds=round(duration, 6),
            )
            raise DataPlaneError(f"Greenmask {operation} failed")
        self._logger.event(
            "info",
            "greenmask",
            "command_finished",
            operation=operation,
            duration_seconds=round(duration, 6),
        )
        return duration

    def validate_and_dump_restore(self, *, source_dsn: str, target_dsn: str) -> GreenmaskResult:
        """Validate transformation, dump source, then restore the newest dump to target."""

        validate_seconds = self._command(
            operation="validate",
            arguments=[
                "validate",
                "--data",
                "--diff",
                "--warnings",
                "--format=json",
                "--transformed-only",
                "--rows-limit=10",
            ],
            database_dsn=source_dsn,
        )
        dump_seconds = self._command(
            operation="dump",
            arguments=["dump"],
            database_dsn=source_dsn,
        )
        if not any(self._generated.dump_dir.iterdir()):
            raise DataPlaneError("Greenmask dump completed without dump artifacts")
        restore_seconds = self._command(
            operation="restore",
            arguments=["restore", "latest"],
            database_dsn=target_dsn,
        )
        return GreenmaskResult(
            dump_id="latest",
            validate_seconds=validate_seconds,
            dump_seconds=dump_seconds,
            restore_seconds=restore_seconds,
        )
