"""Stable command-line entry point for DB Sanitizer jobs."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from db_sanitizer import __version__
from db_sanitizer.errors import SanitizerError
from db_sanitizer.safe_logging import configure_console_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="db-sanitizer", description="Fail-closed PostgreSQL sanitizer"
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="collect, generate, dump, restore, and verify")
    run.add_argument("--policy", required=True, type=Path)
    run.add_argument("--run-id", required=True)
    run.add_argument("--resume", action="store_true", help="resume a compatible interrupted run")

    verify = commands.add_parser("verify", help="re-run required checks for a completed run")
    verify.add_argument("--policy", required=True, type=Path)
    verify.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, exposing only typed safe errors and stable exit codes."""

    configure_console_logging()
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            from db_sanitizer.graph import run_sanitization

            run_sanitization(policy_path=args.policy, run_id=args.run_id, resume=args.resume)
            return 0
        if args.command == "verify":
            from db_sanitizer.graph import verify_existing_run

            verify_existing_run(policy_path=args.policy, run_id=args.run_id)
            return 0
        raise AssertionError("unreachable command")
    except SanitizerError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
    except Exception:
        # Never print unknown exception text: drivers/providers may contain sensitive context.
        print("error: unexpected internal failure", file=sys.stderr)
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
