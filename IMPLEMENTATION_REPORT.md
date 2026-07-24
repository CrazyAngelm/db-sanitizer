# Отчёт о реализации DB Sanitizer

**Статус:** выполнено и проверено локально в Docker.

**Репозиторий:** `https://github.com/CrazyAngelm/db-sanitizer`

**Текущая операционная документация:** [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), [TRACEABILITY_MATRIX.md](TRACEABILITY_MATRIX.md).

## 1. Итог

Реализован контейнеризированный PostgreSQL PoC, который:

1. валидирует явную policy и source schema;
2. потоково собирает только HMAC-ключи уникальных чувствительных значений;
3. пакетно запрашивает безопасный пул синтетических замен у LLM;
4. атомарно фиксирует mappings в SQLite;
5. запускает Greenmask с долгоживущим JSONL mapper;
6. восстанавливает sanitized dump в изолированный target PostgreSQL;
7. выполняет обязательную потоковую верификацию и безопасно пишет отчёты.

Запуск завершается fail closed при отсутствующем mapping, невалидном ответе provider, ошибке Greenmask/dump/restore или любой обязательной failed-проверке.

## 2. Изменения по сравнению с исходным PoC

### Провайдеры

Основной provider — OpenRouter `deepseek/deepseek-v4-flash`, выбранный пользователем для реального demo. Реализован также необязательный локальный `OllamaProvider`:

- discriminated union в `policy/models.py` различает `openrouter` и `ollama`;
- `config/policy.demo.yaml` остаётся OpenRouter-default;
- `config/policy.ollama.yaml` задаёт локальный provider без API key;
- Compose-профиль `ollama` и `ollama-pull` запускаются только через `make demo-ollama`;
- `DeterministicSyntheticProvider` остаётся только test/performance provider.

Все providers используют один `ReplacementProvider` contract и получают только entity type, locale, количество и format constraints. Raw source PII, source HMAC, DSN и secrets не передаются модели.

### Потоковый verifier

Исправлена наиболее существенная масштабируемость verifier:

- PostgreSQL читается именованными server-side cursors с `fetchmany()`;
- source/target HMAC сравниваются дисковой SQLite work table, без полного Python `set`/`Counter`;
- повторные registry lookups имеют ограниченный LRU-кэш (4 096 ключей), поэтому память не растёт с числом строк;
- fallback для configured transformed PK использует HMAC-мультисеты;
- сохранены проверки точного mapping по safe primary key, `NULL`, invalid type, unchanged source, nonintersection и placeholder collapse.

## 3. Компоненты

| Компонент | Реализация |
|---|---|
| Policy | Pydantic policy, env-indirection, schema/identifier validation, provider discriminated union |
| Orchestration | LangGraph `StateGraph`, SQLite checkpoint, `--resume` compatibility checks |
| Registry | SQLite HMAC mapping registry, unique constraints, транзакционные batch assignments |
| LLM | OpenRouter structured JSON, Ollama structured JSON, retry/validation/atomic generator |
| Data plane | Greenmask `Cmd` transformer, JSONL process, HMAC lookup-only mapper |
| PostgreSQL | psycopg 3, safe SQL composition, named cursors, schema inspector |
| Verification | required checks, streaming rows, disk-backed HMAC multiset, JSON/Markdown report |
| Containers | pinned multi-stage Dockerfile, separate test target, Compose profiles |

## 4. Проверки сохранности и ограничение обещания

Автоматически проверяются schema fingerprint, таблицы, configured columns/type/length, row count, `NULL` и distinct stats, PK/UNIQUE, FK и orphan rows, configured mappings, HMAC nonintersection и single-placeholder collapse.

PoC **не заявляет** автоматическое доказательство equivalence для `NOT NULL`, `DEFAULT`, identity parameters, sequences, `CHECK`, обычных indexes, FK actions/deferrability, views и triggers. Они переносятся Greenmask/pg_dump стандартно, но не перечислены как доказанные проверки. Это ограничение отражено в README и ARCHITECTURE.

## 5. Выполненная валидация

| Команда / сценарий | Результат |
|---|---|
| `make lint` | успешно |
| `make test` | 48 passed |
| `make test-integration` | 3 Docker integration tests passed |
| `make test-all` | unit/security + Docker integration passed |
| `make demo` | реальный OpenRouter run, обязательные проверки passed |
| `make perf PERF_ROWS=100000` | passed, 150 100 total rows |
| `make perf PERF_ROWS=1000000` | passed, 1 500 100 total rows |
| tampered target | verifier returns exit 5 |
| missing mapping | mapper fails closed with exit 4 |

### Performance smoke

Оба benchmark запускают fake provider явно; модель не является bottleneck и 400 accepted items равны 400 distinct mappings.

| Requested orders | Total rows | Dump+transform | Verify | Peak RSS |
|---:|---:|---:|---:|---:|
| 100 000 | 150 100 | 2 534.91 rows/s, 59.213 s | 21.651 s | 90 923 008 B |
| 1 000 000 | 1 500 100 | 3 979.84 rows/s, 376.925 s | 88.251 s | 91 779 072 B |

Артефакт 1M сохранён в [docs/benchmarks/benchmark-1000000.json](docs/benchmarks/benchmark-1000000.json). Измерения проведены на Windows 11 / Intel64 / Python 3.12.13 в Docker; это smoke benchmark, не production SLA.

## 6. Security properties

- raw PII отсутствуют в registry, logs, reports и LangGraph state;
- source/target identity сравнивается без учёта user/password, предотвращая destructive same-database run;
- SQL identifiers allow-listed/quoted, значения параметризованы;
- subprocess запускаются argument list без shell interpolation;
- generated config и artifact paths валидируются до исполнения;
- mapper не возвращает исходное значение как fallback;
- отдельный test image не является default runtime image; Compose `sanitizer` собирает `runtime` target.

## 7. Осознанные границы

Это одиночный контейнерный PoC, а не production service. Перед реальными данными нужны отдельные owner-approved policy, key management/rotation, controlled credentials, threat modeling, load validation на фактической схеме, audit retention и независимый security review. Исторические требования перенесены в `docs/history/` и не используются как текущий источник правды.
