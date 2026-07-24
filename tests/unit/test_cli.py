"""CLI exit-code contracts without a database, provider, or subprocess."""

from __future__ import annotations

import pytest

from db_sanitizer.cli import main
from db_sanitizer.errors import DataPlaneError, GenerationError, VerificationError


@pytest.mark.parametrize(
    ("error", "expected_exit_code"),
    (
        (GenerationError("generation failed"), 3),
        (DataPlaneError("data plane failed"), 4),
    ),
)
def test_run_returns_typed_failure_exit_codes(monkeypatch, error, expected_exit_code: int) -> None:
    import db_sanitizer.graph as graph

    def fail_run(**_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(graph, "run_sanitization", fail_run)

    assert (
        main(["run", "--policy", "config/policy.demo.yaml", "--run-id", "unit-cli"])
        == expected_exit_code
    )


def test_verify_returns_required_check_failure_exit_code(monkeypatch) -> None:
    import db_sanitizer.graph as graph

    def fail_verify(**_kwargs: object) -> None:
        raise VerificationError("required check failed")

    monkeypatch.setattr(graph, "verify_existing_run", fail_verify)

    assert main(["verify", "--policy", "config/policy.demo.yaml", "--run-id", "unit-cli"]) == 5
