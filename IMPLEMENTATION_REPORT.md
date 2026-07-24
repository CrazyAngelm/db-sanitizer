# Implementation Report — DB Sanitizer

## Result

The adjusted PoC is implemented and its final real-model demo completed successfully. Greenmask created/restored a sanitized logical dump; `report.json` status is `passed`; all nine required report checks passed.

## Runtime versions

| Component | Version / pin |
|---|---|
| Python | 3.12.13 in image |
| PostgreSQL | 16.14 (`postgres@sha256:92620daddcd947f8d5ab5ba66e848702fe443d87fed30c4cea8e389fd78dfc55`) |
| Greenmask | v0.2.22 (`greenmask@sha256:dad5506baf096965a6e2b6a82f00cc7a361102efdf4c6b4b491ae3c098475254`) |
| LangGraph | 1.2.9 |
| LangGraph SQLite checkpointer | 3.1.0 |
| psycopg | 3.3.4 |
| Pydantic | 2.13.4 |
| OpenRouter model | `deepseek/deepseek-v4-flash` |
| Dependency lock | `uv.lock` |

Docker image pins are in `Dockerfile`/`docker-compose.yml`; Python dependencies are exact and locked by `uv.lock`.

## Executed commands and results

Host: Windows 11 10.0.26200, Intel64 Family 6 Model 170, Docker Desktop 28.3.0, Compose 2.38.1. The host shipped MinGW GNU Make as `mingw32-make`, so it was exposed as `make` for the literal Make targets below.

| Command | Actual result |
|---|---|
| `make lint` | passed (`ruff check` + format check) |
| `make test` | 40 passed, 3 Docker integration tests skipped by default; no network/model required |
| Docker-backed fake workflow/resume/FK regression | passed: 3 tests in the Greenmask-capable sanitizer image; interrupted run resumed without new LLM calls, and duplicate-name composite FKs/MATCH SIMPLE NULL semantics were covered |
| Real OpenRouter four-entity smoke | passed: structured outputs for name/email/phone/address all met constraints |
| `make demo` | passed, real OpenRouter model used, final `.runs/demo/report.json` is `passed` |
| `db-sanitizer verify --policy config/policy.demo.yaml --run-id demo` | passed; regenerated `report.json` with status `passed` |
| Tampered target verification | expected exit **5** for a required mapping failure; target restored from the demo dump and verification passed again |
| Missing-mapping Greenmask run | expected fail-closed `DataPlaneError` exit **4**; no source-value fallback occurred |
| `make perf PERF_ROWS=100000` | passed; generated 150,100 rows and a benchmark artifact before the following final demo cleanup |

## Final real demo evidence

Final run ID: `demo`.

- LLM provider/model: `openrouter` / `deepseek/deepseek-v4-flash`.
- Structured batches: 13.
- Accepted mappings: 236 (59 in each of four groups).
- Rejected synthetic candidates: 68.
- LLM generation time: 621.444 s.
- Greenmask validation / dump / restore: 2.128 s / 2.784 s / 0.418 s.
- Peak sanitizer RSS: 93,036,544 bytes.
- All checks passed: schema fingerprint; schema columns/types; row counts; configured NULL/distinct counts; FKs; PK/UNIQUE; registry mappings; source/target HMAC nonintersection; no placeholder collapse.

Security scan of final `.runs/demo` found no selected synthetic source markers in registry/config/logs/checkpoints/reports/dump outside the explicitly allowed `demo-before-after.md` artifact.

## Performance smoke

`make perf PERF_ROWS=100000` used the **explicit** fake provider (not a fallback) so data-plane performance was not dominated by external LLM latency.

| Rows | Distinct mappings | Collect | LLM | Dump+transform | Restore | Verify | Peak RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 150,100 | 400 | 9.097 s | 42.481 s | 613.773 s | 9.690 s | 1,524.605 s | 107,737,088 B |

Measured dump+transform throughput: 244.55 rows/s. The benchmark explicitly asserts that 400 accepted LLM items equal 400 distinct mapping keys (rather than 150,100 rows). The 100k-row smoke finished without OOM. Its long verification time is dominated by full proof scans on Docker Desktop bind-mounted storage, not by row materialization in the collector/data plane.

## Traceability

| Requirement family | Evidence |
|---|---|
| FR-01/02 | PostgreSQL 16 Compose source → Greenmask dump → target restore; final demo passed |
| FR-03/04 | Four constrained entity generators; real structured OpenRouter batches in final demo |
| FR-05 | Groups + normalization/HMAC + SQLite mapping; final mapping checks passed |
| FR-06/07 | FK/PK/UNIQUE/row verifier checks passed |
| FR-08 | Distinct cardinality and no-single-placeholder checks passed |
| IR-01/02 | Greenmask, LangGraph, psycopg, Pydantic, SQLite and narrow adapters are present |
| IR-03/04 | One LangGraph agent node (`generate_replacements_agent`); remaining nodes deterministic |
| NFR-01/02 | Isolated run IDs, checkpoints, resume compatibility, stable error categories and reports |
| NFR-03 | Server-side collector, SQLite mappings, Greenmask stream and distinct-key LLM batching; 150,100-row smoke passed |
| NFR-04/05 | Provider, normalization, registry and data-plane boundaries are separated |
| NFR-06 / DEL-02 | README documents architecture, trade-offs, limits and operational commands |
| RR-01/02 / DEL-01/03 | `make demo`, README, synthetic-only before/after and final reports |

## Post-review hardening

- Same-database protection now compares credential-free parsed PostgreSQL endpoint/database identities, so different roles or `postgres`/`postgresql` URI aliases cannot bypass the destructive-target guard.
- Generated registry, dump and report names are basename-only policy fields; absolute and traversal paths are rejected before a run directory is used.
- Foreign-key introspection now reads PostgreSQL catalog key pairs by constraint OID and preserves `MATCH SIMPLE`/`MATCH FULL`; the Docker integration regression covers duplicate FK names, composite ordering and valid partially-NULL `MATCH SIMPLE` rows.
- The benchmark writer now fails unless accepted LLM items equal distinct mapping keys, and records that proof in the artifact.

## Deviations and known limits

1. **Intentional user-requested provider deviation:** original normative documents require local Ollama + `qwen3:4b-instruct` and prohibit cloud LLM. The user later explicitly instructed OpenRouter DeepSeek V4 Flash with an `.env` key. This repository therefore intentionally does **not** satisfy original literal AC-010 or the Ollama portion of AC-001/AC-071. It does satisfy the privacy invariant that raw source PII are never sent to the provider.
2. Greenmask `validate --strict` is not used. Greenmask v0.2.22 emits a generic warning for a valid `Cmd` transformer on a UNIQUE column; strict mode would reject the valid demo before dump. The independent required UNIQUE/FK/mapping verifier remains fail-closed.
3. Default `make test` skips Docker-marked integration tests so it remains offline/model-free. The three marked tests were also executed inside the Greenmask-capable sanitizer image; set `DB_SANITIZER_RUN_INTEGRATION=1` in such an environment to execute them through pytest.
4. This remains a PoC: policy completeness is operator-owned; there is no PII discovery, free-text/document handling, production HA/RBAC or multi-writer registry.
