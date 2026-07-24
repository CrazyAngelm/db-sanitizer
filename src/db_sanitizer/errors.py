"""Typed, safe-to-display domain errors and stable CLI exit codes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False)
class SanitizerError(Exception):
    """Base error whose message is deliberately safe for user-facing output."""

    message: str
    exit_code: int = 6

    def __str__(self) -> str:
        return self.message


class PolicyError(SanitizerError):
    """Configuration, environment, or schema validation failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, exit_code=2)


class GenerationError(SanitizerError):
    """LLM request, structured-output, or replacement validation failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, exit_code=3)


class DataPlaneError(SanitizerError):
    """Greenmask, mapper, dump, or restore failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, exit_code=4)


class VerificationError(SanitizerError):
    """Required verification check failed."""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, exit_code=5)
