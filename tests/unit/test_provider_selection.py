"""Provider selection is policy-driven and never relies on credential fallbacks."""

from __future__ import annotations

from db_sanitizer.graph import WorkflowContext, _provider
from db_sanitizer.llm import OllamaProvider, OpenRouterProvider
from db_sanitizer.policy.loader import load_policy, resolve_policy_runtime
from db_sanitizer.safe_logging import JsonlLogger


def _environment() -> dict[str, str]:
    return {
        "SOURCE_DATABASE_URL": "postgresql://source:password@source-db:5432/source",
        "TARGET_DATABASE_URL": "postgresql://target:password@target-db:5432/target",
        "SANITIZER_HMAC_KEY": "x" * 32,
        "OPENROUTER_BASE_URL": "https://openrouter.example/api/v1",
        "OPENROUTER_API_KEY": "test-key",
        "OLLAMA_BASE_URL": "http://ollama:11434",
    }


def _context(tmp_path, policy_path: str) -> WorkflowContext:
    loaded = load_policy(policy_path)
    return WorkflowContext(
        loaded=loaded,
        runtime=resolve_policy_runtime(loaded, _environment()),
        run_dir=tmp_path,
        logger=JsonlLogger(path=tmp_path / "logs.jsonl", run_id="provider-unit"),
    )


def test_default_policy_selects_openrouter(tmp_path) -> None:
    provider = _provider(_context(tmp_path, "config/policy.demo.yaml"))
    try:
        assert isinstance(provider, OpenRouterProvider)
    finally:
        provider.close()


def test_optional_policy_selects_ollama(tmp_path) -> None:
    provider = _provider(_context(tmp_path, "config/policy.ollama.yaml"))
    try:
        assert isinstance(provider, OllamaProvider)
    finally:
        provider.close()
