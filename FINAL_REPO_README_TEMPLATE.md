# DB Sanitizer

> Кратко: контейнеризированный PoC санитизации PostgreSQL с Greenmask, LangGraph и локальной LLM.

## Статус

- Реализовано: [заполнить]
- Проверено на: [ОС/CPU/RAM]
- Версии: [PostgreSQL, Greenmask, Python, LangGraph, Ollama, model]

## Задача

Кратко изложить контекст передачи production-like БД за контур и требования сохранить структуру, связи, объём и разнообразие.

## Границы реализации

### Реализовано

[PostgreSQL, source connection, dump+restore, 4 entity types, consistency groups, verification]

### Не реализовано

[MySQL/MSSQL, UI/API, auto PII discovery, documents/free text, production HA]

## Архитектура

Вставить Mermaid diagram и кратко объяснить разделение control plane/data plane.

## Open-source компоненты и собственный код

| Компонент | Роль | Собственная доработка |
|---|---|---|
| PostgreSQL | Source/target | Inspector/verifier queries |
| Greenmask | Dump/transform/restore | Generated config и external mapper integration |
| LangGraph | Workflow/checkpoints | Конкретные nodes/state |
| Ollama | Local LLM | Provider adapter и validation |
| psycopg | DB access | Collector/inspector |
| SQLite | Mapping/checkpoints | Registry schema |

## Быстрый запуск

### Требования

[Docker/Compose, disk/RAM requirements]

### Команды

```bash
cp .env.example .env
make demo
```

Объяснить, где находятся отчёты и dump.

## Конфигурация policy

Показать компактный фрагмент consistency group и объяснить schema/table/column/entity type/normalization.

## Механика сквозных замен

Описать:

1. normalization;
2. HMAC key;
3. SQLite registry;
4. unique replacement;
5. mapper lookup;
6. одинаковые значения между таблицами.

## Использование LLM

- provider/model;
- structured output;
- batch generation;
- retries/validation;
- какие metadata получает модель;
- доказательство, что raw source PII не отправляются.

## Поток обработки

Перечислить LangGraph nodes и side effects.

## Проверки результата

- schema/types;
- row counts/nulls;
- FK/orphans;
- PK/UNIQUE;
- mapping consistency;
- source/target HMAC intersection;
- distinct cardinality.

## Демонстрация «до/после»

Показывать только synthetic demo values. Привести 2–3 строки, где одна сущность повторяется в разных таблицах и после замены остаётся одинаковой.

## Тесты

```bash
make test
make lint
```

Описать unit/integration/security markers.

## Производительность

```bash
make perf PERF_ROWS=100000
```

Привести фактическую таблицу:

| Rows | Distinct mappings | Collect | LLM | Dump+transform | Restore | Verify | Peak RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|
| ... | ... | ... | ... | ... | ... | ... | ... |

Указать hardware и честно объяснить, что это smoke benchmark.

## Воспроизводимость и resume

Описать run id, run directory, checkpointing, policy/schema/model/secret fingerprint checks.

## Безопасность

- read-only source;
- local LLM;
- no raw PII in registry/logs/report;
- DSN redaction;
- fail closed;
- mapping DB считается чувствительным служебным артефактом.

## Масштабирование и расширение

Объяснить streaming, batch LLM и interfaces для DB adapter/entity type/document adapter/provider.

## Альтернативы и компромиссы

Рассмотреть custom COPY pipeline, built-in-only Greenmask, PostgreSQL Anonymizer, Faker-only, per-row/cloud LLM и причины выбора.

## Ограничения

Честный список ограничений текущего PoC.

## Структура проекта

Кратко показать tree.

## Лицензия

[Указать выбранную OSS-лицензию для тестового репозитория.]
