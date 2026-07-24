# Матрица трассируемости

> **Актуальный runtime-контракт:** основной сценарий использует локальный
> Ollama `qwen3:4b` через `make demo`; `OLLAMA_MODEL` выбирает и скачивает ту
> же модель, что использует policy. OpenRouter `deepseek/deepseek-v4-flash`
> сохранён как необязательный удалённый provider через `make demo-openrouter`.

| Source ID | Реализация/решение | Acceptance checks |
|---|---|---|
| FR-01 | PostgreSQL 16 adapter и demo | AC-001, AC-030 |
| FR-02 | Source connection -> Greenmask dump -> target restore | AC-001, AC-031 |
| FR-03 | Entity-specific constraints и генерация правдоподобных synthetic строк через provider contract | AC-012, AC-013 |
| FR-04 | LangGraph generation agent вызывает Ollama по умолчанию либо необязательный OpenRouter | AC-010, optional remote AC-011 |
| FR-05 | Consistency groups + normalized HMAC + SQLite registry | AC-020..026 |
| FR-06 | PK/FK strategy, Greenmask schema restore, verifier | AC-033..035 |
| FR-07 | Per-table row count verification | AC-031 |
| FR-08 | One-to-one replacements и distinct-cardinality checks | AC-043, AC-044 |
| IR-01 | PostgreSQL, Greenmask, LangGraph, OpenRouter, Ollama, psycopg, Pydantic, pytest | AC-071 |
| IR-02 | Собственный код только policy/registry/agent adapter/mapper/verifier | AC-071, code review |
| IR-03 | LangGraph StateGraph + один LLM agent node | AC-004, AC-011 |
| IR-04 | Нет самописного agent runtime | dependency/code review |
| NFR-01 | Configurable CLI/job, isolated run ids, Docker/Make | AC-001..005 |
| NFR-02 | Validation, checkpoints, resume, exit codes, reports | AC-004, AC-054, AC-064 |
| NFR-03 | Server-side cursors, disk-backed HMAC multisets, Greenmask streaming, batch LLM | AC-050..053, 1M perf smoke |
| NFR-04 | Adapter/provider/normalizer contracts | README review AC-074 |
| NFR-05 | Policy separated from data plane and generation | architecture/code review |
| NFR-06 | Architecture, alternatives and trade-offs in README | AC-074, AC-075 |
| RR-01 | `make demo` in clean environment | AC-001 |
| RR-02 | Exact README quick start | AC-070 |
| RR-03 | Containerized CLI/job chosen and justified | AC-070, AC-071 |
| DEL-01 | Complete source repository | final artifact review |
| DEL-02 | README template sections completed | AC-070..075 |
| DEL-03 | Synthetic-only before/after and reports | AC-065 |

## Правило завершения

Ни одно source requirement нельзя считать выполненным только по наличию кода. У него должен быть либо автоматический acceptance check, либо явно проверяемый пункт документации/code review из таблицы.
