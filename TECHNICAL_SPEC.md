# Техническая спецификация MVP

## 1. Название и формат

Рабочее имя: **DB Sanitizer**.

Формат: контейнеризированная CLI/job-утилита. Она оркестрирует подготовку mapping registry, вызов Greenmask, восстановление и верификацию.

## 2. Термины

- **Policy** — YAML с подключениями через env-переменные, группами и правилами.
- **Consistency group** — набор семантически эквивалентных столбцов, в которых одинаковое исходное значение обязано получить одинаковую замену.
- **Source key** — `HMAC-SHA256(group_id + normalized_source_value)`.
- **Mapping registry** — SQLite-файл с source keys и синтетическими заменами.
- **Run directory** — изолированный каталог артефактов одного запуска.
- **Agent node** — LangGraph-узел, вызывающий LLM для пакетной генерации замен.
- **Data plane** — Greenmask и внешний mapper, работающие в потоке данных.

## 3. Входы

### 3.1. Обязательные

- YAML policy;
- `SOURCE_DATABASE_URL`;
- `TARGET_DATABASE_URL`;
- `SANITIZER_HMAC_KEY`, минимум 32 случайных байта;
- `OLLAMA_BASE_URL`;
- доступная модель Ollama.

### 3.2. Ограничения

- source и target не должны указывать на одну БД;
- source используется read-only;
- target — отдельная пустая/пересоздаваемая БД;
- поддерживаются явно настроенные `text`, `varchar`, `char` columns;
- `NULL` сохраняется;
- одна колонка не может находиться в двух группах;
- один group должен содержать столбцы одного семантического типа.

## 4. Выходы

В `.runs/<run_id>/` должны появиться:

```text
state.sqlite3
mappings.sqlite3
greenmask.generated.yaml
dump/
logs.jsonl
report.json
report.md
```

В обычном режиме ни один артефакт не должен содержать raw source PII.

В demo mode допустим отдельный `demo-before-after.md`, потому что исходные данные демонстрации синтетические.

## 5. Policy contract

Нормативный пример: `templates/sanitizer-policy.example.yaml`.

Policy содержит:

- версию схемы;
- имена env-переменных с DSN и секретами;
- run/storage settings;
- LLM provider/model/batch/retry/timeout;
- mapping registry settings;
- Greenmask settings;
- consistency groups;
- список `schema/table/column` для каждой группы;
- entity type, locale, normalization и generation constraints;
- demo/report settings.

### 5.1. Валидация policy до запуска

Инструмент обязан fail fast, если:

- неизвестна версия policy;
- отсутствует env-переменная;
- HMAC key слишком короткий;
- source равен target;
- колонка отсутствует;
- тип колонки не поддерживается;
- колонка объявлена дважды;
- group пуст;
- ограничения длины разных колонок несовместимы;
- трансформируемый natural PK/FK не покрыт полностью одной группой;
- target не разрешено очищать;
- run directory невозможно создать.

## 6. Нормализация

Нормализация выполняется до HMAC и должна быть одинаковой в collector и mapper.

### 6.1. `human_text`

1. Unicode NFKC.
2. Trim.
3. Схлопывание последовательностей whitespace до одного пробела.
4. Unicode casefold.

Используется для ФИО и адреса.

### 6.2. `email`

1. Unicode NFKC.
2. Trim.
3. Casefold всей строки для MVP.

### 6.3. `phone`

1. Unicode NFKC.
2. Оставить только цифры.

### 6.4. Семантика

- `NULL` не получает mapping.
- Пустая строка считается обычным значением только при `allow_empty: true`, иначе policy validation или runtime validation завершается ошибкой.

## 7. Mapping registry

### 7.1. Минимальная схема SQLite

```text
run_meta
- run_id PRIMARY KEY
- policy_sha256
- source_schema_sha256
- llm_provider
- llm_model
- hmac_key_fingerprint
- created_at
- status

mappings
- group_id
- source_hmac
- replacement NULLABLE
- replacement_hmac NULLABLE
- created_at
- updated_at
PRIMARY KEY (group_id, source_hmac)
UNIQUE (group_id, replacement)
```

Raw source value хранить запрещено.

### 7.2. Коллектор

Для каждой колонки каждой группы:

1. Выполнить `SELECT DISTINCT column FROM schema.table WHERE column IS NOT NULL`.
2. Использовать server-side cursor и настраиваемый fetch size.
3. Нормализовать значение.
4. Рассчитать `HMAC-SHA256(secret, group_id + "\0" + normalized_value)`.
5. Вставить key через `INSERT OR IGNORE`.
6. Сразу удалить raw value из прикладного состояния; не логировать его.

### 7.3. Назначение замен

- Получить все rows с `replacement IS NULL`, отсортированные по `source_hmac`.
- Генерировать замены LLM-пакетами.
- После валидации назначать их keys в стабильном порядке.
- Каждая replacement уникальна внутри group.
- HMAC replacement не должен совпадать ни с одним source key этой группы.
- Транзакционно сохранять каждый принятый batch.
- При resume уже заполненные rows не генерируются повторно.

## 8. Использование LLM

### 8.1. Обязательный provider

Happy path: Ollama HTTP API.

Provider должен быть спрятан за интерфейсом, чтобы позже добавить OpenAI-compatible local endpoint, но в MVP реализуется только Ollama и test fake.

### 8.2. Содержимое запроса

LLM получает только:

```text
entity_type
locale
count
format_description
min_length/max_length
regex или другие простые constraints
```

LLM не получает исходные значения, source HMAC, DSN или схему БД.

### 8.3. Structured output

Ответ:

```json
{
  "items": [
    {"value": "..."}
  ]
}
```

Он валидируется Pydantic-моделью и JSON Schema.

### 8.4. Валидация batch

Каждое значение должно:

- быть строкой и не быть пустым;
- удовлетворять длине;
- удовлетворять regex, если он задан;
- быть уникальным внутри batch;
- не дублировать ранее сохранённые replacements;
- не совпадать с source key после нормализации и HMAC;
- помещаться во все колонки consistency group.

Невалидные элементы отбрасываются, недостающее количество запрашивается повторно. Максимум — `max_retries` на batch, затем запуск завершается с ошибкой.

### 8.5. Поддерживаемые типы

- `person_name`: реалистичное русское «Имя Фамилия»;
- `email`: реалистичный local part, домен только `example.test`;
- `phone`: формат, заданный policy; значение не обязано быть реально маршрутизируемым;
- `address`: правдоподобная строка российского адреса без необходимости существования.

## 9. Greenmask integration

### 9.1. Роль Greenmask

Greenmask отвечает за:

- логический dump PostgreSQL;
- потоковое чтение данных;
- применение transformation config;
- сохранение схемы;
- создание dump, совместимого с восстановлением;
- restore в target.

### 9.2. Generated config

Для каждой таблицы с чувствительными колонками создаётся один `Cmd` transformer:

- driver: JSON/JSONL text;
- executable: Python mapper process;
- в args передаются mapping DB, group/column map и run metadata;
- `validate: true`;
- null values mapper сохраняет без изменения;
- mapper — long-lived process и не вызывает LLM.

### 9.3. Mapper contract

На каждую JSONL-строку mapper:

1. Читает значения настроенных колонок.
2. Для ненулевого значения выбирает group.
3. Применяет ту же нормализацию.
4. Вычисляет source HMAC.
5. Ищет replacement в SQLite.
6. Возвращает replacement в JSONL Greenmask-формате.

Если mapping не найден, mapper должен fail closed: вывести ошибку только с table/column и hash prefix, завершиться ненулевым кодом. Raw value запрещено выводить в stderr/stdout.

## 10. LangGraph orchestration

### 10.1. Graph state

State не содержит raw PII и включает:

```text
run_id
policy_path
policy_sha256
run_dir
schema_fingerprint
mapping_counts
generated_config_path
dump_id/report paths
current_stage
errors_without_raw_data
```

### 10.2. Узлы

```text
load_policy
  -> inspect_schema
  -> collect_mapping_keys
  -> generate_replacements_agent
  -> build_greenmask_config
  -> dump_and_restore
  -> verify_and_report
  -> END
```

Только `generate_replacements_agent` обязан вызывать LLM. Остальные узлы — детерминированные tools/functions.

### 10.3. Checkpointing и resume

- использовать SQLite checkpointer LangGraph;
- `run_id` используется как thread id;
- повторный `run --run-id X --resume` продолжает с последнего завершённого узла;
- resume разрешён только при совпадении policy hash, schema fingerprint, model и HMAC-key fingerprint;
- side-effect nodes должны быть idempotent или проверять существующий артефакт.

## 11. CLI contract

Итоговая команда может называться `db-sanitizer`.

Обязательные команды:

```bash
db-sanitizer run --policy config/policy.yaml --run-id demo
db-sanitizer run --policy config/policy.yaml --run-id demo --resume
db-sanitizer verify --policy config/policy.yaml --run-id demo
```

Обязательные Make targets:

```bash
make demo
make test
make lint
make clean
make perf PERF_ROWS=100000
```

`make demo` должен использовать реальную локальную LLM. Unit/обычные integration tests используют fake provider и не требуют загрузки модели.

### 11.1. Exit codes

| Code | Значение |
|---:|---|
| 0 | Успех, все обязательные проверки пройдены. |
| 2 | Ошибка policy/config/environment. |
| 3 | Ошибка LLM generation/validation. |
| 4 | Ошибка Greenmask dump/restore/mapper. |
| 5 | Верификация не пройдена. |
| 6 | Непредвиденная внутренняя ошибка. |

## 12. Verification

Verifier сравнивает source и target после восстановления.

### 12.1. Структура и объём

- одинаковый набор пользовательских tables;
- одинаковые columns и PostgreSQL types;
- одинаковое количество строк в каждой таблице;
- одинаковое количество `NULL` в каждой настроенной колонке.

### 12.2. Связи

- target restore завершился успешно;
- все foreign key constraints validated;
- для каждого FK нет orphan rows;
- PK и UNIQUE constraints проходят.

### 12.3. Замены

Для таблиц с PK verifier сопоставляет source и target по неизменённому PK и проверяет:

- каждый ненулевой source value изменён;
- target value равен registry replacement для source HMAC;
- одинаковый source key во всех колонках group имеет один target value;
- target values не содержат исходных значений группы по HMAC intersection.

Для таблиц без PK допускается агрегатная проверка, но demo tables обязаны иметь PK.

### 12.4. Разнообразие

Для каждой настроенной колонки:

- `distinct_non_null_before == distinct_non_null_after`;
- доля `NULL` одинакова;
- одна replacement не используется для двух разных source keys внутри group;
- нет единой заглушки для всех строк.

### 12.5. LLM evidence

Report содержит:

- provider и model;
- количество LLM batches;
- число принятых и отклонённых generated items;
- время генерации;
- признак structured output.

Prompts, raw values и полные model responses в report не сохраняются.

## 13. Report contract

Нормативная JSON Schema: `templates/run-report.schema.json`.

`report.md` — человекочитаемое представление тех же результатов.

Статус `passed` возможен только если все проверки severity `required` имеют status `pass`.

## 14. Логи и безопасность

- JSONL structured logs;
- timestamp, level, run_id, stage, event, safe details;
- DSN должен редактироваться до `postgresql://user:***@host/db`;
- raw PII запрещены в логах, exceptions, checkpoints и reports;
- HMAC key только через env, не записывается на диск;
- mapping DB и run directory создаются с максимально ограниченными правами;
- source connection read-only;
- target overwrite требует явной настройки;
- LLM endpoint по умолчанию локальный.

## 15. Демонстрационная БД

Минимум три таблицы:

```text
customers
- id PK
- full_name
- email UNIQUE
- phone
- address

orders
- id PK
- customer_id FK -> customers.id
- billing_name      # дублирует customers.full_name
- contact_email     # дублирует customers.email
- amount
- created_at

support_tickets
- id PK
- customer_id FK -> customers.id
- callback_phone    # дублирует customers.phone
- delivery_address  # дублирует customers.address
- subject
```

Seed должен включать:

- кириллицу;
- повторы между таблицами;
- `NULL`;
- несколько разных форматов исходных телефонов;
- как минимум 50 customers и несколько orders/tickets на клиента;
- отдельный perf seed с 100 000 строк или больше и ограниченным числом уникальных PII.

## 16. Документация итогового репозитория

README должен включать:

- quick start;
- architecture diagram;
- список OSS-компонентов и собственных интеграций;
- точную механику consistency groups/HMAC/registry;
- использование LLM и подтверждение, что raw PII не отправляются модели;
- масштабирование data plane и ограничения LLM plane;
- инструкции demo/test/perf;
- фактический before/after;
- альтернативы и компромиссы;
- ограничения MVP и направления расширения.
