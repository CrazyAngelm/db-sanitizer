# План реализации для coding-агента

## 1. Запрещённый scope creep

До прохождения всех критериев не добавлять:

- UI/API;
- Kubernetes/S3;
- другие СУБД;
- auto-discovery PII;
- документы/free text;
- векторные БД;
- сложную multi-agent дискуссию;
- облачный LLM;
- дополнительные типы данных.

## 2. Целевая структура репозитория

Нормативное дерево также лежит в `templates/project-tree.txt`.

```text
db-sanitizer/
├── src/db_sanitizer/
│   ├── __init__.py
│   ├── cli.py
│   ├── graph.py
│   ├── state.py
│   ├── settings.py
│   ├── errors.py
│   ├── safe_logging.py
│   ├── policy/
│   │   ├── models.py
│   │   ├── loader.py
│   │   └── validator.py
│   ├── postgres/
│   │   ├── connection.py
│   │   ├── inspector.py
│   │   └── collector.py
│   ├── mapping/
│   │   ├── normalization.py
│   │   ├── hmac_key.py
│   │   └── registry.py
│   ├── llm/
│   │   ├── base.py
│   │   ├── models.py
│   │   ├── ollama_provider.py
│   │   ├── fake_provider.py
│   │   └── generator.py
│   ├── greenmask/
│   │   ├── config_builder.py
│   │   ├── runner.py
│   │   └── mapper_process.py
│   └── verify/
│       ├── checks.py
│       ├── runner.py
│       └── report.py
├── config/
│   └── policy.demo.yaml
├── demo/
│   ├── sql/schema.sql
│   ├── sql/seed.sql
│   ├── seed_generator.py
│   └── expected/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── security/
├── scripts/
│   ├── wait_for_postgres.sh
│   └── pull_ollama_model.sh
├── docker-compose.yml
├── Dockerfile
├── Makefile
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

## 3. Этапы

### Этап 0. Scaffold и воспроизводимость

Создать:

- package layout;
- `pyproject.toml` и lock;
- Dockerfile;
- Compose services: `source-db`, `target-db`, `ollama`, `sanitizer`;
- Make targets;
- basic CI/local test command;
- safe `.env.example`.

Definition of done:

- контейнер приложения стартует;
- `make test` запускает пустой pytest suite;
- образы и зависимости закреплены;
- secrets отсутствуют в репозитории.

### Этап 1. Policy и introspection

Реализовать:

- Pydantic policy models;
- YAML loader;
- env resolution;
- PostgreSQL inspector;
- policy/schema validation;
- schema fingerprint;
- safe DSN redaction.

Сначала написать unit tests для всех validation errors.

Definition of done:

- valid demo policy проходит;
- nonexistent/duplicate/unsupported columns отклоняются;
- source==target отклоняется;
- raw DSN password не попадает в test logs.

### Этап 2. Normalization и registry

Реализовать:

- стратегии `human_text`, `email`, `phone`;
- HMAC key service;
- SQLite migrations/init;
- insert/dedup/list unassigned/assign lookup;
- run metadata and compatibility checks;
- permissions.

Definition of done:

- одинаковое нормализованное значение даёт один key;
- разные groups дают разные keys;
- duplicate replacement блокируется;
- raw source string отсутствует в SQLite bytes.

### Этап 3. Потоковый collector

Реализовать:

- безопасное quoting identifiers;
- server-side cursor;
- `SELECT DISTINCT` для каждой configured column;
- configurable fetch size;
- upsert source keys;
- counters без raw values.

Definition of done:

- повторы из разных таблиц одной group дедуплицируются;
- null не добавляется;
- memory не зависит от общего числа строк;
- collector test работает на большом synthetic dataset.

### Этап 4. LLM provider и generation agent

Реализовать:

- provider protocol;
- Ollama provider со structured output;
- fake provider;
- prompt templates для четырёх entity types;
- batch validator/retry;
- transactional assignment;
- LangGraph agent node.

Definition of done:

- fake provider покрывает success, duplicates, invalid format и retry exhaustion;
- prompt leakage test доказывает отсутствие известных source values;
- real Ollama smoke генерирует минимум один валидный batch.

### Этап 5. Greenmask adapter и mapper process

Реализовать:

- generated config builder;
- Greenmask validate/dump/restore runner;
- JSONL mapper process;
- normalization/HMAC/lookup reuse;
- fail-closed errors;
- subprocess timeouts и safe stderr capture.

Definition of done:

- mapper unit tests обрабатывают null, несколько колонок и missing mapping;
- generated config проходит Greenmask validate;
- integration dump/restore работает на одной таблице.

### Этап 6. LangGraph workflow и resume

Реализовать все graph nodes, state, conditional errors, SQLite checkpointer и `--resume`.

Definition of done:

- полный fake-LLM run проходит;
- искусственный сбой после generation возобновляется без повторной генерации;
- несовместимый resume отклоняется.

### Этап 7. Verifier и report

Реализовать:

- schema/table/row/null checks;
- FK orphan checks;
- PK/UNIQUE checks;
- mapping consistency по PK;
- no-source-intersection;
- distinct cardinality;
- JSON Schema compliant report;
- Markdown renderer.

Definition of done:

- каждый намеренно повреждённый fixture вызывает нужный failed check и exit code 5;
- passed невозможен при required failure;
- report не содержит raw source PII.

### Этап 8. Demo, performance smoke и README

Создать демонстрационную схему и seed, завершить `make demo`, `make perf`, before/after и документацию.

Definition of done:

- чистый запуск `make demo` проходит;
- отчёты созданы;
- cross-table duplicates имеют одинаковые replacements;
- FK/row count/UNIQUE/diversity checks pass;
- README заполнен по `FINAL_REPO_README_TEMPLATE.md`;
- benchmark содержит rows, distinct counts, duration, throughput и max RSS с указанием машины.

## 4. Рекомендуемый порядок тестирования

После каждого этапа:

```bash
make lint
make test
```

После этапа 5:

```bash
pytest -m integration
```

После этапа 8:

```bash
make clean
make demo
make perf PERF_ROWS=100000
```

## 5. Правила реализации

- Python type hints обязательны для публичных функций.
- Публичные контракты оформляются Pydantic/dataclass/protocol.
- Не логировать строки из sensitive columns даже в debug.
- Не ловить исключения без преобразования в typed domain error.
- Subprocess commands передавать списком аргументов, без shell interpolation.
- SQL identifiers формировать безопасными средствами psycopg, значения — параметрами.
- Unit tests не зависят от сети или реальной модели.
- Integration tests помечены marker и используют Compose/PostgreSQL.
- Greenmask и Ollama не оборачивать собственными серверами.
- Никакого silent fallback к исходному значению или Faker.

## 6. Финальный self-review coding-агента

Перед сдачей coding-агент обязан:

1. Запустить все команды из README в чистом окружении.
2. Сопоставить реализацию с `TRACEABILITY_MATRIX.md`.
3. Проверить отсутствие PII/secrets в git diff, logs и `.runs` отчётах.
4. Удалить dead code и незаявленные feature flags.
5. Зафиксировать фактические версии и benchmark.
6. Явно перечислить любые отклонения от спецификации.
