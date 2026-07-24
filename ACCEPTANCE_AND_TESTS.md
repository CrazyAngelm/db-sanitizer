# Критерии приёмки и тест-план

## 1. Definition of Done

Решение принимается только если одновременно выполнены условия:

- чистый `make demo` завершается кодом 0;
- реальная локальная LLM использована хотя бы в одном demo batch;
- санитизированный dump создан и восстановлен;
- все required checks имеют `pass`;
- unit/integration/security tests проходят;
- README описывает архитектуру, OSS, LLM, consistency, масштабирование, альтернативы и ограничения;
- нет незаявленного функционала, который усложняет запуск.

## 2. Acceptance criteria

### Запуск и воспроизводимость

| ID | Критерий |
|---|---|
| AC-001 | `make demo` поднимает source, target, Ollama и приложение без ручного редактирования файлов. |
| AC-002 | Все настройки и secrets берутся из policy/env; `.env.example` безопасен. |
| AC-003 | Повторный запуск с новым `run_id` создаёт изолированные артефакты. |
| AC-004 | `--resume` продолжает прерванный совместимый run и не повторяет завершённую LLM-генерацию. |
| AC-005 | Все зависимости и контейнеры закреплены версиями/lock. |

### LLM и реалистичность

| ID | Критерий |
|---|---|
| AC-010 | Demo использует Ollama, а не fake provider. |
| AC-011 | LLM-ответ запрашивается и проверяется как structured output. |
| AC-012 | Для всех четырёх entity types создано хотя бы два различных значения. |
| AC-013 | Email заканчиваются на `example.test`; значения проходят policy constraints. |
| AC-014 | Ни один известный source PII не присутствует в LLM prompt. |
| AC-015 | Invalid/duplicate model output вызывает retry; exhaustion завершает run кодом 3. |

### Сквозные замены

| ID | Критерий |
|---|---|
| AC-020 | Одинаковое имя в `customers.full_name` и `orders.billing_name` имеет одну replacement. |
| AC-021 | Одинаковый email в `customers.email` и `orders.contact_email` имеет одну replacement. |
| AC-022 | Одинаковый телефон в `customers.phone` и `support_tickets.callback_phone` имеет одну replacement. |
| AC-023 | Одинаковый адрес в `customers.address` и `support_tickets.delivery_address` имеет одну replacement. |
| AC-024 | Два разных source keys одной group не получают одну replacement. |
| AC-025 | Mapping registry не содержит raw source values. |
| AC-026 | Missing mapping завершает mapper ошибкой, а не сохраняет исходное значение. |

### Структура, связи и объём

| ID | Критерий |
|---|---|
| AC-030 | Набор demo tables/columns/types одинаков в source и target. |
| AC-031 | Количество строк совпадает для каждой таблицы. |
| AC-032 | Количество `NULL` совпадает для каждой чувствительной колонки. |
| AC-033 | Все foreign keys target validated и orphan count равен 0. |
| AC-034 | PK и UNIQUE constraints сохранены; `customers.email` остаётся уникальным. |
| AC-035 | Surrogate PK/FK значения demo не изменились. |

### Обезличивание и разнообразие

| ID | Критерий |
|---|---|
| AC-040 | Каждый non-null configured source value изменён. |
| AC-041 | Target value соответствует mapping registry для исходного HMAC. |
| AC-042 | Пересечение normalized source и target values по HMAC пусто внутри каждой group. |
| AC-043 | `distinct_non_null_before == distinct_non_null_after` для каждой configured column. |
| AC-044 | Ни одна sensitive column не сведена к одной заглушке, если до обработки было больше одного distinct value. |

### Масштабирование и надёжность

| ID | Критерий |
|---|---|
| AC-050 | Collector использует server-side cursor/fetch batches; тест не материализует все source rows. |
| AC-051 | Greenmask mapper не вызывает LLM и работает как long-lived JSONL process. |
| AC-052 | `make perf PERF_ROWS=100000` завершается без OOM и создаёт benchmark report. |
| AC-053 | Количество LLM generation items зависит от distinct source keys, а не от числа строк. |
| AC-054 | Сбой dump/restore или required verification даёт ненулевой exit code. |

### Безопасность и отчёты

| ID | Критерий |
|---|---|
| AC-060 | Passwords в DSN редактируются в логах и ошибках. |
| AC-061 | Raw source PII отсутствуют в logs, checkpoints, report.json и report.md. |
| AC-062 | HMAC secret не записывается на диск. |
| AC-063 | Report проходит `templates/run-report.schema.json`. |
| AC-064 | `status=passed` выставляется только при pass всех required checks. |
| AC-065 | Demo before/after находится отдельно и явно помечен как synthetic-only. |

### Документация

| ID | Критерий |
|---|---|
| AC-070 | README содержит рабочий quick start и точные команды. |
| AC-071 | README перечисляет Greenmask, LangGraph, Ollama, PostgreSQL и собственные интеграции. |
| AC-072 | README описывает HMAC mapping registry и consistency groups. |
| AC-073 | README объясняет, что LLM не получает raw PII. |
| AC-074 | README описывает потоковую обработку, batch LLM, benchmark и extension points. |
| AC-075 | README содержит альтернативы, компромиссы и честные ограничения MVP. |

## 3. Unit tests

Минимальный набор:

```text
test_normalize_human_text
test_normalize_email
test_normalize_phone
test_hmac_same_input_same_group
test_hmac_different_group
test_registry_deduplicates_keys
test_registry_rejects_duplicate_replacement
test_registry_contains_no_raw_value
test_policy_rejects_duplicate_column
test_policy_rejects_unknown_column
test_policy_rejects_unsupported_type
test_policy_rejects_same_source_target
test_policy_rejects_partial_natural_fk_group
test_llm_schema_validation
test_llm_retries_duplicates
test_llm_retry_exhaustion
test_prompt_contains_no_source_values
test_mapper_transforms_multiple_columns
test_mapper_preserves_null
test_mapper_fails_on_missing_mapping
test_report_pass_logic
```

## 4. Integration tests

### IT-01 Happy path with fake LLM

- поднять source/target PostgreSQL;
- seed demo schema;
- выполнить полный graph с fake provider;
- восстановить dump;
- проверить AC-020..044.

### IT-02 Real Ollama smoke

- выполнить generation для малой policy;
- проверить structured output и минимум четыре entity types;
- не запускать этот тест по умолчанию в unit CI, но запускать в `make demo`.

### IT-03 Resume

- остановить workflow после `generate_replacements_agent`;
- продолжить с `--resume`;
- проверить отсутствие новых LLM calls и успешное завершение.

### IT-04 Broken constraints

- намеренно создать duplicate replacement либо orphan target row;
- verifier обязан вернуть failed required check и exit code 5.

### IT-05 Fail closed

- удалить mapping после generation;
- Greenmask/mapper должен завершиться ошибкой;
- исходное значение не должно попасть в target как fallback.

## 5. Security tests

Использовать заведомо уникальные markers:

```text
PII_LEAK_MARKER_NAME_7F3A
pii-leak-marker@example.org
+79990001122
PII_LEAK_MARKER_ADDRESS_91BC
```

После run рекурсивно проверить files в `.runs/<run_id>` и captured logs. Marker может присутствовать только в synthetic source database/fixture, но не в registry, prompts, state, logs и reports.

## 6. Performance smoke

`make perf PERF_ROWS=100000` должен:

- создать не менее 100 000 rows суммарно;
- использовать ограниченный pool distinct PII, чтобы проверять масштабирование строкового data plane;
- записать hardware/software context;
- записать duration collector/generation/dump/restore/verify;
- записать rows/sec для dump+transform;
- записать peak RSS приложения, если доступно;
- подтвердить, что LLM items count равен distinct keys, а не total rows.

Это smoke/демонстрация архитектуры, а не универсальный SLA.

## 7. Проверка архива перед сдачей

Coding-агент должен приложить в итоговый репозиторий короткий `IMPLEMENTATION_REPORT.md`:

- какие criteria пройдены;
- команды и результаты;
- версии компонентов;
- benchmark;
- отклонения/известные ограничения.
