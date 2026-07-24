# Официальные внешние источники

Проверено 23 июля 2026 года. Исходное тестовое задание является главным источником требований; ссылки ниже подтверждают реализуемость выбранных компонентов.

## Greenmask

- Проект и релизы: https://github.com/GreenmaskIO/greenmask
- Документация: https://docs.greenmask.io/latest/
- Cmd transformer: https://docs.greenmask.io/latest/built_in_transformers/standard_transformers/cmd/
- Transformation engines: https://docs.greenmask.io/latest/built_in_transformers/transformation_engines/
- Выбранная закреплённая версия: `0.2.22` (релиз 1 июля 2026 года).

Значимые свойства: PostgreSQL production-ready, логический dump/restore, streaming transformation, custom external command, deterministic engines и совместимость с `pg_restore`.

## LangGraph

- Overview: https://docs.langchain.com/oss/python/langgraph/overview
- Graph API: https://docs.langchain.com/oss/python/langgraph/graph-api
- Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- PyPI: https://pypi.org/project/langgraph/
- Целевая ветка: `1.2.x`; актуальная при подготовке пакета — `1.2.9`.

## Ollama

- Structured outputs: https://docs.ollama.com/capabilities/structured-outputs
- Generate API: https://docs.ollama.com/api/generate
- Модель: https://ollama.com/library/qwen3:4b-instruct

## PostgreSQL

- `pg_dump`: https://www.postgresql.org/docs/current/app-pgdump.html
- `pg_restore`: https://www.postgresql.org/docs/current/app-pgrestore.html
- SQL dump and parallel dump/restore: https://www.postgresql.org/docs/current/backup-dump.html
- PostgreSQL 16.14 release notes: https://www.postgresql.org/docs/release/16.14/

## Правило для coding-агента

Если API библиотеки отличается от ожидаемого, использовать официальную документацию закреплённой версии, минимально адаптировать реализацию и зафиксировать изменение в `IMPLEMENTATION_REPORT.md`. Не заменять выбранный компонент без доказанной несовместимости.
