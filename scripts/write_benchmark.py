"""Create a PII-free performance-smoke benchmark from completed run artifacts."""

from __future__ import annotations

import argparse
import json
import platform
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


def _events(log_path: Path) -> Iterable[dict[str, object]]:
    for line in log_path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if isinstance(payload, dict):
            yield payload


def _timestamp(event: dict[str, object]) -> datetime:
    return datetime.fromisoformat(str(event["timestamp"]))


def _event_by_name(events: Iterable[dict[str, object]], name: str) -> dict[str, object] | None:
    return next((event for event in events if event.get("event") == name), None)


def write_benchmark(run_dir: Path, output_path: Path, requested_rows: int) -> None:
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    events = list(_events(run_dir / "logs.jsonl"))
    schema_event = _event_by_name(events, "schema_validated")
    collect_event = _event_by_name(events, "mapping_keys_collected")
    dump_event = _event_by_name(events, "dump_restored")
    verify_event = _event_by_name(events, "verification_passed")
    if not all((schema_event, collect_event, dump_event, verify_event)):
        raise ValueError("run logs do not contain complete benchmark timing events")

    collector_seconds = (_timestamp(collect_event) - _timestamp(schema_event)).total_seconds()
    verify_seconds = (_timestamp(verify_event) - _timestamp(dump_event)).total_seconds()
    dump_details = dict(dump_event["details"])
    verify_details = dict(verify_event["details"])
    total_rows = sum(int(table["source_rows"]) for table in report["tables"])
    distinct_mappings = sum(int(group["mapping_count"]) for group in report["groups"])
    accepted_items = int(report["llm"]["accepted_items"])
    if total_rows < requested_rows:
        raise ValueError("performance run did not create the requested minimum row count")
    if accepted_items != distinct_mappings:
        raise ValueError("LLM accepted-item count does not equal distinct mapping keys")
    dump_seconds = float(dump_details["dump_seconds"])
    benchmark = {
        "schema_version": "1.0",
        "run_id": report["run_id"],
        "provider": report["llm"]["provider"],
        "model": report["llm"]["model"],
        "requested_rows": requested_rows,
        "total_rows": total_rows,
        "distinct_mappings": distinct_mappings,
        "llm_accepted_items": accepted_items,
        "llm_items_equal_distinct_mappings": True,
        "timings_seconds": {
            "collect": collector_seconds,
            "llm": float(report["llm"]["duration_seconds"]),
            "dump_transform": dump_seconds,
            "restore": float(dump_details["restore_seconds"]),
            "verify": verify_seconds,
        },
        "dump_transform_rows_per_second": total_rows / dump_seconds if dump_seconds else 0.0,
        "peak_rss_bytes": int(verify_details.get("peak_rss_bytes", 0)),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(benchmark, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path = output_path.with_suffix(".md")
    markdown_path.write_text(
        "# DB Sanitizer performance smoke\n\n"
        f"- Run ID: `{benchmark['run_id']}`\n"
        f"- Provider: `{benchmark['provider']}` / `{benchmark['model']}`\n"
        f"- Rows: {benchmark['total_rows']} (requested at least {requested_rows})\n"
        f"- Distinct mappings: {benchmark['distinct_mappings']}\n"
        f"- Accepted LLM items: {benchmark['llm_accepted_items']} (equals distinct mappings)\n"
        f"- Dump+transform rows/sec: {benchmark['dump_transform_rows_per_second']:.2f}\n"
        f"- Peak RSS bytes: {benchmark['peak_rss_bytes']}\n\n"
        "| Collect | LLM | Dump+transform | Restore | Verify |\n"
        "| ---: | ---: | ---: | ---: | ---: |\n"
        f"| {collector_seconds:.3f} | {benchmark['timings_seconds']['llm']:.3f} | "
        f"{dump_seconds:.3f} | {benchmark['timings_seconds']['restore']:.3f} | "
        f"{verify_seconds:.3f} |\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a DB Sanitizer perf smoke benchmark")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--requested-rows", required=True, type=int)
    args = parser.parse_args()
    write_benchmark(args.run_dir, args.output, args.requested_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
