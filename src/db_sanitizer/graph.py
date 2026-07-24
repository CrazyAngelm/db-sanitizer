"""LangGraph orchestration for the DB Sanitizer control plane."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Must be set before LangGraph serializes a checkpoint.
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from psycopg import sql

from db_sanitizer.errors import GenerationError, PolicyError
from db_sanitizer.greenmask import GeneratedGreenmaskConfig, GreenmaskRunner, build_greenmask_config
from db_sanitizer.llm import (
    DeterministicSyntheticProvider,
    OpenRouterProvider,
    ReplacementGenerator,
    ReplacementProvider,
)
from db_sanitizer.mapping import HMACKey, MappingRegistry, RunMetadata
from db_sanitizer.policy.loader import LoadedPolicy, load_policy, resolve_policy_runtime
from db_sanitizer.policy.validator import validate_policy_against_schema, validate_run_directory
from db_sanitizer.postgres import (
    collect_mapping_keys,
    inspect_schema,
    reset_target_database,
    source_connection,
    target_connection,
)
from db_sanitizer.safe_logging import JsonlLogger
from db_sanitizer.settings import RuntimeSettings
from db_sanitizer.state import RunState
from db_sanitizer.verify import (
    ArtifactPaths,
    LLMStats,
    VerificationContext,
    verify_and_write_report,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ALLOWED_CHECKPOINT_ERRORS = (
    ("db_sanitizer.errors", "PolicyError"),
    ("db_sanitizer.errors", "GenerationError"),
    ("db_sanitizer.errors", "DataPlaneError"),
    ("db_sanitizer.errors", "VerificationError"),
)


@contextmanager
def _open_checkpointer(path: Path):
    """Open strict LangGraph SQLite persistence with safe typed errors allowlisted."""

    with closing(sqlite3.connect(path, check_same_thread=False)) as connection:
        yield SqliteSaver(
            connection,
            serde=JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_CHECKPOINT_ERRORS),
        )


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Ephemeral dependencies deliberately excluded from LangGraph state."""

    loaded: LoadedPolicy
    runtime: RuntimeSettings
    run_dir: Path
    logger: JsonlLogger
    provider_factory: Callable[[], ReplacementProvider] | None = None

    @property
    def registry_path(self) -> Path:
        return self.run_dir / self.loaded.policy.mapping.registry_filename

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.sqlite3"

    @property
    def uses_fake_provider(self) -> bool:
        return (
            self.provider_factory is not None
            or os.environ.get("DB_SANITIZER_USE_FAKE_PROVIDER") == "1"
        )

    @property
    def llm_provider_name(self) -> str:
        return "fake" if self.uses_fake_provider else self.loaded.policy.llm.provider

    @property
    def llm_model_name(self) -> str:
        return "test-fake" if self.uses_fake_provider else self.loaded.policy.llm.model


def _run_metadata(context: WorkflowContext, schema_fingerprint: str) -> RunMetadata:
    return RunMetadata(
        run_id=context.run_dir.name,
        policy_sha256=context.loaded.sha256,
        source_schema_sha256=schema_fingerprint,
        llm_provider=context.llm_provider_name,
        llm_model=context.llm_model_name,
        hmac_key_fingerprint=HMACKey(context.runtime.hmac_key).fingerprint,
    )


def _peak_rss_bytes() -> int:
    """Return the process peak resident set size when the platform exposes it."""

    try:
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1_024
    except (ImportError, AttributeError):
        try:
            import psutil

            return int(psutil.Process().memory_info().rss)
        except Exception:
            return 0


def _private_file(path: Path) -> None:
    # Windows does not implement Unix ownership bits.
    with suppress(OSError):
        os.chmod(path, 0o600)


def _private_directory(path: Path) -> None:
    # Windows does not implement Unix ownership bits.
    with suppress(OSError):
        os.chmod(path, 0o700)


def _prepare_run_directory(loaded: LoadedPolicy, run_id: str, *, resume: bool) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise PolicyError("run ID must contain only safe filename characters")
    root = validate_run_directory(loaded.policy)
    run_dir = root / run_id
    if resume:
        if not run_dir.is_dir():
            raise PolicyError("run directory does not exist; cannot resume")
        return run_dir.resolve()
    try:
        run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise PolicyError("run directory already exists; use --resume or a new run ID") from None
    except OSError:
        raise PolicyError("cannot create isolated run directory") from None
    _private_directory(run_dir)
    return run_dir.resolve()


def _create_context(
    *,
    policy_path: str | Path,
    run_id: str,
    resume: bool,
    provider_factory: Callable[[], ReplacementProvider] | None,
    environment: Mapping[str, str] | None,
) -> WorkflowContext:
    loaded = load_policy(policy_path)
    runtime = resolve_policy_runtime(loaded, environment)
    run_dir = _prepare_run_directory(loaded, run_id, resume=resume)
    logger = JsonlLogger(path=run_dir / "logs.jsonl", run_id=run_id)
    return WorkflowContext(
        loaded=loaded,
        runtime=runtime,
        run_dir=run_dir,
        logger=logger,
        provider_factory=provider_factory,
    )


def _provider(context: WorkflowContext) -> ReplacementProvider:
    if context.provider_factory is not None:
        return context.provider_factory()
    if context.uses_fake_provider:
        return DeterministicSyntheticProvider()
    policy = context.loaded.policy
    return OpenRouterProvider(
        base_url=context.runtime.provider_base_url,
        api_key=context.runtime.provider_api_key,
        model=policy.llm.model,
        timeout_seconds=policy.llm.timeout_seconds,
    )


def _close_provider(provider: ReplacementProvider) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def _build_workflow(context: WorkflowContext, checkpointer: SqliteSaver) -> Any:
    """Build the normative linear graph with one LLM-owning agent node."""

    policy = context.loaded.policy

    def load_policy_node(state: RunState) -> dict[str, object]:
        context.logger.event(
            "info", "load_policy", "policy_loaded", policy_sha256=state["policy_sha256"]
        )
        return {"current_stage": "load_policy"}

    def inspect_schema_node(state: RunState) -> dict[str, object]:
        with source_connection(context.runtime.source_dsn) as connection:
            snapshot = inspect_schema(connection)
        validate_policy_against_schema(policy, snapshot)
        context.logger.event(
            "info",
            "inspect_schema",
            "schema_validated",
            configured_groups=len(policy.groups),
            schema_fingerprint=snapshot.fingerprint,
        )
        return {
            "current_stage": "inspect_schema",
            "schema_fingerprint": snapshot.fingerprint,
        }

    def collect_mapping_keys_node(state: RunState) -> dict[str, object]:
        schema_fingerprint = _state_string(state, "schema_fingerprint")
        hmac_key = HMACKey(context.runtime.hmac_key)
        with MappingRegistry(context.registry_path) as registry:
            registry.initialize(_run_metadata(context, schema_fingerprint))
            with source_connection(context.runtime.source_dsn) as connection:
                collection = collect_mapping_keys(connection, policy, hmac_key, registry)
            counts = {
                group_id: registry.mapping_count(group_id) for group_id in sorted(policy.groups)
            }
        context.logger.event(
            "info",
            "collect_mapping_keys",
            "mapping_keys_collected",
            groups=len(counts),
            total_mapping_keys=sum(counts.values()),
            inserted_keys=sum(stat.inserted_keys for stat in collection.values()),
        )
        return {
            "current_stage": "collect_mapping_keys",
            "mapping_counts": counts,
        }

    def generate_replacements_agent_node(state: RunState) -> dict[str, object]:
        schema_fingerprint = _state_string(state, "schema_fingerprint")
        hmac_key = HMACKey(context.runtime.hmac_key)
        provider = _provider(context)
        try:
            with MappingRegistry(context.registry_path) as registry:
                registry.initialize(_run_metadata(context, schema_fingerprint))
                generator = ReplacementGenerator(
                    provider=provider,
                    registry=registry,
                    hmac_key=hmac_key,
                    batch_size=policy.llm.batch_size,
                    max_retries=policy.llm.max_retries,
                )
                for group_id, group in sorted(policy.groups.items()):
                    generator.generate_group(group_id, group)
                stats = generator.stats
                stored_batches, stored_accepted, stored_rejected, stored_duration = (
                    registry.generation_stats()
                )
                counts = {
                    group_id: registry.mapping_count(group_id, assigned_only=True)
                    for group_id in sorted(policy.groups)
                }
                # Older/interrupted registries can contain a committed mapping batch
                # from immediately before its evidence write. Never emit a report
                # that claims fewer accepted items than the persisted mappings.
                stored_accepted = max(stored_accepted, sum(counts.values()))
        finally:
            _close_provider(provider)

        previous = state.get("llm")
        if stats.batches == 0 and isinstance(previous, dict):
            llm_evidence = previous
        else:
            llm_evidence = {
                "provider": context.llm_provider_name,
                "model": context.llm_model_name,
                "structured_output": True,
                "batches": stored_batches,
                "accepted_items": stored_accepted,
                "rejected_items": stored_rejected,
                "duration_seconds": stored_duration,
            }
        if not isinstance(llm_evidence, dict) or int(llm_evidence.get("batches", 0)) < 1:
            raise GenerationError("no replacement mappings required LLM generation evidence")
        context.logger.event(
            "info",
            "generate_replacements_agent",
            "replacements_generated",
            batches=int(llm_evidence["batches"]),
            accepted_items=int(llm_evidence["accepted_items"]),
            rejected_items=int(llm_evidence["rejected_items"]),
        )
        return {
            "current_stage": "generate_replacements_agent",
            "mapping_counts": counts,
            "llm": llm_evidence,
        }

    def build_greenmask_config_node(state: RunState) -> dict[str, object]:
        generated = build_greenmask_config(
            policy=policy,
            run_dir=context.run_dir,
            registry_path=context.registry_path,
        )
        context.logger.event(
            "info",
            "build_greenmask_config",
            "greenmask_config_generated",
            tables=len(
                {
                    (item.schema_name, item.table)
                    for group in policy.groups.values()
                    for item in group.columns
                }
            ),
        )
        return {
            "current_stage": "build_greenmask_config",
            "generated_config_path": str(generated.config_path),
            "mapper_config_path": str(generated.mapper_config_path),
        }

    def dump_and_restore_node(state: RunState) -> dict[str, object]:
        generated = GeneratedGreenmaskConfig(
            config_path=Path(_state_string(state, "generated_config_path")),
            mapper_config_path=Path(_state_string(state, "mapper_config_path")),
            dump_dir=context.run_dir / policy.greenmask.storage_dirname,
        )
        reset_target_database(context.runtime.target_dsn)
        runner = GreenmaskRunner(
            binary=policy.greenmask.binary,
            generated=generated,
            logger=context.logger,
            mapper_secret_environment={
                policy.mapping.hmac_key_env: context.runtime.hmac_key.decode("utf-8")
            },
        )
        result = runner.validate_and_dump_restore(
            source_dsn=context.runtime.source_dsn,
            target_dsn=context.runtime.target_dsn,
        )
        durations = {
            "validate_seconds": result.validate_seconds,
            "dump_seconds": result.dump_seconds,
            "restore_seconds": result.restore_seconds,
        }
        context.logger.event("info", "dump_and_restore", "dump_restored", **durations)
        return {
            "current_stage": "dump_and_restore",
            "dump_id": result.dump_id,
            "greenmask_durations": durations,
        }

    def verify_and_report_node(state: RunState) -> dict[str, object]:
        report_paths = _verify_and_report(context, state)
        if policy.report.include_synthetic_demo_before_after:
            _write_synthetic_demo_before_after(context)
        runtime_metrics = {"peak_rss_bytes": _peak_rss_bytes()}
        context.logger.event("info", "verify_and_report", "verification_passed", **runtime_metrics)
        return {
            "current_stage": "completed",
            "report_json_path": str(report_paths[0]),
            "report_markdown_path": str(report_paths[1]),
            "finished_at": datetime.now(UTC).isoformat(),
            "runtime_metrics": runtime_metrics,
        }

    graph = StateGraph(RunState)
    graph.add_node("load_policy", load_policy_node)
    graph.add_node("inspect_schema", inspect_schema_node)
    graph.add_node("collect_mapping_keys", collect_mapping_keys_node)
    graph.add_node("generate_replacements_agent", generate_replacements_agent_node)
    graph.add_node("build_greenmask_config", build_greenmask_config_node)
    graph.add_node("dump_and_restore", dump_and_restore_node)
    graph.add_node("verify_and_report", verify_and_report_node)
    graph.add_edge(START, "load_policy")
    graph.add_edge("load_policy", "inspect_schema")
    graph.add_edge("inspect_schema", "collect_mapping_keys")
    graph.add_edge("collect_mapping_keys", "generate_replacements_agent")
    graph.add_edge("generate_replacements_agent", "build_greenmask_config")
    graph.add_edge("build_greenmask_config", "dump_and_restore")
    graph.add_edge("dump_and_restore", "verify_and_report")
    graph.add_edge("verify_and_report", END)
    return graph.compile(checkpointer=checkpointer)


def _state_string(state: Mapping[str, object], key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        raise PolicyError(f"run checkpoint is missing required {key}")
    return value


def _llm_stats_from_state(state: Mapping[str, object]) -> LLMStats:
    payload = state.get("llm")
    if not isinstance(payload, Mapping):
        raise PolicyError("run checkpoint is missing LLM evidence")
    try:
        return LLMStats(
            provider=str(payload["provider"]),
            model=str(payload["model"]),
            structured_output=bool(payload["structured_output"]),
            batches=int(payload["batches"]),
            accepted_items=int(payload["accepted_items"]),
            rejected_items=int(payload["rejected_items"]),
            duration_seconds=float(payload["duration_seconds"]),
        )
    except (KeyError, TypeError, ValueError):
        raise PolicyError("run checkpoint has invalid LLM evidence") from None


def _started_at_from_state(state: Mapping[str, object]) -> datetime:
    try:
        value = _state_string(state, "started_at")
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise PolicyError("run checkpoint has invalid start time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyError("run checkpoint has invalid start time")
    return parsed


def _verify_and_report(context: WorkflowContext, state: Mapping[str, object]) -> tuple[Path, Path]:
    """Run all checks, write reports first, then fail closed on required failures."""

    policy = context.loaded.policy
    json_path = context.run_dir / policy.report.json_filename
    markdown_path = context.run_dir / policy.report.markdown_filename
    verification_context = VerificationContext(
        run_id=_state_string(state, "run_id"),
        policy_sha256=context.loaded.sha256,
        source_schema_sha256=_state_string(state, "schema_fingerprint"),
        started_at=_started_at_from_state(state),
        llm=_llm_stats_from_state(state),
        artifacts=ArtifactPaths(
            sanitized_dump=policy.greenmask.storage_dirname,
            generated_greenmask_config="greenmask.generated.yaml",
            mapping_registry=policy.mapping.registry_filename,
            markdown_report=policy.report.markdown_filename,
        ),
    )
    with MappingRegistry(context.registry_path) as registry:
        registry.initialize(_run_metadata(context, verification_context.source_schema_sha256))
        with (
            source_connection(context.runtime.source_dsn) as source,
            target_connection(context.runtime.target_dsn) as target,
        ):
            outcome = verify_and_write_report(
                policy=policy,
                source=source,
                target=target,
                registry=registry,
                hmac_key=HMACKey(context.runtime.hmac_key),
                context=verification_context,
                json_path=json_path,
                markdown_path=markdown_path,
                raise_on_failure=False,
            )
    if not outcome.passed:
        context.logger.event("error", "verify_and_report", "verification_failed")
        outcome.raise_for_failure()
    return json_path, markdown_path


def _write_synthetic_demo_before_after(context: WorkflowContext) -> Path:
    """Write the sole raw-value artifact, explicitly allowed only for synthetic demo data."""

    policy = context.loaded.policy
    destination = context.run_dir / "demo-before-after.md"
    lines = [
        "# Synthetic-only before/after sample",
        "",
        "> This file is permitted only because `policy.demo.yaml` uses synthetic seed data.",
        "> Do not enable this report for production-like source databases.",
        "",
    ]
    try:
        with (
            source_connection(context.runtime.source_dsn) as source,
            target_connection(context.runtime.target_dsn) as target,
        ):
            source_snapshot = inspect_schema(source)
            for group_id, group in sorted(policy.groups.items()):
                lines.extend(
                    (
                        f"## {group_id}",
                        "",
                        "| Column | Key | Before | After |",
                        "| --- | --- | --- | --- |",
                    )
                )
                rows_written = 0
                for ref in group.columns:
                    primary_keys = tuple(
                        sorted(
                            item.ref.column
                            for item in source_snapshot.columns
                            if item.ref.schema_name == ref.schema_name
                            and item.ref.table == ref.table
                            and item.is_primary_key
                        )
                    )
                    if not primary_keys:
                        continue
                    statement = _demo_sample_statement(ref, primary_keys)
                    with source.cursor() as cursor:
                        cursor.execute(statement, (policy.report.sample_rows,))
                        source_rows = cursor.fetchall()
                    with target.cursor() as cursor:
                        cursor.execute(statement, (policy.report.sample_rows,))
                        target_rows = cursor.fetchall()
                    target_by_key = {tuple(row[:-1]): row[-1] for row in target_rows}
                    for source_row in source_rows:
                        key = tuple(source_row[:-1])
                        target_value = target_by_key.get(key)
                        if target_value is None:
                            continue
                        lines.append(
                            "| "
                            + " | ".join(
                                (
                                    _markdown_demo_value(ref.display_name),
                                    _markdown_demo_value(",".join(str(value) for value in key)),
                                    _markdown_demo_value(str(source_row[-1])),
                                    _markdown_demo_value(str(target_value)),
                                )
                            )
                            + " |"
                        )
                        rows_written += 1
                if rows_written == 0:
                    lines.append("| _no PK-aligned synthetic sample_ | - | - | - |")
                lines.append("")
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _private_file(destination)
    except Exception:
        raise PolicyError(
            "unable to write requested synthetic demo before/after artifact"
        ) from None
    context.logger.event("info", "verify_and_report", "synthetic_demo_before_after_written")
    return destination


def _demo_sample_statement(ref, primary_keys: tuple[str, ...]) -> sql.Composed:
    identifiers = [*(sql.Identifier(key) for key in primary_keys), sql.Identifier(ref.column)]
    return sql.SQL(
        "SELECT {columns} FROM {table} WHERE {value_column} IS NOT NULL ORDER BY {order} LIMIT %s"
    ).format(
        columns=sql.SQL(", ").join(identifiers),
        table=sql.SQL("{}.{}").format(sql.Identifier(ref.schema_name), sql.Identifier(ref.table)),
        value_column=sql.Identifier(ref.column),
        order=sql.SQL(", ").join(sql.Identifier(key) for key in primary_keys),
    )


def _markdown_demo_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _check_resume_compatibility(
    context: WorkflowContext,
    state: Mapping[str, object],
) -> None:
    if _state_string(state, "run_id") != context.run_dir.name:
        raise PolicyError("run checkpoint does not match requested run ID")
    if _state_string(state, "policy_sha256") != context.loaded.sha256:
        raise PolicyError("resume policy hash is incompatible with the checkpoint")
    expected_schema = state.get("schema_fingerprint")
    if isinstance(expected_schema, str):
        with source_connection(context.runtime.source_dsn) as connection:
            current_schema = inspect_schema(connection).fingerprint
        if current_schema != expected_schema:
            raise PolicyError(
                "resume source schema fingerprint is incompatible with the checkpoint"
            )
        if context.registry_path.exists():
            with MappingRegistry(context.registry_path) as registry:
                registry.initialize(_run_metadata(context, current_schema))


def _initial_state(context: WorkflowContext) -> RunState:
    return {
        "run_id": context.run_dir.name,
        "policy_path": str(context.loaded.path),
        "policy_sha256": context.loaded.sha256,
        "run_dir": str(context.run_dir),
        "started_at": datetime.now(UTC).isoformat(),
        "current_stage": "created",
        "errors_without_raw_data": [],
    }


def _checkpoint_state(checkpointer: SqliteSaver, run_id: str) -> dict[str, object] | None:
    config = {"configurable": {"thread_id": run_id}}
    checkpoint_tuple = checkpointer.get_tuple(config)
    if checkpoint_tuple is None:
        return None
    values = checkpoint_tuple.checkpoint.get("channel_values")
    if not isinstance(values, dict):
        raise PolicyError("run checkpoint is invalid")
    return dict(values)


def run_sanitization(
    *,
    policy_path: str | Path,
    run_id: str,
    resume: bool = False,
    provider_factory: Callable[[], ReplacementProvider] | None = None,
    environment: Mapping[str, str] | None = None,
    interrupt_after: tuple[str, ...] | None = None,
) -> RunState:
    """Run or resume the complete LangGraph workflow with durable checkpoints."""

    context = _create_context(
        policy_path=policy_path,
        run_id=run_id,
        resume=resume,
        provider_factory=provider_factory,
        environment=environment,
    )
    config = {"configurable": {"thread_id": run_id}}
    with _open_checkpointer(context.state_path) as checkpointer:
        workflow = _build_workflow(context, checkpointer)
        existing = _checkpoint_state(checkpointer, run_id)
        if resume:
            if existing is None:
                raise PolicyError("no LangGraph checkpoint exists for this run")
            _check_resume_compatibility(context, existing)
            snapshot = workflow.get_state(config)
            if not snapshot.next:
                raise PolicyError("run is already complete; use the verify command instead")
            result = workflow.invoke(
                None,
                config,
                durability="sync",
                interrupt_after=interrupt_after,
            )
        else:
            if existing is not None:
                raise PolicyError("run checkpoint already exists; use --resume or a new run ID")
            result = workflow.invoke(
                _initial_state(context),
                config,
                durability="sync",
                interrupt_after=interrupt_after,
            )
    _private_file(context.state_path)
    return cast(RunState, result)


def verify_existing_run(
    *,
    policy_path: str | Path,
    run_id: str,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Re-run verifier/report generation for a compatible, previously checkpointed run."""

    context = _create_context(
        policy_path=policy_path,
        run_id=run_id,
        resume=True,
        provider_factory=None,
        environment=environment,
    )
    with _open_checkpointer(context.state_path) as checkpointer:
        state = _checkpoint_state(checkpointer, run_id)
    if state is None:
        raise PolicyError("no LangGraph checkpoint exists for this run")
    _check_resume_compatibility(context, state)
    _verify_and_report(context, state)
    if context.loaded.policy.report.include_synthetic_demo_before_after:
        _write_synthetic_demo_before_after(context)
    _private_file(context.state_path)
